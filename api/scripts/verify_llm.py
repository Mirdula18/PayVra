"""Verify the drafting path against a REAL model. The Phase 5 counterpart to verify_razorpay.

Every LLM test in the suite monkeypatches ``drafter.complete``, which proves the plumbing around
the model and nothing about the model itself. A stub always returns well-formed JSON in our exact
schema; a real model returns prose around the JSON, invents a tone tier, writes ``Rs. 23,134``
instead of ``₹23,134``, or quietly drops the opt-out line. Those are the failures that matter, and
only a real call surfaces them -- the same argument that made ``verify_razorpay`` worth writing,
and it found a genuine bug within a minute of first running.

What this asserts:

1. **Templates alone work with the LLM off** -- CLAUDE.md invariant 9, and the floor everything
   else stands on. If this fails, nothing below matters.
2. **A real model returns JSON we can parse** into ``GeneratedMessage`` (FR-8.2).
3. **A real draft passes the validator** -- correct amount, invoice number, payment link, opt-out,
   no banned phrases, and the tier and language actually asked for (FR-8.3).
4. **Hinglish is produced when asked for** (FR-8.5).
5. **A failing model falls back to a template rather than raising** (FR-8.4) -- the invariant that
   keeps a demo alive through an outage.
6. **Generation refuses to run inside a request-response path**, which is enforced in code rather
   than left to reviewer memory.

Run:  python -m scripts.verify_llm            (from api/, venv active)
      python -m scripts.verify_llm --show     (print the drafted messages in full)

Needs one free provider key in .env -- GROQ_API_KEY or GEMINI_API_KEY (ADR-003) -- and
LLM_ENABLED=true. It makes a handful of model calls and writes nothing to the database.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.enums import Channel
from app.generation import drafter, llm, templates
from app.generation.cache import MessageCache
from app.generation.llm import LLMJob, LLMUnavailable
from app.generation.validator import validate
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.money import paise_to_exact
from app.schemas.generation import GeneratedMessage, MessageContext

# Language and ToneTier are Literal aliases, not enums -- these name the values used here.
ENGLISH = "en"
HINGLISH = "hinglish"
TIER_FIRM_REMINDER = 2

DIVIDER = "=" * 78


def _force_utf8_stdout() -> None:
    """A cp1252 console cannot encode U+20B9, and every amount here is in rupees."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


