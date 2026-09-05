import json
import uuid
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from core.database import DBPoolDeal, DBIntentMandate, DBProduct, emit_audit_event
from core.schemas import DealStatus, MandateStatus, EventType, AuditActor


# ──────────────────────────────────────────────
# Deterministic Discount Tiers
# ──────────────────────────────────────────────
DISCOUNT_TIERS = [
    (1, 5.0),    # 1 buyer  -> 5% discount
    (3, 10.0),   # 3 buyers -> 10%
    (5, 15.0),   # 5 buyers -> 15%
    (8, 20.0),   # 8 buyers -> 20%
    (10, 25.0),  # 10 buyers -> 25%
]


def calculate_discount_percent(pool_size: int) -> float:
    """DETERMINISTIC discount tier lookup. No LLM involvement."""
    discount = 0.0
    for threshold, pct in DISCOUNT_TIERS:
        if pool_size >= threshold:
            discount = pct
    return discount


def calculate_discounted_price(retail_price: float, discount_percent: float) -> float:
    """Apply discount. Returns price rounded to 2 decimal places."""
    return round(retail_price * (1 - discount_percent / 100), 2)


class MerchantAgent:
    """
    Merchant Negotiator Agent:
    - Evaluates pool depth and demand
    - Proposes deterministic discount tiers
    - Validates minimum wholesale margin floor
    - Manages pool state transitions

    AI vs DETERMINISTIC:
    - Discount tiers: DETERMINISTIC (lookup table)
    - Margin validation: DETERMINISTIC (floor comparison)
    - Pool state transitions: DETERMINISTIC (threshold logic)
    - Reasoning explanations: TEMPLATE (could be LLM-enhanced)
    """

    def __init__(self, name: str = "PoolPay Merchant Negotiator"):
        self.name = name

    def evaluate_margin(self, product: DBProduct, offered_price: float) -> dict:
        """Check if the offered price meets minimum wholesale margin."""
        margin = offered_price - product.min_wholesale_price
        margin_percent = (margin / product.retail_price) * 100
        acceptable = margin >= 0

        return {
            "acceptable": acceptable,
            "offered_price": offered_price,
            "min_wholesale_price": product.min_wholesale_price,
            "margin": round(margin, 2),
            "margin_percent": round(margin_percent, 2),
            "reasoning": (
                f"Offered price {offered_price:.0f} yields margin of {margin:.0f} "
                f"({margin_percent:.1f}%) against wholesale floor {product.min_wholesale_price:.0f}. "
                f"{'Acceptable.' if acceptable else 'Below wholesale cost — rejected.'}"
            )
        }

    def propose_offer(self, product: DBProduct, pool_size: int) -> dict:
        """Generate a deterministic discount offer based on pool size."""
        discount = calculate_discount_percent(pool_size)
        discounted_price = calculate_discounted_price(product.retail_price, discount)
        margin_check = self.evaluate_margin(product, discounted_price)

        # If discount pushes below wholesale, cap at wholesale + small margin
        if not margin_check["acceptable"]:
            discounted_price = product.min_wholesale_price + 100  # ₹100 minimum margin
            discount = round((1 - discounted_price / product.retail_price) * 100, 1)
            margin_check = self.evaluate_margin(product, discounted_price)

        return {
            "pool_size": pool_size,
            "discount_percent": discount,
            "discounted_price": discounted_price,
            "margin_check": margin_check,
            "reasoning": (
                f"Pool of {pool_size} buyer(s) qualifies for {discount:.0f}% discount. "
                f"Unit price: {discounted_price:.0f}. "
                f"Margin: {margin_check['margin']:.0f} above wholesale floor."
            )
        }

    def join_pool(self, db: Session, mandate: DBIntentMandate) -> DBPoolDeal:
        """Add a mandate to pool. Creates pool if none exists."""
        product = db.query(DBProduct).filter(DBProduct.id == mandate.product_id).first()
        if not product:
            raise ValueError("Product not found")

        # Check margin
        margin_result = self.evaluate_margin(product, product.target_pool_price)
        emit_audit_event(
            db, EventType.MARGIN_CHECK.value,
            actor=self.name, actor_type=AuditActor.MERCHANT_AGENT.value,
            summary=f"Margin check: {margin_result['reasoning']}",
            reasoning=margin_result["reasoning"],
            pool_id=None, buyer_id=mandate.buyer_id,
            metadata=margin_result
        )

        if not margin_result["acceptable"]:
            db.commit()
            raise ValueError(f"Target pool price below wholesale floor: {margin_result['reasoning']}")

        # Find or create pool
        deal = db.query(DBPoolDeal).filter(
            DBPoolDeal.product_id == product.id,
            DBPoolDeal.status.in_([DealStatus.OPEN.value, DealStatus.NEGOTIATING.value,
                                    DealStatus.RECOVERY_IN_PROGRESS.value])
        ).first()

        if not deal:
            deal = DBPoolDeal(
                deal_id=f"pool_{uuid.uuid4().hex[:10]}",
                product_id=product.id,
                threshold=product.pool_threshold,
                target_pool_price=product.target_pool_price,
                status=DealStatus.OPEN.value
            )
            db.add(deal)
            db.flush()

            emit_audit_event(
                db, EventType.POOL_CREATED.value,
                actor=self.name, actor_type=AuditActor.MERCHANT_AGENT.value,
                summary=f"New pool created for {product.title} (threshold: {product.pool_threshold})",
                reasoning=f"No active pool existed for product {product.id}. Created pool {deal.deal_id}.",
                pool_id=deal.deal_id,
                new_state=DealStatus.OPEN.value
            )

        # Check duplicate buyer
        existing = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.buyer_id == mandate.buyer_id,
            DBIntentMandate.status.in_([MandateStatus.ACTIVE.value, MandateStatus.POOLED.value,
                                         MandateStatus.AUTHORIZED.value])
        ).first()
        if existing:
            raise ValueError(f"Buyer {mandate.buyer_id} is already in this pool")

        # Associate mandate to deal
        mandate.pool_deal_id = deal.deal_id
        mandate.status = MandateStatus.POOLED.value
        db.flush()

        # Count & update discount
        current_count = db.query(DBIntentMandate).filter(
            DBIntentMandate.pool_deal_id == deal.deal_id,
            DBIntentMandate.status.in_([MandateStatus.POOLED.value, MandateStatus.AUTHORIZED.value])
        ).count()

        offer = self.propose_offer(product, current_count)
        deal.discount_percent = offer["discount_percent"]
        deal.negotiated_price = offer["discounted_price"]

        emit_audit_event(
            db, EventType.BUYER_JOINED.value,
            actor=self.name, actor_type=AuditActor.MERCHANT_AGENT.value,
            summary=f"Buyer joined pool ({current_count}/{deal.threshold}). Discount: {offer['discount_percent']:.0f}%",
            reasoning=offer["reasoning"],
            pool_id=deal.deal_id, buyer_id=mandate.buyer_id,
            previous_state=deal.status, new_state=deal.status,
            metadata={"pool_count": current_count, "threshold": deal.threshold,
                       "discount_percent": offer["discount_percent"]}
        )

        # Check threshold
        if current_count >= deal.threshold:
            old_status = deal.status
            deal.status = DealStatus.THRESHOLD_MET.value
            emit_audit_event(
                db, EventType.POOL_THRESHOLD_MET.value,
                actor=self.name, actor_type=AuditActor.MERCHANT_AGENT.value,
                summary=f"Pool threshold met! {current_count}/{deal.threshold} buyers for {product.title}",
                reasoning=f"Pool {deal.deal_id} reached {current_count} buyers (threshold {deal.threshold}). Ready for payment processing.",
                pool_id=deal.deal_id,
                previous_state=old_status, new_state=DealStatus.THRESHOLD_MET.value,
                metadata={"buyer_count": current_count, "discount_percent": offer["discount_percent"],
                           "unit_price": offer["discounted_price"]}
            )

        db.commit()
        db.refresh(deal)
        return deal


merchant_agent = MerchantAgent()
