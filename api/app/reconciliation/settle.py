"""The settle path. The most important code in the product (agents/razorpay-integration.md).

**One settle function, not two.** The webhook path and the manual "paid offline" path both call
:func:`settle_invoice` with a different ``source``. Two settle implementations would drift, and
the one that drifts is the one nobody demos.

**Revoking pending actions is the single most important step.** An invoice that settles must have
every pending action cancelled in the *same transaction* as the status change. Miss it and PAYVRA
messages a customer who paid three hours ago — the exact failure that destroys merchant trust, and
the one CLAUDE.md names as the worst in the product.

A deliberate departure from the pseudocode in agents/razorpay-integration.md, which shows
``cancel_outstanding_links(inv)`` inside the settle transaction: **link cancellation is not in the
transaction here.** Cancelling a link is an outbound HTTP call, and putting one inside a
transaction that holds ``FOR UPDATE`` on the invoice means a slow or failing Razorpay stalls, or
rolls back, the revocation. That inverts the risk exactly the wrong way: an uncancelled link on a
paid invoice is cosmetic (the payer has already paid), whereas an unrevoked action is a message to
someone who has. So the database work commits on its own, and links are cancelled afterwards on a
best-effort basis with ``link_hygiene`` sweeping anything left behind.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

from app.audit.log import record as audit_record
from app.clock import now_utc
from app.enums import (
    ActionStatus,
    ActorType,
    PaymentStatus,
    RecoveryState,
    StopReason,
    UnpaidCause,
)
from app.exceptions import NotFoundError, PayvraError
from app.models.action import Action
from app.models.invoice import Invoice
from app.models.promise import Promise

logger = logging.getLogger(__name__)

# Action states that represent work not yet done, and therefore work that must be cancelled when
# the money arrives. `executed` and `failed` are already in the past; `revoked` is already gone.
PENDING_ACTION_STATUSES: tuple[str, ...] = (
    ActionStatus.PROPOSED.value,
    ActionStatus.GATED_PASS.value,
    ActionStatus.AWAITING_APPROVAL.value,
)

# The floor a partial payment de-escalates to. Tier 1 is the gentlest; someone who just paid part
# of what they owe has earned the benefit of the doubt, not a firmer letter (FR-13.4).
MIN_TONE_TIER = 1


class SettleSource:
    """Where a settlement came from. Recorded in the audit entry so the two paths stay legible."""

    WEBHOOK = "webhook"
    MANUAL = "manual"
    #: Learned by asking Razorpay rather than by being told. Distinct from WEBHOOK on purpose:
    #: a reader should be able to see that this settlement was found by a poll, which usually
    #: means a webhook was missed.
    POLL = "poll"


@dataclass
class SettleResult:
    """What the settle actually did. ``revoked_actions`` is the demo's central number."""

    invoice_id: uuid.UUID
    amount_applied_paise: int
    outstanding_before_paise: int
    outstanding_after_paise: int
    fully_settled: bool
    revoked_actions: int
    promises_closed: int
    tone_tier_before: int
    tone_tier_after: int
    source: str
    already_settled: bool = False
    links_cancelled: int = 0
    link_cancellation_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": str(self.invoice_id),
            "amount_applied_paise": self.amount_applied_paise,
            "outstanding_before_paise": self.outstanding_before_paise,
            "outstanding_after_paise": self.outstanding_after_paise,
            "fully_settled": self.fully_settled,
            "revoked_actions": self.revoked_actions,
            "promises_closed": self.promises_closed,
            "tone_tier_before": self.tone_tier_before,
            "tone_tier_after": self.tone_tier_after,
            "source": self.source,
            "already_settled": self.already_settled,
        }


