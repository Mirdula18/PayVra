"""Choose one action per account. **The LLM proposes; deterministic code disposes** (ADR-001).

Three steps, in this order, and the order is the safety property:

1. :func:`policy_action` — the deterministic policy. Always runs, always returns something.
2. :func:`llm_action` — optional. Asks a model to propose from the closed registry.
3. :func:`validate` — registry check, transition check, schema check. Any failure discards the
   model's proposal and the policy's stands.

The policy is computed **first, not as a rescue**. A fallback reached only on failure is a path
nobody exercises; computing it every time means the deterministic answer is always the one in hand
and the model's is an upgrade that has to earn its place. With ``LLM_ENABLED=false`` the whole
module runs on step 1 alone, which is CLAUDE.md invariant 9.

Nothing here decides whether an action may *fire*. That is the gate's job and only the gate's --
see ``guardrails/gate.py``. This module answers "what is the right next move?", never "is it
allowed?", and the two questions are deliberately answered by different code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.agent import registry
from app.enums import ActionType, Channel, RecoveryState, UnpaidCause
from app.generation.llm import LLMJob, LLMUnavailable, available, complete
from app.models.invoice import Invoice
from app.schemas.gate import ProposedAction

logger = logging.getLogger(__name__)

#: Tone tier by attempt number (FR-16.6). The entire escalation ladder.
#:
#: Whether attempt N may fire at all is decided by gate checks 1, 4, 5 and 7 -- contact hours,
#: frequency caps, value/tier approval and stopping rules. Restating any of that here would create
#: a second place where escalation policy lives, and the two would drift.
TIER_BY_ATTEMPT: dict[int, int] = {1: 1, 2: 2, 3: 3}

#: Beyond this the ladder stops climbing. Tier 4 exists but is reserved for a human.
MAX_AGENT_TIER = 3

#: Used only when a caller does not supply the merchant's own cap. Matches the column default so
#: a missing argument cannot silently make the agent more aggressive than the merchant allows.
DEFAULT_LIFETIME_TOUCH_CAP = 6

_SYSTEM = (
    "You are a B2B receivables agent for an Indian business. You propose exactly ONE next action "
    "for an unpaid invoice, chosen from a closed list of tools. You never send anything yourself; "
    "a deterministic policy engine validates your proposal and a compliance gate decides whether "
    "it may run. Reply with JSON only."
)


@dataclass(frozen=True)
class Proposal:
    """One proposed action plus how it was arrived at."""

    action: ProposedAction
    source: str  # "llm" | "policy"
    origin: str = ""  # model id when source == "llm"
    #: Why the model's proposal was discarded, when it was. Recorded, never silently dropped.
    rejection_reason: str | None = None


def attempt_number(invoice: Invoice) -> int:
    """Which attempt this run would be for this invoice. 1-based."""
    return int(invoice.touch_count or 0) + 1


def tier_for_attempt(attempt: int) -> int:
    """Tone tier for an attempt number, clamped to the agent's ceiling (FR-16.6)."""
    return TIER_BY_ATTEMPT.get(attempt, MAX_AGENT_TIER)


