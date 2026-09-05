import json
import hashlib
from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from core.database import (get_db, DBPaymentOrder, DBProcessedWebhook, emit_audit_event)
from core.schemas import EventType, AuditActor, PaymentStatus
from core.razorpay_client import razorpay_wrapper
from agents.payment_agent import payment_agent
from agents.recovery_agent import recovery_agent
from core.database import DBPoolDeal

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Razorpay Webhook Endpoint.

    Security:
    1. Verify webhook signature (HMAC SHA-256)
    2. Check for duplicate event ID (idempotency)
    3. Process payment state transition
    4. Trigger recovery on failure

    Never trust frontend payment status. This endpoint is source of truth.
    """
    # 1. Read raw body for signature verification
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # 2. Extract event metadata
    event_type = payload.get("event", "unknown")
    event_id = payload.get("id", "")  # Razorpay event ID

    # If no event_id, generate one from payload hash to still prevent duplicates
    if not event_id:
        event_id = f"evt_{hashlib.sha256(body_bytes).hexdigest()[:16]}"

    # 3. Verify webhook signature
    signature = request.headers.get("X-Razorpay-Signature", "")

    # Log webhook receipt BEFORE verification
    emit_audit_event(
        db, EventType.WEBHOOK_RECEIVED.value,
        actor="Razorpay", actor_type=AuditActor.RAZORPAY_WEBHOOK.value,
        summary=f"Webhook received: {event_type} (ID: {event_id[:12]}...)",
        reasoning=f"Raw webhook payload received. Signature verification pending.",
        metadata={"event_type": event_type, "event_id": event_id,
                  "has_signature": bool(signature)}
    )

    # Verify signature (skip in mock mode if no signature provided)
    if signature:
        is_valid = razorpay_wrapper.verify_webhook_signature(body_bytes, signature)
        if not is_valid:
            emit_audit_event(
                db, EventType.WEBHOOK_REJECTED.value,
                actor="Razorpay", actor_type=AuditActor.RAZORPAY_WEBHOOK.value,
                summary=f"WEBHOOK REJECTED: Invalid signature for {event_type}",
                reasoning="HMAC SHA-256 signature verification failed. Possible tampering or replay attack.",
                metadata={"event_id": event_id}
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    elif not razorpay_wrapper.is_mock:
        # In production mode, signature is REQUIRED
        emit_audit_event(
            db, EventType.WEBHOOK_REJECTED.value,
            actor="Razorpay", actor_type=AuditActor.RAZORPAY_WEBHOOK.value,
            summary=f"WEBHOOK REJECTED: Missing signature header",
            reasoning="Production mode requires X-Razorpay-Signature header.",
            metadata={"event_id": event_id}
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    # 4. Check for duplicate webhook (idempotency)
    existing = db.query(DBProcessedWebhook).filter(
        DBProcessedWebhook.webhook_event_id == event_id
    ).first()

    if existing:
        emit_audit_event(
            db, EventType.WEBHOOK_DUPLICATE.value,
            actor="Razorpay", actor_type=AuditActor.RAZORPAY_WEBHOOK.value,
            summary=f"Duplicate webhook ignored: {event_id[:12]}...",
            reasoning="Event already processed. Idempotent — no state change.",
            metadata={"event_id": event_id, "original_processed_at": existing.processed_at.isoformat()}
        )
        db.commit()
        return {"status": "DUPLICATE", "event_id": event_id, "message": "Already processed"}

    # 5. Record webhook as processed
    processed = DBProcessedWebhook(
        webhook_event_id=event_id,
        event_type=event_type,
        payload_hash=hashlib.sha256(body_bytes).hexdigest()
    )
    db.add(processed)

    # Log successful verification
    emit_audit_event(
        db, EventType.WEBHOOK_VERIFIED.value,
        actor="Razorpay", actor_type=AuditActor.RAZORPAY_WEBHOOK.value,
        summary=f"Webhook verified: {event_type}",
        reasoning="Signature valid. Event ID not previously processed. Proceeding with state update.",
        metadata={"event_id": event_id, "event_type": event_type}
    )

    # 6. Extract payment details from webhook payload
    event_payload = payload.get("payload", {})
    payment_entity = event_payload.get("payment", {}).get("entity", {})
    order_entity = event_payload.get("order", {}).get("entity", {})

    notes = payment_entity.get("notes", {}) or order_entity.get("notes", {})
    razorpay_order_id = payment_entity.get("order_id") or order_entity.get("id")
    razorpay_payment_id = payment_entity.get("id")

    # 7. Process event
    if event_type in ("order.paid", "payment.authorized", "payment.captured"):
        return _handle_success(db, razorpay_order_id, razorpay_payment_id, event_type)

    elif event_type == "payment.failed":
        error_desc = payment_entity.get("error_description", "Payment failed")
        return _handle_failure(db, razorpay_order_id, error_desc, event_type)

    db.commit()
    return {"status": "IGNORED", "event": event_type}


def _handle_success(db: Session, razorpay_order_id: str,
                    razorpay_payment_id: str, event_type: str) -> dict:
    """Process successful payment webhook."""
    if not razorpay_order_id:
        db.commit()
        return {"status": "NO_ORDER_ID", "event": event_type}

    order = db.query(DBPaymentOrder).filter(
        DBPaymentOrder.razorpay_order_id == razorpay_order_id
    ).first()

    if not order:
        db.commit()
        return {"status": "ORDER_NOT_FOUND", "razorpay_order_id": razorpay_order_id}

    # Idempotent: if already SUCCESS, skip
    if order.status == PaymentStatus.SUCCESS.value:
        db.commit()
        return {"status": "ALREADY_SUCCESS", "order_id": razorpay_order_id}

    payment_agent.mark_payment_success(db, order, razorpay_payment_id)

    # Check pool completion
    deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == order.deal_id).first()
    pool_completed = False
    if deal:
        pool_completed = payment_agent.check_pool_completion(db, deal)

    db.commit()
    return {
        "status": "SUCCESS",
        "event": event_type,
        "order_id": razorpay_order_id,
        "pool_completed": pool_completed
    }


def _handle_failure(db: Session, razorpay_order_id: str,
                    reason: str, event_type: str) -> dict:
    """Process failed payment webhook and trigger recovery."""
    if not razorpay_order_id:
        db.commit()
        return {"status": "NO_ORDER_ID", "event": event_type}

    order = db.query(DBPaymentOrder).filter(
        DBPaymentOrder.razorpay_order_id == razorpay_order_id
    ).first()

    if not order:
        db.commit()
        return {"status": "ORDER_NOT_FOUND", "razorpay_order_id": razorpay_order_id}

    # Idempotent: if already FAILED, skip
    if order.status in (PaymentStatus.FAILED.value, PaymentStatus.RECOVERED.value):
        db.commit()
        return {"status": "ALREADY_PROCESSED", "order_id": razorpay_order_id}

    payment_agent.mark_payment_failed(db, order, reason)

    # Trigger recovery
    deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == order.deal_id).first()
    recovery_result = {}
    if deal:
        recovery_result = recovery_agent.evaluate_and_recover(db, deal, order, reason)

    db.commit()
    return {
        "status": "FAILED",
        "event": event_type,
        "order_id": razorpay_order_id,
        "recovery": recovery_result
    }
