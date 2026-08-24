# Data Model

PostgreSQL 16. **15 domain tables.** All tables carry `merchant_id` for tenant isolation, directly
or through their parent; every query must be scoped. Money is stored in **paise as BIGINT**, never
as float. Timestamps are `TIMESTAMPTZ`, stored UTC, displayed IST.

---

## Enums

Enum-typed columns are stored as **`VARCHAR` + a `CHECK` constraint**, not native PostgreSQL
`ENUM` types. Native enums require a hand-written `ALTER TYPE ... ADD VALUE` migration for every
new value — Alembic autogenerate cannot detect enum value changes — and `action_type`,
`stop_reason`, and `unpaid_cause` are expected to gain values across Phases 3–7. A
`CHECK (col IN (...))` constraint is dropped and recreated by an ordinary migration.

**`app/enums.py` (`StrEnum`) is the single source of truth.** Each `CHECK` value list is
generated from it through the `app.enums.ENUM_COLUMNS` registry, so the schema and the
application cannot drift. `test_enum_parity` asserts every enum value appears in its column's
`CHECK`.

| Enum (`VARCHAR`) | Values |
|---|---|
| `payment_status` | `unpaid`, `partially_paid`, `paid`, `written_off` |
| `recovery_state` | `not_started`, `nudged`, `chasing`, `promised`, `broken_promise`, `escalated`, `human_review`, `stopped`, `settled` |
| `unpaid_cause` | `oversight`, `cash_crunch`, `dispute`, `wrong_contact`, `awaiting_docs`, `refusal`, `unknown` |
| `channel` | `email`, `sms`, `whatsapp` |
| `action_type` | `create_payment_link`, `send_message`, `log_promise`, `offer_installment`, `switch_channel`, `escalate_tier`, `snooze`, `mark_disputed`, `stop` |
| `action_status` | `proposed`, `gated_pass`, `gated_fail`, `awaiting_approval`, `executed`, `failed`, `revoked` |
| `reply_intent` | `dispute`, `promise_to_pay`, `query`, `refusal`, `wrong_contact`, `acknowledgment`, `unclear` |
| `stop_reason` | `settled`, `disputed`, `opted_out`, `broken_promises_exceeded`, `touch_cap_reached`, `no_consent`, `merchant_excluded`, `written_off` |
| `actor_type` | `agent`, `human`, `system`, `counterparty` |
| `batch_status` | `parsing`, `awaiting_repair`, `complete` |
| `batch_row_status` | `ok`, `repair_needed`, `repaired`, `discarded` |

The meaning of each `recovery_state` is documented at the **Recovery state machine** section below.

`batch_rows.error_code` is a deliberate exception to the CHECK-constraint rule and stays plain
`TEXT` — see the `batch_rows` section for why, and for the code vocabulary.

---

## `merchants`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | TEXT | |
| `email` | TEXT | |
| `razorpay_key_id` | TEXT | test mode only |
| `razorpay_key_secret_enc` | TEXT | encrypted at rest |
| `razorpay_webhook_secret_enc` | TEXT | |
| `contact_hour_start` | SMALLINT | default 8 |
| `contact_hour_end` | SMALLINT | default 19 |
| `weekly_touch_cap` | SMALLINT | default 2 |
| `lifetime_touch_cap` | SMALLINT | default 6 |
| `approval_value_threshold_paise` | BIGINT | default 50000000 (₹5L) |
| `approval_tone_tier` | SMALLINT | default 3 |
| `is_paused` | BOOLEAN | global pause |
| `created_at` | TIMESTAMPTZ | |

---