def policy_action(
    invoice: Invoice,
    cause: UnpaidCause,
    channel: Channel,
    *,
    lifetime_touch_cap: int = DEFAULT_LIFETIME_TOUCH_CAP,
) -> ProposedAction:
    """The deterministic policy. Always returns an action; never raises.

    This is the entire product when every model provider is down, so it is written to be readable
    at a glance rather than clever.
    """
    state = invoice.recovery_state
    attempt = attempt_number(invoice)
    tier = tier_for_attempt(attempt)

    if cause is UnpaidCause.DISPUTE:
        return ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.MARK_DISPUTED,
            tone_tier=1,
            rationale="Invoice is disputed; freeze outreach and route to a human.",
            channel=None,
        )

    if cause is UnpaidCause.REFUSAL:
        return ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.STOP,
            tone_tier=1,
            rationale="Counterparty has refused; permanent stop and exception list.",
            channel=None,
        )

    if state == RecoveryState.PROMISED.value:
        return ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.SNOOZE,
            tone_tier=1,
            rationale="A promise to pay is open; do not chase until it is due.",
            channel=None,
        )

    # The agent's tone ladder tops out at tier 3, but "the ladder is exhausted" is a different
    # claim from "never contact this counterparty again", and only the merchant's lifetime cap
    # says the second one.
    #
    # An earlier version stopped permanently at attempt 4 regardless. With a lifetime cap of 6
    # that retired receivables at *half* the permitted budget -- and the three highest-value
    # invoices in the book were the ones it retired, because value correlates with how often they
    # had already been chased. The most valuable accounts were the first the agent gave up on.
    if invoice.touch_count >= lifetime_touch_cap:
        return ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.STOP,
            tone_tier=1,
            rationale=(
                f"{invoice.touch_count} touches made against a lifetime cap of "
                f"{lifetime_touch_cap}; stopping permanently."
            ),
            channel=None,
        )

    # The policy must obey the same registry the model's proposals are validated against, and this
    # guard has to sit above every branch that proposes outreach -- not just the ordinary one.
    # Otherwise the deterministic path -- the one that runs when every provider is down -- is the
    # only path that can propose an illegal transition, which is precisely backwards.
    if not registry.transition_allowed(ActionType.SEND_MESSAGE, state):
        return ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.SNOOZE,
            tone_tier=1,
            rationale=(
                f"recovery_state={state} does not permit outreach; leaving this account alone."
            ),
            channel=None,
        )

    if attempt > MAX_AGENT_TIER:
        # Still within the touch budget, but past the agent's tone ceiling. Propose the message at
        # tier 3 and let the gate route it: check 5 refuses anything at or above the approval tier
        # with "human approval required", which puts the account in the approval queue *as a
        # refusal carrying its reason*. That is the designed escalation path -- a human decides
        # whether to press harder, and the decision is on the record either way.
        return ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.SEND_MESSAGE,
            tone_tier=MAX_AGENT_TIER,
            rationale=(
                f"Attempt {attempt} is past the agent's tone ceiling; escalating further needs a "
                "human, so this goes to the approval queue rather than stopping."
            ),
            channel=channel,
        )

    reason = {
        UnpaidCause.OVERSIGHT: "Likely an oversight; a single gentle reminder.",
        UnpaidCause.CASH_CRUNCH: "Cash-crunched; keep the tone low and make paying easy.",
        UnpaidCause.WRONG_CONTACT: "Engagement suggests the contact may be wrong; try again.",
        UnpaidCause.AWAITING_DOCS: "Awaiting documents; send the invoice reference.",
    }.get(cause, "Standard reminder at the current tier.")

    return ProposedAction(
        invoice_id=invoice.id,
        type=ActionType.SEND_MESSAGE,
        tone_tier=tier,
        rationale=f"Attempt {attempt}, tone tier {tier}. {reason}",
        channel=channel,
    )


def validate(
    candidate: ProposedAction, invoice: Invoice
) -> tuple[bool, str | None]:
    """The three deterministic checks. Returns ``(ok, reason_if_not)``.

    Registry, transition, schema -- in that order. A proposal naming a tool that does not exist is
    a different failure from one naming a real tool at the wrong moment, and the audit entry should
    say which.
    """
    if not registry.is_registered(candidate.type):
        return False, f"{candidate.type} is not in the closed tool registry"

    tool = registry.get(ActionType(candidate.type))
    if not tool.executable:
        return False, f"{candidate.type} is registered but not executable in this phase"

    if not registry.transition_allowed(candidate.type, invoice.recovery_state):
        return False, (
            f"{candidate.type} is not allowed from recovery_state={invoice.recovery_state}"
        )

    if candidate.invoice_id != invoice.id:
        return False, "proposal names a different invoice"

    if tool.is_outbound and candidate.channel is None:
        return False, f"{candidate.type} is outbound but names no channel"

    if not 1 <= candidate.tone_tier <= MAX_AGENT_TIER:
        return False, (
            f"tone tier {candidate.tone_tier} is outside the agent's range 1-{MAX_AGENT_TIER}"
        )

    return True, None


