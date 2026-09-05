"""Pulling settlement state from Razorpay when the webhook never arrived.

A missed webhook is otherwise permanent: the money sits at Razorpay, the book says unpaid, and
nothing in the system ever notices. These tests exist because that happened twice on real
payments, both times because the tunnel forwarding webhooks was not running.

The property that matters most here is **no double-counting**. Razorpay reports a cumulative
``amount_paid``; some of it may already be recorded from a webhook that did land. Settling the
total rather than the difference would inflate the single figure the product is judged on.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.payment_link import PaymentLink
from app.reconciliation import sync
from app.reconciliation.settle import SettleSource, settle_invoice

pytestmark = pytest.mark.usefixtures("db_available")


class _Client:
    """Stands in for Razorpay. Never reaches the network."""

    def __init__(self, status: str, amount_paid: int) -> None:
        self._payload = {"status": status, "amount_paid": amount_paid}
        self.calls = 0

    def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
        self.calls += 1
        return dict(self._payload)


@pytest.fixture()
def link(db_session: Session, gate_invoice: Invoice) -> PaymentLink:
    row = PaymentLink(
        id=uuid.uuid4(),
        invoice_id=gate_invoice.id,
        razorpay_link_id="plink_synctest",
        short_url="https://rzp.io/rzp/synctest",
        amount_paise=int(gate_invoice.outstanding_paise),
        reference_id=gate_invoice.invoice_number,
        status="created",
        expire_by=gate_invoice.created_at,
        accept_partial=True,
        idempotency_key=f"sync-{uuid.uuid4()}",
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_a_paid_link_settles_the_invoice(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice, link: PaymentLink
) -> None:
    owed = int(gate_invoice.outstanding_paise)
    result = sync.sync_link(
        db_session, link, merchant_id=gate_merchant.id, client=_Client("paid", owed)
    )

    assert result.checked is True
    assert result.applied_paise == owed
    assert result.fully_settled is True
    db_session.refresh(gate_invoice)
    assert int(gate_invoice.outstanding_paise) == 0
    assert link.status == "paid"


def test_the_same_payment_is_never_counted_twice(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice, link: PaymentLink
) -> None:
    """**The one that would corrupt the recovery figure.**

    A webhook lands, then a poll runs over the same link. Razorpay still reports the cumulative
    total, so a poll that settled what it was told rather than the difference would book the money
    a second time.
    """
    owed = int(gate_invoice.outstanding_paise)
    half = owed // 2
    settle_invoice(db_session, gate_invoice.id, half, source=SettleSource.WEBHOOK)
    db_session.flush()

    result = sync.sync_link(
        db_session, link, merchant_id=gate_merchant.id, client=_Client("paid", half)
    )

    assert result.already_recorded_paise == half
    assert result.applied_paise == 0, "the webhook already booked this"
    assert result.changed is False
    db_session.refresh(gate_invoice)
    assert int(gate_invoice.outstanding_paise) == owed - half


def test_only_the_unrecorded_remainder_is_applied(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice, link: PaymentLink
) -> None:
    """A webhook caught the first tranche; the poll must apply only the second."""
    owed = int(gate_invoice.outstanding_paise)
    first = owed // 3
    settle_invoice(db_session, gate_invoice.id, first, source=SettleSource.WEBHOOK)
    db_session.flush()

    result = sync.sync_link(
        db_session, link, merchant_id=gate_merchant.id, client=_Client("paid", owed)
    )

    assert result.applied_paise == owed - first
    db_session.refresh(gate_invoice)
    assert int(gate_invoice.outstanding_paise) == 0


def test_an_unpaid_link_changes_nothing(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice, link: PaymentLink
) -> None:
    owed = int(gate_invoice.outstanding_paise)
    result = sync.sync_link(
        db_session, link, merchant_id=gate_merchant.id, client=_Client("created", 0)
    )

    assert result.checked is True
    assert result.changed is False
    db_session.refresh(gate_invoice)
    assert int(gate_invoice.outstanding_paise) == owed


def test_an_overpayment_never_settles_more_than_is_owed(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice, link: PaymentLink
) -> None:
    """Razorpay is the authority on what it collected.

    This row is the authority on what was owed, and an overpayment must not invent recovery.
    """
    owed = int(gate_invoice.outstanding_paise)
    result = sync.sync_link(
        db_session, link, merchant_id=gate_merchant.id, client=_Client("paid", owed * 3)
    )

    assert result.applied_paise == owed
    db_session.refresh(gate_invoice)
    assert int(gate_invoice.outstanding_paise) == 0


def test_razorpay_being_unreachable_does_not_raise(
    db_session: Session, gate_merchant: Merchant, link: PaymentLink
) -> None:
    """A page that polls for payment must not 500 because a third party was briefly down."""
    from app.razorpay.client import RazorpayError

    class _Broken:
        def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
            raise RazorpayError("connection reset")

    result = sync.sync_link(db_session, link, merchant_id=gate_merchant.id, client=_Broken())
    assert result.checked is False
    assert result.error
    assert result.changed is False


def test_an_invoice_with_no_link_reports_rather_than_raises(
    db_session: Session, gate_merchant: Merchant, gate_invoice: Invoice
) -> None:
    result = sync.sync_invoice(db_session, gate_invoice.id, gate_merchant.id)
    assert result.checked is False
    assert result.error
