"""Webhook endpoint: signature, dedupe, ack latency, and the end-to-end settle.

The endpoint is the only unauthenticated write in the application, so the order of operations is
the security boundary: raw bytes, verify, then and only then parse.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.enums import ActionStatus, PaymentStatus, RecoveryState
from app.razorpay.webhooks import verify_signature

pytestmark = pytest.mark.usefixtures("db_available")

API = "/api/v1"
WEBHOOK_PATH = f"{API}/webhooks/razorpay"
SECRET = "test-webhook-secret"


def sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def new_event_id() -> str:
    """A Razorpay-shaped event id. Travels in the header, never in the body."""
    return f"evt_{uuid.uuid4().hex[:16]}"


def link_paid_payload(
    *,
    event: str = "payment_link.paid",
    link_id: str = "plink_test_1",
    reference_id: str = "INV-WH-001",
    amount: int = 500_000,
    amount_paid: int | None = None,
    invoice_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """A Razorpay-shaped envelope. Only the fields reconciliation reads are meaningful.

    Deliberately carries **no top-level ``id``** — the real envelope is
    ``{entity, account_id, event, contains, payload, created_at}`` and the event id arrives in
    the ``x-razorpay-event-id`` header. A fixture that invents a body id would let the handler
    pass a test it fails in production, which is exactly how this bug survived Phase 4.
    """
    return {
        "event": event,
        "created_at": int(time.time()),
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "reference_id": reference_id,
                    "amount": amount,
                    "amount_paid": amount if amount_paid is None else amount_paid,
                    "status": "paid",
                    "notes": {"invoice_id": str(invoice_id) if invoice_id else ""},
                }
            }
        },
    }


def post(
    client: TestClient,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    send_event_id: bool = True,
    secret: str = SECRET,
    tamper: bool = False,
) -> Any:
    """POST a signed envelope, with the event id in the header as Razorpay sends it.

    ``send_event_id=False`` reproduces a verified delivery that carries no id header at all.
    """
    raw = json.dumps(payload).encode()
    signature = sign(raw, secret)
    if tamper:
        raw = raw.replace(b'"amount": 500000', b'"amount": 999999')
    headers = {"X-Razorpay-Signature": signature, "Content-Type": "application/json"}
    if send_event_id:
        headers["X-Razorpay-Event-Id"] = event_id or new_event_id()
    return client.post(WEBHOOK_PATH, content=raw, headers=headers)


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import webhooks as router_mod

    monkeypatch.setattr(router_mod.settings, "razorpay_webhook_secret", SECRET, raising=False)


@pytest.fixture()
def cleanup_events() -> Any:
    """Webhook events are committed by the endpoint, so they need explicit teardown."""
    seen: list[str] = []
    yield seen
    session = SessionLocal()
    try:
        session.execute(
            text("DELETE FROM webhook_events WHERE razorpay_event_id = ANY(:ids)"), {"ids": seen}
        )
        session.commit()
    finally:
        session.close()


# --- signature verification --------------------------------------------------------------------


def test_a_valid_signature_passes() -> None:
    raw = b'{"id":"evt_1","event":"payment_link.paid"}'
    assert verify_signature(raw, sign(raw), SECRET)


def test_a_tampered_body_fails() -> None:
    """The signature is over the exact bytes. One changed digit invalidates it."""
    raw = b'{"id":"evt_1","amount":500000}'
    signature = sign(raw)
    assert not verify_signature(raw.replace(b"500000", b"999999"), signature, SECRET)


def test_the_wrong_secret_fails() -> None:
    """Test-mode and live-mode webhook secrets differ; using the wrong one looks like an attack."""
    raw = b'{"id":"evt_1"}'
    assert not verify_signature(raw, sign(raw, "live-secret"), SECRET)


def test_a_missing_signature_fails() -> None:
    assert not verify_signature(b"{}", "", SECRET)


def test_a_missing_secret_fails() -> None:
    assert not verify_signature(b"{}", sign(b"{}"), "")


def test_verification_uses_a_constant_time_comparison() -> None:
    """`==` returns early on the first differing byte, leaking the correct prefix through timing."""
    import ast
    import pathlib

    from app.razorpay import webhooks as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    func = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "verify_signature"
    )
    calls = {ast.unparse(n.func) for n in ast.walk(func) if isinstance(n, ast.Call)}
    assert "hmac.compare_digest" in calls
    assert not any(
        isinstance(n, ast.Compare) and any(isinstance(op, ast.Eq) for op in n.ops)
        for n in ast.walk(func)
        if "hexdigest" in ast.unparse(n)
    )


# --- the endpoint ------------------------------------------------------------------------------


def test_an_unsigned_request_is_rejected_without_parsing(client: TestClient) -> None:
    """Never parse an unverified body. Deliberately malformed JSON: a 400 proves the parser
    was never reached, because a parse would have raised first."""
    response = client.post(
        WEBHOOK_PATH, content=b"{not json at all", headers={"X-Razorpay-Signature": "deadbeef"}
    )
    assert response.status_code == 400
    assert response.json()["status"] == "invalid"


def test_a_bad_signature_is_rejected(client: TestClient) -> None:
    payload = link_paid_payload()
    raw = json.dumps(payload).encode()
    response = client.post(
        WEBHOOK_PATH, content=raw, headers={"X-Razorpay-Signature": sign(raw, "wrong")}
    )
    assert response.status_code == 400


def test_a_valid_event_is_accepted(client: TestClient, cleanup_events: list[str]) -> None:
    payload = link_paid_payload()
    event_id = new_event_id()
    cleanup_events.append(event_id)
    response = post(client, payload, event_id=event_id)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_a_duplicate_event_id_is_a_no_op_returning_200(
    client: TestClient, cleanup_events: list[str]
) -> None:
    """Deduped by the unique constraint, not by application logic — a SELECT-then-INSERT loses
    to a concurrent redelivery, and Razorpay redelivers.

    Razorpay documents repeat delivery of the same event as expected behaviour, so the second
    call here is the normal case, not an error path.

    The replay is identified by the repeated ``x-razorpay-event-id`` header. Nothing in the body
    distinguishes it — which is the whole point of the header being the dedupe key.
    """
    payload = link_paid_payload()
    event_id = new_event_id()
    cleanup_events.append(event_id)

    first = post(client, payload, event_id=event_id)
    second = post(client, payload, event_id=event_id)

    assert first.json()["status"] == "ok"
    assert second.status_code == 200, "a duplicate must never be a non-2xx; Razorpay would retry"
    assert second.json()["status"] == "duplicate"

    session = SessionLocal()
    try:
        stored = session.execute(
            text("SELECT count(*) FROM webhook_events WHERE razorpay_event_id = :e"),
            {"e": event_id},
        ).scalar_one()
    finally:
        session.close()
    assert stored == 1, "the unique constraint on the header-derived key is the dedupe mechanism"


def test_the_handler_acknowledges_under_200ms(
    client: TestClient, cleanup_events: list[str]
) -> None:
    """Razorpay retries slow handlers, so a payment reconciled inline becomes a delivery storm.

    200 ms is our self-imposed margin, not the documented limit — Razorpay's SLA is a 2XX within
    5 seconds. The tight assertion is deliberate: it fails while there is still 25x of headroom,
    long before a real delivery is at risk.
    """
    payload = link_paid_payload()
    event_id = new_event_id()
    cleanup_events.append(event_id)

    start = time.perf_counter()
    response = post(client, payload, event_id=event_id)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert response.status_code == 200
    assert elapsed_ms < 200, f"handler took {elapsed_ms:.0f}ms"


def test_an_unknown_event_type_is_accepted_not_rejected(
    client: TestClient, cleanup_events: list[str]
) -> None:
    """Never 4xx an unrecognised event — Razorpay will retry it forever."""
    payload = link_paid_payload(event="payment_link.some_future_event")
    event_id = new_event_id()
    cleanup_events.append(event_id)
    assert post(client, payload, event_id=event_id).status_code == 200


def test_the_event_id_header_is_read_case_insensitively(
    client: TestClient, cleanup_events: list[str]
) -> None:
    """HTTP header names are case-insensitive, so the dedupe key must not depend on casing.

    Razorpay's own docs write it lowercase (`x-razorpay-event-id`); reading it with an exact
    match on some other casing would silently drop every event onto the fallback key.
    """
    raw = json.dumps(link_paid_payload()).encode()
    event_id = new_event_id()
    cleanup_events.append(event_id)

    response = client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "X-Razorpay-Signature": sign(raw),
            "x-razorpay-event-id": event_id,  # lowercase, as Razorpay documents it
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200

    session = SessionLocal()
    try:
        stored = session.execute(
            text("SELECT count(*) FROM webhook_events WHERE razorpay_event_id = :e"),
            {"e": event_id},
        ).scalar_one()
    finally:
        session.close()
    assert stored == 1, "the lowercase header must populate razorpay_event_id"


def test_a_verified_payload_without_the_id_header_still_processes(
    client: TestClient, cleanup_events: list[str]
) -> None:
    """A verified payload is genuinely from Razorpay, so a missing id header must not 400.

    Rejecting it would be the worst available outcome: Razorpay retries a non-2xx indefinitely,
    so a 400 on a valid event is an infinite loop. The handler degrades to a body-derived key
    instead, which still dedupes because a redelivery carries identical bytes.
    """
    payload = link_paid_payload(reference_id="INV-NOHEADER-1")
    raw = json.dumps(payload).encode()
    expected_key = "sha256:" + hashlib.sha256(raw).hexdigest()
    cleanup_events.append(expected_key)

    response = post(client, payload, send_event_id=False)

    assert response.status_code == 200, "a verified event must never be rejected for a missing id"
    assert response.json()["status"] == "ok"

    session = SessionLocal()
    try:
        stored = session.execute(
            text("SELECT count(*) FROM webhook_events WHERE razorpay_event_id = :e"),
            {"e": expected_key},
        ).scalar_one()
    finally:
        session.close()
    assert stored == 1, "the fallback key must be the sha256 of the exact raw body"

    # And the fallback still dedupes: the same event redelivered hashes to the same key.
    second = post(client, payload, send_event_id=False)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_a_signed_but_malformed_body_is_rejected(client: TestClient) -> None:
    raw = b"not json"
    response = client.post(WEBHOOK_PATH, content=raw, headers={"X-Razorpay-Signature": sign(raw)})
    assert response.status_code == 400
    assert response.json()["status"] == "malformed"


def test_the_payload_is_never_logged(
    caplog: pytest.LogCaptureFixture, client: TestClient, cleanup_events: list[str]
) -> None:
    """A payload carries counterparty PII. Only the event id and type may be logged."""
    import logging

    payload = link_paid_payload(reference_id="INV-SECRET-9999")
    payload["payload"]["payment_link"]["entity"]["customer"] = {
        "name": "Krishna Textiles",
        "email": "ap@krishnatextiles.example",
    }
    event_id = new_event_id()
    cleanup_events.append(event_id)

    with caplog.at_level(logging.INFO):
        post(client, payload, event_id=event_id)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "ap@krishnatextiles.example" not in logged
    assert "Krishna Textiles" not in logged
    assert event_id in logged, "the event id should be logged"


# --- end to end: webhook settles and revokes ----------------------------------------------------


def test_a_paid_webhook_settles_the_invoice_and_revokes_its_actions(
    client: TestClient, cleanup_events: list[str], api_merchant: uuid.UUID
) -> None:
    """The demo's central moment, driven entirely through the HTTP endpoint."""
    from app.models.action import Action
    from app.models.counterparty import Counterparty
    from app.models.invoice import Invoice
    from app.models.payment_link import PaymentLink

    session = SessionLocal()
    try:
        counterparty = Counterparty(
            id=uuid.uuid4(),
            merchant_id=api_merchant,
            name="Krishna Textiles",
            name_normalized="krishna textiles",
        )
        session.add(counterparty)
        session.flush()
        invoice = Invoice(
            id=uuid.uuid4(),
            merchant_id=api_merchant,
            counterparty_id=counterparty.id,
            invoice_number="INV-WH-E2E",
            amount_paise=500_000,
            outstanding_paise=500_000,
            issue_date=date(2026, 6, 1),
            due_date=date(2026, 7, 1),
            terms_days=30,
            payment_status=PaymentStatus.UNPAID.value,
            recovery_state=RecoveryState.CHASING.value,
        )
        session.add(invoice)
        session.flush()
        session.add(
            PaymentLink(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                razorpay_link_id="plink_e2e",
                short_url="https://rzp.io/i/e2e",
                amount_paise=500_000,
                reference_id="INV-WH-E2E",
                status="created",
                expire_by=datetime.now(UTC) + timedelta(days=7),
                accept_partial=False,
                idempotency_key=uuid.uuid4().hex,
            )
        )
        for status in (ActionStatus.PROPOSED.value, ActionStatus.GATED_PASS.value):
            session.add(
                Action(
                    id=uuid.uuid4(),
                    merchant_id=api_merchant,
                    invoice_id=invoice.id,
                    type="send_message",
                    status=status,
                    proposed_by="agent",
                    rationale="test",
                    scheduled_for=datetime.now(UTC) + timedelta(hours=2),
                )
            )
        session.commit()
        invoice_id = invoice.id
    finally:
        session.close()

    payload = link_paid_payload(
        link_id="plink_e2e", reference_id="INV-WH-E2E", invoice_id=invoice_id
    )
    event_id = new_event_id()
    cleanup_events.append(event_id)

    assert post(client, payload, event_id=event_id).status_code == 200
    # TestClient runs BackgroundTasks synchronously once the response is sent, so by here the
    # reconciliation has already happened -- which is itself the assertion that step 6 fires.

    session = SessionLocal()
    try:
        row = session.execute(
            text(
                "SELECT payment_status, recovery_state, outstanding_paise FROM invoices "
                "WHERE id = :i"
            ),
            {"i": invoice_id},
        ).one()
        remaining = session.execute(
            text(
                "SELECT count(*) FROM actions WHERE invoice_id = :i "
                "AND status IN ('proposed','gated_pass','awaiting_approval')"
            ),
            {"i": invoice_id},
        ).scalar_one()
        revoked_in_audit = session.execute(
            text(
                "SELECT (inputs->>'revoked_actions')::int FROM audit_log "
                "WHERE merchant_id = :m AND action_type = 'reconcile.settle' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"m": api_merchant},
        ).scalar_one()
        processed = session.execute(
            text("SELECT processed_at FROM webhook_events WHERE razorpay_event_id = :e"),
            {"e": event_id},
        ).scalar_one()
    finally:
        session.close()

    assert row[0] == PaymentStatus.PAID.value
    assert row[1] == RecoveryState.SETTLED.value
    assert row[2] == 0
    assert remaining == 0, "no pending action may survive a settlement"
    assert revoked_in_audit == 2, "the revoked count is the demo's central number"
    assert processed is not None, "the background task must have marked the event processed"


def test_reprocessing_the_same_event_is_idempotent(
    client: TestClient, cleanup_events: list[str], api_merchant: uuid.UUID
) -> None:
    from app.reconciliation.processor import process_event

    payload = link_paid_payload(reference_id="INV-NOMATCH-XYZ")
    event_id = new_event_id()
    cleanup_events.append(event_id)
    post(client, payload, event_id=event_id)

    first = process_event(event_id)
    second = process_event(event_id)
    assert second.status == "ignored"
    assert second.detail == "already processed"
    assert first.status in ("unmatched", "settled", "ignored")
