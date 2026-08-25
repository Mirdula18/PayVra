"""``ProposedAction`` and ``GateVerdict`` — the types the guardrail gate consumes and produces.

``GateVerdict`` carries the compliance claim, so its invariants are enforced by the model itself
rather than by the code that builds it: **seven checks, in the fixed ADR-005 order, always**. A
validator rejects a verdict that is short, reordered, or duplicated, which means a partial verdict
cannot be constructed at all -- not merely that the gate happens not to construct one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.enums import ActionType, ActorType, Channel


class CheckName(StrEnum):
    """The seven gate checks. Order here *is* the execution order (ADR-005)."""

    TIME_WINDOW = "time_window"
    FRESHNESS = "freshness"
    CONSENT = "consent"
    FREQUENCY_CAP = "frequency_cap"
    VALUE_THRESHOLD = "value_threshold"
    CONTENT_POLICY = "content_policy"
    STOPPING_RULES = "stopping_rules"


# Cheapest and most consequential first. time_window is a clock comparison; freshness is one
# indexed read and prevents the worst failure mode in the product.
GATE_CHECK_ORDER: tuple[CheckName, ...] = (
    CheckName.TIME_WINDOW,
    CheckName.FRESHNESS,
    CheckName.CONSENT,
    CheckName.FREQUENCY_CAP,
    CheckName.VALUE_THRESHOLD,
    CheckName.CONTENT_POLICY,
    CheckName.STOPPING_RULES,
)

GATE_CHECK_COUNT = len(GATE_CHECK_ORDER)


class DraftMessage(BaseModel):
    """The message an action would send. Checked by content policy; never generated here."""

    channel: Channel
    subject: str | None = None
    body: str
    # What the message claims. content_policy cross-checks these against the live invoice rather
    # than trusting them -- a message quoting a stale amount is the failure this catches.
    quoted_amount_paise: int
    quoted_invoice_number: str
    payment_link_url: str | None = None
    opt_out_url: str | None = None
    sender_name: str | None = None


class ProposedAction(BaseModel):
    """One action the agent (or a human) proposes. Nothing here has been executed.

    Constructed by the planning loop in Phase 6; in Phase 3 the gate is exercised against
    hand-built instances, which is deliberate -- the gate must be verifiable without an agent.
    """

    invoice_id: uuid.UUID
    type: ActionType
    tone_tier: int = Field(ge=1, le=4)
    rationale: str
    proposed_by: ActorType = ActorType.AGENT
    channel: Channel | None = None
    message: DraftMessage | None = None
    scheduled_for: datetime | None = None
    action_id: uuid.UUID | None = None

    # Set only by an explicit human approval, never by the agent. check_value_threshold reads it.
    approved_by: str | None = None

    @property
    def is_outbound(self) -> bool:
        """Whether this action would actually contact someone."""
        return self.type in (ActionType.SEND_MESSAGE, ActionType.SWITCH_CHANNEL)


class CheckResult(BaseModel):
    """One check's verdict. ``reason`` is required on a failure and is what the merchant reads."""

    check: CheckName
    passed: bool
    reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _failure_must_explain(self) -> CheckResult:
        if not self.passed and not self.reason:
            raise ValueError(f"check {self.check} failed without a reason")
        return self


class GateVerdict(BaseModel):
    """The full result of one gate evaluation. Always seven checks, always in order.

    ``passed`` is derived, never supplied: it cannot disagree with the checks it summarises.
    """

    invoice_id: uuid.UUID
    checks: list[CheckResult]
    evaluated_at: datetime
    action_type: ActionType
    action_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _seven_checks_in_order(self) -> GateVerdict:
        """The ADR-005 invariant, enforced at construction.

        "All seven always run, even after one fails" is a claim about the audit record, and a
        claim is worth what its enforcement is worth. Making a short or reordered verdict
        unconstructable means no code path -- including a future one -- can produce a partial
        record by taking an early return.
        """
        actual = tuple(c.check for c in self.checks)
        if actual != GATE_CHECK_ORDER:
            raise ValueError(
                f"a GateVerdict must carry all {GATE_CHECK_COUNT} checks in ADR-005 order; "
                f"got {list(actual)}"
            )
        return self

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    @property
    def blocked_by(self) -> list[str]:
        return [check.check.value for check in self.failures]

    def result_for(self, check: CheckName) -> CheckResult:
        for result in self.checks:
            if result.check is check:
                return result
        raise KeyError(check)  # pragma: no cover - unreachable given the validator

    def as_audit_verdicts(self) -> list[dict[str, Any]]:
        """The ``gate_verdicts`` payload for audit_log, in the api-contracts.md shape."""
        return [
            {
                "check": check.check.value,
                "passed": check.passed,
                **({"reason": check.reason} if check.reason else {}),
                **({"detail": check.detail} if check.detail else {}),
            }
            for check in self.checks
        ]
