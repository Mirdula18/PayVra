"""Payment links: idempotency, the test-mode budget, notify/reminder settings, regeneration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.enums import PaymentStatus, RecoveryState, StopReason
from app.models.contact import Contact
from app.models.invoice import Invoice
from app.models.payment_link import PaymentLink
from app.razorpay import links as links_mod
from app.razorpay.client import RazorpayClient
from app.razorpay.links import (
    LINK_BUDGET,
    RAZORPAY_TEST_MODE_LINK_CAP,
    LinkBudgetExceeded,
    LinkPurpose,
    build_payload,
    create_link,
    links_used,
    regenerate_if_needed,
)

pytestmark = pytest.mark.usefixtures("db_available")


def make_client(created: list[dict[str, Any]] | None = None) -> RazorpayClient:
    """A client whose transport records every create and returns a plausible Razorpay reply."""
    seen = created if created is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content) if request.content else {}
        seen.append(body)
        link_id = f"plink_{uuid.uuid4().hex[:12]}"
        return httpx.Response(
            200,
            json={"id": link_id, "short_url": f"https://rzp.io/i/{link_id}", "status": "created"},
        )

    client = RazorpayClient(key_id="rzp_test_x", key_secret="s")
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.razorpay.test/v1"
    )
    return client


# --- the payload ------------------------------------------------------------------------------


def test_notify_and_reminder_enable_are_both_false(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """Razorpay-sent reminders would bypass our gate, our frequency cap and our audit log.

    A reminder we did not gate is a message we cannot account for -- and "every action passes the
    gate" is a compliance claim we make to a judge. This is not a deliverability setting.
    """
    payload = build_payload(
        gate_invoice,
        None,
        amount_paise=100_000,
        expire_by=datetime.now(UTC) + timedelta(days=7),
        accept_partial=False,
    )
    assert payload["notify"] == {"sms": False, "email": False}
    assert payload["reminder_enable"] is False


def test_reference_id_is_always_the_invoice_number(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """THE reconciliation key: it turns a matching problem into one indexed lookup (ADR-006)."""
    payload = build_payload(
        gate_invoice,
        None,
        amount_paise=100_000,
        expire_by=datetime.now(UTC) + timedelta(days=7),
        accept_partial=False,
    )
    assert payload["reference_id"] == gate_invoice.invoice_number


def test_notes_carry_our_internal_ids(db_session: Session, gate_invoice: Invoice) -> None:
    """Belt and braces if reference_id is ever missing from a payload."""
    payload = build_payload(
        gate_invoice,
        None,
        amount_paise=100_000,
        expire_by=datetime.now(UTC) + timedelta(days=7),
        accept_partial=False,
    )
    assert payload["notes"] == {
        "invoice_id": str(gate_invoice.id),
        "merchant_id": str(gate_invoice.merchant_id),
    }


def test_the_payload_carries_the_contact_but_no_card_field(
    db_session: Session, gate_invoice: Invoice, gate_counterparty: Any
) -> None:
    contact = Contact(
        id=uuid.uuid4(),
        counterparty_id=gate_counterparty.id,
        name="Ravi Kumar",
        email="ap@krishna.example",
        phone="+919876543210",
        is_primary=True,
    )
    db_session.add(contact)
    db_session.flush()

    payload = build_payload(
        gate_invoice,
        contact,
        amount_paise=100_000,
        expire_by=datetime.now(UTC) + timedelta(days=7),
        accept_partial=False,
    )
    assert payload["customer"] == {
        "name": "Ravi Kumar",
        "email": "ap@krishna.example",
        "contact": "+919876543210",
    }
    assert not any("card" in key.lower() for key in payload)


# --- idempotency ------------------------------------------------------------------------------


def test_the_same_invoice_amount_and_purpose_reuses_the_link(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """The test the whole 30-link budget depends on: a retried dispatch must not burn one."""
    created: list[dict[str, Any]] = []
    client = make_client(created)

    first = create_link(db_session, client, gate_invoice)
    second = create_link(db_session, client, gate_invoice)

    assert first.created is True
    assert second.created is False, "the second call must reuse, not create"
    assert first.link.id == second.link.id
    assert len(created) == 1, "Razorpay must be called exactly once"


def test_a_different_amount_creates_a_new_link(db_session: Session, gate_invoice: Invoice) -> None:
    """A different amount is legitimately a different link."""
    client = make_client()
    first = create_link(db_session, client, gate_invoice, amount_paise=100_000)
    second = create_link(db_session, client, gate_invoice, amount_paise=50_000)
    assert first.link.id != second.link.id
    assert second.created is True


def test_a_different_purpose_creates_a_new_link(db_session: Session, gate_invoice: Invoice) -> None:
    client = make_client()
    first = create_link(db_session, client, gate_invoice, purpose=LinkPurpose.COLLECTION)
    second = create_link(db_session, client, gate_invoice, purpose=LinkPurpose.REGENERATION)
    assert first.link.id != second.link.id


def test_a_zero_amount_link_is_refused(db_session: Session, gate_invoice: Invoice) -> None:
    with pytest.raises(ValueError, match="cannot create a link"):
        create_link(db_session, make_client(), gate_invoice, amount_paise=0)


# --- the test-mode budget ----------------------------------------------------------------------


def test_the_budget_sits_below_the_razorpay_cap() -> None:
    """Headroom so a demo does not discover the cap because someone clicked around the dashboard."""
    assert LINK_BUDGET < RAZORPAY_TEST_MODE_LINK_CAP


def test_creation_stops_at_the_budget(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausting the budget requeues the action; it does not fail the invoice."""
    monkeypatch.setattr(links_mod, "LINK_BUDGET", 2)
    client = make_client()

    create_link(db_session, client, gate_invoice, amount_paise=10_000)
    create_link(db_session, client, gate_invoice, amount_paise=20_000)
    with pytest.raises(LinkBudgetExceeded, match="requeue"):
        create_link(db_session, client, gate_invoice, amount_paise=30_000)


