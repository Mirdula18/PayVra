"""Shared test fixtures. DB-backed tests skip cleanly when no database is reachable."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import SessionLocal
from app.razorpay.client import RazorpayClient

# How long the reachability probe waits before declaring the database unavailable. Short on
# purpose: this is a "should I skip?" question, not a connection anyone will use.
DB_PROBE_TIMEOUT_SECONDS = 3

# Cached across the session. Without this, an unreachable database costs the timeout once per
# DB-backed test -- roughly 380 of them -- which is its own kind of hang.
_probe_result: str | None | bool = False


def _probe_failure() -> str | None:
    """Return None if the database is reachable, else a short reason. Never raises, never hangs.

    A dedicated engine with ``connect_timeout``, not the application's. The app engine has no
    timeout, so ``engine.connect()`` against a host that accepts packets but never completes a
    handshake -- a stopped Docker VM, a sleeping Neon endpoint, a dropped VPN -- blocks
    indefinitely. That is exactly what happened here: the guard could only ever fire against a
    fast ECONNREFUSED, so an unavailable database hung the whole run instead of skipping it.
    A guard that only works when the failure is already obvious is not a guard.
    """
    global _probe_result
    if _probe_result is not False:
        return _probe_result  # type: ignore[return-value]

    probe = create_engine(
        settings.database_url,
        connect_args={"connect_timeout": DB_PROBE_TIMEOUT_SECONDS},
        poolclass=NullPool,
    )
    try:
        with probe.connect() as conn:
            conn.execute(text("select 1"))
        _probe_result = None
    except Exception as exc:  # noqa: BLE001 - any failure to connect means "skip", not "error"
        # Deliberately broad. OperationalError is the common case, but a bad URL raises
        # ArgumentError and a missing driver raises ModuleNotFoundError, and neither should
        # surface as 380 errors when the honest answer is "no database here".
        _probe_result = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    finally:
        probe.dispose()
    return _probe_result  # type: ignore[return-value]


@pytest.fixture()
def stub_razorpay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Razorpay transport for live-mode runs.

    Without this a non-dry run reaches the real API, because ``.env`` carries working test-mode
    credentials. A unit test must never depend on a third party being up, and must never spend
    link budget.
    """

    def make() -> RazorpayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            link_id = f"plink_{uuid.uuid4().hex[:12]}"
            return httpx.Response(
                200,
                json={
                    "id": link_id,
                    "short_url": f"https://rzp.io/i/{link_id}",
                    "status": "created",
                },
            )

        client = RazorpayClient(key_id="rzp_test_stub", key_secret="s")
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://api.razorpay.test/v1"
        )
        return client

    from app.agent import runner as runner_mod

    monkeypatch.setattr(runner_mod, "RazorpayClient", make)


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Nothing in the test suite may send a real email. Ever.**

    Not a preference. ``.env`` carries a working Resend key and a real override recipient, so the
    moment a transport existed the runner tests began delivering to an actual inbox -- four or five
    messages on the first full run after Phase 6.5 landed, before anyone noticed. A test that can
    reach a provider will reach it, on every developer machine and in CI, and the failure is silent
    because a successful send looks like a passing test.

    Clearing the API key makes ``email.is_configured()`` false, so the transport refuses before it
    builds a request. Tests that need to exercise delivery stub ``httpx.post`` and re-enable
    configuration explicitly -- opting *in* per test, never inheriting it from the environment.
    """
    from app.delivery import email as email_mod

    monkeypatch.setattr(email_mod.settings, "resend_api_key", "", raising=False)
    monkeypatch.setattr(email_mod.settings, "resend_to_override", "", raising=False)


@pytest.fixture()
def db_available() -> None:
    """Skip the test if the configured database is not reachable (e.g. `make db-up` not run)."""
    failure = _probe_failure()
    if failure is not None:
        pytest.skip(f"database not available: {failure}")


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
        # Children of invoices first. Ordered by FK depth, not alphabetically.
        session.execute(
            text(
                "DELETE FROM promises WHERE invoice_id IN "
                "(SELECT id FROM invoices WHERE merchant_id = :m)"
            ),
            {"m": merchant_id},
        )
        session.execute(
            text(
                "DELETE FROM replies WHERE invoice_id IN "
                "(SELECT id FROM invoices WHERE merchant_id = :m)"
            ),
            {"m": merchant_id},
        )
        session.execute(
            text(
                "DELETE FROM payment_links WHERE invoice_id IN "
                "(SELECT id FROM invoices WHERE merchant_id = :m)"
            ),
            {"m": merchant_id},
        )
        session.execute(
            text("UPDATE actions SET message_id = NULL WHERE merchant_id = :m"), {"m": merchant_id}
        )
        session.execute(
            text(
                "DELETE FROM messages WHERE action_id IN "
                "(SELECT id FROM actions WHERE merchant_id = :m)"
            ),
            {"m": merchant_id},
        )
        session.execute(text("DELETE FROM actions WHERE merchant_id = :m"), {"m": merchant_id})
        # After actions, which carry the FK to it. Missing here until the UI grew a button that
        # starts a run: no test had ever created one for this merchant, so the gap stayed
        # invisible and then surfaced as an FK violation on the merchant delete.
        session.execute(
            text("DELETE FROM recovery_runs WHERE merchant_id = :m"), {"m": merchant_id}
        )
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


# --- guardrail gate fixtures ------------------------------------------------------------------
# Namespaced ``gate_*`` rather than plain ``merchant``/``invoice``: other test modules define
# local fixtures under those names, and a conftest-level one of the same name is the kind of
# quiet override that makes a failure hard to read.


@pytest.fixture()
def gate_merchant(db_session: Session) -> Iterator[Any]:
    """A throwaway merchant, whose committed rows are cleaned up afterwards.

    ``db_session`` rolls back, which is enough for tests that only flush. The batch runner
    **commits** — it has to, so an interrupted run keeps the work it did — so its rows survive the
    rollback and accumulate in the developer's database as ``INV-GATE-*`` links and actions that
    look like real demo data.

    ``audit_log`` is deliberately not cleaned: DELETE on it is a no-op by rule (migration 0001),
    which is the append-only guarantee working as intended. That leaves the merchant row itself
    undeletable too, since audit entries reference it. Test merchants are therefore expected
    residue; their *invoices, links and actions* are not, and those are what pollute a demo.
    """
    from app.models.merchant import Merchant

    row = Merchant(id=uuid.uuid4(), name="GateTest", email=f"{uuid.uuid4()}@test.local")
    db_session.add(row)
    db_session.flush()
    merchant_id = row.id

    yield row

    db_session.rollback()
    cleanup = SessionLocal()
    try:
        # FK order: children before parents.
        for statement in (
            # actions.message_id and messages.action_id reference each other, so neither table
            # can be deleted first. Breaking the link is the only order that works -- and it only
            # became necessary once Phase 6.5 started populating message_id on a confirmed send.
            "UPDATE actions SET message_id = NULL WHERE merchant_id = :m",
            "DELETE FROM messages WHERE action_id IN "
            "(SELECT id FROM actions WHERE merchant_id = :m)",
            "DELETE FROM actions WHERE merchant_id = :m",
            "DELETE FROM payment_links WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE merchant_id = :m)",
            "DELETE FROM promises WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE merchant_id = :m)",
            "DELETE FROM replies WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE merchant_id = :m)",
            "DELETE FROM invoices WHERE merchant_id = :m",
            "DELETE FROM consents WHERE counterparty_id IN "
            "(SELECT id FROM counterparties WHERE merchant_id = :m)",
            "DELETE FROM contacts WHERE counterparty_id IN "
            "(SELECT id FROM counterparties WHERE merchant_id = :m)",
            "DELETE FROM counterparties WHERE merchant_id = :m",
            "DELETE FROM recovery_runs WHERE merchant_id = :m",
        ):
            cleanup.execute(text(statement), {"m": merchant_id})
        cleanup.commit()
    finally:
        cleanup.close()


@pytest.fixture()
def gate_counterparty(db_session: Session, gate_merchant: Any) -> Any:
    from app.models.counterparty import Counterparty

    row = Counterparty(
        id=uuid.uuid4(),
        merchant_id=gate_merchant.id,
        name="Krishna Textiles",
        name_normalized="krishna textiles",
        is_quarantined=False,
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture()
def gate_consent(db_session: Session, gate_counterparty: Any) -> Any:
    from app.enums import Channel
    from app.models.consent import Consent

    row = Consent(
        id=uuid.uuid4(),
        counterparty_id=gate_counterparty.id,
        channel=Channel.EMAIL.value,
        is_permitted=True,
        basis="existing_commercial_relationship",
        granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        opt_out_token="tok123",
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture()
def gate_invoice(db_session: Session, gate_merchant: Any, gate_counterparty: Any) -> Any:
    from app.enums import PaymentStatus, RecoveryState
    from app.models.invoice import Invoice

    row = Invoice(
        id=uuid.uuid4(),
        merchant_id=gate_merchant.id,
        counterparty_id=gate_counterparty.id,
        invoice_number="INV-GATE-001",
        amount_paise=12_45_000,
        outstanding_paise=12_45_000,
        issue_date=date(2026, 6, 1),
        due_date=date(2026, 7, 1),
        terms_days=30,
        payment_status=PaymentStatus.UNPAID.value,
        recovery_state=RecoveryState.CHASING.value,
        touch_count=1,
    )
    db_session.add(row)
    db_session.flush()
    return row
