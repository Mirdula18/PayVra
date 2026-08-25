"""Builders for the generation tests.

**No test in this suite may make a real LLM call.** The fakes here are the only thing
``generation.llm.complete`` is ever replaced with, so a test that reaches a provider is a test
that forgot to patch -- and ``test_no_test_makes_a_real_llm_call`` asserts litellm is never even
imported, which catches that case regardless of how it happens.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Any

from app.enums import Channel
from app.generation.llm import LLMResponse
from app.schemas.generation import MessageContext

MERCHANT = "Sharma Industries"
COUNTERPARTY = "Krishna Textiles"
INVOICE_NUMBER = "INV-2026-1042"
OUTSTANDING_PAISE = 1_24_500_00  # ₹1,24,500
LINK = "https://rzp.io/i/aB3xY9k"
OPT_OUT = "https://payvra.test/o/7f3a9c2e"


def make_context(**overrides: Any) -> MessageContext:
    """A complete, valid drafting context. Overrides bend one thing at a time."""
    base = MessageContext(
        merchant_name=MERCHANT,
        counterparty_name=COUNTERPARTY,
        invoice_number=INVOICE_NUMBER,
        outstanding_paise=OUTSTANDING_PAISE,
        due_date=date(2026, 6, 15),
        days_past_due=71,
        payment_link_url=LINK,
        opt_out_url=OPT_OUT,
        channel=Channel.EMAIL,
        language="en",
        tone_tier=2,
        touch_count=1,
    )
    return replace(base, **overrides) if overrides else base


def good_body(ctx: MessageContext) -> str:
    """A body carrying every required element, so a test can remove exactly one."""
    from app.generation.templates import opt_out_line
    from app.money import paise_to_exact

    return (
        f"Hello {ctx.counterparty_name}, invoice {ctx.invoice_number} for "
        f"{paise_to_exact(ctx.outstanding_paise)} is overdue.\n"
        f"Pay here: {ctx.payment_link_url}\n"
        f"{opt_out_line(ctx.language, ctx.opt_out_url)}\n"
        f"— {ctx.merchant_name}"
    )


def raw_response(text: str) -> LLMResponse:
    """A model response with arbitrary text, for testing the parser's failure paths."""
    return LLMResponse(
        text=text,
        model="fake/test-model",
        prompt_tokens=120,
        completion_tokens=80,
        latency_ms=42.0,
        cost_usd=0.0,
    )


def llm_response(body: str, subject: str | None = "Invoice reminder") -> LLMResponse:
    """What a well-behaved model returns: JSON only."""
    payload = json.dumps({"subject": subject, "body": body})
    return LLMResponse(
        text=payload,
        model="fake/test-model",
        prompt_tokens=120,
        completion_tokens=80,
        latency_ms=42.0,
        cost_usd=0.0,
    )


class FakeLLM:
    """Records calls and replays scripted responses. Never touches a network.

    ``responses`` may hold :class:`LLMResponse` objects to return or exceptions to raise; the
    last entry repeats once exhausted, so a test does not have to count attempts exactly.
    """

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.calls.append({"prompt": prompt, **kwargs})
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    @property
    def call_count(self) -> int:
        return len(self.calls)
