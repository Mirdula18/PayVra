"""The batch runner. **The production caller Phases 3, 4 and 5 do not otherwise have.**

One synchronous pass over the ranked worklist (ADR-009, FR-16):

    diagnose -> propose exactly ONE action -> gate
             -> approved: create link, generate message, record executed
             -> refused:  persist the refusal with its reason, continue

The per-account order is fixed and is not an implementation detail:

* **Link before message** — the draft has to contain a payment link, and gate check 6 verifies it
  is there.
* **Message before gate** — check 6 inspects a *drafted* message. Gating first would fail every
  outbound action rather than pass it vacuously.
* **Gate immediately before execution** — a verdict is a statement about a moment and goes stale
  (``delivery/sender.VERDICT_MAX_AGE``). Generating a whole batch then gating it would fail
  verdicts on age rather than on policy.

**A refusal is a result, not an error.** The run continues, the reason is persisted, and the
refusal list is the artefact that evidences the stopping-rules and audit-trail clauses of the
Track 3 bar. A run with no refusals in it has demonstrated nothing about compliance.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import diagnose as diagnose_mod
from app.agent import propose as propose_mod
from app.audit.log import record as audit_record
from app.clock import IST, now_utc
from app.config import settings
from app.enums import (
    ActionStatus,
    ActorType,
    Channel,
    PaymentStatus,
    RecoveryRunStatus,
    RecoveryState,
)
from app.exceptions import PayvraError
from app.generation import drafter
from app.generation.context import ContextIncomplete, build_context
from app.guardrails.gate import gate
from app.models.action import Action
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.recovery_run import RecoveryRun
from app.razorpay.client import RazorpayClient
from app.razorpay.links import LinkBudgetExceeded, create_link
from app.schemas.gate import CheckName, DraftMessage, ProposedAction
from app.schemas.generation import ToneTier

logger = logging.getLogger(__name__)

#: Outcome strings on the per-account result. Stable — the UI and the report read these.
#:
#: ``executed`` and ``approved`` are deliberately different things. A ``stop`` or ``snooze``
#: genuinely completes: the state change is the whole action. An outbound message does not --
#: there is no delivery transport yet (``delivery/sender.send`` raises ``NotImplementedError``;
#: FR-10 is unbuilt), so the run creates a real payment link, drafts a real message and gets a real
#: gate approval, and then stops.
#:
#: Calling that "executed" would be an over-claim, and ``guardrails/gate.py`` is explicit that the
#: audit log may under-claim but must never over-claim. So it is recorded as ``approved``.
OUTCOME_EXECUTED = "executed"
OUTCOME_APPROVED = "approved"
OUTCOME_REFUSED = "refused"
OUTCOME_SKIPPED = "skipped"
OUTCOME_ERROR = "error"

#: Outcomes meaning "the run acted on this invoice and the gate allowed it". What causal recovery
#: attribution keys on -- a created, live payment link is a real mechanism for payment even before
#: a transport exists to announce it.
ACTED_OUTCOMES = (OUTCOME_EXECUTED, OUTCOME_APPROVED)


@dataclass
class AccountResult:
    """What the run did about one account, and why."""

    invoice_id: uuid.UUID
    invoice_number: str
    outcome: str
    action_type: str | None = None
    tone_tier: int | None = None
    attempt: int | None = None
    cause: str | None = None
    proposal_source: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    reason: str | None = None
    payment_link_url: str | None = None


@dataclass
class RunResult:
    """The whole pass. What a caller prints and what the report reads."""

    recovery_run_id: uuid.UUID
    merchant_id: uuid.UUID
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    contact_window: tuple[int, int]
    window_overridden: bool
    accounts: list[AccountResult] = field(default_factory=list)

    @property
    def executed(self) -> int:
        """Actions that fully completed. Excludes outbound ones, which have no transport yet."""
        return sum(1 for a in self.accounts if a.outcome == OUTCOME_EXECUTED)

    @property
    def approved(self) -> int:
        """Outbound actions the gate allowed: link created, message drafted, nothing delivered."""
        return sum(1 for a in self.accounts if a.outcome == OUTCOME_APPROVED)

    @property
    def acted_on(self) -> int:
        return sum(1 for a in self.accounts if a.outcome in ACTED_OUTCOMES)

    @property
    def refused(self) -> int:
        return sum(1 for a in self.accounts if a.outcome == OUTCOME_REFUSED)

    @property
    def errored(self) -> int:
        return sum(1 for a in self.accounts if a.outcome == OUTCOME_ERROR)


def resolve_contact_window(merchant: Merchant) -> tuple[tuple[int, int], bool]:
    """The contact window in force, and whether an override widened it (FR-16.8).

    Both override values must be set together; a half-configured override is ignored rather than
    half-applied. The result is **only ever a wider window than the merchant's** -- an override
    that would narrow it is discarded, because the knob exists to make a demo possible, not to
    become a way to change policy quietly in either direction.
    """
    start, end = merchant.contact_hour_start, merchant.contact_hour_end
    o_start = settings.contact_window_override_start
    o_end = settings.contact_window_override_end
    if o_start is None or o_end is None:
        return (start, end), False
    if not (0 <= o_start < o_end <= 24):
        logger.warning("ignoring invalid contact window override %s-%s", o_start, o_end)
        return (start, end), False
    widened = (min(start, o_start), max(end, o_end))
    return widened, widened != (start, end)


def _ranked_worklist(db: Session, merchant_id: uuid.UUID, limit: int) -> list[Invoice]:
    """The top N collectable invoices, highest priority first.

    Settled and stopped invoices are excluded here as well as by gate check 7. That is not
    redundancy for its own sake: not loading them keeps a run's limit spent on accounts it can
    actually act on, while the gate remains the thing that *decides*.
    """
    return list(
        db.execute(
            select(Invoice)
            .where(
                Invoice.merchant_id == merchant_id,
                Invoice.payment_status.in_(
                    (PaymentStatus.UNPAID.value, PaymentStatus.PARTIALLY_PAID.value)
                ),
                Invoice.recovery_state.not_in(
                    (RecoveryState.SETTLED.value, RecoveryState.STOPPED.value)
                ),
                Invoice.outstanding_paise > 0,
            )
            .order_by(Invoice.priority_score.desc().nullslast(), Invoice.days_past_due.desc())
            .limit(limit)
        ).scalars()
    )


def _persist_action(
    db: Session,
    *,
    run_id: uuid.UUID,
    merchant_id: uuid.UUID,
    action: ProposedAction,
    status: ActionStatus,
    verdict_payload: list[dict[str, object]],
    failure_reason: str | None,
    proposal_source: str,
    origin: str,
    now: datetime,
) -> Action:
    """Write the Action row. Every proposal is persisted, executed or not."""
    row = Action(
        id=action.action_id or uuid.uuid4(),
        merchant_id=merchant_id,
        invoice_id=action.invoice_id,
        recovery_run_id=run_id,
        type=action.type.value,
        status=status.value,
        channel=action.channel.value if action.channel else None,
        tone_tier=action.tone_tier,
        proposed_by=(ActorType.AGENT if proposal_source == "llm" else ActorType.SYSTEM).value,
        rationale=action.rationale,
        llm_model=origin or None,
        gate_verdicts=verdict_payload,
        gate_failure_reason=failure_reason,
        scheduled_for=now,
        executed_at=now if status is ActionStatus.EXECUTED else None,
    )
    db.add(row)
    db.flush()
    return row


def _record_window_override(
    db: Session, merchant: Merchant, run_id: uuid.UUID, window: tuple[int, int]
) -> None:
    """Write the override into the audit log (FR-16.8).

    This is what makes an out-of-window run compliant *by record*. Without it a widened window is
    invisible after the fact, and "we only ever send inside contact hours" becomes a claim nobody
    can check. The entry is written before any action, so it precedes everything it explains.
    """
    audit_record(
        db,
        merchant_id=merchant.id,
        actor=ActorType.SYSTEM,
        actor_id="agent.runner",
        action_type="run.contact_window_override",
        subject_type="merchant",
        subject_id=merchant.id,
        outcome="applied",
        rationale=(
            f"Contact window widened from "
            f"{merchant.contact_hour_start:02d}:00-{merchant.contact_hour_end:02d}:00 to "
            f"{window[0]:02d}:00-{window[1]:02d}:00 IST by environment configuration. "
            "The gate still evaluated every action against the window in force."
        ),
        inputs={
            "recovery_run_id": str(run_id),
            "configured_window": [merchant.contact_hour_start, merchant.contact_hour_end],
            "window_in_force": [window[0], window[1]],
        },
    )


def _run_audit(
    db: Session,
    *,
    merchant_id: uuid.UUID,
    run_id: uuid.UUID,
    invoice_id: uuid.UUID,
    outcome: str,
    rationale: str,
    inputs: dict[str, object],
) -> None:
    """Per-account audit entry, carrying the run id.

    ``recovery_run_id`` goes inside ``inputs`` rather than into a column of its own, because
    ``inputs`` is part of the hashed canonical form and a new column would not be. Run attribution
    is exactly the field a judge reads off the trail, so it has to be covered by the hash chain --
    an unhashed column could be altered without breaking it. See migration 0006.
    """
    audit_record(
        db,
        merchant_id=merchant_id,
        actor=ActorType.SYSTEM,
        actor_id="agent.runner",
        action_type="run.account",
        subject_type="invoice",
        subject_id=invoice_id,
        outcome=outcome,
        rationale=rationale,
        inputs={"recovery_run_id": str(run_id), **inputs},
    )


def _tone_tier(value: int) -> ToneTier:
    """Narrow a validated tier to the literal type the generation layer takes.

    ``ProposedAction.tone_tier`` is already constrained to 1-4 by its own field validator, so this
    cannot silently coerce an out-of-range value -- it clamps as a belt-and-braces and states the
    range once, rather than scattering ``cast`` calls that assert without checking.
    """
    clamped = max(1, min(int(value), 4))
    return cast(ToneTier, clamped)


def _draft_for(
    db: Session, invoice: Invoice, action: ProposedAction, link_url: str | None
) -> DraftMessage | None:
    """Generate the message this action would send, if it sends one.

    Returns ``None`` for a non-outbound action -- there is nothing to draft, and check 6 exempts
    it. An outbound action with no link cannot produce a compliant draft, so it returns ``None``
    too and the gate refuses it on content policy rather than here.
    """
    if not action.is_outbound or link_url is None:
        return None

    ctx = build_context(
        db,
        invoice,
        channel=action.channel or Channel.EMAIL,
        tone_tier=_tone_tier(action.tone_tier),
        payment_link_url=link_url,
    )
    message = drafter.generate(ctx)
    return DraftMessage(
        channel=ctx.channel,
        subject=message.subject,
        body=message.body,
        quoted_amount_paise=ctx.outstanding_paise,
        quoted_invoice_number=ctx.invoice_number,
        payment_link_url=ctx.payment_link_url,
        opt_out_url=ctx.opt_out_url,
        sender_name=ctx.merchant_name,
    )


def _process_account(
    db: Session,
    *,
    run: RecoveryRun,
    merchant: Merchant,
    invoice: Invoice,
    client: RazorpayClient | None,
    now: datetime,
) -> AccountResult:
    """One account, end to end. Never raises for an expected failure; records it instead."""
    counterparty = diagnose_mod.resolve_counterparty(db, invoice)
    diagnosis = diagnose_mod.diagnose(db, invoice, counterparty)
    attempt = propose_mod.attempt_number(invoice)
    proposal = propose_mod.propose(invoice, diagnosis.cause)
    action = proposal.action

    result = AccountResult(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        outcome=OUTCOME_SKIPPED,
        action_type=action.type.value,
        tone_tier=action.tone_tier,
        attempt=attempt,
        cause=diagnosis.cause.value,
        proposal_source=proposal.source,
    )

    # Pre-flight the gate before spending anything. Six of the seven checks need no message --
    # only content policy does -- so an action already doomed by contact hours, consent, a
    # frequency cap, a value threshold or a stopping rule can be refused now, before a real
    # Razorpay link is created for it.
    #
    # This does not weaken the gate: the full seven-check verdict below still runs immediately
    # before execution, and it is that verdict which authorises the send. This only avoids
    # spending a link (out of a 25-link budget) on an action that was never going to fire. A live
    # run burned six links on refused accounts and then ran out before reaching the accounts it
    # could actually have collected from.
    if action.is_outbound and not run.dry_run:
        preflight = gate(db, action, now=now, write_audit=False)
        doomed = [
            check
            for check in preflight.failures
            if check.check is not CheckName.CONTENT_POLICY
        ]
        if doomed:
            failure = "; ".join(c.reason or "" for c in doomed)
            _persist_action(
                db,
                run_id=run.id,
                merchant_id=merchant.id,
                action=action,
                status=ActionStatus.GATED_FAIL,
                verdict_payload=preflight.as_audit_verdicts(),
                failure_reason=failure,
                proposal_source=proposal.source,
                origin=proposal.origin,
                now=now,
            )
            result.outcome = OUTCOME_REFUSED
            result.blocked_by = [c.check.value for c in doomed]
            result.reason = failure
            _run_audit(
                db,
                merchant_id=merchant.id,
                run_id=run.id,
                invoice_id=invoice.id,
                outcome=OUTCOME_REFUSED,
                rationale=f"Refused before creating a payment link: {failure}",
                inputs={
                    "action_type": action.type.value,
                    "attempt": attempt,
                    "tone_tier": action.tone_tier,
                    "cause": diagnosis.cause.value,
                    "blocked_by": result.blocked_by,
                    "preflight": True,
                    "signals": diagnosis.signals.as_audit_payload(),
                },
            )
            return result

    link_url: str | None = None
    if action.is_outbound:
        if run.dry_run or client is None:
            # A dry run must still produce a draft the gate can inspect, or check 6 would refuse
            # every action for a reason that is an artefact of the dry run rather than of policy.
            link_url = f"{settings.public_base_url.rstrip('/')}/dry-run/{invoice.id}"
        else:
            try:
                link = create_link(db, client, invoice)
                link_url = link.link.short_url
                result.payment_link_url = link_url
            except LinkBudgetExceeded as exc:
                result.outcome = OUTCOME_SKIPPED
                result.reason = str(exc)
                return result
            except PayvraError as exc:
                result.outcome = OUTCOME_ERROR
                result.reason = f"payment link failed: {exc}"
                return result

    try:
        action = action.model_copy(update={"message": _draft_for(db, invoice, action, link_url)})
    except ContextIncomplete as exc:
        result.outcome = OUTCOME_ERROR
        result.reason = f"could not build message context: {exc}"
        return result

    action = action.model_copy(update={"action_id": uuid.uuid4()})
    verdict = gate(db, action, now=now)
    verdict_payload = verdict.as_audit_verdicts()

    if not verdict.passed:
        failure = "; ".join(f.reason or "" for f in verdict.failures)
        _persist_action(
            db,
            run_id=run.id,
            merchant_id=merchant.id,
            action=action,
            status=ActionStatus.GATED_FAIL,
            verdict_payload=verdict_payload,
            failure_reason=failure,
            proposal_source=proposal.source,
            origin=proposal.origin,
            now=now,
        )
        result.outcome = OUTCOME_REFUSED
        result.blocked_by = list(verdict.blocked_by)
        result.reason = failure
        _run_audit(
            db,
            merchant_id=merchant.id,
            run_id=run.id,
            invoice_id=invoice.id,
            outcome=OUTCOME_REFUSED,
            rationale=f"Refused: {failure}",
            inputs={
                "action_type": action.type.value,
                "attempt": attempt,
                "tone_tier": action.tone_tier,
                "cause": diagnosis.cause.value,
                "blocked_by": list(verdict.blocked_by),
                "signals": diagnosis.signals.as_audit_payload(),
            },
        )
        return result

    # Approved. A dry run stops here deliberately: it has produced a real verdict on a real draft,
    # which is the thing worth rehearsing, without spending link budget or contacting anyone.
    #
    # A live run stops here too for anything outbound, because there is nowhere to send it: FR-10
    # delivery is unbuilt. What genuinely completed is recorded as completed, and what did not is
    # recorded as approved. The difference is the whole reason the audit log is worth reading.
    completed = not run.dry_run and not action.is_outbound
    if completed:
        status = ActionStatus.EXECUTED
    elif run.dry_run:
        # A rehearsal, and it must not masquerade as queued work. ``gated_pass`` is one of the
        # PENDING_ACTION_STATUSES that reconciliation revokes on settle, so leaving dry-run rows
        # in it would inflate "revoked N pending actions" -- a number shown on stage -- with
        # actions that were never going to fire. Recorded as revoked, with the verdict intact,
        # because the verdict is the whole point of a dry run.
        status = ActionStatus.REVOKED
    else:
        status = ActionStatus.GATED_PASS

    persisted = _persist_action(
        db,
        run_id=run.id,
        merchant_id=merchant.id,
        action=action,
        status=status,
        verdict_payload=verdict_payload,
        failure_reason=None,
        proposal_source=proposal.source,
        origin=proposal.origin,
        now=now,
    )
    if status is ActionStatus.REVOKED:
        persisted.revoked_at = now
        db.flush()

    if completed:
        invoice.inferred_cause = diagnosis.cause.value
        tool = propose_mod.registry.get(action.type)
        if tool.transitions_to is not None:
            invoice.recovery_state = tool.transitions_to.value

    # touch_count is deliberately NOT incremented for an undelivered message. It feeds the
    # frequency cap, which counts *contacts* -- inflating it with drafts nobody received would
    # suppress future real outreach on the strength of messages that were never sent.
    result.outcome = OUTCOME_EXECUTED if completed else OUTCOME_APPROVED
    _run_audit(
        db,
        merchant_id=merchant.id,
        run_id=run.id,
        invoice_id=invoice.id,
        outcome=result.outcome,
        rationale=action.rationale,
        inputs={
            "action_type": action.type.value,
            "attempt": attempt,
            "tone_tier": action.tone_tier,
            "cause": diagnosis.cause.value,
            "proposal_source": proposal.source,
            "dry_run": run.dry_run,
            "delivered": False if action.is_outbound else None,
            "signals": diagnosis.signals.as_audit_payload(),
        },
    )
    return result


def run(
    db: Session,
    merchant_id: uuid.UUID,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RunResult:
    """One synchronous pass over the ranked worklist. The Phase 6 deliverable.

    Commits per account, so an interrupted run keeps the work it already did and its audit trail
    stays consistent with what actually happened. The run is not checkpointed -- re-running starts
    from the top of the worklist -- but re-running is safe: link creation is idempotent, messages
    are cached, and gate check 2 re-reads payment status immediately before every send.
    """
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        raise PayvraError(f"merchant {merchant_id} not found")

    account_limit = limit if limit is not None else settings.batch_run_default_limit
    window, overridden = resolve_contact_window(merchant)
    moment = (now or now_utc()).astimezone(IST)

    run_row = RecoveryRun(
        id=uuid.uuid4(),
        merchant_id=merchant_id,
        status=RecoveryRunStatus.RUNNING.value,
        started_at=moment,
        account_limit=account_limit,
        dry_run=dry_run,
        contact_hour_start=window[0],
        contact_hour_end=window[1],
        window_overridden=overridden,
    )
    db.add(run_row)
    db.flush()

    if overridden:
        _record_window_override(db, merchant, run_row.id, window)

    # The gate reads the window off the merchant, so an override is applied by setting it there
    # for the duration of the run. The check still executes and still refuses anything outside the
    # window in force -- there is no skip path (FR-16.8).
    configured = (merchant.contact_hour_start, merchant.contact_hour_end)
    merchant.contact_hour_start, merchant.contact_hour_end = window
    db.flush()

    result = RunResult(
        recovery_run_id=run_row.id,
        merchant_id=merchant_id,
        dry_run=dry_run,
        started_at=moment,
        finished_at=None,
        contact_window=window,
        window_overridden=overridden,
    )

    client: RazorpayClient | None = None
    if not dry_run:
        try:
            client = RazorpayClient()
        except PayvraError as exc:
            logger.warning("no Razorpay client (%s); outbound actions will be skipped", exc)

    try:
        invoices = _ranked_worklist(db, merchant_id, account_limit)
        for invoice in invoices:
            try:
                account = _process_account(
                    db, run=run_row, merchant=merchant, invoice=invoice, client=client, now=moment
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001 - one bad account must not lose the run
                db.rollback()
                logger.exception("account failed invoice=%s", invoice.invoice_number)
                account = AccountResult(
                    invoice_id=invoice.id,
                    invoice_number=invoice.invoice_number,
                    outcome=OUTCOME_ERROR,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            result.accounts.append(account)

        run_row.status = RecoveryRunStatus.COMPLETED.value
    except Exception:
        run_row.status = RecoveryRunStatus.FAILED.value
        raise
    finally:
        # Restore the merchant's configured window whatever happened. An override is scoped to the
        # run that asked for it; leaving it applied would silently widen every later run.
        merchant.contact_hour_start, merchant.contact_hour_end = configured
        run_row.finished_at = now_utc()
        run_row.accounts_considered = len(result.accounts)
        run_row.actions_proposed = len(
            [a for a in result.accounts if a.action_type is not None]
        )
        run_row.actions_executed = result.executed
        run_row.actions_refused = result.refused
        result.finished_at = run_row.finished_at
        db.commit()

    return result


__all__ = [
    "OUTCOME_ERROR",
    "OUTCOME_EXECUTED",
    "OUTCOME_REFUSED",
    "OUTCOME_SKIPPED",
    "AccountResult",
    "RunResult",
    "resolve_contact_window",
    "run",
]
