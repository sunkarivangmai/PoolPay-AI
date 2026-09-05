import uuid
import time
import hmac
import hashlib
import razorpay
from core.config import settings


class RazorpayClientWrapper:
    """
    Razorpay API wrapper with transparent mock/simulator fallback.
    Handles: order creation, payment verification, webhook signature verification.
    """

    def __init__(self):
        self.is_mock = settings.is_mock_razorpay
        self.client = None

        if not self.is_mock:
            try:
                self.client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            except Exception:
                self.is_mock = True

    def create_order(self, amount_paise: int, currency: str = "INR",
                     receipt: str = None, notes: dict = None,
                     idempotency_key: str = None) -> dict:
        """Create a Razorpay order. Amount must be in paise (integer)."""
        assert isinstance(amount_paise, int) and amount_paise > 0, "Amount must be positive integer paise"
        receipt_id = receipt or f"rcpt_{uuid.uuid4().hex[:12]}"

        if not self.is_mock and self.client:
            try:
                data = {
                    "amount": amount_paise,
                    "currency": currency,
                    "receipt": receipt_id,
                    "notes": notes or {}
                }
                order = self.client.order.create(data=data)
                return {**order, "is_mock": False}
            except Exception as e:
                # Fall through to mock on API failure
                return self._mock_order(amount_paise, currency, receipt_id, notes, error=str(e))

        return self._mock_order(amount_paise, currency, receipt_id, notes)

    def _mock_order(self, amount_paise: int, currency: str, receipt: str,
                    notes: dict = None, error: str = None) -> dict:
        mock_id = f"order_{uuid.uuid4().hex[:16]}"
        result = {
            "id": mock_id,
            "entity": "order",
            "amount": amount_paise,
            "amount_paid": 0,
            "amount_due": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
            "attempts": 0,
            "notes": notes or {},
            "created_at": int(time.time()),
            "is_mock": True
        }
        if error:
            result["mock_reason"] = f"Razorpay API failed: {error}"
        return result

    def verify_payment_signature(self, razorpay_order_id: str,
                                  razorpay_payment_id: str,
                                  razorpay_signature: str) -> bool:
        """
        Verify Razorpay payment signature after frontend checkout.
        Uses HMAC SHA-256: sign(order_id|payment_id) == signature.
        """
        if self.is_mock:
            # In mock mode, accept signatures starting with "mock_sig_"
            return razorpay_signature.startswith("mock_sig_")

        if self.client:
            try:
                self.client.utility.verify_payment_signature({
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                    "razorpay_signature": razorpay_signature
                })
                return True
            except Exception:
                return False
        return False

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        """
        Verify Razorpay webhook signature.
        HMAC SHA-256 of raw body against X-Razorpay-Signature header.
        """
        if self.is_mock:
            # In mock/demo mode, compute expected sig from mock secret
            expected = hmac.new(
                b"mock_webhook_secret",
                body,
                hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature)

        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        if not webhook_secret:
            return False

        try:
            if self.client:
                self.client.utility.verify_webhook_signature(
                    body.decode("utf-8"), signature, webhook_secret
                )
                return True
            else:
                expected = hmac.new(
                    webhook_secret.encode("utf-8"),
                    body,
                    hashlib.sha256
                ).hexdigest()
                return hmac.compare_digest(expected, signature)
        except Exception:
            return False

    def generate_mock_webhook_signature(self, body: bytes) -> str:
        """Generate valid mock webhook signature for testing."""
        return hmac.new(
            b"mock_webhook_secret",
            body,
            hashlib.sha256
        ).hexdigest()


razorpay_wrapper = RazorpayClientWrapper()