## `counterparties`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `merchant_id` | UUID FK | |
| `name` | TEXT | canonical name |
| `name_normalized` | TEXT | lowercased, suffixes stripped, for fuzzy match |
| `gstin` | TEXT NULL | unique per merchant when present |
| `is_msme` | BOOLEAN | drives MSME Act 45-day flag |
| `preferred_language` | TEXT | `en` or `hinglish`, default `en` |
| `lifetime_revenue_paise` | BIGINT | |
| `avg_days_to_pay` | NUMERIC | rolling, from payment history |
| `broken_promise_count` | SMALLINT | |
| `is_quarantined` | BOOLEAN | no consent basis |
| `is_excluded` | BOOLEAN | merchant opted this account out of automation |
| `created_at` | TIMESTAMPTZ | |

Unique: `(merchant_id, gstin)` where gstin is not null.
Index: `(merchant_id, name_normalized)`.

---

## `contacts`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `counterparty_id` | UUID FK | |
| `name` | TEXT | |
| `email` | TEXT NULL | |
| `phone` | TEXT NULL | E.164 |
| `role` | TEXT NULL | e.g. `ap_head`, `owner`, `accounts` |
| `is_primary` | BOOLEAN | |
| `is_stale` | BOOLEAN | set on bounce or wrong-contact reply |
| `created_at` | TIMESTAMPTZ | |

---

## `consents`

The DPDP ledger. One row per counterparty per channel.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `counterparty_id` | UUID FK | |
| `channel` | channel | |
| `is_permitted` | BOOLEAN | |
| `basis` | TEXT | e.g. `existing_commercial_relationship` |
| `granted_at` | TIMESTAMPTZ | |
| `revoked_at` | TIMESTAMPTZ NULL | opt-out timestamp |
| `opt_out_token` | TEXT | embedded in every message |

Unique: `(counterparty_id, channel)`.

---

## `invoices`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `merchant_id` | UUID FK | |
| `counterparty_id` | UUID FK | |
| `invoice_number` | TEXT | merchant's own number |
| `amount_paise` | BIGINT | original total |
| `outstanding_paise` | BIGINT | reduces on partial payment |
| `currency` | CHAR(3) | `INR` |
| `issue_date` | DATE | |
| `due_date` | DATE | |
| `terms_days` | SMALLINT | |
| `po_ref` | TEXT NULL | |
| `has_gst` | BOOLEAN | |
| `payment_status` | payment_status | |
| `recovery_state` | recovery_state | |
| `inferred_cause` | unpaid_cause | |
| `days_past_due` | INT | computed nightly |
| `aging_bucket` | TEXT | `0-30`, `31-60`, `61-90`, `90+` |
| `crosses_msme_45` | BOOLEAN | |
| `collectability_score` | NUMERIC | 0–1 |
| `priority_score` | NUMERIC | ranking value |
| `priority_reason` | TEXT | plain-English, shown in UI |
| `touch_count` | SMALLINT | lifetime touches |
| `current_tone_tier` | SMALLINT | 1–4 |
| `stop_reason` | stop_reason NULL | set when `recovery_state = 'stopped'` |
| `settled_at` | TIMESTAMPTZ NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Unique: `(merchant_id, invoice_number)`.
Index: `(merchant_id, recovery_state, priority_score DESC)` — this backs the worklist query.

---

## `payment_links`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `invoice_id` | UUID FK | |
| `razorpay_link_id` | TEXT | `plink_...` |
| `short_url` | TEXT | what goes in the message |
| `amount_paise` | BIGINT | may be partial for instalments |
| `reference_id` | TEXT | = `invoice_number`, this is the recon key |
| `status` | TEXT | `created`, `paid`, `partially_paid`, `expired`, `cancelled` |
| `expire_by` | TIMESTAMPTZ | |
| `accept_partial` | BOOLEAN | |
| `idempotency_key` | TEXT | unique |
| `created_at` | TIMESTAMPTZ | |

---

## `actions`

