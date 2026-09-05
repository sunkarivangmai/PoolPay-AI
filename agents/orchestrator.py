"""
Orchestrator: Coordinates agent interactions and manages demo lifecycle.

This is the central coordination point — agents do NOT call each other directly
for cross-cutting operations. The orchestrator routes events to the correct agent.
"""
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from core.database import (DBProduct, DBPoolDeal, DBIntentMandate, DBPaymentOrder,
                           DBNegotiationEvent, emit_audit_event)
from core.schemas import (DealStatus, MandateStatus, PaymentStatus,
                           EventType, AuditActor, FailureScenario)
from agents.buyer_agent import BuyerAgent
from agents.merchant_agent import merchant_agent
from agents.negotiation_agent import negotiation_agent
from agents.payment_agent import payment_agent
from agents.recovery_agent import recovery_agent


class Orchestrator:
    """
    Central orchestrator that coordinates the multi-agent lifecycle:

    1. Buyer submits → BuyerAgent evaluates + signs mandate
    2. MerchantAgent validates margin + joins pool
    3. NegotiationAgent runs negotiation rounds
    4. PaymentAgent creates bounded Razorpay orders
    5. Webhooks → PaymentAgent processes results
    6. On failure → RecoveryAgent evaluates + executes recovery
    """

    def __init__(self):
        self.name = "Orchestrator"

    def get_agent_status(self, db: Session) -> dict:
        """Return current status of all agents."""
        # Check if any pool is in recovery
        recovery_pool = db.query(DBPoolDeal).filter(
            DBPoolDeal.status == DealStatus.RECOVERY_IN_PROGRESS.value
        ).first()

        # Check if any pool is processing payments
        payment_pool = db.query(DBPoolDeal).filter(
            DBPoolDeal.status.in_([DealStatus.PROCESSING_PAYMENTS.value,
                                    DealStatus.THRESHOLD_MET.value])
        ).first()

        return {
            "buyer_agent": "ACTIVE",
            "merchant_agent": "ACTIVE",
            "negotiation_agent": "ACTIVE",
            "payment_agent": "PROCESSING" if payment_pool else "READY",
            "recovery_agent": "ACTIVE" if recovery_pool else "STANDBY",
        }

    def submit_buyer_mandate(self, db: Session, buyer_id: str, buyer_name: str,
                              product_id: str, max_price: float,
                              expiry_minutes: int = 60) -> dict:
        """
        Step 1-2: Buyer submits mandate → BuyerAgent evaluates → MerchantAgent joins pool.
        """
        product = db.query(DBProduct).filter(DBProduct.id == product_id).first()
        if not product:
            raise ValueError("Product not found")

        # Get current pool size for decision context
        deal = db.query(DBPoolDeal).filter(
            DBPoolDeal.product_id == product_id,
            DBPoolDeal.status.in_([DealStatus.OPEN.value, DealStatus.NEGOTIATING.value,
                                    DealStatus.RECOVERY_IN_PROGRESS.value])
        ).first()
        pool_size = 0
        if deal:
            existing_mandate = db.query(DBIntentMandate).filter(
                DBIntentMandate.pool_deal_id == deal.deal_id,
                DBIntentMandate.buyer_id == buyer_id,
                DBIntentMandate.status.in_([MandateStatus.ACTIVE.value, MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
            ).first()
            if existing_mandate:
                raise ValueError(f"Buyer {buyer_id} is already in this pool")

            pool_size = db.query(DBIntentMandate).filter(
                DBIntentMandate.pool_deal_id == deal.deal_id,
                DBIntentMandate.status.in_([MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
            ).count()

        # BuyerAgent evaluates and signs
        agent = BuyerAgent(buyer_id=buyer_id, buyer_name=buyer_name)
        mandate, decision_trace = agent.create_intent_mandate(
            db=db, product=product, max_price=max_price,
            expiry_minutes=expiry_minutes, pool_size=pool_size
        )

        # MerchantAgent validates and joins pool
        deal = merchant_agent.join_pool(db=db, mandate=mandate)

        db.refresh(mandate)
        return {
            "mandate": mandate,
            "deal": deal,
            "decision_trace": decision_trace
        }

    def run_negotiation(self, db: Session, deal_id: str) -> dict:
        """Step 3: Run negotiation rounds for a pool."""
        deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == deal_id).first()
        if not deal:
            raise ValueError("Pool not found")

        product = db.query(DBProduct).filter(DBProduct.id == deal.product_id).first()
        pool_size = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal_id,
            DBIntentMandate.status.in_([MandateStatus.POOLED.value])
        ).count()

        rounds = negotiation_agent.run_negotiation(
            db=db, deal=deal, product=product,
            buyer_target_price=deal.target_pool_price,
            pool_size=pool_size
        )
        return {"deal_id": deal_id, "rounds": rounds, "final_price": deal.negotiated_price}

    def create_payment_orders(self, db: Session, deal_id: str) -> dict:
        """Step 4: Create Razorpay orders for threshold-met pool."""
        deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == deal_id).first()
        if not deal:
            raise ValueError("Pool not found")

        orders = payment_agent.authorize_and_create_orders(db=db, deal=deal)
        return {
            "deal_id": deal_id,
            "orders_created": len(orders),
            "razorpay_order_ids": [o.razorpay_order_id for o in orders]
        }

    def handle_payment_result(self, db: Session, razorpay_order_id: str,
                               success: bool, payment_id: str = None,
                               failure_reason: str = "Card declined") -> dict:
        """Step 5: Process payment result (called by webhook or simulation)."""
        order = db.query(DBPaymentOrder).filter(
            DBPaymentOrder.razorpay_order_id == razorpay_order_id
        ).first()
        if not order:
            raise ValueError(f"Payment order not found: {razorpay_order_id}")

        deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == order.deal_id).first()

        if success:
            payment_agent.mark_payment_success(db, order, payment_id)
            completed = payment_agent.check_pool_completion(db, deal)
            db.commit()
            return {
                "status": "SUCCESS",
                "pool_completed": completed,
                "order_id": order.razorpay_order_id
            }
        else:
            payment_agent.mark_payment_failed(db, order, failure_reason)
            # Trigger recovery
            recovery_result = recovery_agent.evaluate_and_recover(
                db, deal, order, failure_reason
            )
            db.commit()
            return {
                "status": "FAILED",
                "recovery": recovery_result,
                "order_id": order.razorpay_order_id
            }

    def simulate_failure(self, db: Session, deal_id: str,
                          scenario: FailureScenario,
                          target_mandate_id: str = None) -> dict:
        """Step 6: Simulate failure scenarios for demo/testing."""
        deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == deal_id).first()
        if not deal:
            raise ValueError("Pool not found")

        emit_audit_event(
            db, EventType.SIMULATION_TRIGGERED.value,
            actor=self.name, actor_type=AuditActor.ORCHESTRATOR.value,
            summary=f"Simulation: {scenario.value} triggered on pool {deal_id[:8]}",
            reasoning=f"Demo mode failure simulation for testing.",
            pool_id=deal_id,
            metadata={"scenario": scenario.value}
        )

        if scenario == FailureScenario.CARD_DECLINED:
            return self._sim_payment_failure(db, deal, target_mandate_id, "Card declined by issuing bank")

        elif scenario == FailureScenario.PAYMENT_TIMEOUT:
            return self._sim_payment_failure(db, deal, target_mandate_id, "Payment timeout — bank did not respond")

        elif scenario == FailureScenario.BUYER_LEAVES_POOL:
            return self._sim_buyer_leaves(db, deal, target_mandate_id)

        elif scenario == FailureScenario.DUPLICATE_WEBHOOK:
            return self._sim_duplicate_webhook(db, deal)

        elif scenario == FailureScenario.INVALID_WEBHOOK_SIGNATURE:
            return {"status": "REJECTED", "reason": "Invalid webhook signature — request rejected",
                    "scenario": scenario.value}

        elif scenario == FailureScenario.DUPLICATE_ORDER:
            return self._sim_duplicate_order(db, deal)

        elif scenario == FailureScenario.RAZORPAY_API_FAILURE:
            return {"status": "SIMULATED", "reason": "Razorpay API returned 500. Mock fallback activated.",
                    "scenario": scenario.value}

        elif scenario == FailureScenario.POOL_BELOW_THRESHOLD:
            return self._sim_below_threshold(db, deal)

        db.commit()
        return {"status": "UNKNOWN_SCENARIO", "scenario": scenario.value}

    def _sim_payment_failure(self, db, deal, mandate_id, reason):
        order = db.query(DBPaymentOrder).filter(
            DBPaymentOrder.deal_id == deal.deal_id,
            DBPaymentOrder.status.in_([PaymentStatus.ORDER_CREATED.value, PaymentStatus.PROCESSING.value])
        ).first()

        if not order:
            # If no orders yet, pick first mandate and create a mock order first
            mandate = db.query(DBIntentMandate).filter(
                DBIntentMandate.pool_deal_id == deal.deal_id,
                DBIntentMandate.status.in_([MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
            ).first()
            if mandate:
                # Temporarily set threshold met to create orders
                if deal.status in (DealStatus.OPEN.value, DealStatus.NEGOTIATING.value):
                    deal.status = DealStatus.THRESHOLD_MET.value
                    db.flush()
                    try:
                        payment_agent.authorize_and_create_orders(db, deal)
                    except Exception:
                        pass
                order = db.query(DBPaymentOrder).filter(
                    DBPaymentOrder.deal_id == deal.deal_id
                ).first()

        if not order:
            return {"status": "NO_ORDERS", "reason": "No payment orders to fail"}

        return self.handle_payment_result(
            db, order.razorpay_order_id, success=False, failure_reason=reason
        )

    def _sim_buyer_leaves(self, db, deal, mandate_id):
        mandate = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.status == MandateStatus.POOLED.value
        ).first()
        if not mandate:
            return {"status": "NO_BUYERS", "reason": "No active buyers to remove"}

        mandate.status = MandateStatus.CANCELLED.value
        remaining = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.status.in_([MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
        ).count()

        emit_audit_event(
            db, EventType.AGENT_DECISION.value,
            actor=self.name, actor_type=AuditActor.ORCHESTRATOR.value,
            summary=f"Buyer '{mandate.buyer_name}' left pool. {remaining}/{deal.threshold} remaining.",
            reasoning=f"Mandate {mandate.mandate_id} cancelled. Pool below threshold.",
            pool_id=deal.deal_id, buyer_id=mandate.buyer_id
        )
        db.commit()
        return {"status": "BUYER_REMOVED", "remaining": remaining, "threshold": deal.threshold}

    def _sim_duplicate_webhook(self, db, deal):
        return {
            "status": "DUPLICATE_DETECTED",
            "reason": "Webhook event ID already processed. Idempotent — no state change.",
            "scenario": "DUPLICATE_WEBHOOK"
        }

    def _sim_duplicate_order(self, db, deal):
        order = db.query(DBPaymentOrder).filter(
            DBPaymentOrder.deal_id == deal.deal_id
        ).first()
        if order:
            return {
                "status": "IDEMPOTENT_RETURN",
                "reason": f"Order {order.razorpay_order_id} already exists. Idempotency key prevented duplicate.",
                "existing_order_id": order.razorpay_order_id
            }
        return {"status": "NO_ORDERS", "reason": "No existing orders to duplicate"}

    def _sim_below_threshold(self, db, deal):
        # Remove multiple buyers
        mandates = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.status == MandateStatus.POOLED.value
        ).limit(2).all()

        for m in mandates:
            m.status = MandateStatus.CANCELLED.value

        remaining = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.status.in_([MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
        ).count()

        deal.status = DealStatus.FAILED.value if remaining < 2 else DealStatus.RECOVERY_IN_PROGRESS.value

        emit_audit_event(
            db, EventType.POOL_FAILED.value,
            actor=self.name, actor_type=AuditActor.ORCHESTRATOR.value,
            summary=f"Pool dropped below threshold: {remaining}/{deal.threshold}",
            reasoning=f"Multiple buyers left. Pool cannot proceed.",
            pool_id=deal.deal_id,
            new_state=deal.status
        )
        db.commit()
        return {"status": deal.status, "remaining": remaining, "threshold": deal.threshold}


orchestrator = Orchestrator()
