"""Phase 5: message generation, template fallback, validation, breaker, cache.

Three properties matter more than the rest, and each has a test whose failure is unambiguous:

* **``LLM_ENABLED=false`` runs the whole pipeline on templates.** CLAUDE.md invariant 9 -- the
  demo must survive an LLM outage.
* **Unvalidated LLM output is never returned.** Every exit from ``generate`` is validated or is a
  template.
* **No test here makes a real LLM call.** Asserted structurally, not by convention.

None of these needs a database. That is deliberate: the generation layer takes a plain context,
so its tests run in milliseconds and in CI with no services at all.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from app.enums import Channel
from app.generation import drafter, llm, templates
from app.generation.cache import MessageCache, context_key
from app.generation.llm import BREAKERS, LLMJob, LLMUnavailable
from app.generation.validator import validate
from app.guardrails.policy_content import BannedCategory
from app.money import paise_to_exact
from app.schemas.generation import LANGUAGES, TONE_TIERS
from tests.generation_support import (
    FakeLLM,
    good_body,
    llm_response,
    make_context,
    raw_response,
)


@pytest.fixture(autouse=True)
def _isolate_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the LLM off, a clean breaker, and an empty cache.

    Off by default is the important half: a test that wants the LLM path must say so, so a
    forgotten patch surfaces as a template result rather than as a live call.
    """
    monkeypatch.setattr(llm.settings, "llm_enabled", False, raising=False)
    llm.reset()
    from app.generation.cache import MESSAGE_CACHE

    MESSAGE_CACHE.clear()


# =================================================================================================
# templates — built first, and proven first
# =================================================================================================


def test_the_template_grid_is_complete() -> None:
    """4 tiers x 2 languages x 3 channels. A missing cell is an unsendable message."""
    assert len(templates.grid()) == 24
    for tier, language, channel in templates.grid():
        assert templates.get(tier, language, channel) is not None