def settle_invoice(
    db: Session,
    invoice_id: uuid.UUID,
    amount_paise: int,
    *,
    source: str,
    reference: str | None = None,
    paid_on: datetime | None = None,
    actor: ActorType = ActorType.SYSTEM,
    actor_id: str | None = None,
) -> SettleResult:
    """Apply a payment, revoke every pending action, close open promises, write the audit entry.

    Does **not** commit — the caller owns the transaction. That is what makes the atomicity claim
    testable: if anything below raises, the caller's rollback takes the status change with it, and
    an invoice can never end up marked paid with its outreach still queued.

    Idempotent against replay: a webhook redelivered after the invoice already settled applies
    nothing and returns ``already_settled=True`` rather than driving ``outstanding`` negative.
    """
    if amount_paise <= 0:
        raise ValueError(f"settle amount must be positive, got {amount_paise}")

    # FOR UPDATE: two webhooks for the same invoice can arrive concurrently, and both would
    # otherwise read the same `outstanding` and each subtract from it.
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError(f"invoice {invoice_id} not found")

    outstanding_before = invoice.outstanding_paise
    tone_before = invoice.current_tone_tier

    if invoice.payment_status == PaymentStatus.PAID.value or outstanding_before <= 0:
        logger.info("settle no-op, already settled invoice=%s source=%s", invoice_id, source)
        return SettleResult(
            invoice_id=invoice_id,
            amount_applied_paise=0,
            outstanding_before_paise=outstanding_before,
            outstanding_after_paise=outstanding_before,
            fully_settled=True,
            revoked_actions=0,
            promises_closed=0,
            tone_tier_before=tone_before,
            tone_tier_after=tone_before,
            source=source,
            already_settled=True,
        )

    # Never let an overpayment drive outstanding negative; apply at most what is owed.
    applied = min(amount_paise, outstanding_before)
    invoice.outstanding_paise = outstanding_before - applied
    fully_settled = invoice.outstanding_paise <= 0

    if fully_settled:
        invoice.payment_status = PaymentStatus.PAID.value
        invoice.recovery_state = RecoveryState.SETTLED.value
        invoice.stop_reason = StopReason.SETTLED.value
        invoice.settled_at = paid_on or now_utc()
    else:
        # FR-13.4: partial payment re-enters the loop at a *lower* tone. They are paying; the
        # right response to good faith is not a firmer letter.
        invoice.payment_status = PaymentStatus.PARTIALLY_PAID.value
        invoice.current_tone_tier = max(MIN_TONE_TIER, invoice.current_tone_tier - 1)
        invoice.inferred_cause = UnpaidCause.CASH_CRUNCH.value

    # --- THE critical step ---------------------------------------------------------------------
    # Same transaction as the status change above. If this raises, the caller rolls back and the
    # invoice does not end up "paid" with live outreach behind it.
    revoked = _revoke_pending_actions(db, invoice_id)
    promises_closed = _close_open_promises(db, invoice_id, kept=fully_settled)

    db.flush()

    audit_record(
        db,
        merchant_id=invoice.merchant_id,
        actor=actor,
        actor_id=actor_id or source,
        action_type="reconcile.settle",
        subject_type="invoice",
        subject_id=invoice_id,
        # 'executed' is correct here: unlike a gate verdict, the money has actually moved. This is
        # the first place in the codebase that writes it (see the C2 note in guardrails/gate.py).
        outcome="executed",
        rationale=(
            f"Payment of {applied} paise received via {source}. "
            f"Revoked {revoked} pending action(s), closed {promises_closed} promise(s). "
            + (
                "Invoice fully settled."
                if fully_settled
                else f"{invoice.outstanding_paise} paise still outstanding; "
                f"tone tier {tone_before} -> {invoice.current_tone_tier}."
            )
        ),
        inputs={
            "amount_paise": applied,
            "source": source,
            "reference": reference,
            "outstanding_before_paise": outstanding_before,
            "outstanding_after_paise": invoice.outstanding_paise,
            "revoked_actions": revoked,
            "promises_closed": promises_closed,
        },
    )

    logger.info(
        "settled invoice=%s source=%s applied=%d revoked=%d fully_settled=%s",
        invoice_id,
        source,
        applied,
        revoked,
        fully_settled,
    )

    return SettleResult(
        invoice_id=invoice_id,
        amount_applied_paise=applied,
        outstanding_before_paise=outstanding_before,
        outstanding_after_paise=invoice.outstanding_paise,
        fully_settled=fully_settled,
        revoked_actions=revoked,
        promises_closed=promises_closed,
        tone_tier_before=tone_before,
        tone_tier_after=invoice.current_tone_tier,
        source=source,
    )


def _revoke_pending_actions(db: Session, invoice_id: uuid.UUID) -> int:
    """Cancel every action that has not yet happened. Returns how many.

    A bulk UPDATE rather than a loop: it is one statement, it cannot partially apply, and there is
    no window between reading the set and revoking it in which a dispatcher could claim one.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Action)
            .where(Action.invoice_id == invoice_id, Action.status.in_(PENDING_ACTION_STATUSES))
            .values(status=ActionStatus.REVOKED.value, revoked_at=now_utc())
            .execution_options(synchronize_session=False)
        ),
    )
    return int(result.rowcount or 0)


def _close_open_promises(db: Session, invoice_id: uuid.UUID, *, kept: bool) -> int:
    """Resolve open promises. A full settlement keeps them; a partial one supersedes them."""
    status = "kept" if kept else "superseded"
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Promise)
            .where(Promise.invoice_id == invoice_id, Promise.status == "open")
            .values(status=status, resolved_at=now_utc())
            .execution_options(synchronize_session=False)
        ),
    )
    return int(result.rowcount or 0)


def cancel_links_after_settle(
    db: Session, invoice_id: uuid.UUID, client: Any | None = None
) -> tuple[int, list[str]]:
    """Cancel outstanding links for a settled invoice (FR-9.5). **Call after the commit.**

    Deliberately outside the settle transaction — see the module docstring. Failures are collected
    and returned rather than raised: ``link_hygiene`` retries them daily, and nothing about a
    stale link justifies undoing a settlement.
    """
    from app.razorpay.links import cancel_link, links_for_invoice

    if client is None:
        from app.razorpay.client import RazorpayClient

        try:
            client = RazorpayClient()
        except PayvraError as exc:
            return 0, [f"razorpay client unavailable: {exc}"]

    cancelled = 0
    errors: list[str] = []
    for link in links_for_invoice(db, invoice_id, live_only=True):
        try:
            if cancel_link(db, client, link):
                cancelled += 1
            else:
                errors.append(f"{link.razorpay_link_id}: not cancelled")
        except PayvraError as exc:  # pragma: no cover - cancel_link already swallows
            errors.append(f"{link.razorpay_link_id}: {exc}")
    return cancelled, errors
