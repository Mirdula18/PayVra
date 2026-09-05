"""Human-in-the-loop actions: what a person can do to an account from the UI.

**Every one of these goes through the gate, not around it.** ``check_value_threshold`` already
reads ``ProposedAction.approved_by`` -- an approval is an *input* the gate weighs, never a flag
that skips it. So "Approve and send" re-evaluates all seven checks with the approval recorded, and
if some other rule refuses (outside contact hours, no consent, frequency cap) the send still does
not happen and the refusal is still written down. A human can supply the permission the gate asks
for; a human cannot overrule the gate.

Each action writes its own audit entry with ``actor=human``, so the trail distinguishes what the
agent decided from what a person decided. That distinction is the whole reason a reviewer trusts
the log: an invoice that closed because someone clicked a button should never read like one the
agent recovered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.agent import runner
from app.agent.metrics import SETTLE_ACTION_TYPE
from app.audit.log import record as audit_record
from app.clock import now_utc
from app.config import settings
from app.delivery import email, sender
from app.enums import (
    ActionStatus,
    ActionType,
    ActorType,
    Channel,
    RecoveryState,
    StopReason,
    UnpaidCause,
)
from app.exceptions import NotFoundError, PayvraError
from app.generation.context import ContextIncomplete
from app.guardrails.gate import gate
from app.models.action import Action
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.message import Message
from app.models.payment_link import PaymentLink
from app.money import paise_to_exact
from app.razorpay.client import RazorpayClient
from app.razorpay.links import LinkBudgetExceeded, create_link
from app.reconciliation import manual
from app.reconciliation.sync import SyncResult, sync_invoice
from app.schemas.gate import ProposedAction


#: What the UI shows after an action. ``ok`` decides the colour, not whether anything was written --
#: a refused send is a successful *decision* and still produces an audit entry.
@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str


def _placeholder_link(invoice_id: uuid.UUID) -> str:
    """Stand-in used only when previewing an invoice that has no payment link yet.

    Defined once because two places have to agree on it byte for byte: the preview that puts it
    in the textarea, and the send that swaps it back out for the real link.
    """
    return f"{settings.public_base_url.rstrip('/')}/preview/{invoice_id}"


def _existing_link_url(db: Session, invoice_id: uuid.UUID) -> str | None:
    """The most recent payment link for this invoice, if one has been created."""
    row = (
        db.execute(
            select(PaymentLink)
            .where(PaymentLink.invoice_id == invoice_id)
            .order_by(desc(PaymentLink.created_at))
        )
        .scalars()
        .first()
    )
    return str(row.short_url) if row is not None else None


def _load(db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID) -> Invoice:
    """Merchant-scoped, so another tenant's invoice reads as absent rather than forbidden."""
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFoundError("invoice not found")
    return invoice


def latest_action(db: Session, invoice: Invoice) -> Action | None:
    """The most recent proposal on this invoice, whatever became of it."""
    return db.execute(
        select(Action)
        .where(Action.invoice_id == invoice.id)
        .order_by(desc(Action.created_at))
    ).scalars().first()


def preview(db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID) -> runner.Draft | None:
    """The message a human would be approving, drafted exactly as the agent would draft it.

    Uses a placeholder link rather than creating a real one: previewing must not spend Razorpay
    budget, and a person opening a page to *look* has not decided to send anything yet. The real
    link is created at send time.
    """
    invoice = _load(db, invoice_id, merchant_id)
    action = ProposedAction(
        invoice_id=invoice.id,
        type=ActionType.SEND_MESSAGE,
        tone_tier=max(1, min(int(invoice.current_tone_tier), 4)),
        rationale="Preview",
        channel=Channel.EMAIL,
    )
    # The real link when there is one. Previewing with a stand-in and then sending the edited
    # text verbatim shipped a body whose link did not exist, and check 6 refused it for exactly
    # that -- the preview has to show what would actually go out.
    link_url = _existing_link_url(db, invoice.id) or _placeholder_link(invoice.id)
    try:
        return runner._draft_for(db, invoice, action, link_url)
    except ContextIncomplete:
        # No consent on file, no contact, nothing to build an opt-out link from. The account page
        # still has to render -- a preview is a convenience, and letting it raise would take down
        # the only screen that explains *why* the account cannot be contacted.
        return None


