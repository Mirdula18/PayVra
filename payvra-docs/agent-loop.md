# The Agent Loop

This is the heart of PAYVRA. Read this before touching anything in `agent/` or `guardrails/`.

---

## Core principle

**The LLM proposes. The policy engine disposes. Only then does a tool run.**

The LLM's entire job is to produce a small structured object:

```python
class ProposedAction(BaseModel):
    action: ActionType           # must be in the closed registry
    channel: Channel | None
    tone_tier: Literal[1, 2, 3, 4]
    inferred_cause: UnpaidCause
    rationale: str               # one sentence, shown in the UI
    confidence: float
```

It cannot call an API. It cannot send a message. It cannot create a payment link. It emits JSON,
and deterministic Python decides what happens next.

Every judge will ask some version of "what stops it going rogue?" This is the answer, and you
should be able to open `agent/registry.py` and `guardrails/gate.py` on screen while saying it.

---

## LangGraph state machine

```
        ┌──────────┐
        │ observe  │  gather invoice, counterparty, history, signals
        └────┬─────┘
             v
        ┌──────────┐
        │ diagnose │  infer unpaid_cause from behavioural signals
        └────┬─────┘
             v
        ┌──────────┐
        │   plan   │  LLM proposes ProposedAction
        └────┬─────┘
             v
        ┌──────────┐      reject
        │ validate │──────────────> ┌──────────┐
        └────┬─────┘                │ fallback │  deterministic policy
             │ accept               └────┬─────┘
             v                           │
        ┌──────────┐ <───────────────────┘
        │  queue   │  write Action{status: proposed, scheduled_for}
        └──────────┘
```

`plan_day` runs this graph once per eligible invoice. It does **not** execute — it only queues.
Execution happens later, in the dispatch window, behind the gate.

---

## Node: observe

Assembles the decision context. Pure DB reads, no LLM.

```python
class ObservationContext(BaseModel):
    invoice: InvoiceSnapshot          # amount, dpd, state, tone_tier, touch_count
    counterparty: CounterpartySnapshot # avg_days_to_pay, broken_promises, lifetime_revenue
    history: list[TimelineEvent]       # last 10 events
    signals: BehaviouralSignals
    constraints: Constraints           # caps remaining, approval thresholds, permitted channels
```

`BehaviouralSignals` is the interesting part:

| Signal | Derived from |
|---|---|
| `link_opened_unpaid_count` | `messages.clicked_at` with no matching payment |
| `email_bounced` | `messages.delivery_status = 'bounced'` |
| `zero_engagement` | no opens, no clicks, no replies across all touches |
| `partial_payment_received` | `outstanding_paise < amount_paise` |
| `last_reply_intent` | most recent `replies.intent` |
| `historically_reliable` | `avg_days_to_pay` within terms + 15, zero broken promises |
| `first_slip` | first time this counterparty has gone past due |

---

## Node: diagnose

Maps signals to a cause. **Rules first, LLM only for ambiguity.**

| Signal pattern | Cause | Confidence |
|---|---|---|
| `link_opened_unpaid_count >= 2` | `cash_crunch` | high |
| `partial_payment_received` | `cash_crunch` | high |
| `email_bounced` or `last_reply_intent = wrong_contact` | `wrong_contact` | high |
| `last_reply_intent = dispute` | `dispute` | high |
| `last_reply_intent = refusal` | `refusal` | high |
| `historically_reliable` and `first_slip` and `dpd < 15` | `oversight` | high |
| `zero_engagement` and `touch_count >= 3` | `wrong_contact` | medium |
| reply mentions PO / GRN / invoice copy | `awaiting_docs` | medium (LLM) |
| none of the above | `unknown` | — |

Only the `awaiting_docs` case genuinely needs the LLM. Everything else is a lookup.
This is deliberate: an LLM call you can replace with an `if` statement is a liability, not a feature.

---

## Node: plan

The LLM call. Prompt template lives in `prompts/llm-prompts.md`.

Given the observation context and the diagnosed cause, propose one action. The prompt includes:
- the closed tool registry with descriptions
- the allowed transitions from the current `recovery_state`
- remaining touch budget
- the cause -> intervention mapping table below, as guidance not as law

**Cause to intervention mapping:**

| Cause | Right intervention | Wrong intervention |
|---|---|---|
| `oversight` | Single gentle nudge, tier 1–2, no escalation | Escalating a good customer's first slip |
| `cash_crunch` | Offer instalment split, tone stays low, extend link expiry | Firmer tone — they already want to pay |
| `dispute` | `mark_disputed`, freeze everything, route to human | Any further dunning |
| `wrong_contact` | `switch_channel`, request AP contact, mark contact stale | Repeating the same dead channel |
| `awaiting_docs` | Send invoice copy / PO reference, tier 1 | Chasing payment before they can process it |
| `refusal` | `stop`, exception list | Escalation |
| `unknown` | Standard sequence at current tier | Guessing |

---

## Node: validate

Deterministic. Three checks, all must pass.

1. **Registry check** — is `action` in the closed tool registry?
2. **Transition check** — does this action imply a transition allowed from the current
   `recovery_state`? (Table in `architecture/data-model.md`.)
3. **Schema check** — did the LLM return well-formed JSON matching `ProposedAction`?

