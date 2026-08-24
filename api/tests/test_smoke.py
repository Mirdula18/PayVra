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
    # Phase 1 (migration 0003): ingestion provenance and the repair queue.
    "batches",
    "batch_rows",
}


def test_all_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def _api_paths() -> set[str]:
    """Every path this app actually exposes, read from its OpenAPI schema.

    Not ``app.routes``: routers added via ``include_router`` appear there as an opaque
    ``_IncludedRouter`` with neither a ``.path`` nor nested ``.routes``. The OpenAPI schema is
    both accurate and the thing the frontend is generated against.
    """
    from app.main import app

    return set(app.openapi()["paths"])


def test_app_imports_and_defines_health() -> None:
    assert "/health" in _api_paths()


def test_ingestion_routes_are_mounted_under_the_api_prefix() -> None:
    """architecture/api-contracts.md: base path is /api/v1, and these four endpoints exactly."""
    from app.main import API_PREFIX

    paths = _api_paths()
    assert f"{API_PREFIX}/batches" in paths
    assert f"{API_PREFIX}/batches/{{batch_id}}/repairs" in paths
    assert f"{API_PREFIX}/batches/{{batch_id}}/repairs/{{row_id}}" in paths
    assert f"{API_PREFIX}/batches/{{batch_id}}/mapping" in paths


def test_now_utc_is_timezone_aware() -> None:
    assert now_utc().tzinfo is UTC
    assert today().year >= 2026


def test_money_display_is_indian() -> None:
    assert paise_to_display(120_000) == "₹1,200"  # ₹1,200
    assert paise_to_display(42_000_000) == "₹4.2L"  # ₹4.2 lakh
    assert paise_to_display(1_400_000_000) == "₹1.4Cr"  # ₹1.4 crore