def approve_and_send(
    db: Session,
    invoice_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    actor_id: str,
    body_override: str | None = None,
) -> Outcome:
    """Record a human approval, re-gate, and send if every check passes.

    The approval satisfies ``check_value_threshold`` and nothing else. All seven checks run again
    on fresh state -- so an approval granted this morning cannot be spent at midnight, and an
    invoice paid since the review is caught by the freshness check rather than emailed anyway.
    """
    invoice = _load(db, invoice_id, merchant_id)
    now = now_utc()

    action = ProposedAction(
        invoice_id=invoice.id,
        type=ActionType.SEND_MESSAGE,
        tone_tier=max(1, min(int(invoice.current_tone_tier), 4)),
        rationale=f"Approved by {actor_id} from the review queue.",
        proposed_by=ActorType.HUMAN,
        channel=Channel.EMAIL,
        approved_by=actor_id,
    )

    # A link is real money and finite budget, so it is created only once the person has committed.
    try:
        client = RazorpayClient()
        link = create_link(db, client, invoice)
        link_url = link.link.short_url
    except LinkBudgetExceeded as exc:
        return Outcome(False, f"No payment link could be created: {exc}")
    except PayvraError as exc:
        return Outcome(False, f"Payment link failed: {exc}")

    try:
        draft = runner._draft_for(db, invoice, action, link_url)
    except ContextIncomplete as exc:
        return Outcome(False, f"Cannot draft a compliant message: {exc}")
    if draft is None:
        return Outcome(False, "Nothing could be drafted for this invoice.")
    if body_override and body_override.strip():
        # A body typed against the preview may still hold the stand-in URL. Swapping it for the
        # link just created keeps an edited message sendable; without this, touching the textarea
        # at all guaranteed a content-policy refusal.
        edited = body_override.strip().replace(_placeholder_link(invoice.id), link_url)
        draft = runner.Draft(
            message=draft.message.model_copy(update={"body": edited}),
            contact=draft.contact,
            source=draft.source,
            origin=draft.origin,
        )
    action = action.model_copy(update={"message": draft.message})

    verdict = gate(db, action, now=now)

    action_row = Action(
        id=uuid.uuid4(),
        merchant_id=merchant_id,
        invoice_id=invoice.id,
        type=action.type.value,
        status=ActionStatus.GATED_PASS.value if verdict.passed else ActionStatus.GATED_FAIL.value,
        channel=Channel.EMAIL.value,
        tone_tier=action.tone_tier,
        proposed_by=ActorType.HUMAN.value,
        rationale=action.rationale,
        gate_verdicts=[c.model_dump(mode="json") for c in verdict.checks],
        gate_failure_reason=None if verdict.passed else ", ".join(verdict.blocked_by),
        # Both are NOT NULL on the table. The runner sets them and this path must too -- omitting
        # them turned every approved send into a 500 at the insert, after the gate had already
        # passed and before anything was recorded.
        scheduled_for=now,
        executed_at=None,
    )
    db.add(action_row)
    db.flush()

    if not verdict.passed:
        db.commit()
        return Outcome(
            False,
            "The gate refused this send even with your approval: "
            + ", ".join(verdict.blocked_by).replace("_", " ")
            + ". The refusal is in the audit log.",
        )

    try:
        result = sender.send(
            action,
            verdict,
            contact_email=draft.contact.email if draft.contact else None,
            now=now,
        )
    except (email.DeliveryError, sender.GateNotPassedError) as exc:
        action_row.status = ActionStatus.FAILED.value
        db.flush()
        audit_record(
            db,
            merchant_id=merchant_id,
            actor=ActorType.HUMAN,
            actor_id=actor_id,
            action_type="invoice.send_failed",
            subject_type="invoice",
            subject_id=invoice.id,
            outcome="refused",
            rationale=f"Approved send failed at the transport: {exc}",
            inputs={"error": str(exc)},
        )
        db.commit()
        return Outcome(False, f"Approved, but the send failed: {exc}")

    action_row.status = ActionStatus.EXECUTED.value
    action_row.executed_at = now
    runner._persist_message(db, action_row=action_row, draft=draft, result=result)
    invoice.touch_count = int(invoice.touch_count) + 1
    if invoice.recovery_state in (
        RecoveryState.NOT_STARTED.value,
        RecoveryState.HUMAN_REVIEW.value,
    ):
        invoice.recovery_state = RecoveryState.CHASING.value
    db.flush()

    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.HUMAN,
        actor_id=actor_id,
        action_type="invoice.approved_send",
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="executed",
        rationale=f"Human-approved reminder sent to {result.to}.",
        inputs={
            "provider_message_id": result.provider_message_id,
            "tone_tier": action.tone_tier,
            "edited": bool(body_override and body_override.strip()),
        },
    )
    db.commit()
    return Outcome(True, f"Sent. Provider reference {result.provider_message_id}.")