Any failure → log it, discard the proposal, run `fallback`. Log the rejection with the raw LLM
output. Being able to show a rejected hallucination on stage is worth more than never having one.

---

## Node: fallback

The deterministic policy. Also the entire product if every LLM provider is down.

```
if recovery_state == 'not_started' and dpd < 0:      tier 1, email     (pre-due courtesy)
if recovery_state == 'not_started' and dpd >= 0:     tier 1, email     (first nudge)
if recovery_state == 'nudged' and dpd >= 5:          tier 2, preferred channel
if recovery_state == 'chasing' and dpd >= 15:        tier 3, escalate  (needs approval)
if recovery_state == 'broken_promise':               tier +1, escalate
if recovery_state == 'promised':                     snooze until promised_date + 1
if touch_count >= lifetime_cap:                      stop, reason = touch_cap_reached
if broken_promise_count >= 3:                        stop, reason = broken_promises_exceeded
```

Test this path explicitly. Set `LLM_ENABLED=false` and verify the batch still runs end to end.

---

## The tool registry (closed)

Anything not on this list is rejected at validate.

| Tool | Effect | Requires approval |
|---|---|---|
| `create_payment_link` | Razorpay link, `reference_id` = invoice number | No |
| `send_message` | Generate + gate + send on a channel | Tier 3+ or above value threshold |
| `log_promise` | Record a PTP, suppress outreach until date+1 | No |
| `offer_installment` | Two links at partial amounts | No |
| `switch_channel` | Mark contact stale, try alternate channel | No |
| `escalate_tier` | Raise tone tier by one | Yes at tier 3+ |
| `snooze` | Defer this invoice to a date | No |
| `mark_disputed` | Freeze all outreach, route to human | No |
| `stop` | Permanent stop, exception list, record reason | No |

Note the asymmetry: **stopping never needs approval; escalating does.** The system is free to
be gentler on its own and must ask permission to be firmer.

---

## The gate — seven checks, in order

Runs in the dispatch window, immediately before execution. Ordered so the cheapest and most
consequential checks run first.

```python
def gate(action: ProposedAction, ctx: ExecutionContext) -> GateVerdict:
    checks = []
    checks.append(check_time_window(ctx))        # 1
    checks.append(check_freshness(ctx))          # 2
    checks.append(check_consent(ctx))            # 3
    checks.append(check_frequency_cap(ctx))      # 4
    checks.append(check_value_threshold(ctx))    # 5
    checks.append(check_content_policy(action))  # 6
    checks.append(check_stopping_rules(ctx))     # 7
    return GateVerdict(passed=all(c.passed for c in checks), checks=checks)
```

**All seven always run** even after one fails. The full verdict array goes into the audit log —
a partial record is a weaker demo and a weaker audit.

| # | Check | Fails when | On failure |
|---|---|---|---|
| 1 | `time_window` | outside 08:00–19:00 IST | requeue to next window |
| 2 | `freshness` | invoice paid since proposal | revoke action permanently |
| 3 | `consent` | channel not permitted, opted out, quarantined | drop, log |
| 4 | `frequency_cap` | >2 touches this week or >6 lifetime | requeue or stop |
| 5 | `value_threshold` | above threshold or tier 3+ without approval | move to approval queue |
| 6 | `content_policy` | banned phrase, missing amount / invoice no. / link / opt-out | regenerate once, then template |
| 7 | `stopping_rules` | settled, disputed, opted out, 3 broken promises, cap reached | permanent stop |

Check 2 is the one that saves you from the worst failure mode in this product. Do not skip it
because "the scheduler just ran."

**Content policy — banned in every generated message:**
legal threats, credit-rating threats, references to family or personal assets, disclosure to third
parties, shaming language, ALL CAPS demands, fake urgency ("final notice" unless tier 4), any
claim of legal action not actually being taken.

**Required in every generated message:** correct outstanding amount, invoice number, payment link,
opt-out mechanism, sender identification.

---

## Where the LLM is genuinely used

Four places. If you add a fifth, justify it here first.

| Use | Why a rule can't do it | Model |
|---|---|---|
| Reply intent classification | Hinglish, unstructured, regex-hostile | Groq, fast |
| Promised-date extraction | "next Tuesday tak clear kar dunga" | Groq |
| Action proposal | Weighs many signals against a strategy | Groq |
| Message drafting | Tone-adaptive, multilingual, personalised | Gemini |

Everywhere else — aging, scoring, ranking, gating, reconciliation — is deterministic code.
That ratio is a design achievement, not a shortcoming. Say so in the pitch.

---

## Idempotency

Every stage must survive being run twice.

- `plan_day` — unique on `(invoice_id, date)`; a second run updates rather than duplicates
- `dispatch_window` — claims actions with `SELECT ... FOR UPDATE SKIP LOCKED`
- `create_payment_link` — idempotency key = `sha256(invoice_id + amount + purpose)`
- Webhook processing — unique on `razorpay_event_id`
- `promise_sweep` — a promise already marked broken is skipped

---

## What "audit trail" means concretely

For every action, executed or not, `audit_log` holds: who proposed it, what they saw, why they
chose it, all seven gate verdicts, what happened, and a hash linking to the previous entry.

The demo moment is not showing what the agent *did*. It is opening the audit log, filtering to
`outcome = blocked`, and showing the judges every message the system **refused to send**, with
the reason attached to each one.

Build the UI so that filter is one click.
