import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from core.schemas import MandateStatus, EventType, AuditActor
from core.database import DBIntentMandate, DBProduct, DBPoolDeal, emit_audit_event

SECRET_MANDATE_KEY = "poolpay_ap2_agent_key_hash_sec"


class BuyerAgent:
    """
    Buyer Agent: Evaluates whether joining a pool is beneficial,
    generates a bounded AP2 Intent Mandate with cryptographic signature,
    and produces structured decision traces.

    RESPONSIBILITIES:
    - Evaluate pool join decision (structured reasoning)
    - Estimate potential savings
    - Sign bounded authorization mandates (HMAC SHA-256)
    - Never executes payments directly

    AI vs DETERMINISTIC boundary:
    - Savings calculation: DETERMINISTIC
    - Mandate signing: DETERMINISTIC (cryptographic)
    - Join decision confidence: RULE-BASED (could be LLM-enhanced)
    - Reasoning text: TEMPLATE-BASED (could be LLM-generated)
    """

    def __init__(self, buyer_id: str, buyer_name: str):
        self.buyer_id = buyer_id
        self.buyer_name = buyer_name

    def evaluate_pool_join(self, product: DBProduct, max_price: float,
                           pool_size: int = 0) -> dict:
        """
        Structured decision: should this buyer join the pool?
        Returns AgentDecisionTrace-compatible dict.
        """
        savings = product.retail_price - product.target_pool_price
        savings_percent = (savings / product.retail_price) * 100
        budget_headroom = max_price - product.target_pool_price

        # Deterministic decision rules
        if max_price < product.target_pool_price:
            return {
                "decision": "REJECT",
                "reasoning": f"Budget limit ({max_price:.0f}) is below the pool target price ({product.target_pool_price:.0f}). Cannot participate.",
                "confidence": 1.0,
                "expected_savings": 0,
                "max_authorized_amount": max_price,
                "action_taken": "MANDATE_REJECTED"
            }

        # Confidence based on savings and headroom
        confidence = min(0.99, 0.5 + (savings_percent / 100) + (budget_headroom / product.retail_price))
        confidence = round(confidence, 2)

        return {
            "decision": "JOIN_POOL",
            "reasoning": (
                f"Expected group discount of {savings_percent:.0f}% "
                f"(saving {savings:.0f} per unit). "
                f"Budget headroom: {budget_headroom:.0f} above pool price. "
                f"Pool has {pool_size} buyer(s), threshold is {product.pool_threshold}."
            ),
            "confidence": confidence,
            "expected_savings": round(savings, 2),
            "max_authorized_amount": max_price,
            "action_taken": "MANDATE_SIGNED"
        }

    def generate_mandate_signature(self, mandate_id: str, product_id: str,
                                    max_price: float, expires_at_iso: str) -> str:
        """AP2 Intent Mandate HMAC-SHA256 signature."""
        payload = f"{mandate_id}:{self.buyer_id}:{product_id}:{max_price}:{expires_at_iso}".encode("utf-8")
        sig = hmac.new(SECRET_MANDATE_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return f"ap2_sig_{sig[:24]}"

    def create_intent_mandate(self, db: Session, product: DBProduct,
                               max_price: float, expiry_minutes: int = 60,
                               pool_size: int = 0) -> tuple:
        """
        Creates a signed intent mandate after evaluating the join decision.
        Returns (mandate, decision_trace).
        """
        # 1. Evaluate decision
        decision_trace = self.evaluate_pool_join(product, max_price, pool_size)

        if decision_trace["decision"] == "REJECT":
            emit_audit_event(
                db, EventType.PAYMENT_AUTHORIZATION_DENIED.value,
                actor=self.buyer_name, actor_type=AuditActor.BUYER_AGENT.value,
                summary=f"Buyer Agent rejected pool join for {product.title}",
                reasoning=decision_trace["reasoning"],
                buyer_id=self.buyer_id,
                metadata=decision_trace
            )
            db.commit()
            raise ValueError(decision_trace["reasoning"])

        # 2. Generate mandate
        mandate_id = f"mandate_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=expiry_minutes)
        idempotency_key = f"idem_{self.buyer_id}_{product.id}_{uuid.uuid4().hex[:8]}"

        sig_hash = self.generate_mandate_signature(
            mandate_id, product.id, max_price, expires_at.isoformat()
        )

        mandate = DBIntentMandate(
            mandate_id=mandate_id,
            buyer_id=self.buyer_id,
            buyer_name=self.buyer_name,
            product_id=product.id,
            max_price=max_price,
            authorized_price=product.target_pool_price,
            quantity=1,
            expires_at=expires_at,
            status=MandateStatus.ACTIVE.value,
            signature_hash=sig_hash,
            idempotency_key=idempotency_key
        )
        db.add(mandate)

        # 3. Emit audit event
        emit_audit_event(
            db, EventType.MANDATE_SIGNED.value,
            actor=self.buyer_name, actor_type=AuditActor.BUYER_AGENT.value,
            summary=f"Buyer Agent '{self.buyer_name}' signed AP2 mandate for {product.title}",
            reasoning=decision_trace["reasoning"],
            buyer_id=self.buyer_id,
            new_state=MandateStatus.ACTIVE.value,
            metadata={
                "mandate_id": mandate_id,
                "max_price": max_price,
                "authorized_price": product.target_pool_price,
                "signature_hash": sig_hash,
                "confidence": decision_trace["confidence"],
                "expected_savings": decision_trace["expected_savings"]
            }
        )

        db.commit()
        db.refresh(mandate)
        return mandate, decision_trace