Every proposal, gated or not, executed or not. This is the spine of the audit story.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `merchant_id` | UUID FK | |
| `invoice_id` | UUID FK | |
| `type` | action_type | |
| `status` | action_status | |
| `channel` | channel NULL | |
| `tone_tier` | SMALLINT NULL | |
| `proposed_by` | actor_type | `agent` or `human` |
| `rationale` | TEXT | why the agent chose this |
| `llm_model` | TEXT NULL | which model produced the proposal |
| `gate_verdicts` | JSONB | `[{check, passed, reason}]` — all 7, always |
| `gate_failure_reason` | TEXT NULL | |
| `message_id` | UUID FK NULL | |
| `scheduled_for` | TIMESTAMPTZ | when the dispatch window should pick it up |
| `executed_at` | TIMESTAMPTZ NULL | |
| `revoked_at` | TIMESTAMPTZ NULL | set when the invoice settles |
| `created_at` | TIMESTAMPTZ | |

Index: `(merchant_id, status, scheduled_for)` — backs the dispatch query.
Index: `(invoice_id)` — backs the revoke-on-settle sweep.

---

## `messages`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `action_id` | UUID FK | |
| `channel` | channel | |
| `contact_id` | UUID FK | |
| `subject` | TEXT NULL | email only |
| `body` | TEXT | |
| `language` | TEXT | `en` / `hinglish` |
| `tone_tier` | SMALLINT | |
| `source` | TEXT | `llm` or `template` |
| `content_hash` | TEXT | for caching |
| `validation_passed` | BOOLEAN | |
| `provider_message_id` | TEXT NULL | |
| `delivery_status` | TEXT | `queued`, `sent`, `delivered`, `bounced`, `failed` |
| `opened_at` / `clicked_at` | TIMESTAMPTZ NULL | engagement signals |
| `created_at` | TIMESTAMPTZ | |

---

## `promises`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `invoice_id` | UUID FK | |
| `reply_id` | UUID FK | source of the promise |
| `promised_date` | DATE | |
| `promised_amount_paise` | BIGINT NULL | null = full outstanding |
| `confidence` | NUMERIC | extractor confidence 0–1 |
| `status` | TEXT | `open`, `kept`, `broken`, `superseded` |
| `resolved_at` | TIMESTAMPTZ NULL | |
| `created_at` | TIMESTAMPTZ | |

---

## `replies`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `invoice_id` | UUID FK NULL | may arrive unlinked |
| `counterparty_id` | UUID FK | |
| `channel` | channel | |
| `raw_text` | TEXT | |
| `intent` | reply_intent | |
| `confidence` | NUMERIC | |
| `extracted_date` | DATE NULL | |
| `routed_to_human` | BOOLEAN | true when confidence < threshold |
| `received_at` | TIMESTAMPTZ | |

---

## `webhook_events`

Idempotency table. Insert-first, process-second.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `razorpay_event_id` | TEXT UNIQUE | dedupe key |
| `event_type` | TEXT | `payment_link.paid` etc |
| `raw_payload` | JSONB | |
| `signature_valid` | BOOLEAN | |
| `processed_at` | TIMESTAMPTZ NULL | |
| `received_at` | TIMESTAMPTZ | |

The unique constraint on `razorpay_event_id` *is* the dedupe mechanism. Attempt the insert;
on conflict, return 200 and do nothing.

---

## `audit_log`

Append-only. Hash-chained. No UPDATE, no DELETE, no TRUNCATE — enforced at the database.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | ordering matters, hence serial not UUID |
| `merchant_id` | UUID FK | |
| `actor` | actor_type | |
| `actor_id` | TEXT NULL | user id, agent run id, or `system` |
| `action_type` | TEXT | |
| `subject_type` | TEXT | `invoice`, `counterparty`, `action` |
| `subject_id` | UUID | |
| `inputs` | JSONB | what the decision saw |
| `rationale` | TEXT | why |
| `gate_verdicts` | JSONB | all checks, pass and fail |
| `outcome` | TEXT | `executed`, `blocked`, `stopped`, `approved`, `rejected` |
| `prev_hash` | TEXT | SHA-256 of the previous row's canonical form |
| `entry_hash` | TEXT | SHA-256 of this row's canonical form + prev_hash |
| `created_at` | TIMESTAMPTZ | |

