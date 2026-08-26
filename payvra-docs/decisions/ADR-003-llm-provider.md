# ADR-003 — LiteLLM over Groq + Gemini free tiers, no GPU

**Status:** Accepted
**Date:** 2026-08-23

## Context

The team has no budget for paid LLM inference. The system makes four kinds of model call:
reply classification, date extraction, action proposal, and message drafting. A 100-invoice batch
at three touches each is roughly 300 generation calls plus ~60 classification calls.

## Decision

Use **LiteLLM** as an abstraction layer over multiple free-tier providers:

| Job | Provider | Model | Why |
|---|---|---|---|
| Reply classification, date extraction, action proposal | Groq | `openai/gpt-oss-20b` | Fast, published limits (30 RPM, 1,000 RPD, 12K TPM, 100K TPD), good structured output |
| Message drafting | Google Gemini (AI Studio) | `gemini-3.6-flash` | Best free-tier Indic and code-mixed language handling; no credit card |
| Fallback | OpenRouter | `:free` variants | 20 RPM, 50 RPD until $10 credits purchased |

**The provider split is the decision; the model ids are not.** Both originals
(`llama-3.3-70b-versatile`, `gemini-2.0-flash`) were retired by their providers and every call
404'd — which the template fallback swallowed, so the system looked healthy while never once
reaching a model. Model ids are expected to churn: `scripts/verify_llm` exists to catch it, and
the table above records what was last verified working rather than what was chosen at design time.

Two consequences of that episode are now load-bearing, both in `generation/llm.py`:

* **Each job falls back across providers**, not just to OpenRouter. One provider having a bad
  afternoon should not cost us a job when another configured key would serve it.
* **Thinking is disabled** (`reasoning_effort="none"`). Current flagship models spend the token
  budget on internal reasoning before emitting anything; `gemini-3.6-flash` at `max_tokens=1080`
  returned `finish_reason='length'` with `completion_tokens=1076` and an empty body. Every job
  here is short structured output, not a reasoning problem.

**No GPU anywhere in the stack.** Every model call is a hosted inference API call over HTTPS.
The backend runs in 1 vCPU / 512 MB.

## Rationale

Splitting by job matters. Classification needs speed and structured output; Groq wins. Drafting
needs Hinglish fluency; Gemini wins. LiteLLM makes the split a one-line model string change and
gives automatic fallback when a provider rate-limits.

Rate limits are enforced at the organisation level on Groq, so creating extra API keys does not
multiply quota, and doing so would violate terms. Plan around the real limit instead.

Google no longer publishes free-tier rate limits publicly — they are visible only in AI Studio
once signed in. Read your project's actual numbers there; do not plan against a blog post.

## Alternatives considered

**Anthropic Claude API direct.** Best quality, matches Razorpay's own Agent Studio stack.
Rejected purely on cost.

**Self-hosted Llama on a rented GPU.** Rejected: cost, ops burden, and it adds a failure mode to
a live demo for zero judging benefit.

**Ollama locally.** Rejected: laptop-dependent, cannot deploy, slow on CPU, and the demo machine
becomes a single point of failure.

## Consequences

**Good:**
- Zero inference cost; full demo run well under $1
- Provider swap is a config change
- "Runs on a ₹500/month container with zero GPU spend" is a credible engineering-maturity claim
- Automatic failover across three providers

**Bad:**
- Free tiers can tighten limits or degrade without warning
- Some providers may use prompts for training — check terms; prefer providers with no-training
  policies, and minimise PII in prompts regardless
- Quality below frontier models; mitigated by narrow, well-specified prompts

**Critical operational note:** at 30 RPM, a 300-call batch takes 10+ minutes. Never run a full
batch live on stage. Pre-compute batches and make only 3–5 calls live. See `prompts/demo-script.md`.

**Required mitigations:**
- Exponential backoff with jitter, max 3 retries
- Circuit breaker: 2 consecutive failures for a call type → deterministic template for 5 minutes
- Content-hash cache; never regenerate identical messages
- `LLM_ENABLED=false` must run the full pipeline on templates alone
