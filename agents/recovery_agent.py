from sqlalchemy.orm import Session
from core.database import (DBPoolDeal, DBPaymentOrder, DBIntentMandate, DBProduct,
                           emit_audit_event)
from core.schemas import (RecoveryAction, PaymentStatus, MandateStatus, DealStatus,
                           EventType, AuditActor)


class RecoveryAgent:
    """
    Recovery Agent: Evaluates failed payments and recommends recovery actions.
    All recovery actions MUST pass deterministic business rules.

    Recovery options:
    1. RETRY_PAYMENT — if failure was transient
    2. REQUEST_REAUTH — if mandate expired
    3. REPLACE_BUYER — open replacement slot
    4. CONTINUE_REDUCED — if margin buffer covers shortfall
    5. RENEGOTIATE — recalculate discount for smaller pool
    6. CANCEL_POOL — if recovery impossible

    AI vs DETERMINISTIC:
    - Recovery decision: RULE-BASED (deterministic checks)
    - Margin arithmetic: DETERMINISTIC
    - Reasoning explanation: TEMPLATE (could be LLM-generated)
    """

    def __init__(self):
        self.name = "RecoveryAgent"

    def evaluate_and_recover(self, db: Session, deal: DBPoolDeal,
                              failed_order: DBPaymentOrder,
                              failure_reason: str = "Card declined") -> dict:
        """
        Evaluate failure context and execute recovery action.
        Returns structured recovery decision trace.
        """
        product = db.query(DBProduct).filter(DBProduct.id == deal.product_id).first()
        if not product:
            raise ValueError("Product not found")

        old_status = deal.status
        deal.status = DealStatus.RECOVERY_IN_PROGRESS.value

        emit_audit_event(
            db, EventType.RECOVERY_STARTED.value,
            actor=self.name, actor_type=AuditActor.RECOVERY_AGENT.value,
            summary=f"Recovery started for pool {deal.deal_id[:8]} — {failure_reason}",
            reasoning=f"Payment order {failed_order.razorpay_order_id} failed. Evaluating recovery options.",
            pool_id=deal.deal_id, buyer_id=failed_order.buyer_id,
            transaction_id=failed_order.razorpay_order_id,
            previous_state=old_status, new_state=DealStatus.RECOVERY_IN_PROGRESS.value,
            metadata={"failure_reason": failure_reason}
        )

        # Count remaining successful/pending orders
        all_orders = db.query(DBPaymentOrder).filter(
            DBPaymentOrder.deal_id == deal.deal_id
        ).all()

        successful = [o for o in all_orders if o.status == PaymentStatus.SUCCESS.value]
        failed = [o for o in all_orders if o.status == PaymentStatus.FAILED.value]
        remaining = len(all_orders) - len(failed)

        # Evaluate recovery options (DETERMINISTIC)
        unit_price = deal.negotiated_price or deal.target_pool_price
        unit_margin = unit_price - product.min_wholesale_price
        total_remaining_margin = remaining * unit_margin
        shortfall_cost = len(failed) * product.min_wholesale_price

        decision = self._decide_recovery(
            remaining=remaining,
            threshold=deal.threshold,
            total_remaining_margin=total_remaining_margin,
            shortfall_cost=shortfall_cost,
            unit_margin=unit_margin,
            failure_reason=failure_reason,
            failed_count=len(failed)
        )

        # Execute recovery action
        result = self._execute_recovery(
            db, deal, product, decision, remaining, failed, unit_margin,
            total_remaining_margin, shortfall_cost
        )

        db.commit()
        return result

    def _decide_recovery(self, remaining: int, threshold: int,
                          total_remaining_margin: float, shortfall_cost: float,
                          unit_margin: float, failure_reason: str,
                          failed_count: int) -> RecoveryAction:
        """DETERMINISTIC recovery decision rules."""

        # If only transient failure (timeout), suggest retry
        if "timeout" in failure_reason.lower():
            return RecoveryAction.RETRY_PAYMENT

        # If margin buffer can absorb shortfall
        if total_remaining_margin >= shortfall_cost and failed_count <= 1:
            return RecoveryAction.CONTINUE_REDUCED

        # If pool still above minimum viable (threshold - 1)
        if remaining >= threshold - 1 and failed_count <= 1:
            return RecoveryAction.REPLACE_BUYER

        # If too many failures
        if failed_count >= threshold // 2:
            return RecoveryAction.CANCEL_POOL

        # Default: try renegotiation
        return RecoveryAction.RENEGOTIATE

    def _execute_recovery(self, db: Session, deal: DBPoolDeal, product: DBProduct,
                           action: RecoveryAction, remaining: int,
                           failed: list, unit_margin: float,
                           total_remaining_margin: float, shortfall_cost: float) -> dict:
        """Execute the chosen recovery action."""

        if action == RecoveryAction.CONTINUE_REDUCED:
            net_margin = total_remaining_margin - shortfall_cost
            deal.status = DealStatus.COMPLETED.value

            result = {
                "action": action.value,
                "status": "RECOVERED",
                "reasoning": (
                    f"Merchant absorbed shortfall from profit buffer. "
                    f"Available margin ({total_remaining_margin:.0f}) covers "
                    f"wholesale cost ({shortfall_cost:.0f}). "
                    f"Net merchant margin: {net_margin:.0f}. "
                    f"Pool completed with {remaining} buyers."
                ),
                "remaining_buyers": remaining,
                "absorbed_cost": shortfall_cost,
                "net_margin": net_margin,
                "confidence": 0.95
            }

            emit_audit_event(
                db, EventType.RECOVERY_COMPLETED.value,
                actor=self.name, actor_type=AuditActor.RECOVERY_AGENT.value,
                summary=f"Recovery successful: Merchant absorbed shortfall ({shortfall_cost:.0f})",
                reasoning=result["reasoning"],
                pool_id=deal.deal_id,
                previous_state=DealStatus.RECOVERY_IN_PROGRESS.value,
                new_state=DealStatus.COMPLETED.value,
                metadata=result
            )

            # Update failed orders to RECOVERED
            for fo in failed:
                fo.status = PaymentStatus.RECOVERED.value
            return result

        elif action == RecoveryAction.REPLACE_BUYER:
            deal.status = DealStatus.RECOVERY_IN_PROGRESS.value

            result = {
                "action": action.value,
                "status": "AWAITING_REPLACEMENT",
                "reasoning": (
                    f"Opening 15-minute replacement slot. "
                    f"Pool needs 1 replacement buyer to maintain threshold. "
                    f"Current: {remaining}/{deal.threshold}."
                ),
                "remaining_buyers": remaining,
                "needed": deal.threshold - remaining,
                "confidence": 0.70
            }

            emit_audit_event(
                db, EventType.RECOVERY_DECISION.value,
                actor=self.name, actor_type=AuditActor.RECOVERY_AGENT.value,
                summary=f"Recovery: Opening replacement slot ({remaining}/{deal.threshold})",
                reasoning=result["reasoning"],
                pool_id=deal.deal_id,
                new_state=DealStatus.RECOVERY_IN_PROGRESS.value,
                metadata=result
            )
            return result

        elif action == RecoveryAction.CANCEL_POOL:
            deal.status = DealStatus.FAILED.value

            result = {
                "action": action.value,
                "status": "CANCELLED",
                "reasoning": (
                    f"Too many payment failures ({len(failed)}/{deal.threshold}). "
                    f"Pool cannot be recovered. Cancelling and refunding successful payments."
                ),
                "remaining_buyers": remaining,
                "confidence": 1.0
            }

            emit_audit_event(
                db, EventType.RECOVERY_FAILED.value,
                actor=self.name, actor_type=AuditActor.RECOVERY_AGENT.value,
                summary=f"Recovery failed: Pool cancelled due to excessive failures",
                reasoning=result["reasoning"],
                pool_id=deal.deal_id,
                previous_state=DealStatus.RECOVERY_IN_PROGRESS.value,
                new_state=DealStatus.FAILED.value,
                metadata=result
            )
            return result

        else:
            # RETRY_PAYMENT, RENEGOTIATE, REQUEST_REAUTH
            result = {
                "action": action.value,
                "status": "PENDING_ACTION",
                "reasoning": f"Recovery action {action.value} recommended. Awaiting execution.",
                "remaining_buyers": remaining,
                "confidence": 0.60
            }

            emit_audit_event(
                db, EventType.RECOVERY_DECISION.value,
                actor=self.name, actor_type=AuditActor.RECOVERY_AGENT.value,
                summary=f"Recovery recommendation: {action.value}",
                reasoning=result["reasoning"],
                pool_id=deal.deal_id,
                metadata=result
            )
            return result


recovery_agent = RecoveryAgent()
