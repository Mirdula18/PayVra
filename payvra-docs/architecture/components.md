# Components

Module layout under `api/app/`. Each section states the module's single responsibility,
its public interface, and what it must never do.

---

## `ingestion/`

**Responsibility:** turn arbitrary tabular input into canonical `Invoice` and `Counterparty` records.

```
ingestion/
  parsers.py        CSV/XLSX -> raw rows
  mapper.py         header mapping (rules first, LLM fallback for unknowns)
  normalizer.py     raw row -> Invoice, validation, repair queue
  matcher.py        counterparty resolution (GSTIN exact > fuzzy name)
```

Interface: `ingest_batch(merchant_id, file) -> IngestResult{created, updated, repaired, duplicates}`

Never: contact anyone. Never: assume a date format — parse explicitly, reject ambiguity to repair.

---

## `scoring/`

**Responsibility:** compute aging, exposure, and collectability; produce the ranked worklist.

```
scoring/
  aging.py          days_past_due, buckets, MSME 45-day flag
  features.py       feature extraction per invoice
  model.py          scoring (rules for MVP, LightGBM optional)
  worklist.py       ranking + plain-English reason generation
```

Features used: `days_past_due`, `amount`, `share_of_exposure`, `cp_avg_days_to_pay`,
`broken_promise_count`, `engagement_rate`, `has_dispute`, `lifetime_revenue`, `touch_count`.

Ranking: `priority = p_collectable * amount_at_risk * urgency_multiplier`

Interface: `rescore(merchant_id) -> None`, `get_worklist(merchant_id, limit) -> list[WorklistRow]`

Never: call an LLM in the ranking path. The reason string is templated from feature weights, so
it is always available and always consistent with the score.

---

## `agent/`

**Responsibility:** diagnose cause, propose one action per eligible account.

```
agent/
  graph.py          LangGraph state machine definition
  nodes.py          observe / diagnose / plan / validate nodes
  registry.py       the closed tool registry
  policy.py         deterministic fallback policy
  diagnosis.py      cause inference from behavioural signals
```

Interface: `plan_day(merchant_id) -> list[ProposedAction]`

The LLM proposes `{action, channel, tone_tier, rationale}`. `validate` rejects anything outside
the registry or the current state's allowed transitions, and `policy.py` supplies the fallback.

Never: execute anything. Never: create a payment link. Never: send a message. This module only
produces proposals.

---

## `guardrails/`

**Responsibility:** the gate. Deterministic, ordered, exhaustively logged.

```
guardrails/
  gate.py           the 7 checks, in order
  policy_content.py banned phrases, required elements
  stopping.py       stopping-rule evaluation
```

Interface: `gate(action: ProposedAction) -> GateVerdict{passed, checks[], reason}`

Every verdict, pass or fail, is written to `audit_log` by the caller.

Never: contain an LLM call. Never: be bypassable. Never: "warn and continue" — a failed check
halts the action.

---

## `generation/`

**Responsibility:** produce the message text.

```
generation/
  drafter.py        LLM call via LiteLLM, schema-constrained
  validator.py      amount, invoice number, link, opt-out, banned phrases
  templates.py      deterministic fallback per tone tier and language
  cache.py          content-hash cache
```

Interface: `draft(context: DraftContext) -> Message{subject, body, tone_tier, language, source}`
where `source` is `llm` or `template`.

Never: return unvalidated LLM output. Never: run in a request-response path.

---

## `razorpay/`

**Responsibility:** every interaction with Razorpay.

```
razorpay/
  client.py         HTTP client, auth, idempotency keys, retry/backoff
  links.py          create / notify / cancel / regenerate Payment Links
  invoices.py       Invoices API (P1)
  smart_collect.py  virtual accounts (P1)
  webhooks.py       HMAC verification, event parsing
```

Interface:
`create_link(invoice, expire_by, accept_partial) -> PaymentLink`
`notify(link_id, medium) -> None`
`cancel(link_id) -> None`
`verify_signature(raw_body: bytes, signature: str) -> bool`

Never: parse the webhook body before verifying the signature. Never: use live keys.
Never: store card data.

---

## `delivery/`

**Responsibility:** actually send, and ingest receipts.

```
delivery/
  email.py          Resend
  sms.py            MSG91 / Twilio
  whatsapp.py       Meta Cloud API sandbox
  receipts.py       delivery / bounce / open / click ingestion
  inbound.py        reply webhook -> Reply record
```

Interface: `send(channel, contact, message) -> DeliveryResult{provider_id, status}`

Never: send without a `GateVerdict.passed == True` in scope.

---

## `replies/`

**Responsibility:** understand what a customer said back.

```
replies/
  classifier.py     intent classification via LLM
  extractor.py      promised-date extraction (incl. Hinglish)
  router.py         intent -> state transition
```

Intents: `dispute`, `promise_to_pay`, `query`, `refusal`, `wrong_contact`, `acknowledgment`, `unclear`

Confidence below `REPLY_CONFIDENCE_THRESHOLD` routes to the human queue. Never guess.

---

## `reconciliation/`

**Responsibility:** settle invoices and stop outreach.

```
reconciliation/
  handler.py        webhook event dispatch, dedupe on event.id
  settle.py         mark paid, revoke scheduled jobs, close promises
  manual.py         "mark paid offline"
```

The revoke step is the most important line of code in the product. An invoice that settles
must have every pending Action for it cancelled in the same transaction.

---

## `audit/`

**Responsibility:** the append-only record.

```
audit/
  log.py            write entries, compute prev_hash chain
  query.py          filtered retrieval
```

Interface: `record(actor, action_type, subject, inputs, rationale, verdicts, outcome) -> AuditEntry`

Never: UPDATE. Never: DELETE. Enforced with a DB trigger, not just convention.

---

## `scheduler/`

**Responsibility:** run jobs on time.

```
scheduler/
  jobs.py           job definitions
  registry.py       APScheduler setup
```

| Job | Cadence | Does |
|---|---|---|
| `refresh_aging` | 00:30 daily | Recompute DPD, buckets, exposure |
| `rescore_worklist` | 01:00 daily | Rescore with yesterday's engagement |
| `plan_day` | 01:30 daily | Agent proposes today's actions |
| `dispatch_window` | every 15 min, 08:00–19:00 | Gate + generate + send due actions |
| `promise_sweep` | 09:00 daily | Broken promises -> escalate |
| `link_hygiene` | 10:00 daily | Regenerate expiring links, cancel settled ones |
| `digest` | 07:30 daily | Assemble the morning summary |

All jobs must be idempotent. A double-run must never double-send.

---

## `web/` (frontend)

```
src/
  pages/
    Upload.tsx         batch import + column mapping + repair queue
    Consent.tsx        per-counterparty consent + quarantine
    Worklist.tsx       the ranked queue (primary screen)
    ReviewPlan.tsx     14-day preview before activation
    Account.tsx        per-counterparty timeline
    Dashboard.tsx      recovered / needs-you / promises / exceptions
    AuditLog.tsx       filterable audit trail
    Settings.tsx       guardrail configuration
  components/
    GateVerdictBadge.tsx    shows pass/fail per check
    ToneTierPill.tsx
    ReasonChip.tsx          plain-English ranking reason
    RecoveryCounter.tsx     the headline number
```

`Dashboard.tsx` and `AuditLog.tsx` are the two screens judges will spend the most time on.
Build them last, but budget real time for them.