def _prompt(invoice: Invoice, cause: UnpaidCause, attempt: int, channel: Channel) -> str:
    tools = "\n".join(
        f"- {t.type.value}: {t.description}"
        for t in registry.REGISTRY.values()
        if t.executable and registry.transition_allowed(t.type, invoice.recovery_state)
    )
    return (
        f"INVOICE {invoice.invoice_number}\n"
        f"outstanding_paise: {invoice.outstanding_paise}\n"
        f"days_past_due: {invoice.days_past_due}\n"
        f"recovery_state: {invoice.recovery_state}\n"
        f"diagnosed_cause: {cause.value}\n"
        f"attempt_number: {attempt} (tone tier {tier_for_attempt(attempt)} is standard here)\n"
        f"preferred_channel: {channel.value}\n\n"
        f"TOOLS YOU MAY PROPOSE:\n{tools}\n\n"
        "Reply with JSON only:\n"
        '{"action": "<tool>", "tone_tier": <1-3>, "rationale": "<one sentence>"}'
    )


def llm_action(
    invoice: Invoice, cause: UnpaidCause, channel: Channel
) -> tuple[ProposedAction | None, str, str | None]:
    """Ask a model to propose. Returns ``(action_or_None, origin, failure_reason)``.

    Never raises: an unavailable or misbehaving model must cost this invoice its *upgrade*, not
    its turn. The policy action is already in hand when this is called.
    """
    if not available(LLMJob.PROPOSAL):
        return None, "", "LLM unavailable"

    attempt = attempt_number(invoice)
    try:
        response = complete(
            _prompt(invoice, cause, attempt, channel),
            job=LLMJob.PROPOSAL,
            system=_SYSTEM,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
    except LLMUnavailable as exc:
        return None, "", str(exc)
    except Exception as exc:  # noqa: BLE001 - a proposal bug must not strand the invoice
        logger.exception("proposal failed invoice=%s", invoice.invoice_number)
        return None, "", f"{type(exc).__name__}: {exc}"

    try:
        raw = json.loads(response.text)
        action_type = ActionType(str(raw["action"]))
        tone_tier = int(raw.get("tone_tier", tier_for_attempt(attempt)))
        rationale = str(raw.get("rationale", "")).strip() or "proposed by model"
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        return None, response.model, f"unparseable proposal: {exc}"

    tool = registry.REGISTRY.get(action_type)
    candidate = ProposedAction(
        invoice_id=invoice.id,
        type=action_type,
        tone_tier=max(1, min(tone_tier, MAX_AGENT_TIER)),
        rationale=rationale,
        channel=channel if (tool and tool.is_outbound) else None,
    )
    return candidate, response.model, None


def propose(
    invoice: Invoice,
    cause: UnpaidCause,
    *,
    channel: Channel = Channel.EMAIL,
    lifetime_touch_cap: int = DEFAULT_LIFETIME_TOUCH_CAP,
) -> Proposal:
    """One action for this invoice. The deterministic answer unless the model beats it."""
    fallback = policy_action(invoice, cause, channel, lifetime_touch_cap=lifetime_touch_cap)

    candidate, origin, failure = llm_action(invoice, cause, channel)
    if candidate is None:
        return Proposal(action=fallback, source="policy", rejection_reason=failure)

    ok, reason = validate(candidate, invoice)
    if not ok:
        logger.info(
            "discarding model proposal invoice=%s type=%s: %s",
            invoice.invoice_number,
            candidate.type,
            reason,
        )
        return Proposal(action=fallback, source="policy", origin=origin, rejection_reason=reason)

    return Proposal(action=candidate, source="llm", origin=origin)


__all__ = [
    "DEFAULT_LIFETIME_TOUCH_CAP",
    "MAX_AGENT_TIER",
    "TIER_BY_ATTEMPT",
    "Proposal",
    "attempt_number",
    "llm_action",
    "policy_action",
    "propose",
    "tier_for_attempt",
    "validate",
]
