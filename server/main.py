import json
import uuid
import logging
from typing import List, Optional
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from core.config import settings
from core.database import (init_db, reset_db, get_db, DBProduct, DBPoolDeal,
                           DBIntentMandate, DBPaymentOrder, DBAuditLog,
                           DBNegotiationEvent, emit_audit_event)
from core.schemas import (
    ProductResponse, MandateCreate, IntentMandateSchema,
    PoolDealResponse, AuditLogResponse, AgentDecisionTrace,
    AgentStatusResponse, FailureSimulationRequest, DemoResetResponse,
    NegotiationEventSchema, PaymentOrderSchema,
    MandateStatus, DealStatus, PaymentStatus, EventType, AuditActor,
    FailureScenario
)
from agents.orchestrator import orchestrator
from server.webhooks import router as webhooks_router

# Structured logging — never logs secrets
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("poolpay")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("PoolPay AI backend started (mock_mode=%s, demo_mode=%s)",
                settings.is_mock_razorpay, settings.DEMO_MODE)
    yield

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(webhooks_router)


# ──────────────────────────────────────────────
# Health & Status
# ──────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "mock_mode": settings.is_mock_razorpay,
        "demo_mode": settings.DEMO_MODE,
        "docs": "/docs"
    }

@app.get("/api/agent-status", response_model=AgentStatusResponse)
def get_agent_status(db: Session = Depends(get_db)):
    """Return current status of all 5 agents."""
    status = orchestrator.get_agent_status(db)
    return AgentStatusResponse(**status)


# ──────────────────────────────────────────────
# Products
# ──────────────────────────────────────────────

@app.get("/api/products", response_model=List[ProductResponse])
def get_products(db: Session = Depends(get_db)):
    products = db.query(DBProduct).all()
    result = []
    for p in products:
        deal = db.query(DBPoolDeal).filter(
            DBPoolDeal.product_id == p.id,
            DBPoolDeal.status.in_([
                DealStatus.OPEN.value, DealStatus.NEGOTIATING.value,
                DealStatus.THRESHOLD_MET.value, DealStatus.PROCESSING_PAYMENTS.value,
                DealStatus.RECOVERY_IN_PROGRESS.value
            ])
        ).first()
        result.append(ProductResponse(
            id=p.id, title=p.title, description=p.description,
            image_url=p.image_url, retail_price=p.retail_price,
            target_pool_price=p.target_pool_price,
            min_wholesale_price=p.min_wholesale_price,
            pool_threshold=p.pool_threshold,
            active_pool_id=deal.deal_id if deal else None,
            created_at=p.created_at.isoformat()
        ))
    return result


# ──────────────────────────────────────────────
# Buyer Mandates
# ──────────────────────────────────────────────

