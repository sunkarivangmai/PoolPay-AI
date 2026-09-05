from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


# ──────────────────────────────────────────────
# Payment & Pool State Machines
# ──────────────────────────────────────────────

class PaymentStatus(str, Enum):
    """Complete payment state machine. Backend/webhook is source of truth."""
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    ORDER_CREATED = "ORDER_CREATED"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERED = "RECOVERED"

class MandateStatus(str, Enum):
    ACTIVE = "ACTIVE"
    POOLED = "POOLED"
    AUTHORIZED = "AUTHORIZED"      # payment authorization granted
    ORDER_CREATED = "ORDER_CREATED" # razorpay order exists
    EXECUTED = "EXECUTED"           # payment succeeded
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

class DealStatus(str, Enum):
    OPEN = "OPEN"
    NEGOTIATING = "NEGOTIATING"
    THRESHOLD_MET = "THRESHOLD_MET"
    PROCESSING_PAYMENTS = "PROCESSING_PAYMENTS"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    RECOVERY_IN_PROGRESS = "RECOVERY_IN_PROGRESS"
    FAILED = "FAILED"

class NegotiationDecision(str, Enum):
    OFFER = "OFFER"
    COUNTER_OFFER = "COUNTER_OFFER"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"

class RecoveryAction(str, Enum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    REQUEST_REAUTH = "REQUEST_REAUTH"
    REPLACE_BUYER = "REPLACE_BUYER"
    CONTINUE_REDUCED = "CONTINUE_REDUCED"
    RENEGOTIATE = "RENEGOTIATE"
    CANCEL_POOL = "CANCEL_POOL"


# ──────────────────────────────────────────────
# Actors & Event Types
# ──────────────────────────────────────────────

class AuditActor(str, Enum):
    BUYER_AGENT = "BUYER_AGENT"
    MERCHANT_AGENT = "MERCHANT_AGENT"
    NEGOTIATION_AGENT = "NEGOTIATION_AGENT"
    PAYMENT_AGENT = "PAYMENT_AGENT"
    RECOVERY_AGENT = "RECOVERY_AGENT"
    ORCHESTRATOR = "ORCHESTRATOR"
    RAZORPAY_WEBHOOK = "RAZORPAY_WEBHOOK"
    SYSTEM = "SYSTEM"

class EventType(str, Enum):
    # Pool lifecycle
    POOL_CREATED = "POOL_CREATED"
    BUYER_JOINED = "BUYER_JOINED"
    POOL_THRESHOLD_MET = "POOL_THRESHOLD_MET"
    POOL_COMPLETED = "POOL_COMPLETED"
    POOL_FAILED = "POOL_FAILED"
    # Agent decisions
    AGENT_DECISION = "AGENT_DECISION"
    MANDATE_SIGNED = "MANDATE_SIGNED"
    MARGIN_CHECK = "MARGIN_CHECK"
    # Negotiation
    NEGOTIATION_STARTED = "NEGOTIATION_STARTED"
    OFFER_CREATED = "OFFER_CREATED"
    COUNTER_OFFER_CREATED = "COUNTER_OFFER_CREATED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_REJECTED = "OFFER_REJECTED"
    # Payment
    PAYMENT_AUTHORIZED = "PAYMENT_AUTHORIZED"
    PAYMENT_AUTHORIZATION_DENIED = "PAYMENT_AUTHORIZATION_DENIED"
    RAZORPAY_ORDER_CREATED = "RAZORPAY_ORDER_CREATED"
    PAYMENT_ATTEMPTED = "PAYMENT_ATTEMPTED"
    PAYMENT_SUCCESS = "PAYMENT_SUCCESS"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    # Webhook
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    WEBHOOK_VERIFIED = "WEBHOOK_VERIFIED"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED"
    WEBHOOK_DUPLICATE = "WEBHOOK_DUPLICATE"
    # Recovery
    RECOVERY_STARTED = "RECOVERY_STARTED"
    RECOVERY_DECISION = "RECOVERY_DECISION"
    RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    # System
    SYSTEM_RESET = "SYSTEM_RESET"
    SIMULATION_TRIGGERED = "SIMULATION_TRIGGERED"

class FailureScenario(str, Enum):
    CARD_DECLINED = "CARD_DECLINED"
    PAYMENT_TIMEOUT = "PAYMENT_TIMEOUT"
    DUPLICATE_WEBHOOK = "DUPLICATE_WEBHOOK"
    INVALID_WEBHOOK_SIGNATURE = "INVALID_WEBHOOK_SIGNATURE"
    BUYER_LEAVES_POOL = "BUYER_LEAVES_POOL"
    POOL_BELOW_THRESHOLD = "POOL_BELOW_THRESHOLD"
    RAZORPAY_API_FAILURE = "RAZORPAY_API_FAILURE"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"


# ──────────────────────────────────────────────
# Request/Response Schemas
# ──────────────────────────────────────────────

class ProductResponse(BaseModel):
    id: str
    title: str
    description: str
    image_url: Optional[str] = None
    retail_price: float
    target_pool_price: float
    min_wholesale_price: float
    pool_threshold: int
    active_pool_id: Optional[str] = None
    created_at: str

class MandateCreate(BaseModel):
    buyer_id: str = Field(..., min_length=1, max_length=100)
    buyer_name: str = Field(..., min_length=1, max_length=200)
    product_id: str = Field(..., min_length=1)
    max_price: float = Field(..., gt=0, le=1000000)
    expiry_minutes: int = Field(default=60, ge=5, le=1440)

class IntentMandateSchema(BaseModel):
    mandate_id: str
    buyer_id: str
    buyer_name: str
    product_id: str
    max_price: float
    authorized_price: float
    quantity: int = 1
    expires_at: str
    status: str
    signature_hash: str
    created_at: str

class PoolDealResponse(BaseModel):
    deal_id: str
    product_id: str
    product_title: str
    target_pool_price: float
    retail_price: float
    threshold: int
    current_count: int
    status: str
    discount_percent: float = 0.0
    participating_mandates: List[IntentMandateSchema] = []
    razorpay_order_ids: List[str] = []
    created_at: str

class AgentDecisionTrace(BaseModel):
    """Structured agent decision — never exposes chain-of-thought."""
    agent: str
    input_summary: Dict[str, Any]
    decision: str
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)
    max_authorized_amount: Optional[float] = None
    expected_savings: Optional[float] = None
    action_taken: str
    timestamp: str