def update_contact(
    db: Session,
    invoice_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    email_address: str,
    name: str | None,
    actor_id: str,
) -> Outcome:
    """Fix a wrong contact, and let the account resume.

    ``wrong_contact`` is the one diagnosis a human can actually resolve in seconds and the agent
    cannot resolve at all -- it has no way to discover an address nobody told it. Correcting it
    clears the stale flag and returns the invoice to the queue, because the reason it stopped no
    longer holds.
    """
    invoice = _load(db, invoice_id, merchant_id)
    address = email_address.strip()
    if "@" not in address or len(address) < 5:
        return Outcome(False, "That does not look like an email address.")

    counterparty = db.get(Counterparty, invoice.counterparty_id)
    if counterparty is None:  # pragma: no cover - FK guarantees this
        raise NotFoundError("counterparty not found")

    existing = db.execute(
        select(Contact)
        .where(Contact.counterparty_id == counterparty.id)
        .order_by(desc(Contact.is_primary))
    ).scalars().first()

    if existing is not None:
        previous = existing.email
        existing.email = address
        existing.is_stale = False
        existing.is_primary = True
        if name and name.strip():
            existing.name = name.strip()
    else:
        previous = None
        db.add(
            Contact(
                id=uuid.uuid4(),
                counterparty_id=counterparty.id,
                name=(name or counterparty.name).strip(),
                email=address,
                is_primary=True,
                is_stale=False,
            )
        )

    if invoice.inferred_cause == UnpaidCause.WRONG_CONTACT.value:
        invoice.inferred_cause = UnpaidCause.UNKNOWN.value
    if invoice.recovery_state == RecoveryState.STOPPED.value and invoice.stop_reason in (
        None,
        StopReason.NO_CONSENT.value,
    ):
        invoice.recovery_state = RecoveryState.NOT_STARTED.value
        invoice.stop_reason = None
    db.flush()

    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.HUMAN,
        actor_id=actor_id,
        action_type="contact.updated",
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="applied",
        rationale=f"Contact corrected to {address}.",
        inputs={"previous": previous, "current": address},
    )
    db.commit()
    return Outcome(True, f"Contact updated to {address}. This account can be chased again.")


def mark_disputed(
    db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID, *, reason: str, actor_id: str
) -> Outcome:
    """Freeze outreach because the customer disagrees with the invoice."""
    _load(db, invoice_id, merchant_id)
    manual.mark_disputed(
        db,
        invoice_id,
        merchant_id=merchant_id,
        reason=reason.strip() or "Marked by merchant",
        actor_id=actor_id,
    )
    db.commit()
    return Outcome(True, "Marked disputed. All outreach on this invoice is frozen.")


