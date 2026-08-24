# Agent: Backend

**Scope:** `api/app/` — FastAPI, ingestion, scoring, endpoints, scheduler.
**Prerequisites:** `/CLAUDE.md`, `architecture/components.md`, `architecture/api-contracts.md`

---

## Ground rules

1. **Money is `BIGINT` paise.** Never float, never `Decimal` in the DB. Convert at the UI boundary only.
2. **Every query is scoped to `merchant_id`** from the auth token, never from a request parameter.
3. **No LLM call in a request-response path.** If a human action needs generation, enqueue it.
4. **Timestamps are `TIMESTAMPTZ`, stored UTC.** Convert to IST only for display and for the
   `time_window` guardrail check.
5. **Every scheduled job is idempotent.** A double-run must never double-send.

---

## Layout

```
api/app/
  main.py              FastAPI app, router registration, lifespan (starts scheduler)
  config.py            pydantic-settings, reads .env
  db.py                engine, session factory, get_db dependency
  models/              SQLAlchemy models, one file per table group
  schemas/             Pydantic request/response models
  routers/             one file per resource group in api-contracts.md
  ingestion/           parsers, mapper, normalizer, matcher
  scoring/             aging, features, model, worklist
  scheduler/           jobs.py, registry.py
  audit/               log.py, query.py
  deps.py              shared dependencies (auth, tenant scoping)
```

---

## Build order within backend

1. `config.py`, `db.py`, models, Alembic migration — nothing else works without these
2. `audit/log.py` — every other module writes to it, build it early
3. `ingestion/` — get real data in
4. `scoring/` — get the worklist working
5. `routers/` — expose it
6. `scheduler/` — automate it

---

## Ingestion notes

**Column mapping.** Rules first: a dictionary of known header variants (`"Bill No"`, `"Invoice #"`,
`"Voucher No"` → `invoice_number`). Only headers that fail rule matching go to the LLM, one call
for the whole header row, not per column.

**Date parsing.** Indian exports use `DD/MM/YYYY` and `DD-MM-YY`. **Never** let a parser guess
between `DD/MM` and `MM/DD`. Detect the format from the batch (if any value has day > 12, the
format is determined), and if genuinely ambiguous, send the whole batch to the repair queue and
ask. Silently misparsing dates corrupts every downstream calculation.

**Counterparty matching.** GSTIN exact match wins absolutely. Otherwise normalise
(lowercase, strip `pvt`, `ltd`, `private`, `limited`, `llp`, `&`, `.`, extra whitespace) and use
`rapidfuzz.token_sort_ratio` with a threshold of 88. Below 88, create a new counterparty — a false
merge is far worse than a duplicate, because it merges payment histories and consent records.

**Duplicates.** Unique on `(merchant_id, invoice_number)`. On conflict, update `outstanding_paise`
and `payment_status` only. Never overwrite `recovery_state`, `touch_count`, or `current_tone_tier` —
that would reset an in-flight recovery sequence.

---

## Scoring notes

Weights are in ADR-008. Keep `features.py` (extraction) strictly separate from `model.py`
(combination) so the model can swap without touching feature code.

**The reason string is not optional.** Template it from the top three contributing features:

```python
f"₹{amount_lakhs}L, {dpd} days. {top_feature_phrase}."
# "₹4.2L, 68 days. This customer has paid late twice before but always paid."
```

Log every score with its full feature vector to `audit_log`. When real payment outcomes exist,
that log is the training set for ADR-008's LightGBM migration.

Never call an LLM in the ranking path.

---

## Endpoint notes

Follow `architecture/api-contracts.md` exactly — the frontend is built against it.

- Paginate anything that can exceed 100 rows
- `GET /worklist` is the hot path; it must hit the
  `(merchant_id, recovery_state, priority_score DESC)` index. Verify with `EXPLAIN`.
- Return `422` with the specific failing rule when a merchant's message edit breaks content policy
- Never return a stack trace; use the error envelope

---

## Scheduler notes

Job functions are plain callables taking `merchant_id`. Do not decorate them with framework
specifics — ADR-007 depends on that portability for the eventual Celery migration.

```python
def dispatch_window(merchant_id: UUID) -> None: ...
```

Claim work with `SELECT ... FOR UPDATE SKIP LOCKED`. Persist scheduler state via
`SQLAlchemyJobStore` — an in-memory scheduler loses promise follow-ups on container restart,
which is a product failure.

Expose `scheduler.running` and `next_dispatch` on `GET /health` so a dead scheduler is visible
rather than silent.

---

## Testing priorities

Test these. Everything else can be manually verified.

1. Date parsing across `DD/MM/YYYY`, `DD-MM-YY`, `YYYY-MM-DD`, and the ambiguous case
2. Counterparty matching — no false merges at the 88 threshold
3. Duplicate ingestion does not reset recovery state
4. Scoring is deterministic — same input, same score, same reason
5. Job idempotency — run twice, assert one send
6. Tenant isolation — merchant A cannot read merchant B's anything