```sql
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

-- RULEs do not cover TRUNCATE. Without this trigger, `TRUNCATE audit_log CASCADE` silently
-- empties the table -- including indirectly, via TRUNCATE ... CASCADE from `merchants`.
-- Raises unconditionally: no GUC, no session flag, no bypass. The seed rebuilds the schema
-- (alembic downgrade base && alembic upgrade head) rather than truncating.
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_truncate();
```

Index: `(merchant_id, created_at DESC)` — backs the default reverse-chronological `GET /audit` feed.
Index: `(merchant_id, outcome)` — backs the AuditLog screen's one-click `blocked` filter.

For the precise scope of the append-only guarantee — what it does and does not protect against —
see **Audit log** in `docs/glossary.md`. It is tamper-*evident* against a privileged operator,
not tamper-*proof*.

---

## `metrics_snapshots`

Daily rollup so the dashboard never recomputes DSO on the fly.

`dso_days` **must** be computed via `app/metrics.py::collection_period_days` — the single source
of truth, shared with the seed summary and `GET /metrics.dso_before_days`. Do not re-derive the
formula at the call site and do not hardcode a plausible-looking figure: api-contracts.md is
explicit that this number is computed, never asserted. Three independent implementations is how
the dashboard ends up contradicting the seed mid-demo.

Note it measures from the **issue** date and is amount-weighted; it is not mean days-past-due.
See the metric note in `agents/data-and-seed.md`.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `merchant_id` | UUID FK | |
| `snapshot_date` | DATE | |
| `total_outstanding_paise` | BIGINT | |
| `recovered_paise` | BIGINT | since previous snapshot |
| `dso_days` | NUMERIC | amount-weighted collection period, from the **issue** date |
| `recovery_rate` | NUMERIC | |
| `promise_kept_rate` | NUMERIC | |
| `invoices_by_state` | JSONB | |

Unique: `(merchant_id, snapshot_date)`.

---

## `batches`

One uploaded invoice file and its ingestion outcome. Added in Phase 1 (migration `0003`).

Holds the resolved `column_mapping` so `POST /batches/{id}/mapping` can re-parse without the
merchant re-uploading, and the per-outcome counters `POST /batches` returns.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `merchant_id` | UUID FK | tenant scope; every batch query filters on it |
| `name` | TEXT NULL | merchant-supplied label, e.g. "August ledger" |
| `filename` | TEXT | the uploaded file's name, for the repair UI |
| `column_mapping` | JSONB | original header -> canonical field, e.g. `{"Bill No": "invoice_number"}` |
| `row_count` | INT | data rows parsed, excluding the header |
| `created_count` | INT | new invoices |
| `updated_count` | INT | existing invoices whose money changed |
| `duplicate_count` | INT | rows matching an existing `(merchant_id, invoice_number)` |
| `repair_count` | INT | rows currently needing merchant input; decrements as they are repaired |
| `status` | batch_status | `parsing` / `awaiting_repair` / `complete` |
| `created_at` | TIMESTAMPTZ | |

The original file is **not** stored. Invoice files are merchant PII and retaining them is a
liability with no upside — `batch_rows` already holds every parsed row, which is all a re-parse
needs.

---

## `batch_rows`

The raw parsed row behind every ingested invoice and every repair-queue item. Added in Phase 1
(migration `0003`).

