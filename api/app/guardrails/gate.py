"""The guardrail gate: seven deterministic checks, fixed order, all seven always evaluated.

ADR-005. Runs in the dispatch window, immediately before execution -- never at planning time.

Four properties this module exists to guarantee:

1. **All seven checks always run**, even after one fails. Enforced by
   :class:`~app.schemas.gate.GateVerdict`, which cannot be constructed with fewer than seven
   results in ADR-005 order, so no early return can produce a partial audit record.
2. **No LLM call, ever.** Every check is a clock comparison, an indexed read, or a regex.
3. **No "warn and continue."** ``GateVerdict.passed`` is derived from the checks, and the
   delivery layer refuses a verdict that is not passed (see :mod:`app.delivery.sender`).
4. **Freshness re-reads the invoice from the database at gate time.** Hours pass between planning
   at 01:30 and dispatch; the invoice may have been paid in between. Chasing someone who has
   already paid is, per CLAUDE.md, the single worst failure this product has.

Every verdict, pass and fail, is written to ``audit_log`` by :func:`gate` itself, so a caller
cannot forget to record one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record as audit_record
from app.clock import IST, now_utc
from app.enums import ActionType, ActorType, PaymentStatus
from app.guardrails import policy_content, stopping
from app.models.action import Action
from app.models.consent import Consent
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.schemas.gate import (
    CheckName,
    CheckResult,
    GateVerdict,
    ProposedAction,
)

# Touches counted per rolling week, not per calendar week: a Monday reset would let a merchant
# send on Sunday and again on Monday and call it two weeks.
WEEK_DAYS = 7

# The audit outcome a *passing* gate writes. Passing the gate is authorisation, not execution:
# it means "policy permits this", not "this was sent". Writing 'executed' here would let a crash
# between gate and send leave the log claiming a message went out that never did.
#
# The audit log may under-claim. It may never over-claim. So the 'executed' entry is written by
# the delivery layer, after the transport confirms the send -- see app.delivery.sender.
#
# 'approved' is the value from the documented vocabulary in architecture/data-model.md
# (executed | blocked | stopped | approved | rejected). An earlier version wrote 'passed', which
# is outside that set and would have broken any consumer filtering on it.
GATE_PASSED_OUTCOME = "approved"
GATE_BLOCKED_OUTCOME = "blocked"

# Actions that *are* the stopping decision. Exempt from check 7, because refusing them would leave
# an invoice that must stop permanently unable to reach the stopped state. See
# check_stopping_rules for the full reasoning.
STOPPING_IMPLEMENTED_BY = frozenset({ActionType.STOP, ActionType.MARK_DISPUTED})


class GateError(Exception):
    """The gate could not be evaluated at all -- a missing invoice, merchant, or counterparty.

    Distinct from a *failed* check. A failed check is a normal, logged outcome; this means the
    context the gate needs does not exist, and no verdict can honestly be produced.
    """


@dataclass
class ExecutionContext:
    """Everything the gate reads, loaded at gate time.

    Deliberately holds the live :class:`Session` rather than a snapshot: check 2 must re-query the
    invoice itself, and a context that cached ``payment_status`` at construction would reintroduce
    exactly the staleness the check exists to catch.
    """

    db: Session
    merchant: Merchant
    invoice: Invoice
    counterparty: Counterparty
    now_ist: datetime

    def fresh_payment_status(self) -> tuple[str, int]:
        """Re-read payment status and outstanding straight from the database. Never cached.

        ``expire`` first, so an ORM identity-map copy loaded earlier in this transaction cannot
        satisfy the read from memory. The whole point of check 2 is to see what is true *now*.
        """
        self.db.expire(self.invoice, ["payment_status", "outstanding_paise", "settled_at"])
        row = self.db.execute(
            select(Invoice.payment_status, Invoice.outstanding_paise).where(
                Invoice.id == self.invoice.id
            )
        ).one()
        return row[0], int(row[1])


def load_context(
    db: Session, action: ProposedAction, *, now: datetime | None = None
) -> ExecutionContext:
    """Assemble the gate's context. All reads, no writes, no LLM."""
    invoice = db.get(Invoice, action.invoice_id)
    if invoice is None:
        raise GateError(f"invoice {action.invoice_id} not found")
    merchant = db.get(Merchant, invoice.merchant_id)
    if merchant is None:  # pragma: no cover - FK guarantees this
        raise GateError(f"merchant {invoice.merchant_id} not found")
    counterparty = db.get(Counterparty, invoice.counterparty_id)
    if counterparty is None:  # pragma: no cover - FK guarantees this
        raise GateError(f"counterparty {invoice.counterparty_id} not found")

    # IST, always. A naive datetime or a UTC hour would silently shift the contact window by
    # 5h30m and send at 02:30 local.
    moment = (now or now_utc()).astimezone(IST)
    return ExecutionContext(
        db=db, merchant=merchant, invoice=invoice, counterparty=counterparty, now_ist=moment
    )