def resolve_dispute(
    db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID, *, actor_id: str
) -> Outcome:
    """Un-freeze an invoice whose dispute has been settled.

    The counterpart to ``mark_disputed``. Without it a dispute is a one-way door, and every
    resolved disagreement would need a database client to clear -- which is exactly the kind of
    thing that quietly turns a product into a spreadsheet.
    """
    invoice = _load(db, invoice_id, merchant_id)
    if invoice.stop_reason != StopReason.DISPUTED.value:
        return Outcome(False, "This invoice is not marked disputed.")

    invoice.recovery_state = RecoveryState.NOT_STARTED.value
    invoice.stop_reason = None
    invoice.inferred_cause = UnpaidCause.UNKNOWN.value
    db.flush()

    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.HUMAN,
        actor_id=actor_id,
        action_type="invoice.dispute_resolved",
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="applied",
        rationale="Dispute resolved by the merchant; outreach may resume.",
        inputs={},
    )
    db.commit()
    return Outcome(True, "Dispute resolved. The agent may chase this account again.")


def stop_chasing(
    db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID, *, reason: str, actor_id: str
) -> Outcome:
    """A human ends outreach permanently. Recorded as ``merchant_excluded``.

    Distinct from every rule-driven stop: the reason says a person decided this, not a policy,
    which is what someone auditing the book needs to be able to tell apart.
    """
    invoice = _load(db, invoice_id, merchant_id)
    invoice.recovery_state = RecoveryState.STOPPED.value
    invoice.stop_reason = StopReason.MERCHANT_EXCLUDED.value
    db.flush()

    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.HUMAN,
        actor_id=actor_id,
        action_type="invoice.stopped",
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="stopped",
        rationale=f"Stopped by the merchant: {reason.strip() or 'no reason given'}.",
        inputs={"reason": reason.strip()},
    )
    db.commit()
    return Outcome(True, "Outreach stopped. The agent will not contact this account again.")


def mark_paid(
    db: Session,
    invoice_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    amount_rupees: str,
    method: str,
    reference: str | None,
    actor_id: str,
) -> Outcome:
    """Record a payment that arrived outside Razorpay -- cheque, NEFT, RTGS.

    Runs the identical settle path a webhook runs, so the money is counted, pending outreach is
    revoked and the audit entry is written by the same code. The only difference on record is
    ``actor=human``, which is exactly the difference that matters.
    """
    invoice = _load(db, invoice_id, merchant_id)
    try:
        paise = int(round(float(amount_rupees.replace(",", "").strip()) * 100))
    except ValueError:
        return Outcome(False, "Enter the amount in rupees, for example 25000.")
    if paise <= 0:
        return Outcome(False, "The amount must be more than zero.")
    if paise > int(invoice.outstanding_paise):
        return Outcome(False, "That is more than the amount outstanding on this invoice.")
    if method not in manual.OFFLINE_METHODS:
        return Outcome(False, "Choose how the payment arrived.")

    result = manual.mark_paid_offline(
        db,
        invoice.id,
        merchant_id=merchant_id,
        amount_paise=paise,
        method=method,
        reference=(reference or "").strip() or None,
        actor_id=actor_id,
    )
    db.commit()

    # A partial payment is not the end of the chase, and saying so would be a lie the operator
    # would discover the next time the agent contacted this customer.
    if result.fully_settled:
        return Outcome(True, "Payment recorded in full. Outreach on this invoice has stopped.")
    return Outcome(
        True,
        f"Part payment recorded. {paise_to_exact(result.outstanding_after_paise)} still "
        f"outstanding, so the agent will keep chasing the balance.",
    )


__all__ = [
    "DemoState",
    "Outcome",
    "Step",
    "demo_candidates",
    "demo_create_link",
    "check_payment",
    "demo_state",
    "escalate",
    "approve_and_send",
    "latest_action",
    "mark_disputed",
    "mark_paid",
    "preview",
    "resolve_dispute",
    "stop_chasing",
    "update_contact",
]


