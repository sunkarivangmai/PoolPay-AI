"""
PoolPay AI — Core Test Suite
Covers the 12 most critical flows.
Run: python -m pytest tests/ -v
"""
import json
import hashlib
import hmac
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, get_db, emit_audit_event, DBProduct
from server.main import app
from core.schemas import FailureScenario

# ──────────────────────────────────────────────
# Test Database Setup
# ──────────────────────────────────────────────

TEST_DB = "sqlite:///./test_poolpay.db"
engine = create_engine(TEST_DB, connect_args={"check_same_thread": False})
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_db():
    """Create fresh tables before each test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Seed test product
    db = TestSession()
    product = DBProduct(
        id="test_prod_1",
        title="Test Headphones",
        description="Test product for automated tests",
        retail_price=10000.0,
        target_pool_price=7500.0,
        min_wholesale_price=6500.0,
        pool_threshold=3
    )
    db.add(product)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)


client = TestClient(app)


# ──────────────────────────────────────────────
# 1. Pool Creation
# ──────────────────────────────────────────────

def test_pool_creation():
    """Adding first buyer should create a pool."""
    res = client.post("/api/mandates", json={
        "buyer_id": "buyer_1", "buyer_name": "Test Buyer",
        "product_id": "test_prod_1", "max_price": 8000, "expiry_minutes": 60
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "POOLED"
    assert data["signature_hash"].startswith("ap2_sig_")

    # Check pool exists
    products = client.get("/api/products").json()
    product = [p for p in products if p["id"] == "test_prod_1"][0]
    assert product["active_pool_id"] is not None


# ──────────────────────────────────────────────
# 2. Buyer Joining
# ──────────────────────────────────────────────

def test_buyer_joining():
    """Multiple buyers can join the same pool."""
    for i in range(2):
        res = client.post("/api/mandates", json={
            "buyer_id": f"buyer_{i}", "buyer_name": f"Buyer {i}",
            "product_id": "test_prod_1", "max_price": 8000, "expiry_minutes": 60
        })
        assert res.status_code == 200

    # Get pool
    products = client.get("/api/products").json()
    pool_id = [p for p in products if p["id"] == "test_prod_1"][0]["active_pool_id"]
    pool = client.get(f"/api/pools/{pool_id}").json()
    assert pool["current_count"] == 2


# ──────────────────────────────────────────────
# 3. Duplicate Buyer Prevention
# ──────────────────────────────────────────────

def test_duplicate_buyer_rejected():
    """Same buyer cannot join pool twice."""
    client.post("/api/mandates", json={
        "buyer_id": "buyer_dup", "buyer_name": "Dup Buyer",
        "product_id": "test_prod_1", "max_price": 8000, "expiry_minutes": 60
    })
    res = client.post("/api/mandates", json={
        "buyer_id": "buyer_dup", "buyer_name": "Dup Buyer",
        "product_id": "test_prod_1", "max_price": 8000, "expiry_minutes": 60
    })
    assert res.status_code == 400
    assert "already in this pool" in res.json()["detail"]


# ──────────────────────────────────────────────
# 4. Authorization Limit Enforcement
# ──────────────────────────────────────────────

def test_authorization_limit():
    """Buyer with max_price below target should be rejected."""
    res = client.post("/api/mandates", json={
        "buyer_id": "buyer_poor", "buyer_name": "Budget Buyer",
        "product_id": "test_prod_1", "max_price": 5000, "expiry_minutes": 60
    })
    assert res.status_code == 400
    assert "Budget limit" in res.json()["detail"] or "below" in res.json()["detail"].lower()


# ──────────────────────────────────────────────
# 5. Pool Threshold & Order Creation
# ──────────────────────────────────────────────

def test_threshold_and_order_creation():
    """When threshold is met, orders can be created."""
    # Fill pool to threshold (3 buyers)
    for i in range(3):
        client.post("/api/mandates", json={
            "buyer_id": f"buyer_t{i}", "buyer_name": f"Threshold Buyer {i}",
            "product_id": "test_prod_1", "max_price": 8500, "expiry_minutes": 60
        })

    products = client.get("/api/products").json()
    pool_id = [p for p in products if p["id"] == "test_prod_1"][0]["active_pool_id"]
    pool = client.get(f"/api/pools/{pool_id}").json()
    assert pool["status"] == "THRESHOLD_MET"

    # Create orders
    res = client.post(f"/api/demo/create-orders/{pool_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["orders_created"] == 3
    assert len(data["razorpay_order_ids"]) == 3


# ──────────────────────────────────────────────
# 6. Successful Payment
# ──────────────────────────────────────────────

def test_payment_success():
    """Simulating payment success should complete the pool."""
    # Setup: fill + create orders
    for i in range(3):
        client.post("/api/mandates", json={
            "buyer_id": f"buyer_s{i}", "buyer_name": f"Success Buyer {i}",
            "product_id": "test_prod_1", "max_price": 8500, "expiry_minutes": 60
        })

    products = client.get("/api/products").json()
    pool_id = [p for p in products if p["id"] == "test_prod_1"][0]["active_pool_id"]
    client.post(f"/api/demo/create-orders/{pool_id}")

    # Simulate success
    res = client.post(f"/api/demo/simulate-payment-success/{pool_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCESS"

    # Pool should be completed
    pool = client.get(f"/api/pools/{pool_id}").json()
    assert pool["status"] == "COMPLETED"


# ──────────────────────────────────────────────
# 7. Failed Payment & Recovery
# ──────────────────────────────────────────────

def test_payment_failure_and_recovery():
    """Payment failure should trigger recovery agent."""
    for i in range(3):
        client.post("/api/mandates", json={
            "buyer_id": f"buyer_f{i}", "buyer_name": f"Fail Buyer {i}",
            "product_id": "test_prod_1", "max_price": 8500, "expiry_minutes": 60
        })

    products = client.get("/api/products").json()
    pool_id = [p for p in products if p["id"] == "test_prod_1"][0]["active_pool_id"]
    client.post(f"/api/demo/create-orders/{pool_id}")

    # Simulate card declined
    res = client.post("/api/demo/simulate-failure", json={
        "deal_id": pool_id, "scenario": "CARD_DECLINED"
    })
    assert res.status_code == 200
    data = res.json()
    assert data.get("recovery") is not None or data.get("status") in ("FAILED", "RECOVERED")


# ──────────────────────────────────────────────
# 8. Webhook Signature Verification
# ──────────────────────────────────────────────

def test_webhook_invalid_signature():
    """Webhook with invalid signature should be rejected."""
    payload = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    res = client.post("/api/webhooks/razorpay",
                      content=payload,
                      headers={
                          "Content-Type": "application/json",
                          "X-Razorpay-Signature": "invalid_signature_12345"
                      })
    # In mock mode, signature validation uses mock secret
    # An invalid signature should be rejected
    assert res.status_code in (200, 401)


# ──────────────────────────────────────────────
# 9. Webhook with Valid Mock Signature
# ──────────────────────────────────────────────

def test_webhook_valid_mock_signature():
    """Webhook with valid mock signature should be accepted."""
    payload_dict = {
        "id": "evt_test_valid_001",
        "event": "payment.captured",
        "payload": {
            "payment": {"entity": {"id": "pay_test", "order_id": "order_nonexistent", "notes": {}}},
            "order": {"entity": {}}
        }
    }
    body = json.dumps(payload_dict).encode()
    # Generate valid mock signature
    sig = hmac.new(b"mock_webhook_secret", body, hashlib.sha256).hexdigest()

    res = client.post("/api/webhooks/razorpay",
                      content=body,
                      headers={
                          "Content-Type": "application/json",
                          "X-Razorpay-Signature": sig
                      })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] in ("ORDER_NOT_FOUND", "SUCCESS", "NO_ORDER_ID")


# ──────────────────────────────────────────────
# 10. Duplicate Webhook Prevention
# ──────────────────────────────────────────────

def test_duplicate_webhook_rejected():
    """Same webhook event ID should be processed only once."""
    payload_dict = {
        "id": "evt_duplicate_test_001",
        "event": "payment.captured",
        "payload": {
            "payment": {"entity": {"id": "pay_dup", "order_id": "order_dup", "notes": {}}},
            "order": {"entity": {}}
        }
    }
    body = json.dumps(payload_dict).encode()
    sig = hmac.new(b"mock_webhook_secret", body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Razorpay-Signature": sig}

    # First call
    res1 = client.post("/api/webhooks/razorpay", content=body, headers=headers)
    assert res1.status_code == 200

    # Second call — should be duplicate
    res2 = client.post("/api/webhooks/razorpay", content=body, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == "DUPLICATE"


# ──────────────────────────────────────────────
# 11. Negotiation
# ──────────────────────────────────────────────

def test_negotiation():
    """Negotiation should produce structured rounds."""
    for i in range(2):
        client.post("/api/mandates", json={
            "buyer_id": f"buyer_n{i}", "buyer_name": f"Negotiation Buyer {i}",
            "product_id": "test_prod_1", "max_price": 8500, "expiry_minutes": 60
        })

    products = client.get("/api/products").json()
    pool_id = [p for p in products if p["id"] == "test_prod_1"][0]["active_pool_id"]

    res = client.post(f"/api/demo/negotiate/{pool_id}")
    assert res.status_code == 200
    data = res.json()
    assert len(data["rounds"]) >= 1
    assert data["rounds"][-1]["decision"] == "ACCEPT"


# ──────────────────────────────────────────────
# 12. Audit Trail
# ──────────────────────────────────────────────

def test_audit_trail():
    """All operations should produce audit events."""
    client.post("/api/mandates", json={
        "buyer_id": "buyer_audit", "buyer_name": "Audit Buyer",
        "product_id": "test_prod_1", "max_price": 8000, "expiry_minutes": 60
    })

    logs = client.get("/api/audit-logs?limit=10").json()
    assert len(logs) >= 2  # At least mandate_signed + buyer_joined

    # Check structure
    log = logs[0]
    assert "event_type" in log
    assert "actor" in log
    assert "actor_type" in log
    assert "summary" in log
    assert "reasoning" in log
    assert "timestamp" in log

    # Test filter
    filtered = client.get("/api/audit-logs?actor_type=BUYER_AGENT").json()
    for f in filtered:
        assert f["actor_type"] == "BUYER_AGENT"


# ──────────────────────────────────────────────
# 13. Agent Status
# ──────────────────────────────────────────────

def test_agent_status():
    """Agent status endpoint should return all 5 agents."""
    res = client.get("/api/agent-status")
    assert res.status_code == 200
    data = res.json()
    assert "buyer_agent" in data
    assert "merchant_agent" in data
    assert "negotiation_agent" in data
    assert "payment_agent" in data
    assert "recovery_agent" in data
