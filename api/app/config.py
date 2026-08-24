"""Application settings, loaded from the repo-root ``.env`` (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> app -> api -> <repo root>; the .env lives at the repo root so the same file
# is found regardless of the cwd a command runs from (uvicorn, alembic, python -m app.seed).
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core
    env: str = "dev"
    log_level: str = "INFO"

    # Database — the only switch between local Docker Postgres and Neon.
    # Local Docker publishes on host 5433 (docker-compose.yml) to dodge a native Postgres on 5432.
    database_url: str = "postgresql+psycopg://payvra:payvra@localhost:5433/payvra"

    # Scheduler
    scheduler_heartbeat_seconds: int = 60

    # Agent / LLM
    llm_enabled: bool = False
    reply_confidence_threshold: float = 0.70
    groq_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""

    # Razorpay (test mode only)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Delivery providers
    resend_api_key: str = ""
    msg91_api_key: str = ""
    whatsapp_token: str = ""

    # Secrets
    app_encryption_key: str = ""


settings = Settings()