# --- check 1: time window -------------------------------------------------------------------------


def check_time_window(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """08:00-19:00 IST (CLAUDE.md invariant 3; FR-7.1).

    Half-open on the upper bound: 19:00:00 is outside the window. "Nothing sends after 7pm" is
    the promise, and a message landing at 19:00:00 breaks it.

    Non-outbound actions are exempt -- logging a promise or marking a dispute contacts nobody, so
    the contact window has no bearing on them.
    """
    if not action.is_outbound:
        return CheckResult(
            check=CheckName.TIME_WINDOW,
            passed=True,
            detail={"exempt": True, "reason": "action does not contact anyone"},
        )

    start, end = ctx.merchant.contact_hour_start, ctx.merchant.contact_hour_end
    hour = ctx.now_ist.hour
    inside = start <= hour < end
    return CheckResult(
        check=CheckName.TIME_WINDOW,
        passed=inside,
        reason=(
            None
            if inside
            else f"{ctx.now_ist.strftime('%H:%M')} IST is outside {start:02d}:00-{end:02d}:00"
        ),
        detail={
            "now_ist": ctx.now_ist.isoformat(),
            "window": f"{start:02d}:00-{end:02d}:00 IST",
        },
    )


# --- check 2: freshness ---------------------------------------------------------------------------


def check_freshness(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """Re-read payment status from the database. **The check that matters most** (FR-7.2).

    ADR-005: "Check only at planning time. Rejected. Hours pass between planning (01:30) and
    dispatch." Nothing passed into the gate is trusted for this -- the status is fetched fresh,
    every time, immediately before execution.
    """
    payment_status, outstanding_paise = ctx.fresh_payment_status()

    settled = payment_status in (PaymentStatus.PAID.value, PaymentStatus.WRITTEN_OFF.value)
    nothing_owed = outstanding_paise <= 0
    passed = not (settled or nothing_owed)

    reason = None
    if settled:
        reason = f"invoice is {payment_status}; it was paid after this action was proposed"
    elif nothing_owed:
        reason = "nothing is outstanding on this invoice"

    return CheckResult(
        check=CheckName.FRESHNESS,
        passed=passed,
        reason=reason,
        detail={
            "payment_status": payment_status,
            "outstanding_paise": outstanding_paise,
            "read_at": ctx.now_ist.isoformat(),
        },
    )


# --- check 3: consent -----------------------------------------------------------------------------


def check_consent(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """Channel permitted, opt-out not exercised, counterparty not quarantined (FR-7.3, FR-2).

    Quarantine is the default for a newly imported counterparty: no consent basis has been
    confirmed, so there is nothing to rely on and we do not contact them (FR-2.3).
    """
    if not action.is_outbound:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=True,
            detail={"exempt": True, "reason": "action does not contact anyone"},
        )

    if ctx.counterparty.is_quarantined:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=False,
            reason="counterparty is quarantined; no consent basis has been confirmed",
            detail={"quarantined": True},
        )
    if ctx.counterparty.is_excluded:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=False,
            reason="counterparty has been excluded from automation by the merchant",
            detail={"excluded": True},
        )

    channel = action.channel or (action.message.channel if action.message else None)
    if channel is None:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=False,
            reason="outbound action names no channel, so consent cannot be established",
            detail={},
        )

    consent = ctx.db.execute(
        select(Consent).where(
            Consent.counterparty_id == ctx.counterparty.id,
            Consent.channel == str(channel),
        )
    ).scalar_one_or_none()

    if consent is None:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=False,
            reason=f"no consent record for {channel}",
            detail={"channel": str(channel)},
        )
    if consent.revoked_at is not None:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=False,
            reason=f"consent for {channel} was revoked on {consent.revoked_at.date().isoformat()}",
            detail={"channel": str(channel), "revoked_at": consent.revoked_at.isoformat()},
        )
    if not consent.is_permitted:
        return CheckResult(
            check=CheckName.CONSENT,
            passed=False,
            reason=f"{channel} is not a permitted channel for this counterparty",
            detail={"channel": str(channel)},
        )

    return CheckResult(
        check=CheckName.CONSENT,
        passed=True,
        detail={"channel": str(channel), "basis": consent.basis},
    )


