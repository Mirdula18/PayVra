# PAYVRA

**Pay. Recover. Grow.**

An autonomous B2B receivables-recovery agent for Indian businesses, built on Razorpay.

Indian SMEs wait an average of 73 days to get paid, with several lakh crore rupees locked in
overdue invoices. The problem isn't visibility — every accounting system produces an aging
report. The problem is that recovery is manual, un-prioritised, emotionally awkward, and
disconnected from the act of actually collecting money.

PAYVRA closes that loop.

## What it does

1. **Ingests** a batch of open invoices (CSV, Google Sheet, Tally export, Zoho Books)
2. **Ranks** them by recoverable money — `P(collectable) x amount x urgency` — not by age
3. **Diagnoses** why each invoice is unpaid: forgotten, disputed, cash-crunched, wrong contact
4. **Executes** a bounded multi-channel dunning sequence, with a live Razorpay Payment Link
   embedded in every message
5. **Tracks** promises to pay and auto-follows-up when they break
6. **Escalates** within hard guardrails — RBI contact hours, consent checks, frequency caps,
   human approval above a value threshold
7. **Reconciles** automatically on webhook, cancelling all pending outreach the moment money lands
8. **Reports** rupees recovered, DSO reduction, and a complete audit trail of every action taken
   *and every action refused*

## Built for

Razorpay AI Buildathon — **Track 3: AI Revenue Recovery**

## Quick start — Docker

Nothing on the host but Docker. Postgres, the migrations and the API all run in containers.

```bash
cp .env.example .env        # fill in Razorpay test keys + one free LLM key
docker compose up -d --build --wait
docker compose run --rm api python -m app.seed      # 120 synthetic invoices, first run only
```

Then <http://localhost:8000/ui/login>. `--wait` blocks until the API answers a real request
through Postgres, so a successful exit means the whole path is up rather than that a socket is
listening.

```bash
docker compose logs -f api                          # follow the server
docker compose run --rm api python -m scripts.run_batch --report <run-id>
docker compose down                                 # stop; the data stays
```

> **Never `docker compose down -v`.** That deletes the `payvra_pgdata` volume, and on a machine
> holding demo state it destroys recovery runs that cannot be regenerated — a reseed produces a
> different book, and money already collected was collected against links that exist at Razorpay.

## Quick start — host `.venv`

Still supported, and what the test suite, `ruff` and `mypy` run against.

```bash
cp .env.example .env
make install
make db-up                  # Postgres in Docker + migrations on the host
make seed                   # 120 synthetic invoices
make dev                    # API on :8000, web on :5173
```

Both paths share one Postgres. `.env` points at `localhost:5433` for the host tools; compose
overrides `DATABASE_URL` to `db:5432` for the containers, because inside the compose network the
database is a service name rather than a published host port. Run one or the other — they both
want port 8000.

You need, at minimum:
- Razorpay **test mode** key id + secret + webhook secret
- One free LLM key: Groq or Google AI Studio (see `decisions/ADR-003-llm-provider.md`)
- A tunnel for webhooks: `cloudflared tunnel --url http://localhost:8000`

## Documentation

Start at [`CLAUDE.md`](./CLAUDE.md). It routes to everything else.

## Known limitations

Deliberate scope cuts, listed so they are not mistaken for oversights. Each is a conscious
trade made to get the recovery loop working end-to-end within a hackathon; none is load-bearing
for the parts of the system that are meant to be judged.

### Authentication is a placeholder

**The bearer token is the merchant's UUID.** There is no signing, no expiry, no user table, no
password, and no roles. `Authorization: Bearer <merchant-uuid>` is looked up directly against
`merchants.id` in `api/app/deps.py`. Anyone who knows or guesses a merchant id can act as that
merchant. **This must not be deployed anywhere real as-is.**

What *is* built and tested is the isolation **shape**, which is the part that would be expensive
to retrofit:

- `merchant_id` is resolved once, in a dependency, from the `Authorization` header
- **no endpoint accepts a merchant id from a path, query, or body parameter** — there is no code
  path that reads caller-supplied identity
- every query is scoped by the resolved merchant
- a cross-tenant resource returns **404, not 403** — "this exists but is not yours" leaks the
  existence of another tenant's data
- a token naming a merchant that does not exist fails closed with 401, rather than silently
  returning an empty result set

Ten tests cover this (`api/tests/test_batches_api.py`). Replacing the placeholder with real token
verification changes one function and nothing above it.

### Other cuts

| Limitation | Detail |
|---|---|
| LLM column mapping is a stub | `ingestion/mapper.llm_map_headers` returns `{}`. The rule dictionary covers Tally/Zoho/Busy exports; anything else is resolved by the merchant via `POST /batches/{id}/mapping`. Lands in Phase 5. |
| Historical DSO is synthetic | Only the latest `metrics_snapshots` row carries a computed collection period. The 13-day run-up is a ramp that terminates on the real figure — reconstructing true as-of-date DSO needs historical balances the seed does not model. |
| Audit log is tamper-*evident*, not tamper-*proof* | A superuser with `ALTER TABLE` can drop the rules and trigger. The hash chain makes that detectable, not preventable. See **Audit log** in `docs/glossary.md`. |
| Original upload files are not retained | `batch_rows` stores every parsed row instead. Invoice files are merchant PII and keeping them is a liability with no upside. |
| Razorpay test mode only | No real money moves. |

## Status

Hackathon MVP. Razorpay test mode only. No real money moves.