@pytest.mark.parametrize("tier", TONE_TIERS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_llm_disabled_produces_a_valid_message_for_every_tier_and_language(
    tier: int, language: str
) -> None:
    """The eight tier/language combinations, with the LLM off. The fallback, proven.

    This is the test CLAUDE.md invariant 9 rests on: with no model, no key and no network, every
    combination still produces a message that passes the same content policy the gate applies.
    """
    for channel in Channel:
        ctx = make_context(tone_tier=tier, language=language, channel=channel)
        message = drafter.generate(ctx)

        assert message.source == "template", "LLM is off; nothing may claim an llm source"
        assert message.fallback_reason == "LLM_ENABLED=false"
        result = validate(message, ctx)
        assert result.valid, f"t{tier}/{language}/{channel.value}: {result.summary}"


def test_templates_state_the_exact_amount_not_an_abbreviation() -> None:
    """``₹4.2L`` cannot be reconciled against a ledger and fails the required-amount check."""
    ctx = make_context(outstanding_paise=42_00_000_00)  # ₹42,00,000
    message = templates.render(ctx)
    assert "₹42,00,000" in message.body
    assert "L" not in message.body.split("₹42,00,000")[0][-3:]
    assert validate(message, ctx).valid


def test_email_templates_carry_a_subject_and_others_do_not() -> None:
    assert templates.render(make_context(channel=Channel.EMAIL)).subject
    assert templates.render(make_context(channel=Channel.SMS)).subject is None
    assert templates.render(make_context(channel=Channel.WHATSAPP)).subject is None


def test_only_tier_4_templates_claim_finality() -> None:
    """"Final notice" below tier 4 is fake urgency, which policy_content tier-gates."""
    for tier in (1, 2, 3):
        body = templates.render(make_context(tone_tier=tier)).body.lower()
        assert "final notice" not in body
    assert "final notice" in templates.render(make_context(tone_tier=4)).body.lower()


def test_promise_context_is_rendered_when_present() -> None:
    note = "Promised to pay by 12 Aug, not received."
    message = templates.render(make_context(promise_context=note))
    assert note in message.body


# =================================================================================================
# validator — reuses policy_content, never redefines it
# =================================================================================================


def test_the_validator_uses_the_gate_s_own_phrase_list() -> None:
    """One definition, not two. A second phrase list is a control with two sources of truth."""
    import pathlib

    path = pathlib.Path(__file__).parents[1] / "app" / "generation" / "validator.py"
    source = path.read_text(encoding="utf-8")
    assert "from app.guardrails import policy_content" in source
    assert "BANNED_PATTERNS" not in source.replace("``policy_content.BANNED_PATTERNS``", "")


def test_validator_catches_a_missing_payment_link() -> None:
    ctx = make_context()
    body = good_body(ctx).replace(ctx.payment_link_url, "")
    result = validate(llm_message(body, ctx), ctx)
    assert not result.valid
    assert "payment_link" in result.missing_elements


def test_validator_catches_a_missing_opt_out() -> None:
    ctx = make_context()
    body = good_body(ctx).replace(ctx.opt_out_url, "")
    result = validate(llm_message(body, ctx), ctx)
    assert not result.valid
    assert "opt_out" in result.missing_elements


def test_validator_catches_a_wrong_amount() -> None:
    """The amount is checked against the invoice, not against what the message claims."""
    ctx = make_context()
    body = good_body(ctx).replace(paise_to_exact(ctx.outstanding_paise), "₹99,999")
    result = validate(llm_message(body, ctx), ctx)
    assert not result.valid
    assert "amount" in result.missing_elements


def test_validator_catches_a_missing_invoice_number() -> None:
    ctx = make_context()
    body = good_body(ctx).replace(ctx.invoice_number, "some other reference")
    result = validate(llm_message(body, ctx), ctx)
    assert not result.valid
    assert "invoice_number" in result.missing_elements


def test_validator_catches_a_missing_sender() -> None:
    ctx = make_context()
    body = good_body(ctx).replace(ctx.merchant_name, "")
    result = validate(llm_message(body, ctx), ctx)
    assert not result.valid
    assert "sender_identification" in result.missing_elements


# One phrase per banned category. `fake_urgency` is checked at a tier where it is not permitted.
_BANNED_SAMPLES: list[tuple[BannedCategory, str, int]] = [
    (BannedCategory.LEGAL_THREAT, "We will begin legal action against you.", 2),
    (BannedCategory.CREDIT_THREAT, "This will affect your credit rating.", 2),
    (BannedCategory.PERSONAL_ASSETS, "We will pursue your personal assets.", 2),
    (BannedCategory.THIRD_PARTY_DISCLOSURE, "We will inform your customers.", 2),
    (BannedCategory.SHAMING, "This is shameful conduct from your side.", 2),
    (BannedCategory.FAKE_URGENCY, "This is your final notice.", 2),
    (BannedCategory.ALL_CAPS_DEMAND, "PAY THIS INVOICE IMMEDIATELY NOW", 2),
]


@pytest.mark.parametrize(("category", "phrase", "tier"), _BANNED_SAMPLES)
def test_validator_catches_each_banned_category(
    category: BannedCategory, phrase: str, tier: int
) -> None:
    ctx = make_context(tone_tier=tier)
    result = validate(llm_message(f"{good_body(ctx)}\n{phrase}", ctx), ctx)
    assert not result.valid, f"{category.value} was not caught"
    assert category.value in result.banned_categories


def test_final_notice_is_permitted_at_tier_4() -> None:
    """Tier-gated, not banned outright: at tier 4 the finality is real."""
    ctx = make_context(tone_tier=4)
    result = validate(llm_message(f"{good_body(ctx)}\nThis is your final notice.", ctx), ctx)
    assert result.valid, result.summary


def test_validator_rejects_a_draft_in_the_wrong_tier_or_language() -> None:
    ctx = make_context(tone_tier=2, language="en")
    message = llm_message(good_body(ctx), ctx).model_copy(update={"tone_tier": 4})
    assert not validate(message, ctx).valid


# =================================================================================================
# drafter — the LLM path, always with a fake
# =================================================================================================


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.settings, "llm_enabled", True, raising=False)


def test_a_valid_llm_draft_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    ctx = make_context()
    fake = FakeLLM(llm_response(good_body(ctx)))
    monkeypatch.setattr(drafter, "complete", fake)

    message = drafter.generate(ctx, cache=MessageCache())

    assert message.source == "llm"
    assert fake.call_count == 1
    assert validate(message, ctx).valid


