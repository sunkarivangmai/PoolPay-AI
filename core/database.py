import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Integer, Text, DateTime, ForeignKey, create_engine, Index
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from core.config import settings


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────
# Products
# ──────────────────────────────────────────────
class DBProduct(Base):
    __tablename__ = "products"
    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    image_url = Column(String, nullable=True)
    retail_price = Column(Float, nullable=False)
    target_pool_price = Column(Float, nullable=False)
    min_wholesale_price = Column(Float, nullable=False)
    pool_threshold = Column(Integer, default=5)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Intent Mandates (Buyer Authorization)
# ──────────────────────────────────────────────
class DBIntentMandate(Base):
    __tablename__ = "intent_mandates"
    mandate_id = Column(String, primary_key=True)
    buyer_id = Column(String, nullable=False, index=True)
    buyer_name = Column(String, nullable=False)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    pool_deal_id = Column(String, ForeignKey("pool_deals.deal_id"), nullable=True, index=True)
    max_price = Column(Float, nullable=False)
    authorized_price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)
    expires_at = Column(DateTime, nullable=False)
    status = Column(String, default="ACTIVE", index=True)
    signature_hash = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Pool Deals
# ──────────────────────────────────────────────
class DBPoolDeal(Base):
    __tablename__ = "pool_deals"
    deal_id = Column(String, primary_key=True)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    threshold = Column(Integer, nullable=False)
    target_pool_price = Column(Float, nullable=False)
    negotiated_price = Column(Float, nullable=True)
    discount_percent = Column(Float, default=0.0)
    status = Column(String, default="OPEN", index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Payment Orders (Per-Buyer Razorpay Orders)
# ──────────────────────────────────────────────
class DBPaymentOrder(Base):
    __tablename__ = "payment_orders"
    order_id = Column(String, primary_key=True)
    deal_id = Column(String, ForeignKey("pool_deals.deal_id"), nullable=False, index=True)
    mandate_id = Column(String, ForeignKey("intent_mandates.mandate_id"), nullable=False, index=True)
    buyer_id = Column(String, nullable=False)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String, default="INR")
    razorpay_order_id = Column(String, nullable=True, unique=True)
    razorpay_payment_id = Column(String, nullable=True)
    razorpay_signature = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=False, unique=True)
    status = Column(String, default="PENDING", index=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Negotiation Events
# ──────────────────────────────────────────────
class DBNegotiationEvent(Base):
    __tablename__ = "negotiation_events"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    deal_id = Column(String, ForeignKey("pool_deals.deal_id"), nullable=False, index=True)
    round_number = Column(Integer, nullable=False)
    buyer_agent = Column(String, nullable=False)
    merchant_agent = Column(String, nullable=False)
    pool_size = Column(Integer, nullable=False)
    buyer_offer = Column(Float, nullable=False)
    merchant_counter = Column(Float, nullable=False)
    discount_percent = Column(Float, nullable=False)
    decision = Column(String, nullable=False)
    reasoning = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Processed Webhooks (Duplicate Prevention)
# ──────────────────────────────────────────────
class DBProcessedWebhook(Base):
    __tablename__ = "processed_webhooks"
    webhook_event_id = Column(String, primary_key=True)
    event_type = Column(String, nullable=False)
    processed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    payload_hash = Column(String, nullable=True)


# ──────────────────────────────────────────────
# Unified Audit / Event Log
# ──────────────────────────────────────────────
class DBAuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(String, nullable=False, index=True)
    actor = Column(String, nullable=False)
    actor_type = Column(String, nullable=False, index=True)
    pool_id = Column(String, nullable=True, index=True)
    buyer_id = Column(String, nullable=True)
    transaction_id = Column(String, nullable=True)
    summary = Column(Text, nullable=False)
    reasoning = Column(Text, nullable=False)
    previous_state = Column(String, nullable=True)
    new_state = Column(String, nullable=True)
    metadata_json = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


# ──────────────────────────────────────────────
# Engine & Session
# ──────────────────────────────────────────────
engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def emit_audit_event(
    db,
    event_type: str,
    actor: str,
    actor_type: str,
    summary: str,
    reasoning: str,
    pool_id: str = None,
    buyer_id: str = None,
    transaction_id: str = None,
    previous_state: str = None,
    new_state: str = None,
    metadata: dict = None
):
    """Central audit event emitter. Every important action goes through here."""
    log = DBAuditLog(
        event_type=event_type,
        actor=actor,
        actor_type=actor_type,
        pool_id=pool_id,
        buyer_id=buyer_id,
        transaction_id=transaction_id,
        summary=summary,
        reasoning=reasoning,
        previous_state=previous_state,
        new_state=new_state,
        metadata_json=json.dumps(metadata) if metadata else None
    )
    db.add(log)
    return log


def init_db():
    """Create all tables and seed sample products."""
    Base.metadata.create_all(bind=engine)
    _seed_products()


def reset_db():
    """Drop and recreate all tables. Used in demo mode."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _seed_products()


def _seed_products():
    db = SessionLocal()
    try:
        if db.query(DBProduct).count() == 0:
            products = [
                DBProduct(
                    id="prod_airpods_pro",
                    title="Apple AirPods Pro (2nd Gen)",
                    description="Active Noise Cancellation, Adaptive Audio, USB-C Charging Case.",
                    image_url="https://images.unsplash.com/photo-1600294037681-c80b4cb5b434?w=600&auto=format&fit=crop&q=80",
                    retail_price=24900.0,
                    target_pool_price=18900.0,
                    min_wholesale_price=17500.0,
                    pool_threshold=5
                ),
                DBProduct(
                    id="prod_sony_headphones",
                    title="Sony WH-1000XM5 Headphones",
                    description="Industry leading noise cancellation with dual processors & 8 mics.",
                    image_url="https://images.unsplash.com/photo-1546435770-a3e426bf472b?w=600&auto=format&fit=crop&q=80",
                    retail_price=29990.0,
                    target_pool_price=22490.0,
                    min_wholesale_price=21000.0,
                    pool_threshold=5
                ),
                DBProduct(
                    id="prod_keychron_k2",
                    title="Keychron K2 Wireless Keyboard",
                    description="75% Layout Bluetooth Mechanical Keyboard with RGB Backlight.",
                    image_url="https://images.unsplash.com/photo-1587829741301-dc798b83add3?w=600&auto=format&fit=crop&q=80",
                    retail_price=8999.0,
                    target_pool_price=6499.0,
                    min_wholesale_price=5999.0,
                    pool_threshold=4
                )
            ]
            db.add_all(products)
            db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
