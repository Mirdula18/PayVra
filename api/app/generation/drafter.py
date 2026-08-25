"""Message drafting: prompt 2 from prompts/llm-prompts.md, schema-constrained (FR-8.1, FR-8.2).

This is the public entry point for the whole generation layer. The pipeline agents/agent-engine.md
specifies is::

    build context -> LLM via LiteLLM -> parse to schema -> validate content -> cache -> return

with one property that matters more than the rest: **it never returns unvalidated LLM output**.
Every path out of :func:`generate` either returns a message the validator passed, or returns a
template. There is no third outcome, and no configuration that produces one.

The order of the guards is deliberate:

1. **Cache first.** A hit costs nothing and cannot be rate-limited.
2. **Kill switch and breaker next.** Both are checked before a prompt is even built, so
   ``LLM_ENABLED=false`` does no work at all.
3. **Two attempts, then the template.** FR-8.4. Each attempt is validated independently.
4. **Anything unexpected falls back.** A bug in the drafting path must degrade to a template, not
   raise into a dispatch loop and strand the invoice.

Only validated messages are cached, so one bad generation can never be served twice.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.enums import Channel
from app.generation import templates
from app.generation.cache import MESSAGE_CACHE, MessageCache
from app.generation.llm import LLMJob, LLMUnavailable, available, complete
from app.generation.validator import MAX_DRAFT_ATTEMPTS, validate
from app.money import paise_to_exact
from app.schemas.generation import GeneratedMessage, MessageContext

logger = logging.getLogger(__name__)

# Per channel, from prompts/llm-prompts.md `channel_format_note`.
_CHANNEL_NOTE: dict[Channel, str] = {
    Channel.EMAIL: "Use short paragraphs. No markdown.",
    Channel.SMS: "Single paragraph, under 300 characters.",
    Channel.WHATSAPP: "Conversational, 2-3 short lines. No formal letter structure.",
}

_MAX_WORDS: dict[Channel, int] = {Channel.EMAIL: 180, Channel.SMS: 45, Channel.WHATSAPP: 80}

_HINGLISH_INSTRUCTION = (
    "Write in natural Hinglish - conversational Hindi-English code-mixing in Latin script,\n"
    "the way Indian business people actually write on WhatsApp. Not formal Hindi, not\n"
    "translated English. Keep the amount, invoice number, and link in English/numerals."
)

_ENGLISH_INSTRUCTION = (
    "Write in clear, professional Indian business English. Plain and direct; no flowery openings."
)

_SYSTEM = (
    "You draft payment reminder messages for Indian B2B suppliers. You respond with JSON only. "
    "You never invent facts, never threaten legal or credit consequences, and never reference "
    "anything outside the business relationship."
)

_TONE_TIER_BLOCK = """TONE TIER {tone_tier} MEANS:
  1 - Courtesy. Friendly, assumes oversight. No pressure.
  2 - Gentle reminder. Warm but clear about the overdue status.
  3 - Firm. Professional, direct, states the business impact. Not aggressive.
  4 - Formal notice. Businesslike, references terms, states next steps factually."""


def build_prompt(ctx: MessageContext) -> str:
    """Prompt 2, verbatim from prompts/llm-prompts.md, with this invoice's facts substituted.

    The MUST INCLUDE block mirrors ``policy_content.find_missing_elements`` and the MUST NOT block
    mirrors ``policy_content.BANNED_PATTERNS``. That overlap is intentional and one-directional:
    telling the model the rules raises the hit rate, but the validator is what *enforces* them.
    The prompt is an optimisation; it is never the control.
    """
    amount = paise_to_exact(ctx.outstanding_paise)
    opt_out = templates.opt_out_line(ctx.language, ctx.opt_out_url)
    language_instruction = (
        _HINGLISH_INSTRUCTION if ctx.language == "hinglish" else _ENGLISH_INSTRUCTION
    )
    promise_context = ctx.promise_context or "No promise to pay on record."

    opening = (
        f"Write a payment reminder from {ctx.merchant_name} "
        f"to {ctx.counterparty_name} in India."
    )
    return f"""{opening}

CONTEXT
  Invoice: {ctx.invoice_number}
  Amount outstanding: {amount}
  Due date: {ctx.due_date.isoformat()}
  Days overdue: {ctx.days_past_due}
  Payment link: {ctx.payment_link_url}
  Channel: {ctx.channel.value}
  Language: {ctx.language}
  Tone tier: {ctx.tone_tier}
  Prior contact: {ctx.touch_count} previous message(s)
  {promise_context}

