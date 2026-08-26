"""Asynchronous webhook processing — everything the handler defers so it can ack in under 200 ms.

Razorpay retries any handler that is slow or non-2xx, so the endpoint's only job is: verify,
record, acknowledge. All the reconciliation work happens here, afterwards, on its own session.

Processing is **idempotent**. A redelivered event is deduped at the endpoint by the unique
constraint, but a *reprocessed* one (a retry of this function after a crash) must also be safe:
``settle_invoice`` no-ops on an already-settled invoice, and ``processed_at`` records that the
event has been handled.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.db import SessionLocal
from app.enums import PaymentStatus, RecoveryState
from app.exceptions import PayvraError
from app.models.invoice import Invoice
from app.models.payment_link import PaymentLink
from app.models.webhook_event import WebhookEvent
from app.razorpay import webhooks
from app.reconciliation.settle import SettleSource, cancel_links_after_settle, settle_invoice

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessOutcome:
    event_id: str
    event_type: str
    status: str  # settled | partial | regenerated | ignored | unmatched | error
    invoice_id: uuid.UUID | None = None
    revoked_actions: int = 0
    detail: str | None = None


def process_event(event_id: str) -> ProcessOutcome:
    """Process one stored webhook event on its own session. Safe to call twice."""
    db = SessionLocal()
    try:
        outcome = _process(db, event_id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("webhook processing failed event_id=%s", event_id)
        return ProcessOutcome(event_id=event_id, event_type="", status="error")
    finally:
        db.close()

    # Link cancellation is outbound HTTP and stays outside the settle transaction; see
    # reconciliation/settle.py for why. Best effort, after the commit.
    if outcome.status == "settled" and outcome.invoice_id is not None:
        db = SessionLocal()
        try:
            cancelled, errors = cancel_links_after_settle(db, outcome.invoice_id)
            db.commit()
            if errors:
                logger.info("link cancellation left %d for link_hygiene", len(errors))
            else:
                logger.info("cancelled %d link(s) after settle", cancelled)
        except PayvraError:
            db.rollback()
        finally:
            db.close()

    return outcome


def _process(db: Session, event_id: str) -> ProcessOutcome:
    event = db.execute(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    ).scalar_one_or_none()
    if event is None:
        return ProcessOutcome(event_id=event_id, event_type="", status="error", detail="not found")

    if event.processed_at is not None:
        return ProcessOutcome(
            event_id=event_id,
            event_type=event.event_type,
            status="ignored",
            detail="already processed",
        )

    # The stored row's key is the one the endpoint resolved from the header (or the body-derived
    # fallback), so reuse it rather than re-deriving: the payload alone does not carry an id.
    facts = webhooks.extract(dict(event.raw_payload), event_id=event.razorpay_event_id)
    event.processed_at = now_utc()

    if not facts.is_handled:
        # Unknown event types are logged and accepted. Never 4xx an unrecognised event --
        # Razorpay retries a non-2xx forever (agents/razorpay-integration.md).
        logger.info("ignoring unhandled webhook %s", webhooks.safe_log_fields(facts))
        return ProcessOutcome(event_id=event_id, event_type=facts.event_type, status="ignored")

    invoice = _match_invoice(db, facts)
    if invoice is None:
        logger.warning("no invoice matched webhook %s", webhooks.safe_log_fields(facts))
        return ProcessOutcome(
            event_id=event_id,
            event_type=facts.event_type,
            status="unmatched",
        )

    if facts.event_type in webhooks.CANCEL_EVENTS:
        _mark_link_status(db, facts, "cancelled")
        return ProcessOutcome(
            event_id=event_id,
            event_type=facts.event_type,
            status="ignored",
            invoice_id=invoice.id,
            detail="link cancelled; no invoice state change",
        )

    if facts.event_type in webhooks.EXPIRY_EVENTS:
        return _handle_expiry(db, facts, invoice, event_id)

    # paid / partially_paid. Trust the amount actually paid, not the link's face value: a
    # partially-paid link reports the full amount in `amount` and the received sum in `amount_paid`.
    applied = facts.amount_paid_paise or facts.amount_paise
    if applied <= 0:
        return ProcessOutcome(
            event_id=event_id,
            event_type=facts.event_type,
            status="ignored",
            invoice_id=invoice.id,
            detail="event carried no amount",
        )

    result = settle_invoice(
        db, invoice.id, applied, source=SettleSource.WEBHOOK, reference=facts.razorpay_link_id
    )
    _mark_link_status(db, facts, "paid" if result.fully_settled else "partially_paid")

    return ProcessOutcome(
        event_id=event_id,
        event_type=facts.event_type,
        status="settled" if result.fully_settled else "partial",
        invoice_id=invoice.id,
        revoked_actions=result.revoked_actions,
    )


def _handle_expiry(
    db: Session, facts: webhooks.WebhookFacts, invoice: Invoice, event_id: str
) -> ProcessOutcome:
    """FR-13.5: regenerate only while the invoice is still unpaid and not stopped."""
    _mark_link_status(db, facts, "expired")

    if invoice.payment_status in (PaymentStatus.PAID.value, PaymentStatus.WRITTEN_OFF.value) or (
        invoice.recovery_state in (RecoveryState.SETTLED.value, RecoveryState.STOPPED.value)
    ):
        return ProcessOutcome(
            event_id=event_id,
            event_type=facts.event_type,
            status="ignored",
            invoice_id=invoice.id,
            detail="invoice settled or stopped; not regenerating",
        )

    from app.razorpay.client import RazorpayClient
    from app.razorpay.links import LinkPurpose, create_link

    try:
        client = RazorpayClient()
        create_link(db, client, invoice, purpose=LinkPurpose.REGENERATION)
    except PayvraError as exc:
        logger.warning("could not regenerate link for invoice=%s: %s", invoice.id, exc)
        return ProcessOutcome(
            event_id=event_id,
            event_type=facts.event_type,
            status="ignored",
            invoice_id=invoice.id,
            detail=f"regeneration deferred: {exc}",
        )

    return ProcessOutcome(
        event_id=event_id,
        event_type=facts.event_type,
        status="regenerated",
        invoice_id=invoice.id,
    )


def _match_invoice(db: Session, facts: webhooks.WebhookFacts) -> Invoice | None:
    """Resolve the invoice. ``reference_id`` first — that is what it is for (ADR-006).

    Falls back to the ``notes.invoice_id`` we set at creation, then to the stored link row. Three
    routes because a webhook that cannot be matched is money we have received and not recorded.
    """
    if facts.razorpay_link_id:
        link = db.execute(
            select(PaymentLink).where(PaymentLink.razorpay_link_id == facts.razorpay_link_id)
        ).scalar_one_or_none()
        if link is not None:
            return db.get(Invoice, link.invoice_id)

    if facts.invoice_id_note:
        try:
            invoice = db.get(Invoice, uuid.UUID(facts.invoice_id_note))
        except ValueError:
            invoice = None
        if invoice is not None:
            return invoice

    if facts.reference_id:
        # invoice_number is unique per merchant, not globally; a bare reference_id can only be
        # trusted when it resolves to exactly one row. The suffix is stripped first: a regenerated
        # link carries "INV-001-R2", and this fallback exists precisely for the case where the
        # link row and notes.invoice_id have both failed us.
        from app.razorpay.links import base_reference_id

        matches = list(
            db.execute(
                select(Invoice)
                .where(Invoice.invoice_number == base_reference_id(facts.reference_id))
                .limit(2)
            ).scalars()
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("reference_id matched %d invoices; refusing to guess", len(matches))
    return None


def _mark_link_status(db: Session, facts: webhooks.WebhookFacts, status: str) -> None:
    if not facts.razorpay_link_id:
        return
    link = db.execute(
        select(PaymentLink).where(PaymentLink.razorpay_link_id == facts.razorpay_link_id)
    ).scalar_one_or_none()
    if link is not None:
        link.status = status
        db.flush()