class Checks:
    """Collects results so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.results: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.results.append((ok, label, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if detail:
            print(f"         {detail}")
        return ok

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [r for r in self.results if not r[0]]


def sample_context(db: Session) -> MessageContext:
    """A context built from real seeded data, falling back to a synthetic one.

    Real data is preferred because it is what the model will actually be handed -- an Indian
    company name, a lakh-scale amount, a genuine invoice number. But ``build_context`` requires a
    live payment link, and requiring one here would make a drafting probe depend on Razorpay
    credentials and test-mode budget. When no link exists, a synthetic context with the same shape
    is used instead: this script is testing the model, not the database.
    """
    from app.generation.context import ContextIncomplete, build_context

    merchant = db.execute(
        select(Merchant)
        .join(Invoice, Invoice.merchant_id == Merchant.id)
        .group_by(Merchant.id)
        .order_by(func.count(Invoice.id).desc())
        .limit(1)
    ).scalar_one_or_none()

    if merchant is not None:
        invoice = db.execute(
            select(Invoice)
            .where(Invoice.merchant_id == merchant.id, Invoice.outstanding_paise > 0)
            .order_by(Invoice.priority_score.desc().nullslast())
            .limit(1)
        ).scalar_one_or_none()
        if invoice is not None:
            try:
                ctx = build_context(
                    db, invoice, channel=Channel.EMAIL, tone_tier=TIER_FIRM_REMINDER
                )
                print(f"  context from live data: {merchant.name} / {invoice.invoice_number}")
                return ctx
            except ContextIncomplete as exc:
                print(f"  no live payment link ({exc.__class__.__name__}), using a synthetic one")

    print("  context: synthetic")
    return MessageContext(
        merchant_name="Nandi Industrial Supplies Pvt Ltd",
        counterparty_name="Krishna Textiles Pvt Ltd",
        invoice_number="INV-2026-1020",
        outstanding_paise=23_13_400,
        due_date=date.today() - timedelta(days=34),
        days_past_due=34,
        payment_link_url="https://rzp.io/rzp/XujoMSm",
        opt_out_url="https://payvra.test/opt-out/tok123",
        channel=Channel.EMAIL,
        language=ENGLISH,
        tone_tier=TIER_FIRM_REMINDER,
        touch_count=2,
        invoice_id=uuid.uuid4(),
    )


def show(message: GeneratedMessage, *, enabled: bool) -> None:
    if not enabled:
        return
    print()
    print("  ---- drafted message " + "-" * 55)
    if message.subject:
        print(f"  SUBJECT: {message.subject}")
    for line in message.body.splitlines():
        print(f"  {line}")
    print("  " + "-" * 76)


def preflight(checks: Checks) -> bool:
    print(DIVIDER)
    print("PREFLIGHT -- provider credentials")
    print(DIVIDER)

    providers = {
        "GROQ_API_KEY": settings.groq_api_key,
        "GEMINI_API_KEY": settings.gemini_api_key,
        "OPENROUTER_API_KEY": settings.openrouter_api_key,
    }
    live = {name: v for name, v in providers.items() if v and not v.startswith("dummy")}
    for name, value in providers.items():
        state = "set" if name in live else ("placeholder" if value else "empty")
        print(f"  {name:<20} {state}")

    ok = checks.check(
        bool(live),
        "at least one provider key is configured",
        "get a free key from console.groq.com or aistudio.google.com, then set it in .env",
    )
    ok &= checks.check(
        settings.llm_enabled,
        "LLM_ENABLED=true",
        "the template path is proven either way; the model path needs this on",
    )
    if ok:
        routes = ", ".join(llm.MODEL_ROUTES[LLMJob.DRAFTING])
        print(f"\n  drafting route order: {routes}")
    return ok


def verify_templates(ctx: MessageContext, checks: Checks, *, show_body: bool) -> None:
    """Invariant 9: the whole pipeline must run on templates alone."""
    print()
    print(DIVIDER)
    print("1 -- templates alone, with the model switched off")
    print(DIVIDER)

    message = templates.render(ctx, fallback_reason="verify_llm: forced")
    result = validate(message, ctx)

    checks.check(message.source == "template", "a template message is produced", message.origin)
    checks.check(
        result.valid,
        "the template passes the same validator a draft must pass",
        result.summary or "no violations",
    )
    show(message, enabled=show_body)


def verify_live_draft(ctx: MessageContext, checks: Checks, *, show_body: bool) -> None:
    """The thing no stub can prove: a real model, parsed and validated."""
    print()
    print(DIVIDER)
    print("2 + 3 -- a REAL model drafts, parses and validates")
    print(DIVIDER)

    # Forced to English so this and check 4 exercise different paths. A seeded counterparty may
    # resolve to Hinglish, in which case both would otherwise test the same one.
    ctx = replace(ctx, language=ENGLISH)

    # A fresh cache, or a previous run's message would be returned without calling the model.
    with llm.worker_path():
        message = drafter.generate(ctx, cache=MessageCache())

    if message.source != "llm":
        checks.check(
            False,
            "the model produced the message (not a fallback)",
            f"fell back to a template: {message.fallback_reason}",
        )
        show(message, enabled=show_body)
        return

    checks.check(True, "the model produced the message", f"model={message.origin}")
    checks.check(
        bool(message.body.strip()), "the draft has a body", f"{len(message.body)} chars"
    )

    result = validate(message, ctx)
    checks.check(
        result.valid,
        "the real draft passes the validator (FR-8.3)",
        result.summary or "amount, invoice number, link, opt-out all present; no banned phrases",
    )
    checks.check(
        message.tone_tier == ctx.tone_tier,
        "the model honoured the requested tone tier",
        f"asked {ctx.tone_tier}, got {message.tone_tier}",
    )
    checks.check(
        paise_to_exact(ctx.outstanding_paise) in message.body,
        "the exact rupee amount appears, unabbreviated",
        f"looking for {paise_to_exact(ctx.outstanding_paise)}",
    )
    checks.check(
        ctx.payment_link_url in message.body,
        "the payment link is in the body",
        "a dunning message without a way to pay is the one thing this product cannot ship",
    )
    show(message, enabled=show_body)


def verify_hinglish(ctx: MessageContext, checks: Checks, *, show_body: bool) -> None:
    """FR-8.5. Worth its own check: the model is likeliest to drift on the non-English path."""
    print()
    print(DIVIDER)
    print("4 -- Hinglish (FR-8.5)")
    print(DIVIDER)

    hinglish = replace(ctx, language=HINGLISH)
    with llm.worker_path():
        message = drafter.generate(hinglish, cache=MessageCache())

    if message.source != "llm":
        checks.check(
            False, "a Hinglish draft came from the model", f"fell back: {message.fallback_reason}"
        )
        show(message, enabled=show_body)
        return

    checks.check(True, "a Hinglish draft came from the model", f"model={message.origin}")
    result = validate(message, hinglish)
    checks.check(
        result.valid,
        "the Hinglish draft passes the validator",
        result.summary or "no violations",
    )
    show(message, enabled=show_body)


def verify_fallback(ctx: MessageContext, checks: Checks) -> None:
    """FR-8.4 and invariant 9: a broken model degrades, it does not raise."""
    print()
    print(DIVIDER)
    print("5 -- a failing model falls back instead of raising (FR-8.4)")
    print(DIVIDER)

    def explode(*args: object, **kwargs: object) -> object:
        raise LLMUnavailable("verify_llm: simulated provider outage")

    original = drafter.complete
    drafter.complete = explode  # type: ignore[assignment]
    try:
        with llm.worker_path():
            message = drafter.generate(ctx, cache=MessageCache())
    except Exception as exc:  # noqa: BLE001 - the whole point is that this must not happen
        checks.check(False, "a provider outage does not raise", f"{type(exc).__name__}: {exc}")
        return
    finally:
        drafter.complete = original  # type: ignore[assignment]

    checks.check(True, "a provider outage does not raise")
    checks.check(
        message.source == "template",
        "the outage degraded to a template",
        f"reason recorded: {message.fallback_reason}",
    )
    checks.check(
        validate(message, ctx).valid, "the fallback message is itself valid and sendable"
    )


def verify_request_path_guard(ctx: MessageContext, checks: Checks) -> None:
    """A multi-second model call inside a webhook handler is a Razorpay retry storm."""
    print()
    print(DIVIDER)
    print("6 -- generation refuses to run in a request-response path")
    print(DIVIDER)

    with llm.request_path():
        message = drafter.generate(ctx, cache=MessageCache())

    checks.check(
        message.source == "template",
        "an in-request generate falls back rather than calling the model",
        f"reason: {message.fallback_reason}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify drafting against a real model.")
    parser.add_argument(
        "--show", action="store_true", help="print each drafted message in full"
    )
    args = parser.parse_args(argv)
    _force_utf8_stdout()

    checks = Checks()
    db = SessionLocal()
    try:
        print(DIVIDER)
        print("CONTEXT")
        print(DIVIDER)
        ctx = sample_context(db)
        print(
            f"  {ctx.counterparty_name} owes {paise_to_exact(ctx.outstanding_paise)} "
            f"on {ctx.invoice_number}, {ctx.days_past_due} days past due"
        )

        credentials_ok = preflight(checks)
        verify_templates(ctx, checks, show_body=args.show)

        if credentials_ok:
            verify_live_draft(ctx, checks, show_body=args.show)
            verify_hinglish(ctx, checks, show_body=args.show)
        else:
            print()
            print("  Skipping the live-model checks: no usable provider key, or LLM_ENABLED=false.")
            print("  The template path above is what CI proves; this is the half it cannot.")

        verify_fallback(ctx, checks)
        verify_request_path_guard(ctx, checks)
    finally:
        db.close()

    print()
    print(DIVIDER)
    if checks.failed:
        print(f"RESULT: {len(checks.failed)} CHECK(S) FAILED")
        for _, label, detail in checks.failed:
            print(f"  - {label}: {detail}")
        print(DIVIDER)
        return 1
    scope = "live model" if credentials_ok else "templates only -- model path not exercised"
    print(f"RESULT: all {len(checks.results)} checks passed ({scope})")
    print(DIVIDER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