class NegotiationEventSchema(BaseModel):
    round_number: int
    buyer_agent: str
    merchant_agent: str
    pool_size: int
    buyer_offer: float
    merchant_counter: float
    discount_percent: float
    decision: str
    reasoning: str
    timestamp: str

class PaymentOrderSchema(BaseModel):
    order_id: str
    deal_id: str
    mandate_id: str
    buyer_id: str
    amount_paise: int
    currency: str = "INR"
    razorpay_order_id: Optional[str] = None
    idempotency_key: str
    status: str
    created_at: str
    updated_at: str

class PaymentAuthorizationPolicy(BaseModel):
    """Bounded payment authorization — agent NEVER gets unrestricted access."""
    max_transaction_amount: float
    allowed_currency: str = "INR"
    allowed_product_id: str
    allowed_pool_id: str
    authorization_expiry: str
    buyer_id: str
    mandate_id: str

class AuditLogResponse(BaseModel):
    id: str
    event_type: str
    actor: str
    actor_type: str
    pool_id: Optional[str] = None
    buyer_id: Optional[str] = None
    transaction_id: Optional[str] = None
    summary: str
    reasoning: str
    previous_state: Optional[str] = None
    new_state: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: str

class AgentStatusResponse(BaseModel):
    buyer_agent: str = "ACTIVE"
    merchant_agent: str = "ACTIVE"
    negotiation_agent: str = "ACTIVE"
    payment_agent: str = "READY"
    recovery_agent: str = "STANDBY"

class FailureSimulationRequest(BaseModel):
    deal_id: str = Field(..., min_length=1)
    scenario: FailureScenario
    target_mandate_id: Optional[str] = None

class DemoResetResponse(BaseModel):
    status: str
    message: str
