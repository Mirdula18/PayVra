"""Import + wiring smoke tests. No database required."""

from __future__ import annotations

from datetime import UTC

from app.clock import now_utc, today
from app.models import Base
from app.money import paise_to_display

EXPECTED_TABLES = {
    "merchants",
    "counterparties",
    "contacts",
    "consents",
    "invoices",
    "payment_links",
    "actions",
    "messages",
    "promises",
    "replies",
    "webhook_events",
    "audit_log",
    "metrics_snapshots",
}


def test_all_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_app_imports_and_defines_health() -> None:
    from app.main import app

    routes = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in routes


def test_now_utc_is_timezone_aware() -> None:
    assert now_utc().tzinfo is UTC
    assert today().year >= 2026


def test_money_display_is_indian() -> None:
    assert paise_to_display(120_000) == "₹1,200"  # ₹1,200
    assert paise_to_display(42_000_000) == "₹4.2L"  # ₹4.2 lakh
    assert paise_to_display(1_400_000_000) == "₹1.4Cr"  # ₹1.4 crore
