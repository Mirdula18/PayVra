"""Stopping rules — check 7, and the one CLAUDE.md calls absolute.

Invariant 8: *settled, disputed, opted out, 3 broken promises, or touch cap reached -> permanent
stop, move to exception list, never contact again.* There is no override, no "one last try", and
no tier that escapes them.

Kept in its own module because these conditions are also the ones the reconciliation, reply, and
promise-sweep paths need to evaluate in later phases. They must reach exactly one implementation:
a stopping rule enforced in three places is a stopping rule enforced inconsistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.enums import PaymentStatus, RecoveryState, StopReason

# CLAUDE.md invariant 8. Not a merchant setting: it cannot be raised.
BROKEN_PROMISE_LIMIT = 3


class StopTrigger(StrEnum):
    """Why an invoice must never be contacted again.

    ``MERCHANT_PAUSED`` is the odd one out and is deliberately marked: it is a *temporary*
    merchant-wide halt (the ``POST /settings/pause`` kill switch), not a permanent per-invoice
    stop. It is evaluated here because ADR-005 fixes the gate at seven checks and an eighth would
    break the "always seven verdicts" invariant -- but it lifts on resume, and nothing should
    move the invoice to the exception list on the strength of it.
    """

    SETTLED = "settled"
    WRITTEN_OFF = "written_off"
    DISPUTED = "disputed"
    OPTED_OUT = "opted_out"
    BROKEN_PROMISES_EXCEEDED = "broken_promises_exceeded"
    TOUCH_CAP_REACHED = "touch_cap_reached"
    MERCHANT_EXCLUDED = "merchant_excluded"
    ALREADY_STOPPED = "already_stopped"
    QUARANTINED = "quarantined"
    MERCHANT_PAUSED = "merchant_paused"


# Everything except a pause is permanent and warrants the exception list.
_TEMPORARY = {StopTrigger.MERCHANT_PAUSED}


@dataclass(frozen=True)
class StopDecision:
    """Whether recovery must stop, and why. ``triggers`` lists *all* reasons, not the first."""

    triggers: tuple[StopTrigger, ...]
    detail: dict[str, object]

    @property
    def must_stop(self) -> bool:
        return bool(self.triggers)

    @property
    def is_permanent(self) -> bool:
        """True when any trigger is permanent, so the caller can move to the exception list."""
        return any(trigger not in _TEMPORARY for trigger in self.triggers)

    @property
    def stop_reason(self) -> str | None:
        """The ``invoices.stop_reason`` value to persist, or None for a temporary halt."""
        for trigger in self.triggers:
            if trigger in _TEMPORARY:
                continue
            try:
                return StopReason(trigger.value).value
            except ValueError:
                # QUARANTINED and ALREADY_STOPPED describe a state rather than a stop_reason.
                continue
        return None

    def describe(self) -> str:
        return ", ".join(trigger.value for trigger in self.triggers)


def evaluate(
    *,
    payment_status: str,
    recovery_state: str,
    stop_reason: str | None,
    touch_count: int,
    lifetime_touch_cap: int,
    broken_promise_count: int,
    counterparty_opted_out: bool,
    counterparty_quarantined: bool,
    counterparty_excluded: bool,
    merchant_paused: bool,
    inferred_cause: str | None = None,
) -> StopDecision:
    """Evaluate every stopping rule. Pure -- takes plain values, so it is trivially testable.

    Collects *all* triggers rather than returning on the first, for the same reason the gate runs
    all seven checks: the audit record is worth more complete, and a merchant asking "why did this
    stop?" deserves the whole answer.
    """
    triggers: list[StopTrigger] = []

    if payment_status == PaymentStatus.PAID.value:
        triggers.append(StopTrigger.SETTLED)
    if payment_status == PaymentStatus.WRITTEN_OFF.value:
        triggers.append(StopTrigger.WRITTEN_OFF)

    if recovery_state == RecoveryState.SETTLED.value:
        triggers.append(StopTrigger.SETTLED)
    elif recovery_state == RecoveryState.STOPPED.value:
        if stop_reason == StopReason.MERCHANT_EXCLUDED.value:
            triggers.append(StopTrigger.MERCHANT_EXCLUDED)
        elif stop_reason == StopReason.DISPUTED.value:
            triggers.append(StopTrigger.DISPUTED)
        elif stop_reason == StopReason.OPTED_OUT.value:
            triggers.append(StopTrigger.OPTED_OUT)
        else:
            triggers.append(StopTrigger.ALREADY_STOPPED)

    # A dispute freezes outreach whether or not the state machine has caught up yet: the reply
    # classifier may have set the cause moments before this gate ran.
    if inferred_cause == "dispute" and StopTrigger.DISPUTED not in triggers:
        triggers.append(StopTrigger.DISPUTED)

    if counterparty_opted_out and StopTrigger.OPTED_OUT not in triggers:
        triggers.append(StopTrigger.OPTED_OUT)
    if counterparty_quarantined:
        triggers.append(StopTrigger.QUARANTINED)
    if counterparty_excluded and StopTrigger.MERCHANT_EXCLUDED not in triggers:
        triggers.append(StopTrigger.MERCHANT_EXCLUDED)

    if broken_promise_count >= BROKEN_PROMISE_LIMIT:
        triggers.append(StopTrigger.BROKEN_PROMISES_EXCEEDED)
    if touch_count >= lifetime_touch_cap:
        triggers.append(StopTrigger.TOUCH_CAP_REACHED)

    if merchant_paused:
        triggers.append(StopTrigger.MERCHANT_PAUSED)

    # Deduplicate while preserving evaluation order.
    seen: dict[StopTrigger, None] = {}
    for trigger in triggers:
        seen.setdefault(trigger, None)

    return StopDecision(
        triggers=tuple(seen),
        detail={
            "payment_status": payment_status,
            "recovery_state": recovery_state,
            "touch_count": touch_count,
            "lifetime_touch_cap": lifetime_touch_cap,
            "broken_promise_count": broken_promise_count,
            "broken_promise_limit": BROKEN_PROMISE_LIMIT,
        },
    )
