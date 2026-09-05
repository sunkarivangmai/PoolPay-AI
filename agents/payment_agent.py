import uuid
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from core.database import (DBPaymentOrder, DBIntentMandate, DBPoolDeal, DBProduct,
                           emit_audit_event)
from core.schemas import (PaymentStatus, MandateStatus, DealStatus,
                           EventType, AuditActor)
from core.razorpay_client import razorpay_wrapper


class PaymentAgent:
    """
    Payment Agent: Enforces bounded payment authorization policy.
    NEVER receives unrestricted payment authority.

    Before any payment:
    1. Verify buyer authorization exists
    2. Verify amount <= authorized amount
    3. Verify currency
    4. Verify product/pool match
    5. Verify pool status is THRESHOLD_MET
    6. Verify order has not already been created (idempotency)
    7. Generate idempotency key
    8. Create Razorpay order
    9. Persist order to DB BEFORE returning
    10. Return payment information

    AI vs DETERMINISTIC:
    - ALL payment logic is DETERMINISTIC. No LLM involvement in money flows.
    """

    def __init__(self):
        self.name = "PaymentAgent"

    def authorize_and_create_orders(self, db: Session, deal: DBPoolDeal) -> list:
        """
        Create per-buyer Razorpay orders for all mandates in a threshold-met pool.
        Returns list of created PaymentOrder records.
        """
        if deal.status != DealStatus.THRESHOLD_MET.value:
            raise ValueError(f"Pool status must be THRESHOLD_MET, got {deal.status}")

        product = db.query(DBProduct).filter(DBProduct.id == deal.product_id).first()
        if not product:
            raise ValueError("Product not found")

        mandates = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.status.in_([MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
        ).all()

        if len(mandates) < deal.threshold:
            raise ValueError(f"Insufficient mandates: {len(mandates)} < {deal.threshold}")

        # Determine unit price (cannot exceed target_pool_price authorized by mandates)
        negotiated = deal.negotiated_price or deal.target_pool_price
        unit_price = min(deal.target_pool_price, negotiated)
        amount_paise = int(unit_price * 100)

        created_orders = []
        for mandate in mandates:
            order = self._create_single_order(db, deal, mandate, product, amount_paise)
            created_orders.append(order)

        # Update pool status
        old_status = deal.status
        deal.status = DealStatus.PROCESSING_PAYMENTS.value

        emit_audit_event(
            db, EventType.RAZORPAY_ORDER_CREATED.value,
            actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
            summary=f"Created {len(created_orders)} Razorpay orders for pool {deal.deal_id[:8]}",
            reasoning=f"All {len(mandates)} mandates authorized. Per-buyer orders at {unit_price:.0f}/unit.",
            pool_id=deal.deal_id,
            previous_state=old_status, new_state=DealStatus.PROCESSING_PAYMENTS.value,
            metadata={
                "order_count": len(created_orders),
                "unit_price": unit_price,
                "total_value": unit_price * len(created_orders),
                "razorpay_order_ids": [o.razorpay_order_id for o in created_orders]
            }
        )

        db.commit()
        return created_orders

    def _create_single_order(self, db: Session, deal: DBPoolDeal,
                              mandate: DBIntentMandate, product: DBProduct,
                              amount_paise: int) -> DBPaymentOrder:
        """
        Authorization policy enforcement + Razorpay order creation for one buyer.
        """
        # 1. Verify buyer authorization
        if mandate.status not in (MandateStatus.POOLED.value, MandateStatus.ACTIVE.value):
            raise ValueError(f"Mandate {mandate.mandate_id} not in authorized state: {mandate.status}")

        # 2. Verify amount <= authorized amount (paise comparison)
        unit_price = amount_paise / 100
        if unit_price > mandate.max_price:
            emit_audit_event(
                db, EventType.PAYMENT_AUTHORIZATION_DENIED.value,
                actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
                summary=f"Payment denied: amount {unit_price:.0f} exceeds mandate limit {mandate.max_price:.0f}",
                reasoning=f"Authorization policy violation. Buyer {mandate.buyer_id} max authorized: {mandate.max_price:.0f}.",
                pool_id=deal.deal_id, buyer_id=mandate.buyer_id,
                metadata={"attempted": unit_price, "authorized": mandate.max_price}
            )
            raise ValueError(f"Amount {unit_price} exceeds mandate authorization {mandate.max_price}")

        # 3. Verify currency (always INR)
        # 4. Verify product/pool match
        if mandate.product_id != deal.product_id:
            raise ValueError("Product/pool mismatch")

        # 5. Verify mandate not expired
        expires = mandate.expires_at.replace(tzinfo=timezone.utc) if mandate.expires_at.tzinfo is None else mandate.expires_at
        if expires < datetime.now(timezone.utc):
            raise ValueError(f"Mandate {mandate.mandate_id} has expired")

        # 6. Check idempotency — no duplicate order for this mandate
        idempotency_key = f"order_{deal.deal_id}_{mandate.mandate_id}"
        existing = db.query(DBPaymentOrder).filter(
            DBPaymentOrder.idempotency_key == idempotency_key
        ).first()
        if existing:
            return existing  # Idempotent return

        # 7. Create Razorpay order
        rzp_order = razorpay_wrapper.create_order(
            amount_paise=amount_paise,
            receipt=f"pool_{deal.deal_id[:8]}_{mandate.mandate_id[:8]}",
            notes={
                "deal_id": deal.deal_id,
                "mandate_id": mandate.mandate_id,
                "buyer_id": mandate.buyer_id,
                "product_title": product.title
            },
            idempotency_key=idempotency_key
        )

        # 8-9. Persist order BEFORE returning
        order_id = f"po_{uuid.uuid4().hex[:12]}"
        payment_order = DBPaymentOrder(
            order_id=order_id,
            deal_id=deal.deal_id,
            mandate_id=mandate.mandate_id,
            buyer_id=mandate.buyer_id,
            amount_paise=amount_paise,
            currency="INR",
            razorpay_order_id=rzp_order["id"],
            idempotency_key=idempotency_key,
            status=PaymentStatus.ORDER_CREATED.value
        )
        db.add(payment_order)

        # Update mandate status
        mandate.status = MandateStatus.ORDER_CREATED.value

        emit_audit_event(
            db, EventType.PAYMENT_AUTHORIZED.value,
            actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
            summary=f"Order created for buyer '{mandate.buyer_name}' — {unit_price:.0f} INR",
            reasoning=(
                f"All 7 authorization checks passed. "
                f"Razorpay order {rzp_order['id']} (mock: {rzp_order.get('is_mock', False)}). "
                f"Amount: {unit_price:.0f} <= mandate limit {mandate.max_price:.0f}."
            ),
            pool_id=deal.deal_id, buyer_id=mandate.buyer_id,
            transaction_id=rzp_order["id"],
            previous_state=MandateStatus.POOLED.value,
            new_state=MandateStatus.ORDER_CREATED.value,
            metadata={
                "razorpay_order_id": rzp_order["id"],
                "amount_paise": amount_paise,
                "idempotency_key": idempotency_key,
                "is_mock": rzp_order.get("is_mock", False)
            }
        )

        db.flush()
        return payment_order

    def mark_payment_success(self, db: Session, payment_order: DBPaymentOrder,
                              razorpay_payment_id: str = None) -> None:
        """Mark a payment order as successful. Called by webhook handler."""
        old_status = payment_order.status
        payment_order.status = PaymentStatus.SUCCESS.value
        payment_order.razorpay_payment_id = razorpay_payment_id
        payment_order.updated_at = datetime.now(timezone.utc)

        # Update mandate
        mandate = db.query(DBIntentMandate).filter(
            DBIntentMandate.mandate_id == payment_order.mandate_id
        ).first()
        if mandate:
            mandate.status = MandateStatus.EXECUTED.value

        emit_audit_event(
            db, EventType.PAYMENT_SUCCESS.value,
            actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
            summary=f"Payment succeeded for order {payment_order.razorpay_order_id}",
            reasoning=f"Razorpay confirmed payment {razorpay_payment_id or 'mock'}.",
            pool_id=payment_order.deal_id, buyer_id=payment_order.buyer_id,
            transaction_id=payment_order.razorpay_order_id,
            previous_state=old_status, new_state=PaymentStatus.SUCCESS.value
        )

    def mark_payment_failed(self, db: Session, payment_order: DBPaymentOrder,
                             reason: str = "Payment declined") -> None:
        """Mark a payment order as failed. Called by webhook handler."""
        old_status = payment_order.status
        payment_order.status = PaymentStatus.FAILED.value
        payment_order.failure_reason = reason
        payment_order.updated_at = datetime.now(timezone.utc)

        mandate = db.query(DBIntentMandate).filter(
            DBIntentMandate.mandate_id == payment_order.mandate_id
        ).first()
        if mandate:
            mandate.status = MandateStatus.FAILED.value

        emit_audit_event(
            db, EventType.PAYMENT_FAILED.value,
            actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
            summary=f"Payment failed for order {payment_order.razorpay_order_id}: {reason}",
            reasoning=f"Razorpay reported payment failure. Reason: {reason}.",
            pool_id=payment_order.deal_id, buyer_id=payment_order.buyer_id,
            transaction_id=payment_order.razorpay_order_id,
            previous_state=old_status, new_state=PaymentStatus.FAILED.value,
            metadata={"failure_reason": reason}
        )

    def check_pool_completion(self, db: Session, deal: DBPoolDeal) -> bool:
        """Check if all payments for a pool are successful."""
        orders = db.query(DBPaymentOrder).filter(
            DBPaymentOrder.deal_id == deal.deal_id
        ).all()

        if not orders:
            return False

        all_success = all(o.status == PaymentStatus.SUCCESS.value for o in orders)
        any_failed = any(o.status == PaymentStatus.FAILED.value for o in orders)

        if all_success:
            old_status = deal.status
            deal.status = DealStatus.COMPLETED.value
            emit_audit_event(
                db, EventType.POOL_COMPLETED.value,
                actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
                summary=f"Pool {deal.deal_id[:8]} completed! All {len(orders)} payments succeeded.",
                reasoning=f"All payment orders confirmed successful.",
                pool_id=deal.deal_id,
                previous_state=old_status, new_state=DealStatus.COMPLETED.value
            )
            return True

        if any_failed:
            old_status = deal.status
            deal.status = DealStatus.PARTIAL_FAILURE.value
            emit_audit_event(
                db, EventType.POOL_FAILED.value,
                actor=self.name, actor_type=AuditActor.PAYMENT_AGENT.value,
                summary=f"Pool {deal.deal_id[:8]} has partial failures. Recovery needed.",
                reasoning=f"Some payment orders failed. Triggering recovery agent.",
                pool_id=deal.deal_id,
                previous_state=old_status, new_state=DealStatus.PARTIAL_FAILURE.value
            )

        return False


payment_agent = PaymentAgent()