@app.post("/api/mandates", response_model=IntentMandateSchema)
def submit_mandate(req: MandateCreate, db: Session = Depends(get_db)):
    """Submit buyer mandate. BuyerAgent evaluates + signs, MerchantAgent validates + joins pool."""
    logger.info("Mandate submission: buyer=%s product=%s max_price=%.0f",
                req.buyer_id, req.product_id, req.max_price)
    try:
        result = orchestrator.submit_buyer_mandate(
            db=db, buyer_id=req.buyer_id, buyer_name=req.buyer_name,
            product_id=req.product_id, max_price=req.max_price,
            expiry_minutes=req.expiry_minutes
        )
        mandate = result["mandate"]
        return IntentMandateSchema(
            mandate_id=mandate.mandate_id, buyer_id=mandate.buyer_id,
            buyer_name=mandate.buyer_name, product_id=mandate.product_id,
            max_price=mandate.max_price, authorized_price=mandate.authorized_price,
            quantity=mandate.quantity, expires_at=mandate.expires_at.isoformat(),
            status=mandate.status, signature_hash=mandate.signature_hash,
            created_at=mandate.created_at.isoformat()
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ──────────────────────────────────────────────
# Pool Deals
# ──────────────────────────────────────────────

@app.get("/api/pools/{deal_id}", response_model=PoolDealResponse)
def get_pool(deal_id: str, db: Session = Depends(get_db)):
    deal = db.query(DBPoolDeal).filter(DBPoolDeal.deal_id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Pool not found")

    product = db.query(DBProduct).filter(DBProduct.id == deal.product_id).first()
    mandates = db.query(DBIntentMandate).filter(DBIntentMandate.pool_deal_id == deal_id).all()
    orders = db.query(DBPaymentOrder).filter(DBPaymentOrder.deal_id == deal_id).all()

    mandate_schemas = [
        IntentMandateSchema(
            mandate_id=m.mandate_id, buyer_id=m.buyer_id, buyer_name=m.buyer_name,
            product_id=m.product_id, max_price=m.max_price,
            authorized_price=m.authorized_price, quantity=m.quantity,
            expires_at=m.expires_at.isoformat(), status=m.status,
            signature_hash=m.signature_hash, created_at=m.created_at.isoformat()
        ) for m in mandates
    ]

    return PoolDealResponse(
        deal_id=deal.deal_id, product_id=deal.product_id,
        product_title=product.title if product else "Product",
        target_pool_price=deal.target_pool_price,
        retail_price=product.retail_price if product else 0,
        threshold=deal.threshold,
        current_count=len([m for m in mandates if m.status not in
                           (MandateStatus.FAILED.value, MandateStatus.CANCELLED.value, MandateStatus.EXPIRED.value)]),
        status=deal.status,
        discount_percent=deal.discount_percent or 0.0,
        participating_mandates=mandate_schemas,
        razorpay_order_ids=[o.razorpay_order_id for o in orders if o.razorpay_order_id],
        created_at=deal.created_at.isoformat()
    )


# ──────────────────────────────────────────────
# Payment Orders
# ──────────────────────────────────────────────

@app.get("/api/payment-orders/{deal_id}", response_model=List[PaymentOrderSchema])
def get_payment_orders(deal_id: str, db: Session = Depends(get_db)):
    orders = db.query(DBPaymentOrder).filter(DBPaymentOrder.deal_id == deal_id).all()
    return [
        PaymentOrderSchema(
            order_id=o.order_id, deal_id=o.deal_id, mandate_id=o.mandate_id,
            buyer_id=o.buyer_id, amount_paise=o.amount_paise, currency=o.currency,
            razorpay_order_id=o.razorpay_order_id, idempotency_key=o.idempotency_key,
            status=o.status, created_at=o.created_at.isoformat(),
            updated_at=o.updated_at.isoformat()
        ) for o in orders
    ]


# ──────────────────────────────────────────────
# Negotiation
# ──────────────────────────────────────────────

@app.get("/api/negotiations/{deal_id}", response_model=List[NegotiationEventSchema])
def get_negotiations(deal_id: str, db: Session = Depends(get_db)):
    events = db.query(DBNegotiationEvent).filter(
        DBNegotiationEvent.deal_id == deal_id
    ).order_by(DBNegotiationEvent.round_number).all()
    return [
        NegotiationEventSchema(
            round_number=e.round_number, buyer_agent=e.buyer_agent,
            merchant_agent=e.merchant_agent, pool_size=e.pool_size,
            buyer_offer=e.buyer_offer, merchant_counter=e.merchant_counter,
            discount_percent=e.discount_percent, decision=e.decision,
            reasoning=e.reasoning, timestamp=e.timestamp.isoformat()
        ) for e in events
    ]


# ──────────────────────────────────────────────
# Audit Log
# ──────────────────────────────────────────────

@app.get("/api/audit-logs", response_model=List[AuditLogResponse])
def get_audit_logs(
    limit: int = Query(50, le=200),
    actor_type: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(DBAuditLog).order_by(DBAuditLog.timestamp.desc())

    if actor_type and actor_type != "ALL":
        query = query.filter(DBAuditLog.actor_type == actor_type)
    if event_type:
        query = query.filter(DBAuditLog.event_type == event_type)

    logs = query.limit(limit).all()
    result = []
    for log in logs:
        meta = None
        if log.metadata_json:
            try:
                meta = json.loads(log.metadata_json)
            except Exception:
                meta = {"raw": log.metadata_json}
        result.append(AuditLogResponse(
            id=log.id, event_type=log.event_type, actor=log.actor,
            actor_type=log.actor_type, pool_id=log.pool_id,
            buyer_id=log.buyer_id, transaction_id=log.transaction_id,
            summary=log.summary, reasoning=log.reasoning,
            previous_state=log.previous_state, new_state=log.new_state,
            metadata=meta, timestamp=log.timestamp.isoformat()
        ))
    return result


# ──────────────────────────────────────────────
# Demo Controls (only active in DEMO_MODE)
# ──────────────────────────────────────────────

@app.post("/api/demo/reset", response_model=DemoResetResponse)
def demo_reset(db: Session = Depends(get_db)):
    """Reset database to clean state."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")
    reset_db()
    logger.info("Database reset via demo control")
    return DemoResetResponse(status="SUCCESS", message="Database reset. Ready for demo.")

@app.post("/api/demo/auto-fill-pool/{product_id}")
def demo_auto_fill(product_id: str, count: int = Query(4, ge=1, le=10),
                   db: Session = Depends(get_db)):
    """Add simulated buyer agents to a pool."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")

    product = db.query(DBProduct).filter(DBProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    demo_buyers = [
        ("buyer_ai_alpha", "Alpha Buyer Agent"),
        ("buyer_ai_beta", "Beta Autonomous Bot"),
        ("buyer_ai_gamma", "Gamma Smart Shopper"),
        ("buyer_ai_delta", "Delta Protocol Agent"),
        ("buyer_ai_epsilon", "Epsilon Hedge Agent"),
        ("buyer_ai_zeta", "Zeta Market Agent"),
        ("buyer_ai_eta", "Eta Bargain Agent"),
        ("buyer_ai_theta", "Theta Price Agent"),
    ]

    added = []
    for i in range(min(count, len(demo_buyers))):
        b_id, b_name = demo_buyers[i]
        try:
            result = orchestrator.submit_buyer_mandate(
                db=db, buyer_id=b_id, buyer_name=b_name,
                product_id=product_id,
                max_price=product.target_pool_price * 1.15,
                expiry_minutes=120
            )
            added.append(result["mandate"].mandate_id)
        except ValueError:
            continue  # Skip duplicates

    logger.info("Demo auto-fill: added %d buyers to product %s", len(added), product_id)
    return {"status": "SUCCESS", "added": len(added), "product_id": product_id}

@app.post("/api/demo/negotiate/{deal_id}")
def demo_negotiate(deal_id: str, db: Session = Depends(get_db)):
    """Run negotiation rounds for a pool."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")
    try:
        result = orchestrator.run_negotiation(db, deal_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/demo/create-orders/{deal_id}")
def demo_create_orders(deal_id: str, db: Session = Depends(get_db)):
    """Create Razorpay orders for a threshold-met pool."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")
    try:
        result = orchestrator.create_payment_orders(db, deal_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/demo/simulate-payment-success/{deal_id}")
def demo_payment_success(deal_id: str, db: Session = Depends(get_db)):
    """Simulate all payments succeeding for a pool."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")

    orders = db.query(DBPaymentOrder).filter(
        DBPaymentOrder.deal_id == deal_id,
        DBPaymentOrder.status.in_([PaymentStatus.ORDER_CREATED.value, PaymentStatus.PROCESSING.value])
    ).all()

    if not orders:
        raise HTTPException(status_code=400, detail="No pending orders found")

    results = []
    for order in orders:
        r = orchestrator.handle_payment_result(
            db, order.razorpay_order_id, success=True,
            payment_id=f"pay_mock_{uuid.uuid4().hex[:12]}"
        )
        results.append(r)

    return {"status": "SUCCESS", "orders_processed": len(results), "results": results}

@app.post("/api/demo/simulate-failure")
def demo_simulate_failure(req: FailureSimulationRequest, db: Session = Depends(get_db)):
    """Simulate various failure scenarios."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")

    logger.info("Failure simulation: scenario=%s pool=%s", req.scenario.value, req.deal_id)
    try:
        result = orchestrator.simulate_failure(
            db, req.deal_id, req.scenario, req.target_mandate_id
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/demo/simulate-webhook/{deal_id}")
def demo_simulate_webhook(deal_id: str, event_type: str = Query("payment.captured"),
                           db: Session = Depends(get_db)):
    """Simulate a Razorpay webhook event with proper signature."""
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode not enabled")

    order = db.query(DBPaymentOrder).filter(
        DBPaymentOrder.deal_id == deal_id
    ).first()

    if not order:
        raise HTTPException(status_code=400, detail="No payment orders found for this pool")

    return {
        "status": "WEBHOOK_SIMULATED",
        "event_type": event_type,
        "razorpay_order_id": order.razorpay_order_id,
        "note": "Use POST /api/webhooks/razorpay with proper signature to process"
    }
