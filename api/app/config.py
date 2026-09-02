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

    # Public base URL, used to build the opt-out links embedded in every outbound message.
    # Must be reachable by a recipient: a localhost opt-out link is not an opt-out mechanism.
    public_base_url: str = "http://localhost:8000"

    # --- Batch runner (Phase 6, ADR-009) ---

    # How many worklist rows one run may touch by default. Small on purpose: a run creates real
    # Razorpay links against a 25-link budget (razorpay/links.py), so the budget is the binding
    # constraint, not runtime.
    batch_run_default_limit: int = 5

    # Widens the contact-hours window for a run, and ONLY by explicit environment variable
    # (FR-16.8). Both must be set together or neither applies.
    #
    # This is not a bypass. Gate check 1 still executes and still refuses anything outside the
    # window it is given -- the window is a value the check reads, never a rule it can be told to
    # skip. An active override is written to the audit log and stamped on the recovery_runs row,
    # so an out-of-window run is compliant *by record*: a reader can see the window was widened
    # rather than having to take someone's word that it was not.
    #
    # Rehearsing inside 08:00-19:00 IST remains the recommended path. See the Phase 9 runbook.
    contact_window_override_start: int | None = None
    contact_window_override_end: int | None = None

    # Razorpay refuses payment links above a maximum amount, measured between Rs 5L and Rs 14L on
    # the live test account (ADR-006). Links are capped at this value and collected in tranches
    # with accept_partial, so an over-ceiling invoice fails predictably here rather than as a 400
    # mid-run on the highest-value account in the worklist.
    link_amount_ceiling_paise: int = 50_000_000  # Rs 5,00,000

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

    # Resend's shared testing sender. Using it means no domain to buy and no DNS to verify -- and
    # in exchange Resend will deliver only to the address the account is registered under. That
    # constraint is the whole reason RESEND_TO_OVERRIDE exists.
    resend_from: str = "onboarding@resend.dev"

    # **Every outbound email goes here instead of the counterparty's real address.**
    #
    # Not a convenience. The seeded contacts are @example.co.in, a reserved undeliverable domain,
    # and a demo database full of realistic-looking addresses is one config mistake away from
    # mailing strangers about invoices they do not owe. Sending is only enabled at all when this is
    # set, so the default state of the system is "cannot email anyone".
    #
    # Clearing it does not unlock real recipients; it disables sending. Real per-counterparty
    # delivery needs a verified domain and a deliberate change here.
    resend_to_override: str = ""

    msg91_api_key: str = ""
    whatsapp_token: str = ""

    # Secrets
    app_encryption_key: str = ""


settings = Settings()
