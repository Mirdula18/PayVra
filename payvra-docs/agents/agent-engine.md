# Agent: Agent Engine

**Scope:** `api/app/agent/`, `guardrails/`, `generation/`, `replies/`
**Prerequisites:** `/CLAUDE.md`, `architecture/agent-loop.md`, `decisions/ADR-001`, `ADR-005`

This is the most important module in the product and the one judges will probe hardest.

---

## The invariant, restated

**The LLM proposes. Deterministic code disposes.**

The LLM emits a `ProposedAction` and nothing else. It has no API access, cannot send a message,
and cannot create a payment link. If you find yourself giving an LLM a tool that touches money or
a customer, stop — you are breaking ADR-001.

---

## LangGraph boundary

LangGraph imports appear in **exactly two files**: `agent/graph.py` and `agent/nodes.py`.
Nowhere else. ADR-004 depends on this boundary for a cheap framework swap. Do not leak
`langgraph` or `langchain` types into schemas, routers, or the guardrail engine.

---

## Node implementation

### `observe` — no LLM
Pure DB reads assembling `ObservationContext`. The `BehaviouralSignals` derivations are in
`architecture/agent-loop.md`. Get these right; every downstream decision depends on them.

### `diagnose` — rules first, LLM only for `awaiting_docs`
The signal-to-cause table in `architecture/agent-loop.md` is exhaustive for high-confidence cases.
Only genuinely ambiguous replies reach the LLM.

If you find yourself adding an LLM call to a case the table already covers, you are adding cost,
latency, and non-determinism for nothing.

### `plan` — the LLM call
Prompt in `prompts/llm-prompts.md`. Must include: the closed tool registry, allowed transitions
from the current state, remaining touch budget, and the cause-to-intervention table.

Request JSON output. Parse into `ProposedAction`. On parse failure, retry once with a repair
prompt that includes the validation error. On second failure, go to `fallback`.

### `validate` — no LLM, three checks
1. Is `action` in the registry?
2. Does it imply an allowed transition from the current `recovery_state`?
3. Does the output match the `ProposedAction` schema?

Any failure → log the **raw LLM output** alongside the rejection reason, then `fallback`.
Keep those logs. Being able to show a judge a rejected hallucination is worth more than never
having produced one.

### `fallback` — deterministic policy
The ladder in `architecture/agent-loop.md`. This is also the entire product when every LLM provider
is down.

**Test it explicitly:** set `LLM_ENABLED=false` and confirm a full batch runs end to end.
Put this in CI.

---

## Guardrail gate

Seven checks, fixed order, **all seven always evaluated** even after one fails.

```python
class CheckResult(BaseModel):
    check: str
    passed: bool
    reason: str | None = None

class GateVerdict(BaseModel):
    passed: bool
    checks: list[CheckResult]   # always length 7
```

Rules:
- No LLM call inside the gate, ever
- No "warn and continue" — a failed check halts the action
- The caller writes the full verdict array to `audit_log` regardless of outcome
- `send()` in the delivery layer takes a `GateVerdict` as a required argument. Make it structurally
  impossible to send without one.

**Check 2, `freshness`, is the one that matters most.** Re-read `invoices.payment_status` from the
DB immediately before sending. Hours pass between planning at 01:30 and dispatch. Do not skip it
because "the scheduler just ran."

Check 6, `content_policy`: banned and required elements are listed in `architecture/agent-loop.md`.
On failure, regenerate once, then fall back to a template.

---

## Generation

```python
class Message(BaseModel):
    subject: str | None
    body: str
    tone_tier: Literal[1, 2, 3, 4]
    language: Literal["en", "hinglish"]
    source: Literal["llm", "template"]
```

Pipeline: build context → LLM via LiteLLM → parse to schema → validate content → cache by
content hash → return.

Validation asserts: correct outstanding amount, invoice number present, payment link present,
opt-out present, no banned phrases. Two failures → template.

**Templates are not a stub.** Write four real templates per language per tone tier. They must be
good enough to demo unaided. Write them first, before the LLM path — that way the fallback is
proven by construction rather than hoped for.

---

## Reply handling

Classification and date extraction are separate LLM calls with separate prompts. Do not combine
them; combined prompts degrade both.

Date extraction must handle Hinglish relative dates: "next Tuesday tak", "agle hafte",
"month end tak", "15 tareekh ko". Resolve relative to `received_at` in IST, not UTC — a message
received at 23:30 IST is 18:00 UTC the previous day, and "tomorrow" resolves differently.

Confidence below `REPLY_CONFIDENCE_THRESHOLD` (default 0.7) routes to the human queue.
**Never guess on a dispute.** A misclassified dispute means dunning someone with a legitimate
complaint, which is the worst thing this product can do.

---

## LLM client rules

All calls go through one wrapper in `generation/llm.py`. No direct LiteLLM calls elsewhere.

The wrapper handles: model routing by job type (ADR-003), exponential backoff with jitter,
circuit breaker (2 consecutive failures → template for 5 minutes), token and latency logging,
content-hash caching, and the `LLM_ENABLED` kill switch.

Never call an LLM inside a request-response path. Never call one inside the gate. Never call one
in the ranking path.

---

## Testing priorities

1. `validate` rejects out-of-registry actions and illegal transitions
2. Each of the seven gate checks blocks correctly in isolation
3. All seven verdicts are recorded even when check 1 fails
4. `freshness` blocks a send for an invoice paid between planning and dispatch
5. Content validation catches a missing payment link and a banned phrase
6. `LLM_ENABLED=false` runs a full batch on templates
7. Hinglish date extraction across at least ten real phrasings
8. Stopping rules fire at exactly 3 broken promises and exactly the touch cap
