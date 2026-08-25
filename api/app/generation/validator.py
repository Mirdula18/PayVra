"""Message validation (FR-8.3). **Never return unvalidated LLM output.**

This module owns exactly one question: is this drafted message safe and correct enough to hand to
the gate? It is deliberately the same question gate check 6 asks, answered earlier and cheaply, so
a bad draft is regenerated rather than blocked at send time.

**Every rule here is imported from :mod:`app.guardrails.policy_content`. None is redefined.**
That module is the single definition of what a message may never say and must always contain
(ADR-005). A second phrase list in the generation layer would be a compliance control with two
sources of truth, and the one that drifts is the one that stops matching the gate -- producing
drafts that validate here and are then rejected at the gate, or worse, the reverse. If a phrase
needs adding, it goes in ``policy_content`` and both layers get it.

The relationship to the gate is *belt and braces, not duplication*: this runs at draft time
against the context's amount, the gate re-runs at send time against a fresh database read. A
partial payment landing between the two is caught by the gate, which is exactly the split
CLAUDE.md invariant 4 asks for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.guardrails import policy_content
from app.schemas.generation import GeneratedMessage, MessageContext

logger = logging.getLogger(__name__)

# FR-8.4. Two failed drafts and the deterministic template wins. Not three: each attempt is a
# model call against a free-tier quota, and a model that has produced two invalid drafts for the
# same context is not usually one prompt away from a good one.
MAX_DRAFT_ATTEMPTS = 2


@dataclass(frozen=True)
class ValidationResult:
    """Why a draft passed or failed.

    ``reasons`` is merchant-readable and is what reaches the audit log.
    """

    valid: bool
    reasons: list[str] = field(default_factory=list)
    # Machine-readable, for tests and metrics: banned categories and missing element names.
    banned_categories: list[str] = field(default_factory=list)
    missing_elements: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.valid

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "valid"


def validate(message: GeneratedMessage, ctx: MessageContext) -> ValidationResult:
    """Check one drafted message against the content policy and the invoice's facts.

    Checks the subject *and* body for banned phrases -- a threat in a subject line is a threat --
    but required elements only in the body, since an opt-out URL crammed into a subject line is
    not a working opt-out.
    """
    full_text = f"{message.subject or ''}\n{message.body}"

    violations = policy_content.find_banned(full_text, tone_tier=ctx.tone_tier)
    shouted = policy_content.find_all_caps_demand(full_text)
    if shouted:
        violations.append(shouted)

    missing = policy_content.find_missing_elements(
        message.body,
        outstanding_paise=ctx.outstanding_paise,
        invoice_number=ctx.invoice_number,
        payment_link_url=ctx.payment_link_url,
        opt_out_url=ctx.opt_out_url,
        sender_name=ctx.merchant_name,
    )

    reasons: list[str] = []
    if violations:
        reasons.append(
            "banned content: "
            + "; ".join(f"{v.description} ({v.evidence!r})" for v in violations)
        )
    if missing:
        reasons.append("missing required elements: " + ", ".join(m.value for m in missing))

    # A drafted message must also agree with the tier and language it was asked for. A model that
    # returns a tier-4 formal notice when tier 1 was requested has produced a compliant message
    # that is still wrong for this counterparty.
    if message.tone_tier != ctx.tone_tier:
        reasons.append(f"tone tier {message.tone_tier} does not match requested {ctx.tone_tier}")
    if message.language != ctx.language:
        reasons.append(f"language {message.language!r} does not match requested {ctx.language!r}")
    if not message.body.strip():
        reasons.append("empty body")

    result = ValidationResult(
        valid=not reasons,
        reasons=reasons,
        banned_categories=sorted({v.category for v in violations}),
        missing_elements=[m.value for m in missing],
    )
    if not result.valid:
        # The reasons, never the body: a rejected draft can contain anything the model produced.
        logger.info(
            "draft rejected invoice=%s source=%s reasons=%s",
            ctx.invoice_number,
            message.source,
            result.summary,
        )
    return result


__all__ = ["MAX_DRAFT_ATTEMPTS", "ValidationResult", "validate"]
