"""Shared test fixtures. DB-backed tests skip cleanly when no database is reachable."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import engine


@pytest.fixture()
def db_available() -> None:
    """Skip the test if the configured database is not reachable (e.g. `make db-up` not run)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database not available: {exc}")