def test_reuse_does_not_consume_budget(
    db_session: Session, gate_invoice: Invoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idempotent repeat is served from our own database before the budget is even checked."""
    monkeypatch.setattr(links_mod, "LINK_BUDGET", 1)
    client = make_client()
    create_link(db_session, client, gate_invoice)
    again = create_link(db_session, client, gate_invoice)
    assert again.created is False


def test_links_used_counts_only_this_merchant(
    db_session: Session, gate_invoice: Invoice, gate_merchant: Any
) -> None:
    create_link(db_session, make_client(), gate_invoice)
    assert links_used(db_session, gate_merchant.id) == 1
    assert links_used(db_session, uuid.uuid4()) == 0


def test_the_seed_creates_no_payment_links() -> None:
    """120 seed invoices would blow a 30-link cap six times over on `make seed` alone."""
    import ast
    import pathlib

    from app.seed import builder

    tree = ast.parse(pathlib.Path(builder.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("razorpay" in name for name in imported), "the seed must not touch Razorpay"


# --- regeneration -----------------------------------------------------------------------------


def _add_link(
    db: Session, invoice: Invoice, *, expires_in: timedelta, status: str = "created"
) -> PaymentLink:
    link = PaymentLink(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        razorpay_link_id=f"plink_{uuid.uuid4().hex[:12]}",
        short_url="https://rzp.io/i/x",
        amount_paise=invoice.outstanding_paise,
        reference_id=invoice.invoice_number,
        status=status,
        expire_by=datetime.now(UTC) + expires_in,
        accept_partial=False,
        idempotency_key=uuid.uuid4().hex,
    )
    db.add(link)
    db.flush()
    return link


def test_a_link_near_expiry_regenerates_while_unpaid(
    db_session: Session, gate_invoice: Invoice
) -> None:
    _add_link(db_session, gate_invoice, expires_in=timedelta(hours=6))
    result = regenerate_if_needed(db_session, make_client(), gate_invoice)
    assert result is not None
    assert result.created is True


def test_a_link_far_from_expiry_is_left_alone(db_session: Session, gate_invoice: Invoice) -> None:
    _add_link(db_session, gate_invoice, expires_in=timedelta(days=10))
    assert regenerate_if_needed(db_session, make_client(), gate_invoice) is None


def test_a_settled_invoice_never_regenerates(db_session: Session, gate_invoice: Invoice) -> None:
    """Handing a fresh link to someone who has already paid is the same class of mistake as
    messaging them."""
    _add_link(db_session, gate_invoice, expires_in=timedelta(hours=1))
    gate_invoice.payment_status = PaymentStatus.PAID.value
    gate_invoice.outstanding_paise = 0
    db_session.flush()
    assert regenerate_if_needed(db_session, make_client(), gate_invoice) is None


def test_a_stopped_invoice_never_regenerates(db_session: Session, gate_invoice: Invoice) -> None:
    _add_link(db_session, gate_invoice, expires_in=timedelta(hours=1))
    gate_invoice.recovery_state = RecoveryState.STOPPED.value
    gate_invoice.stop_reason = StopReason.DISPUTED.value
    db_session.flush()
    assert regenerate_if_needed(db_session, make_client(), gate_invoice) is None


def test_an_invoice_with_no_live_link_is_left_alone(
    db_session: Session, gate_invoice: Invoice
) -> None:
    """Nothing to regenerate. Creating one here would be issuing a link nobody asked for."""
    _add_link(db_session, gate_invoice, expires_in=timedelta(hours=1), status="expired")
    assert regenerate_if_needed(db_session, make_client(), gate_invoice) is None


def test_link_hygiene_job_is_registered_at_1000_daily() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.scheduler.registry import LINK_HYGIENE_JOB_ID, register_jobs

    scheduler = BackgroundScheduler(jobstores={"default": {"type": "memory"}})
    register_jobs(scheduler)
    job = scheduler.get_job(LINK_HYGIENE_JOB_ID)

    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "10"
    assert fields["minute"] == "0"
    assert job.func_ref == "app.scheduler.jobs:link_hygiene_all"
