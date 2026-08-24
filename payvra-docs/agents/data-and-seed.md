# Agent: Data and Seed

**Scope:** `api/alembic/`, `api/app/models/`, `api/app/seed/`
**Prerequisites:** `/CLAUDE.md`, `architecture/data-model.md`

The seed data is not throwaway. Judges will look at it, and unconvincing data undermines
everything else.

---

## Migrations

Alembic, one migration per logical change. Never edit an applied migration.

`audit_log` needs its append-only rules in the migration itself, not in application code:

```sql
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;
```

Required indexes, all in the initial migration:

```sql
CREATE INDEX idx_worklist ON invoices (merchant_id, recovery_state, priority_score DESC);
CREATE INDEX idx_dispatch ON actions (merchant_id, status, scheduled_for);
CREATE INDEX idx_actions_invoice ON actions (invoice_id);        -- revoke-on-settle
CREATE UNIQUE INDEX idx_webhook_dedupe ON webhook_events (razorpay_event_id);
CREATE UNIQUE INDEX idx_invoice_number ON invoices (merchant_id, invoice_number);
CREATE INDEX idx_cp_match ON counterparties (merchant_id, name_normalized);
```

`idx_worklist` backs the hot path. `idx_actions_invoice` backs the most important write path.
Verify both with `EXPLAIN`.

---

## Seed dataset — the specification

**Target: 120 invoices across 34 counterparties.** Enough to look real, small enough to reason about
and to stay within Razorpay test mode's 30-link cap when demoing.

### Counterparty archetypes

Distribution matters more than volume. Every archetype must be present, because each one exercises
a different branch of the agent's diagnosis logic.

| Archetype | Count | Behaviour | Exercises |
|---|---|---|---|
| Reliable, occasionally late | 12 | Pays in 35–50 days, always pays | `oversight`, tier 1 only |
| Chronic slow payer | 8 | Pays in 75–95 days, needs chasing | Full sequence |
| Cash-crunched | 5 | Opens links repeatedly, pays partially | `cash_crunch`, instalment path |
| Promise-breaker | 3 | Promises, misses, promises again | PTP tracking, broken-promise escalation |
| Disputer | 2 | Replies with a genuine dispute | `dispute`, freeze, human routing |
| Wrong contact | 2 | Emails bounce | `wrong_contact`, channel switch |
| Ghost | 2 | Zero engagement ever | Touch cap, exception list |

Realistic Indian B2B names: `Sundaram Auto Components Pvt Ltd`, `Krishna Textiles`,
`Meridian Logistics LLP`, `Anand Enterprises`. Avoid `Acme Corp` and `Test Company 1`.

Include **deliberate name variants** across some invoices — `Sundaram Auto Components Pvt Ltd`
and `Sundaram Auto Comp.` — so the fuzzy matcher demonstrably works.

### Invoice distribution

- Amounts: log-normal, ₹18,000 to ₹14,00,000. Median around ₹1.8L. Real B2B invoices are not uniform.
- Terms: 82% on 0–30 day terms, matching the Recordent finding
- Aging: seed so the batch lands near a **73-day average collection period** (amount-weighted,
  DSO formula, measured from the **issue** date) — our headline stat. This is **NOT** a 73-day
  mean days-past-due, which is measured from the **due** date and is a different, much smaller
  number (~27 days on this batch). The two are routinely confused; do not "fix" the seed to make
  mean DPD equal 73.

  > **Why they cannot both be 73.** A 73-day mean DPD is arithmetically incompatible with the
  > aging-bucket distribution below. Pinning all four of the lower buckets at their exact upper
  > bounds gives 48×0 + 26×30 + 22×60 + 14×90 = 3360 DPD-days over 110 invoices — a mean of
  > 28.0 even if the 90+ bucket were zero. A mean of 73 over 120 invoices needs 8760 DPD-days,
  > leaving 5400 for the 10 invoices in the 90+ bucket: an average of **540 days past due**
  > (~18 months) each. Either the bucket distribution holds or mean DPD is 73; never both.

  Both figures are printed on every seed run, always labelled, so they cannot be mistaken for
  each other. Both come from `app/metrics.py` — the single source of truth for this formula,
  shared with `metrics_snapshots.dso_days` and (Phase 1) `GET /metrics`.
- Aging buckets: roughly 40% current, 22% in 0–30, 18% in 31–60, 12% in 61–90, 8% at 90+
- 6 invoices with partial payments already applied
- 4 invoices crossing the MSME Act 45-day threshold, on `is_msme = true` counterparties
- 8 rows with deliberate defects (missing due date, unparseable amount, ambiguous date format) so
  the repair queue has something in it

### History

Backdate 60 days of realistic history: sent messages, opens, clicks, three inbound replies
(one dispute, one Hinglish promise, one wrong-contact), two broken promises, four settled invoices.

**Include at least one Hinglish reply verbatim:** `"bhai next Tuesday tak clear kar dunga, GST invoice bhejo"`
This is a demo moment. It must be in the seed data, not typed live.

### Audit log

Seed audit entries for the backdated history, **including blocked actions**. At minimum:
- one blocked by `time_window`
- one blocked by `frequency_cap`
- one blocked by `stopping_rules` (3 broken promises)
- one blocked by `freshness` (invoice paid between planning and dispatch)

Without these, the "show me what it refused to do" demo has nothing to show. This is not optional.

---

## Seed script

The seed is a package at `api/app/seed/`, run as `python -m app.seed` so it imports `app.*`
without `sys.path` hacks. The Make targets wrap it:

```bash
make seed          # 120 invoices, 34 counterparties, 60 days history
make seed-reset    # truncate and reseed
make seed-demo     # the exact curated state for the pitch
```

**Determinism.** A fixed `RANDOM_SEED` fixes the *shape* — the same companies, amounts, and
relative day-offsets on every run — but every date is anchored to `today()` at seed time, not to
a pinned calendar date. Anchoring to a fixed `DEMO_DATE` would make the aging wrong on whatever
day you actually present; anchoring to `today()` keeps the batch correctly aged while `seed-demo`
still yields the same *logical* dataset every run. Running it minutes before the pitch cannot
surprise you. (If a later phase needs frozen wall-clock time, add an `ENV=demo` switch to
`app/clock.py` then — not now.)

Keep it under 30 seconds. You will run it more than you expect.

---

## What not to do

- Do not use `Faker` defaults for company names — American names in an Indian B2B demo are an
  instant credibility loss
- Do not make every invoice the same round amount
- Do not make the data too clean. Real AR data is messy; the repair queue is a feature, and an
  empty one looks like the feature does not exist
- Do not seed real GSTINs or real phone numbers. Use structurally valid but non-existent values,
  and phone numbers in the `+91-9999-9xxxxx` reserved-looking range
- Do not exceed 30 Razorpay test links across the demo