Storing `raw` verbatim is what lets the repair queue show the merchant *exactly* what came out of
their file, rather than a normalised guess at it, and lets a corrected row be reprocessed without
re-uploading. `invoice_id` links back once the row successfully becomes an Invoice.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `batch_id` | UUID FK | |
| `row_number` | INT | 1-based as the merchant sees it; the header is row 1, so the first data row is 2 |
| `raw` | JSONB | header -> cell text, exactly as parsed. Never normalised |
| `status` | batch_row_status | `ok` / `repair_needed` / `repaired` / `discarded` |
| `error_code` | TEXT NULL | see below |
| `error_detail` | TEXT NULL | human-readable, names the offending value |
| `invoice_id` | UUID FK NULL | set once the row becomes an Invoice |
| `created_at` | TIMESTAMPTZ | |

Index: `(batch_id, status)` — backs `GET /batches/{id}/repairs`, which filters to
`status = 'repair_needed'`.

`error_code` is deliberately plain `TEXT` rather than a CHECK-constrained enum: validation rules
gain codes often, and a migration per new code is friction with no integrity payoff. The
application-side vocabulary is `RepairErrorCode` in `app/enums.py`, and it is what the UI switches
on:

| Code | Meaning |
|---|---|
| `missing_invoice_number` | the invoice number cell is blank |
| `missing_counterparty` | the customer name cell is blank |
| `missing_amount` | the amount cell is blank |
| `unparseable_amount` | not numeric after cleaning, e.g. `Rs. Twelve Thousand` |
| `non_positive_amount` | zero or negative invoice amount |
| `missing_due_date` | FR-1.3 — a row without a due date cannot be aged |
| `unparseable_date` | not a recognised date shape under the batch's format |
| `impossible_date` | a real shape but not a real date, e.g. `32/02/2026` |
| `due_before_issue` | the dates contradict each other |
| `invalid_gstin` | not a valid 15-character GSTIN |
| `ambiguous_counterparty` | two or more existing counterparties matched at or above the fuzzy threshold; the merchant picks, because a false merge combines payment histories and consent records and cannot be undone |
| `ambiguous_date_format` | **batch-level** — DD/MM could not be told from MM/DD |
| `unmapped_required_column` | **batch-level** — no column mapped to a required field |

The two batch-level codes apply to *every* row of the batch. When the date format cannot be
resolved from the file's own evidence, the whole batch goes to the repair queue rather than any
row being imported under a guessed format — see `agents/backend.md` and
`app/ingestion/normalizer.py`.

---

## `apscheduler_jobs` (infrastructure, not a domain table)

Created **at runtime by APScheduler**, not by an Alembic migration — it will not appear in
`0001_initial_schema.py`, but it does appear in `\dt`. Listed here only so the table count
reconciles: the **15 domain tables** above are the domain model (13 from migration `0001`, plus
`batches` and `batch_rows` from `0003`); `apscheduler_jobs` and `alembic_version` are
infrastructure, for 17 relations total in a migrated database.

APScheduler owns the schema and may change it across versions, so nothing in `app/` reads or
writes it directly — treat it as opaque. See ADR-007 (APScheduler in-process, Postgres job store).

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR(191) PK | job id, assigned by APScheduler |
| `next_run_time` | DOUBLE PRECISION NULL | epoch seconds; NULL when the job is paused |
| `job_state` | BYTEA | pickled job definition — opaque to this application |

Also carries `ix_apscheduler_jobs_next_run_time` on `next_run_time`, created by APScheduler.
Neither this index nor the two above count toward the project's own named-index inventory.

---

## Recovery state machine

Allowed transitions. The agent's proposal is rejected if it implies a transition not listed here.

```
not_started    -> nudged, stopped
nudged         -> chasing, promised, human_review, settled, stopped
chasing        -> promised, escalated, human_review, settled, stopped
promised       -> settled, broken_promise, human_review, stopped
broken_promise -> chasing, escalated, human_review, stopped
escalated      -> human_review, settled, stopped
human_review   -> chasing, escalated, settled, stopped
stopped        -> (terminal, except settled via manual/offline payment)
settled        -> (terminal)
```

`settled` and `stopped` are terminal. Reaching either revokes every pending action for the invoice.
