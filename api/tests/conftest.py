"""Shared test fixtures. DB-backed tests skip cleanly when no database is reachable."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine


@pytest.fixture()
def db_available() -> None:
    """Skip the test if the configured database is not reachable (e.g. `make db-up` not run)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database not available: {exc}")


@pytest.fixture()
def db_session(db_available: None) -> Iterator[Session]:
    """A session whose work is always rolled back, so the seeded database survives the suite.

    Tests must not call ``commit()`` on this session. Anything needing a committed transaction
    (the API tests) uses ``client`` instead, which cleans up after itself explicitly.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def api_merchant(db_available: None) -> Iterator[uuid.UUID]:
    """A committed throwaway merchant, for tests that go through HTTP.

    The API commits its own transactions, so these cannot ride on ``db_session``'s rollback.
    Everything created under this merchant is deleted afterwards, children first.
    """
    from app.models.merchant import Merchant

    merchant_id = uuid.uuid4()
    session = SessionLocal()
    try:
        session.add(Merchant(id=merchant_id, name="ApiTest", email=f"{merchant_id}@test.local"))
        session.commit()
    finally:
        session.close()

    yield merchant_id

    session = SessionLocal()
    try:
        # Children before parents; batch_rows references invoices, invoices reference
        # counterparties, and everything references the merchant.
        session.execute(
            text(
                "DELETE FROM batch_rows WHERE batch_id IN "
                "(SELECT id FROM batches WHERE merchant_id = :m)"
            ),
            {"m": merchant_id},
        )
        session.execute(text("DELETE FROM batches WHERE merchant_id = :m"), {"m": merchant_id})
        session.execute(text("DELETE FROM invoices WHERE merchant_id = :m"), {"m": merchant_id})
        session.execute(
            text(
                "DELETE FROM contacts WHERE counterparty_id IN "
                "(SELECT id FROM counterparties WHERE merchant_id = :m)"
            ),
            {"m": merchant_id},
        )
        session.execute(
            text("DELETE FROM counterparties WHERE merchant_id = :m"), {"m": merchant_id}
        )
        # The merchant row itself can only go if nothing scored it. audit_log has an FK to
        # merchants and its DELETE is a silent no-op (migration 0001's append-only rules), so a
        # merchant with audit entries is *permanently* undeletable -- by design. That is the
        # guarantee working, not a leak: a tenant's compliance trail outliving the tenant is the
        # entire point of an append-only log. Such merchants are left behind as an id plus their
        # audit rows, with no invoices or counterparties attached.
        has_audit = session.execute(
            text("SELECT EXISTS (SELECT 1 FROM audit_log WHERE merchant_id = :m)"),
            {"m": merchant_id},
        ).scalar_one()
        if not has_audit:
            session.execute(text("DELETE FROM merchants WHERE id = :m"), {"m": merchant_id})
        session.commit()
    finally:
        session.close()


@pytest.fixture()
def client(db_available: None) -> TestClient:
    """FastAPI TestClient.

    Deliberately *not* used as a context manager: entering one runs the lifespan, which would
    start a real APScheduler (and its Postgres job store) for every test. Exception handlers and
    routing work without it; only ``GET /health`` needs ``app.state.scheduler``.
    """
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)
