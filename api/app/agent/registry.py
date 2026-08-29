"""The closed tool registry, and the recovery-state transitions each tool implies.

**Anything not on this list is rejected at validate.** That sentence is the whole point of the
module: a proposal naming a tool that does not exist here never reaches the gate, let alone a
transport. The registry is data, not code paths, so "what can the agent do?" is answerable by
reading one table rather than by auditing call sites.

Two asymmetries are deliberate and load-bearing:

* **Stopping never needs approval; escalating does.** The system is free to be gentler on its own
  and must ask permission to be firmer.
* **A tool being *registered* is not permission to *use* it.** Registration says the action exists
  and is well-formed. Whether it may fire, now, against this invoice, is the gate's decision and
  only the gate's -- see ``guardrails/gate.py``. Nothing here duplicates a gate check.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.enums import ActionType, RecoveryState


@dataclass(frozen=True)
class Tool:
    """One entry in the closed registry."""

    type: ActionType
    description: str
    #: Whether executing this tool contacts a counterparty. Drives the outbound-only gate checks.
    is_outbound: bool
    #: Recovery states this tool may be proposed from. Empty means "any state".
    allowed_from: frozenset[RecoveryState]
    #: The state the invoice moves to on successful execution. ``None`` leaves it unchanged.
    transitions_to: RecoveryState | None
    #: Whether Phase 6's runner can execute it. See ``UNIMPLEMENTED`` below.
    executable: bool = True


# States from which any further outreach is forbidden. Kept here as the registry's own view of
# "terminal", and enforced independently by gate check 7 -- two layers agreeing, not one trusting
# the other.
TERMINAL_STATES: frozenset[RecoveryState] = frozenset(
    {RecoveryState.SETTLED, RecoveryState.STOPPED}
)

_CHASEABLE: frozenset[RecoveryState] = frozenset(
    {
        RecoveryState.NOT_STARTED,
        RecoveryState.NUDGED,
        RecoveryState.CHASING,
        RecoveryState.BROKEN_PROMISE,
        RecoveryState.ESCALATED,
    }
)

REGISTRY: dict[ActionType, Tool] = {
    ActionType.CREATE_PAYMENT_LINK: Tool(
        type=ActionType.CREATE_PAYMENT_LINK,
        description="Create a Razorpay payment link for the outstanding amount.",
        is_outbound=False,
        allowed_from=_CHASEABLE,
        transitions_to=None,
    ),
    ActionType.SEND_MESSAGE: Tool(
        type=ActionType.SEND_MESSAGE,
        description="Draft, gate and send a dunning message carrying the payment link.",
        is_outbound=True,
        allowed_from=_CHASEABLE,
        transitions_to=RecoveryState.CHASING,
    ),
    ActionType.SNOOZE: Tool(
        type=ActionType.SNOOZE,
        description="Defer this invoice; contact nobody today.",
        is_outbound=False,
        # Every non-terminal state, including human_review. Snoozing is the honest "leave this
        # account alone" move, and it must be available wherever outreach is not -- otherwise the
        # policy has no legal action for an invoice a human is already holding, and the one path
        # that runs when every provider is down is the one with nothing valid to propose.
        allowed_from=frozenset(set(RecoveryState) - TERMINAL_STATES),
        transitions_to=None,
    ),
    ActionType.MARK_DISPUTED: Tool(
        type=ActionType.MARK_DISPUTED,
        description="Freeze all outreach and route to a human.",
        is_outbound=False,
        allowed_from=frozenset(_CHASEABLE | {RecoveryState.PROMISED}),
        transitions_to=RecoveryState.HUMAN_REVIEW,
    ),
    ActionType.STOP: Tool(
        type=ActionType.STOP,
        description="Permanent stop. Move to the exception list and never contact again.",
        is_outbound=False,
        allowed_from=frozenset(),  # any state; stopping is always allowed
        transitions_to=RecoveryState.STOPPED,
    ),
    # --- registered, not executable by the Phase 6 runner -----------------------------------
    ActionType.ESCALATE_TIER: Tool(
        type=ActionType.ESCALATE_TIER,
        description="Raise the tone tier by one. Phase 6 escalates via the attempt counter.",
        is_outbound=False,
        allowed_from=_CHASEABLE,
        transitions_to=RecoveryState.ESCALATED,
        executable=False,
    ),
    ActionType.SWITCH_CHANNEL: Tool(
        type=ActionType.SWITCH_CHANNEL,
        description="Mark the contact stale and try an alternate channel.",
        is_outbound=True,
        allowed_from=_CHASEABLE,
        transitions_to=None,
        executable=False,
    ),
    ActionType.LOG_PROMISE: Tool(
        type=ActionType.LOG_PROMISE,
        description="Record a promise to pay. Requires reply handling (Phase 7).",
        is_outbound=False,
        allowed_from=_CHASEABLE,
        transitions_to=RecoveryState.PROMISED,
        executable=False,
    ),
    ActionType.OFFER_INSTALLMENT: Tool(
        type=ActionType.OFFER_INSTALLMENT,
        description="Offer a strategic instalment split for a cash-crunched counterparty.",
        is_outbound=True,
        allowed_from=_CHASEABLE,
        transitions_to=None,
        executable=False,
    ),
}

#: Tools the registry knows but the Phase 6 runner cannot carry out.
#:
#: They stay registered on purpose. Removing them would make a proposal naming one look like a
#: hallucinated tool rather than a real capability that is not wired yet, and the two need
#: different responses -- one is a model failure, the other is a roadmap entry.
#:
#: ``offer_installment`` is **open, not deferred** (ADR-006 option C): the ceiling split gives its
#: machinery a P0 reason to exist, and whether the agent should also propose a *strategic* split
#: is a decision for when it is wired.
UNIMPLEMENTED: frozenset[ActionType] = frozenset(
    tool.type for tool in REGISTRY.values() if not tool.executable
)


def is_registered(action_type: ActionType | str) -> bool:
    """Whether this is a tool at all. The first of the three validate checks."""
    try:
        return ActionType(action_type) in REGISTRY
    except ValueError:
        return False


def get(action_type: ActionType) -> Tool:
    """The registry entry. Raises ``KeyError`` for an unregistered type, which is a caller bug."""
    return REGISTRY[action_type]


def transition_allowed(action_type: ActionType, state: RecoveryState | str) -> bool:
    """Whether this tool may be proposed from this recovery state.

    A tool with an empty ``allowed_from`` is permitted from anywhere -- that is ``stop``, and it
    being unrestricted is the point: the system may always choose to do less.
    """
    tool = REGISTRY.get(ActionType(action_type))
    if tool is None:
        return False
    if not tool.allowed_from:
        return True
    try:
        current = RecoveryState(state)
    except ValueError:
        return False
    return current in tool.allowed_from


__all__ = [
    "REGISTRY",
    "TERMINAL_STATES",
    "UNIMPLEMENTED",
    "Tool",
    "get",
    "is_registered",
    "transition_allowed",
]