# --- demo mode ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One stage of the loop, and whether it has happened yet.

    ``state`` is one of done | active | waiting | todo. It is *computed* from the database on
    every render rather than stored in a wizard, so the board is incapable of claiming a step
    happened when the row that proves it does not exist. Reloading mid-demo shows the truth
    rather than a remembered position.
    """

    key: str
    title: str
    state: str
    detail: str


@dataclass
class DemoState:
    invoice: Invoice | None = None
    counterparty: Counterparty | None = None
    contact: Contact | None = None
    link: Any = None
    message: Any = None
    draft: Any = None
    settlements: int = 0
    recovered_paise: int = 0
    steps: tuple[Step, ...] = ()
    tone_tier: int = 1
    touch_count: int = 0
    paid: bool = False
    fully_paid: bool = False
    sync: Any = None


def demo_candidates(db: Session, merchant_id: uuid.UUID) -> list[tuple[Invoice, Counterparty]]:
    """Invoices that can actually complete the story.

    Filtered to ones under the Razorpay link ceiling and not already stopped -- picking an account
    the flow cannot finish is the fastest way to waste a recording session.
    """
    return [
        (invoice, counterparty)
        for invoice, counterparty in db.execute(
            select(Invoice, Counterparty)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .where(
                Invoice.merchant_id == merchant_id,
                Invoice.outstanding_paise > 0,
                Invoice.recovery_state != RecoveryState.STOPPED.value,
            )
            .order_by(desc(Invoice.priority_score).nullslast())
            .limit(8)
        ).all()
    ]


def check_payment(db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID) -> SyncResult:
    """Ask Razorpay whether this invoice's link has been paid, and settle the difference.

    This is what makes "waiting for payment" resolve without a webhook tunnel. A missed webhook
    is otherwise permanent -- the money sits at Razorpay and the book never learns -- and the
    board would spin for ever on a payment that had already happened.
    """
    result = sync_invoice(db, invoice_id, merchant_id)
    if result.changed:
        db.commit()
    elif result.checked:
        db.commit()  # the link's own status may have moved even when no money did
    return result


def demo_state(
    db: Session, merchant_id: uuid.UUID, invoice_id: str | None, *, poll: bool = False
) -> DemoState:
    """Everything the demo board renders, derived from what is actually in the database.

    With ``poll``, Razorpay is asked about the link first, so a payment made a moment ago is
    already settled by the time the steps below are computed.
    """
    state = DemoState()
    if not invoice_id:
        return state
    try:
        wanted = uuid.UUID(invoice_id)
    except ValueError:
        return state

    row = db.execute(
        select(Invoice, Counterparty)
        .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
        .where(Invoice.id == wanted, Invoice.merchant_id == merchant_id)
    ).one_or_none()
    if row is None:
        return state

    state.invoice, state.counterparty = row
    invoice = state.invoice

    if poll and int(invoice.outstanding_paise) > 0:
        state.sync = check_payment(db, invoice.id, merchant_id)
        db.refresh(invoice)

    state.contact = (
        db.execute(
            select(Contact)
            .where(Contact.counterparty_id == state.counterparty.id)
            .order_by(desc(Contact.is_primary))
        )
        .scalars()
        .first()
    )
    state.link = (
        db.execute(
            select(PaymentLink)
            .where(PaymentLink.invoice_id == invoice.id)
            .order_by(desc(PaymentLink.created_at))
        )
        .scalars()
        .first()
    )
    state.message = (
        db.execute(
            select(Message)
            .join(Action, Action.id == Message.action_id)
            .where(Action.invoice_id == invoice.id)
            .order_by(desc(Message.created_at))
        )
        .scalars()
        .first()
    )

    settle_rows = db.execute(
        select(AuditLog.inputs["amount_paise"].astext).where(
            AuditLog.merchant_id == merchant_id,
            AuditLog.action_type == SETTLE_ACTION_TYPE,
            AuditLog.subject_id == invoice.id,
        )
    ).all()
    state.settlements = len(settle_rows)
    state.recovered_paise = sum(int(r[0] or 0) for r in settle_rows)
    state.tone_tier = int(invoice.current_tone_tier)
    state.touch_count = int(invoice.touch_count)
    state.draft = preview(db, invoice.id, merchant_id)
    state.paid = state.recovered_paise > 0
    state.fully_paid = int(invoice.outstanding_paise) <= 0

    sent = state.message is not None
    linked = state.link is not None

    state.steps = (
        Step(
            "detect",
            "Detect",
            "done",
            f"{invoice.days_past_due} days past due · "
            f"{paise_to_exact(invoice.outstanding_paise)} at risk",
        ),
        Step(
            "diagnose",
            "Diagnose",
            "done",
            invoice.priority_reason or "Ranked by recoverable value.",
        ),
        Step(
            "link",
            "Create payment link",
            "done" if linked else "active",
            state.link.short_url if linked else "One real Razorpay link, for this invoice only.",
        ),
        Step(
            "draft",
            "Draft the reminder",
            "done" if sent else ("active" if linked else "todo"),
            (
                state.message.subject
                if sent
                else (state.draft.message.subject if state.draft else "Needs a contact on file.")
            ),
        ),
        Step(
            "send",
            "Send it",
            "done" if sent else ("active" if linked else "todo"),
            (
                f"Delivered · provider {state.message.provider_message_id}"
                if sent
                else "All seven checks run first, and can still refuse."
            ),
        ),
        Step(
            "track",
            "Wait for payment",
            "done" if state.paid else ("waiting" if sent else "todo"),
            (
                f"{state.settlements} settlement(s) · {paise_to_exact(state.recovered_paise)} in"
                if state.paid
                else "Pay the link and this page updates on its own."
            ),
        ),
        Step(
            "escalate",
            "Escalate if unpaid",
            "done"
            if (not state.paid and state.tone_tier > 1)
            else ("active" if (sent and not state.paid) else "todo"),
            (
                "Not needed — they paid."
                if state.paid
                else f"Currently tone tier {state.tone_tier}."
            ),
        ),
        Step(
            "recovered",
            "Recovery recorded",
            "done" if state.fully_paid else ("active" if state.paid else "todo"),
            (
                f"{paise_to_exact(state.recovered_paise)} recovered and attributed"
                if state.paid
                else "Reconciled by a signed webhook, never by us marking it so."
            ),
        ),
    )
    return state


def demo_create_link(db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID) -> Outcome:
    """Step 2 alone, so a recording can pause on the link before anything is sent."""
    invoice = _load(db, invoice_id, merchant_id)
    try:
        result = create_link(db, RazorpayClient(), invoice)
    except LinkBudgetExceeded as exc:
        return Outcome(False, f"Link budget exhausted: {exc}")
    except PayvraError as exc:
        return Outcome(False, f"Razorpay refused the link: {exc}")
    db.commit()
    verb = "Created" if result.created else "Reused the existing"
    return Outcome(True, f"{verb} payment link — {result.link.short_url}")


def escalate(
    db: Session, invoice_id: uuid.UUID, merchant_id: uuid.UUID, *, actor_id: str
) -> Outcome:
    """Raise the tone for the next attempt, when a reminder has gone unanswered.

    **Stops at the merchant's approval tier rather than climbing past it.** Escalation is the one
    direction the agent may not take alone, so the ceiling is the whole point -- a button that
    walked a customer to the firmest tone unattended is exactly what the approval rule exists to
    prevent. Reaching the ceiling routes the account to human review instead.
    """
    invoice = _load(db, invoice_id, merchant_id)
    merchant = db.get(Merchant, merchant_id)
    ceiling = int(merchant.approval_tone_tier) if merchant else 3

    if int(invoice.outstanding_paise) <= 0:
        return Outcome(False, "This invoice is settled — there is nothing to escalate.")

    before = int(invoice.current_tone_tier)
    after = min(before + 1, ceiling)
    invoice.current_tone_tier = after
    invoice.recovery_state = (
        RecoveryState.HUMAN_REVIEW.value if after >= ceiling else RecoveryState.CHASING.value
    )
    db.flush()

    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.HUMAN,
        actor_id=actor_id,
        action_type="invoice.escalated",
        subject_type="invoice",
        subject_id=invoice.id,
        outcome="applied",
        rationale=f"Escalated from tone tier {before} to {after} after no payment.",
        inputs={"tone_tier_before": before, "tone_tier_after": after, "ceiling": ceiling},
    )
    db.commit()

    if after >= ceiling:
        return Outcome(
            True,
            f"Escalated to tier {after} — the approval ceiling. This account now needs a human "
            f"before anything further goes out.",
        )
    return Outcome(True, f"Escalated to tone tier {after}. The next reminder will be firmer.")
