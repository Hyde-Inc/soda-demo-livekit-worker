"""Environment configuration using pydantic-settings."""

from pydantic_settings import BaseSettings

from dotenv import load_dotenv
import os
load_dotenv(dotenv_path=".env.local")

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL")

    # Celery (defaults to Redis URL if not set)
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None

    # Dialer configuration
    DEFAULT_MAX_PARALLEL_CALLS: int = 20
    DEFAULT_REFILL_THRESHOLD: int = 20
    DEFAULT_MAX_ATTEMPTS: int = 2

    # Timezone
    TIMEZONE: str = "Asia/Kolkata"

    # Inflight timeout in minutes (for reconciliation)
    INFLIGHT_TIMEOUT_MINUTES: int = 15

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def celery_broker(self) -> str:
        """Get Celery broker URL, defaulting to Redis URL."""
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        """Get Celery result backend URL, defaulting to Redis URL."""
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL


settings = Settings()