# --- check 4: frequency cap -----------------------------------------------------------------------


def _touches_since(ctx: ExecutionContext, since: datetime) -> int:
    """Executed outbound actions to this counterparty at or after ``since``.

    Counted per *counterparty*, not per invoice: a customer with six overdue invoices must not
    receive six messages a week because each invoice is individually under its cap.

    Compared as a full ``timestamptz``, never date-truncated. An earlier version used
    ``func.date(executed_at) >= (now - 7 days).date()``, which was wrong twice over: it widened
    the window to as much as eight days (a touch at 00:01 on the boundary date still counted),
    and ``date()`` on a ``timestamptz`` resolves in the *session* timezone -- UTC in practice --
    so the window silently shifted 5h30m away from the IST business day it is supposed to track.
    Instant-to-instant comparison has neither problem.
    """
    return int(
        ctx.db.execute(
            select(func.count())
            .select_from(Action)
            .join(Invoice, Invoice.id == Action.invoice_id)
            .where(
                Invoice.counterparty_id == ctx.counterparty.id,
                Action.merchant_id == ctx.merchant.id,
                Action.executed_at.is_not(None),
                Action.executed_at >= since,
            )
        ).scalar_one()
    )


def check_frequency_cap(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """Max 2 touches/week per counterparty, max 6 per invoice lifetime (FR-7.4).

    Blocks on the 3rd touch in any rolling 7 days, and the 7th in an invoice lifetime -- it
    fails when the existing count is **more than** the cap, so exactly 2 this week still passes.

    The week is a **rolling 7 days from now, in IST**, not a calendar week. A calendar week would
    let two touches on Sunday and two on Monday both pass while the counterparty received four
    messages in 24 hours, which is not what a frequency cap is for.
    """
    if not action.is_outbound:
        return CheckResult(
            check=CheckName.FREQUENCY_CAP,
            passed=True,
            detail={"exempt": True, "reason": "action does not contact anyone"},
        )

    # Rolling, to the second, anchored on the IST "now" this gate is evaluating at.
    window_start = ctx.now_ist - timedelta(days=WEEK_DAYS)
    weekly = _touches_since(ctx, window_start)
    lifetime = ctx.invoice.touch_count
    weekly_cap = ctx.merchant.weekly_touch_cap
    lifetime_cap = ctx.merchant.lifetime_touch_cap

    reasons = []
    if weekly > weekly_cap:
        reasons.append(
            f"{weekly} touches in the last {WEEK_DAYS} days exceeds the cap of {weekly_cap}"
        )
    if lifetime > lifetime_cap:
        reasons.append(f"{lifetime} lifetime touches exceeds the cap of {lifetime_cap}")

    return CheckResult(
        check=CheckName.FREQUENCY_CAP,
        passed=not reasons,
        reason="; ".join(reasons) or None,
        detail={
            "touches_this_week": weekly,
            "window_start_ist": window_start.isoformat(),
            "weekly_cap": weekly_cap,
            "lifetime_touches": lifetime,
            "lifetime_cap": lifetime_cap,
        },
    )


# --- check 5: value threshold ---------------------------------------------------------------------


def check_value_threshold(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """Above the merchant's value threshold, or tier 3+, requires human approval (FR-7.5, FR-7.6).

    Both conditions are reported when both apply, so an approval queue entry says why it is there.

    **Non-outbound actions are exempt**, the same exemption ``check_time_window`` makes and for the
    same reason: approval governs *contacting* someone, and an action that contacts nobody cannot
    breach a contact-conduct rule. Without this, the registry's central asymmetry inverts --
    "stopping never needs approval; escalating does" -- because ``stop``, ``snooze`` and
    ``mark_disputed`` all read the invoice's outstanding amount and would be blocked on high-value
    accounts. The effect is that the *safest* action becomes the one requiring permission, and a
    high-value account that should be stopped sits in limbo instead.

    Surfaced when the Phase 6 runner first proposed ``stop`` for a Rs 14L invoice and the gate
    refused it. Every existing test here proposes ``send_message``, which is why the gate's own
    suite could not have caught it.
    """
    if not action.is_outbound:
        return CheckResult(
            check=CheckName.VALUE_THRESHOLD,
            passed=True,
            detail={
                "exempt": True,
                "reason": "action does not contact anyone; approval governs contact",
                "action_type": action.type.value,
            },
        )

    threshold = ctx.merchant.approval_value_threshold_paise
    tone_ceiling = ctx.merchant.approval_tone_tier
    outstanding = ctx.invoice.outstanding_paise
    approved = bool(action.approved_by)

    reasons = []
    if outstanding > threshold:
        reasons.append(
            f"outstanding {outstanding} paise exceeds the approval threshold of {threshold} paise"
        )
    if action.tone_tier >= tone_ceiling:
        reasons.append(
            f"tone tier {action.tone_tier} is at or above the approval tier {tone_ceiling}"
        )

    needs_approval = bool(reasons)
    passed = approved or not needs_approval

    return CheckResult(
        check=CheckName.VALUE_THRESHOLD,
        passed=passed,
        reason=None if passed else "human approval required: " + "; ".join(reasons),
        detail={
            "outstanding_paise": outstanding,
            "approval_value_threshold_paise": threshold,
            "tone_tier": action.tone_tier,
            "approval_tone_tier": tone_ceiling,
            "requires_approval": needs_approval,
            "approved_by": action.approved_by,
        },
    )


# --- check 6: content policy ----------------------------------------------------------------------


def check_content_policy(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """Banned phrases and required elements (FR-7.7). No LLM; regex and string containment.

    The amount is validated against the invoice's **live** outstanding, so a draft written before
    a partial payment landed fails here rather than going out with a superseded figure.
    """
    if not action.is_outbound:
        return CheckResult(
            check=CheckName.CONTENT_POLICY,
            passed=True,
            detail={"exempt": True, "reason": "action sends no message"},
        )
    if action.message is None:
        return CheckResult(
            check=CheckName.CONTENT_POLICY,
            passed=False,
            reason="outbound action carries no drafted message",
            detail={},
        )

    message = action.message
    _, outstanding = ctx.fresh_payment_status()
    full_text = f"{message.subject or ''}\n{message.body}"

    violations = policy_content.find_banned(full_text, tone_tier=action.tone_tier)
    shouted = policy_content.find_all_caps_demand(full_text)
    if shouted:
        violations.append(shouted)

    missing = policy_content.find_missing_elements(
        message.body,
        outstanding_paise=outstanding,
        invoice_number=ctx.invoice.invoice_number,
        payment_link_url=message.payment_link_url,
        opt_out_url=message.opt_out_url,
        sender_name=message.sender_name,
    )

    reasons = []
    if violations:
        reasons.append(
            "banned content: " + "; ".join(f"{v.description} ({v.evidence!r})" for v in violations)
        )
    if missing:
        reasons.append("missing required elements: " + ", ".join(m.value for m in missing))
    if message.quoted_amount_paise != outstanding:
        reasons.append(
            f"message quotes {message.quoted_amount_paise} paise but {outstanding} is outstanding"
        )

    return CheckResult(
        check=CheckName.CONTENT_POLICY,
        passed=not reasons,
        reason=" | ".join(reasons) or None,
        detail={
            "violations": [v.as_dict() for v in violations],
            "missing_elements": [m.value for m in missing],
            "outstanding_paise": outstanding,
            "quoted_amount_paise": message.quoted_amount_paise,
        },
    )


# --- check 7: stopping rules ----------------------------------------------------------------------


def check_stopping_rules(ctx: ExecutionContext, action: ProposedAction) -> CheckResult:
    """Settled, disputed, opted out, 3 broken promises, cap reached (FR-7.8).

    Absolute -- CLAUDE.md invariant 8. Delegates to :mod:`app.guardrails.stopping` so the
    reconciliation, reply and promise-sweep paths in later phases evaluate the identical rules.

    **The actions that implement stopping are exempt.** ``stop`` and ``mark_disputed`` contact
    nobody and are the very outcome the rule demands. Refusing them means an invoice that has hit
    the touch cap can never actually be moved to ``stopped``: the runner proposes the stop, the
    gate refuses it, the invoice stays in limbo, and every later run re-proposes and is refused
    again. The exception list -- the artefact that evidences stopping rules -- would stay empty
    precisely for the accounts that belong on it.

    ``snooze`` and ``create_payment_link`` are deliberately NOT exempt despite also being
    non-outbound: creating a payment link for an invoice that has already settled is exactly the
    kind of mistake this check exists to prevent.
    """
    if action.type in STOPPING_IMPLEMENTED_BY:
        return CheckResult(
            check=CheckName.STOPPING_RULES,
            passed=True,
            detail={
                "exempt": True,
                "reason": "this action implements the stopping decision rather than defying it",
                "action_type": action.type.value,
            },
        )

    payment_status, _ = ctx.fresh_payment_status()
    opted_out = ctx.db.execute(
        select(func.count())
        .select_from(Consent)
        .where(
            Consent.counterparty_id == ctx.counterparty.id,
            Consent.revoked_at.is_not(None),
        )
    ).scalar_one()

    decision = stopping.evaluate(
        payment_status=payment_status,
        recovery_state=ctx.invoice.recovery_state,
        stop_reason=ctx.invoice.stop_reason,
        touch_count=ctx.invoice.touch_count,
        lifetime_touch_cap=ctx.merchant.lifetime_touch_cap,
        broken_promise_count=ctx.counterparty.broken_promise_count,
        counterparty_opted_out=bool(opted_out) and _all_channels_revoked(ctx),
        counterparty_quarantined=ctx.counterparty.is_quarantined,
        counterparty_excluded=ctx.counterparty.is_excluded,
        merchant_paused=ctx.merchant.is_paused,
        inferred_cause=ctx.invoice.inferred_cause,
    )

    return CheckResult(
        check=CheckName.STOPPING_RULES,
        passed=not decision.must_stop,
        reason=(f"stopping rule: {decision.describe()}" if decision.must_stop else None),
        detail={
            **decision.detail,
            "triggers": [t.value for t in decision.triggers],
            "permanent": decision.is_permanent,
            "stop_reason": decision.stop_reason,
        },
    )


def _all_channels_revoked(ctx: ExecutionContext) -> bool:
    """Opt-out is all-channel and permanent (FR-2.5), so a single revoked row is not an opt-out.

    A merchant may legitimately revoke SMS while keeping email; that is a channel preference and
    check 3's business, not a stopping rule.
    """
    total, revoked = ctx.db.execute(
        select(
            func.count(),
            func.count().filter(Consent.revoked_at.is_not(None)),
        ).where(Consent.counterparty_id == ctx.counterparty.id)
    ).one()
    return bool(total) and total == revoked


# --- the gate -------------------------------------------------------------------------------------

# Fixed order, one entry per CheckName. A missing or reordered entry makes the GateVerdict
# unconstructable, so this tuple and ADR-005 cannot silently diverge.
CHECKS = (
    check_time_window,
    check_freshness,
    check_consent,
    check_frequency_cap,
    check_value_threshold,
    check_content_policy,
    check_stopping_rules,
)


def gate(
    db: Session,
    action: ProposedAction,
    *,
    now: datetime | None = None,
    write_audit: bool = True,
) -> GateVerdict:
    """Evaluate all seven checks and record the verdict. The only sanctioned path to sending.

    Every check runs even after an earlier one fails -- there is no short-circuit and no
    "warn and continue". A failed check halts the action; the caller cannot proceed because
    :func:`app.delivery.sender.send` refuses a verdict that did not pass.

    The audit entry is written here rather than by the caller, so a verdict cannot exist
    unrecorded (FR-7.9). Exactly one entry is written per call, carrying all seven verdicts, with
    ``outcome`` of ``approved`` or ``blocked`` -- never ``executed``. See
    :data:`GATE_PASSED_OUTCOME`.
    """
    ctx = load_context(db, action, now=now)

    results = [check(ctx, action) for check in CHECKS]
    verdict = GateVerdict(
        invoice_id=action.invoice_id,
        checks=results,
        evaluated_at=ctx.now_ist,
        action_type=action.type,
        action_id=action.action_id,
    )

    if write_audit:
        audit_record(
            db,
            merchant_id=ctx.merchant.id,
            actor=ActorType.SYSTEM,
            actor_id="guardrails.gate",
            action_type=f"gate.{action.type.value}",
            subject_type="invoice",
            subject_id=action.invoice_id,
            outcome=GATE_PASSED_OUTCOME if verdict.passed else GATE_BLOCKED_OUTCOME,
            rationale=(
                action.rationale
                if verdict.passed
                else f"blocked by {', '.join(verdict.blocked_by)}: "
                + "; ".join(f.reason or "" for f in verdict.failures)
            ),
            gate_verdicts=verdict.as_audit_verdicts(),
            inputs={
                "action_type": action.type.value,
                "tone_tier": action.tone_tier,
                "channel": str(action.channel) if action.channel else None,
                "proposed_by": str(action.proposed_by),
                "action_id": str(action.action_id) if action.action_id else None,
            },
        )

    return verdict


__all__ = [
    "CHECKS",
    "GATE_BLOCKED_OUTCOME",
    "GATE_PASSED_OUTCOME",
    "ExecutionContext",
    "GateError",
    "check_consent",
    "check_content_policy",
    "check_freshness",
    "check_frequency_cap",
    "check_stopping_rules",
    "check_time_window",
    "check_value_threshold",
    "gate",
    "load_context",
]
