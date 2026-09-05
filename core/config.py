import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PROJECT_NAME: str = "PoolPay AI"
    VERSION: str = "2.0.0"
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./poolpay.db")

    # Razorpay Credentials (must be set via .env for real mode)
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    # Simulation mode: when True OR when no real keys provided, use mock Razorpay
    MOCK_RAZORPAY: bool = os.getenv("MOCK_RAZORPAY", "true").lower() in ("true", "1", "yes")

    @property
    def is_mock_razorpay(self) -> bool:
        """True if no real Razorpay keys are configured or mock mode is explicit."""
        if self.MOCK_RAZORPAY:
            return True
        if not self.RAZORPAY_KEY_ID or not self.RAZORPAY_KEY_SECRET:
            return True
        if self.RAZORPAY_KEY_ID.startswith("rzp_test_poolpay"):
            return True
        return False

    # Demo mode: enables demo control endpoints
    DEMO_MODE: bool = os.getenv("DEMO_MODE", "true").lower() in ("true", "1", "yes")

settings = Settings()