def test_two_validation_failures_fall_through_to_the_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-8.4. Two strikes, then determinism -- and exactly two model calls, not more."""
    _enable(monkeypatch)
    ctx = make_context()
    # Both drafts threaten legal action, so both fail the content policy.
    bad = llm_response(f"{good_body(ctx)}\nWe will start legal action.")
    fake = FakeLLM(bad, bad)
    monkeypatch.setattr(drafter, "complete", fake)

    message = drafter.generate(ctx, cache=MessageCache())

    assert message.source == "template"
    assert fake.call_count == 2, "a third attempt would burn free-tier quota for nothing"
    assert "legal action" in (message.fallback_reason or "")
    assert validate(message, ctx).valid, "the fallback itself must be valid"


def test_a_first_bad_draft_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    ctx = make_context()
    fake = FakeLLM(
        llm_response(f"{good_body(ctx)}\nWe will start legal action."),
        llm_response(good_body(ctx)),
    )
    monkeypatch.setattr(drafter, "complete", fake)

    message = drafter.generate(ctx, cache=MessageCache())

    assert message.source == "llm"
    assert fake.call_count == 2


def test_unparseable_json_counts_as_a_failed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    ctx = make_context()
    monkeypatch.setattr(
        drafter,
        "complete",
        FakeLLM(raw_response("not json at all"), raw_response('{"subject": "s"}')),
    )
    message = drafter.generate(ctx, cache=MessageCache())
    assert message.source == "template", "unparseable and body-less drafts both count as failures"


def test_a_json_fenced_response_still_parses() -> None:
    """Models wrap JSON in fences and prefaces; that is not a validation failure."""
    ctx = make_context()
    raw = f'Here you go:\n```json\n{{"subject": "S", "body": {_json(good_body(ctx))}}}\n```'
    draft = drafter.parse_draft(raw, ctx)
    assert draft.body.startswith("Hello")


def test_an_llm_exception_falls_back_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drafting bug must degrade to a template, not strand the invoice in a dispatch loop."""
    _enable(monkeypatch)
    ctx = make_context()
    monkeypatch.setattr(drafter, "complete", FakeLLM(RuntimeError("provider exploded")))
    message = drafter.generate(ctx, cache=MessageCache())
    assert message.source == "template"
    assert "provider exploded" in (message.fallback_reason or "")


def test_the_prompt_carries_the_facts_and_the_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_context(language="hinglish", tone_tier=3)
    prompt = drafter.build_prompt(ctx)
    assert ctx.invoice_number in prompt
    assert paise_to_exact(ctx.outstanding_paise) in prompt
    assert ctx.payment_link_url in prompt
    assert ctx.opt_out_url in prompt
    assert "Hinglish" in prompt
    assert "MUST NOT INCLUDE" in prompt


# =================================================================================================
# circuit breaker
# =================================================================================================


def test_the_breaker_opens_after_two_consecutive_failures() -> None:
    breaker = BREAKERS.for_job(LLMJob.DRAFTING)
    assert not breaker.is_open

    breaker.record_failure()
    assert not breaker.is_open, "one failure is a blip, not an outage"

    breaker.record_failure()
    assert breaker.is_open, "two consecutive failures must open the breaker (ADR-003)"


def test_the_breaker_closes_after_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = BREAKERS.for_job(LLMJob.DRAFTING)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open

    # Advance the clock past the reset window rather than sleeping through it.
    opened_at = breaker.opened_at
    assert opened_at is not None
    monkeypatch.setattr(
        llm.time, "monotonic", lambda: opened_at + llm.CIRCUIT_RESET_SECONDS + 1
    )
    assert not breaker.is_open, "the breaker must half-open after 5 minutes"


def test_a_success_resets_the_failure_count() -> None:
    breaker = BREAKERS.for_job(LLMJob.DRAFTING)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert not breaker.is_open, "failures must be consecutive to count"


def test_breakers_are_per_job_type() -> None:
    """Groq rate-limiting classification says nothing about Gemini's ability to draft."""
    drafting = BREAKERS.for_job(LLMJob.DRAFTING)
    drafting.record_failure()
    drafting.record_failure()
    assert drafting.is_open
    assert not BREAKERS.for_job(LLMJob.CLASSIFICATION).is_open


def test_an_open_breaker_makes_the_drafter_use_a_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    breaker = BREAKERS.for_job(LLMJob.DRAFTING)
    breaker.record_failure()
    breaker.record_failure()

    fake = FakeLLM(llm_response("should never be reached"))
    monkeypatch.setattr(drafter, "complete", fake)

    message = drafter.generate(make_context(), cache=MessageCache())

    assert message.source == "template"
    assert fake.call_count == 0, "an open breaker must not call out at all"
    assert "circuit open" in (message.fallback_reason or "")