{_TONE_TIER_BLOCK.format(tone_tier=ctx.tone_tier)}

{language_instruction}

MUST INCLUDE
  - The exact amount {amount}
  - The invoice number {ctx.invoice_number}
  - The payment link {ctx.payment_link_url}
  - The opt-out line: "{opt_out}"
  - Sender identification as {ctx.merchant_name}

MUST NOT INCLUDE
  - Legal threats, or any claim of legal action not actually being taken
  - Credit rating or blacklist threats
  - References to personal assets, family, or anything outside the business relationship
  - Shaming language, or mention of the debt to any third party
  - ALL CAPS demands
  - "Final notice" unless tone tier is 4
  - Invented facts. Use only what is above.

Keep it under {_MAX_WORDS[ctx.channel]} words. {_CHANNEL_NOTE[ctx.channel]}

Respond with JSON only:
{{
  "subject": "<subject line, or null for sms/whatsapp>",
  "body": "<message text>"
}}"""


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_draft(raw: str, ctx: MessageContext) -> GeneratedMessage:
    """Parse a model response into the schema (FR-8.2). Raises ``ValueError`` on anything else.

    Tolerates the two things models reliably do to JSON -- wrapping it in a ```json fence, and
    prefacing it with a sentence -- by taking the outermost braces. It does not tolerate missing
    or empty bodies: an unparseable draft is a failed attempt, and a failed attempt is what the
    two-strike rule counts.
    """
    match = _JSON_BLOCK.search(raw or "")
    if not match:
        raise ValueError("no JSON object in model response")

    try:
        payload: Any = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"model response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("model response JSON was not an object")

    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("model response carried no body")

    subject = payload.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        subject = None
    # SMS and WhatsApp have no subject line; a model that supplies one anyway is ignored rather
    # than rejected, since the body is the part that gets sent.
    if ctx.channel is not Channel.EMAIL:
        subject = None

    return GeneratedMessage(
        subject=subject,
        body=body.strip(),
        tone_tier=ctx.tone_tier,
        language=ctx.language,
        source="llm",
        origin="",
    )


def generate(
    ctx: MessageContext,
    *,
    cache: MessageCache | None = None,
    max_attempts: int = MAX_DRAFT_ATTEMPTS,
) -> GeneratedMessage:
    """Draft a message for this context. **Always returns a validated message.**

    Falls back to :func:`templates.render` when the LLM is off, rate-limited, broken, or produces
    ``max_attempts`` drafts the validator rejects. The returned message records which happened, in
    ``source`` and ``fallback_reason``, so the audit trail can tell "disabled" from "degraded".
    """
    store = cache if cache is not None else MESSAGE_CACHE

    cached = store.get(ctx)
    if cached is not None:
        return cached

    if not available(LLMJob.DRAFTING):
        reason = "LLM_ENABLED=false" if not _enabled() else "drafting circuit open"
        message = templates.render(ctx, fallback_reason=reason)
        store.put(ctx, message)
        return message

    failures: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = complete(
                build_prompt(ctx),
                job=LLMJob.DRAFTING,
                system=_SYSTEM,
                max_tokens=_MAX_WORDS[ctx.channel] * 6,
                response_format={"type": "json_object"},
            )
        except LLMUnavailable as exc:
            # The breaker or the kill switch. Retrying inside this loop would not help.
            message = templates.render(ctx, fallback_reason=str(exc))
            store.put(ctx, message)
            return message
        except Exception as exc:  # noqa: BLE001 - a drafting bug must not strand the invoice
            logger.exception("unexpected drafting failure invoice=%s", ctx.invoice_number)
            message = templates.render(ctx, fallback_reason=f"{type(exc).__name__}: {exc}")
            store.put(ctx, message)
            return message

        try:
            draft = parse_draft(response.text, ctx)
        except ValueError as exc:
            failures.append(f"attempt {attempt}: {exc}")
            continue

        draft = draft.model_copy(update={"origin": response.model})
        result = validate(draft, ctx)
        if result.valid:
            store.put(ctx, draft)
            return draft
        failures.append(f"attempt {attempt}: {result.summary}")

    # FR-8.4: two failures, deterministic template.
    reason = " | ".join(failures) or "validation failed"
    logger.info(
        "falling back to template after %d rejected draft(s) invoice=%s",
        max_attempts,
        ctx.invoice_number,
    )
    message = templates.render(ctx, fallback_reason=reason)
    store.put(ctx, message)
    return message


def _enabled() -> bool:
    from app.generation.llm import is_enabled

    return is_enabled()


__all__ = ["build_prompt", "generate", "parse_draft"]