def test_complete_refuses_when_the_kill_switch_is_off() -> None:
    with pytest.raises(LLMUnavailable, match="LLM_ENABLED=false"):
        llm.complete("hi", job=LLMJob.DRAFTING)


def test_complete_refuses_inside_a_request_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Never call an LLM inside a request-response path" — enforced, not documented."""
    _enable(monkeypatch)
    with llm.request_path(), pytest.raises(llm.LLMInRequestPath):
        llm.complete("hi", job=LLMJob.DRAFTING)


def test_worker_path_clears_the_request_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    with llm.request_path():
        assert llm.in_request_path()
        with llm.worker_path():
            assert not llm.in_request_path()


# =================================================================================================
# cache
# =================================================================================================


def test_the_cache_returns_identical_content_without_a_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    ctx = make_context()
    fake = FakeLLM(llm_response(good_body(ctx)))
    monkeypatch.setattr(drafter, "complete", fake)
    cache = MessageCache()

    first = drafter.generate(ctx, cache=cache)
    second = drafter.generate(ctx, cache=cache)

    assert first.body == second.body
    assert first.subject == second.subject
    assert fake.call_count == 1, "a cache hit must not reach the model"
    assert cache.stats["hits"] == 1


def test_a_changed_amount_is_a_different_cache_key() -> None:
    """Anything that changes the message must change the key, or a stale amount goes out."""
    base = make_context()
    assert context_key(base) != context_key(make_context(outstanding_paise=999_00))
    assert context_key(base) != context_key(make_context(tone_tier=4))
    assert context_key(base) != context_key(make_context(language="hinglish"))
    assert context_key(base) != context_key(make_context(channel=Channel.SMS))
    assert context_key(base) != context_key(make_context(payment_link_url="https://x.test/y"))


def test_the_cache_is_bounded() -> None:
    cache = MessageCache(max_entries=3)
    for i in range(10):
        ctx = make_context(invoice_number=f"INV-{i}")
        cache.put(ctx, templates.render(ctx))
    assert len(cache) == 3


def test_a_cached_message_cannot_be_mutated_by_a_caller() -> None:
    cache = MessageCache()
    ctx = make_context()
    cache.put(ctx, templates.render(ctx))

    first = cache.get(ctx)
    assert first is not None
    first.body = "tampered"

    second = cache.get(ctx)
    assert second is not None
    assert second.body != "tampered"


# =================================================================================================
# the structural guarantee
# =================================================================================================


def test_no_test_makes_a_real_llm_call() -> None:
    """litellm must never be imported during the suite.

    Stronger than "we remembered to patch": if any test reached ``llm.complete`` with the switch
    on and no fake, the lazy import inside it would put litellm in ``sys.modules``. This asserts
    it is not there, so a live call cannot pass unnoticed.
    """
    assert "litellm" not in sys.modules, "a test reached the real LLM path"


def test_llm_is_the_only_module_importing_litellm() -> None:
    """agents/agent-engine.md: all calls go through one wrapper."""
    import ast
    import pathlib

    app_dir = pathlib.Path(__file__).parents[1] / "app"
    offenders: list[str] = []

    for path in app_dir.rglob("*.py"):
        if path.name == "llm.py":
            continue
        # Parsed, not grepped: a docstring that *names* litellm is documentation, while
        # `import litellm` is the thing that must not exist. A substring search cannot tell
        # them apart and fails on its own module docstring.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            if "litellm" in roots:
                offenders.append(path.relative_to(app_dir).as_posix())

    assert not offenders, f"litellm imported outside generation/llm.py: {offenders}"


# --- helpers -------------------------------------------------------------------------------------


def llm_message(body: str, ctx: Any) -> Any:
    """A GeneratedMessage marked as LLM output, for validator tests."""
    from app.schemas.generation import GeneratedMessage

    return GeneratedMessage(
        subject="Invoice reminder",
        body=body,
        tone_tier=ctx.tone_tier,
        language=ctx.language,
        source="llm",
        origin="fake/test-model",
    )


def _json(value: str) -> str:
    import json

    return json.dumps(value)
