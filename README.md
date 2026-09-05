# PAYVRA

**Pay. Recover. Grow.**

An autonomous B2B receivables-recovery agent for Indian SMEs, built on Razorpay rails.

Indian SMEs wait an average of 73 days to get paid, with several lakh crore rupees locked in
overdue invoices. The problem isn't visibility — every accounting system produces an aging report.
The problem is that recovery is manual, un-prioritised, emotionally awkward, and disconnected from
the act of actually collecting money.

PAYVRA closes that loop: it decides who to chase, drafts the message, attaches a live payment link,
refuses to send anything that breaks a rule, and reconciles the money when it arrives — writing
down every decision, including the ones it declined to act on.

Built for the **Razorpay AI Buildathon — Track 3: AI Revenue Recovery**.

---

## The bar, and the evidence

> *"Don't just identify the problem. Show measured money recovered across a batch, with compliant
> escalation, stopping rules, and an audit trail."*

Four clauses. Each has one artefact with real numbers behind it, produced by real runs against the
live Razorpay test API — not a passing test, not a module that exists.

| # | Clause | Evidence |
|---|---|---|
| 1 | **Measured money recovered** | **₹3,18,154** across a 20-account batch (run `c8e62a04`), collected through real Razorpay Payment Links and reconciled by signed webhooks. Plus **₹6,00,000** on a single ₹14L invoice, collected in tranches |
| 2 | **Compliant escalation** | Tone tiers 1 → 2 → 3 across attempts, with tier 3 refused by the gate pending human approval. The agent may soften on its own; it must ask permission to harden |
| 3 | **Stopping rules** | **59 gate refusals** spanning five of the seven rule families — value threshold (43), contact hours (25), stopping rules (12), frequency cap, freshness — each persisted with the check that stopped it and shown beside the sends, not on a separate tab |
| 4 | **Audit trail** | **390 hash-chained entries**, append-only at the database level, filterable to refusals. One real email delivery with the provider's message ID written back against the action |

Numbers are from the recorded demo database. `payvra-docs/requirements/track3-bar.md` maps each
clause to the code that produces it.

### The number we lead with is the smaller one

Recovery is reported two ways, and the headline is deliberately the more conservative:

- **Causal** — money received against invoices *this run acted on and the gate approved*. No upper
  time bound, because a counterparty pays hours or days after the run finishes.
- **Time-window** — everything that arrived while the run was open, regardless of cause.

Causal excludes accounts where the gate refused to make contact and the customer paid anyway. That
money is real and it is not claimed, because it wasn't earned. A collections figure that can't
survive *"how do you know your agent caused that?"* isn't worth more than a smaller one that can.

---

## Quick start

Nothing on the host but Docker — Postgres, the migrations and the API all run in containers.

```bash
cp .env.example .env        # Razorpay test keys + one free LLM key
docker compose up -d --build --wait
docker compose run --rm api python -m app.seed      # 120 synthetic invoices, first run only
```

Then open <http://localhost:8000/ui/login>. The token *is* the merchant UUID (see
[limitations](#authentication-is-a-placeholder)); the login page lists the seeded merchants, so
pick one from there.

`--wait` blocks until the API healthcheck fetches `/ui/login` — a real request through Postgres —
so a clean exit means the whole path works, not that a socket is listening. About 45 seconds cold.

```bash
docker compose ps                                   # both containers healthy?
docker compose logs -f api                          # follow the server
docker compose run --rm api python -m scripts.run_batch --report <run-id>
docker compose down                                 # stop; the data stays
```

> [!WARNING]
> **Never `docker compose down -v`.** That deletes the `payvra_pgdata` volume. On a machine holding
> demo state it destroys recovery runs that cannot be regenerated — a reseed builds a *different*
> book, and money already collected was collected against links that exist at Razorpay.

<details>
<summary><b>Host <code>.venv</code> alternative</b> — what the test suite, ruff and mypy run against</summary>

```bash
cp .env.example .env
make install
make db-up                  # Postgres in Docker + migrations on the host
make seed
make dev                    # API on :8000
```

Both paths share one Postgres. `.env` points at `localhost:5433` for host tools; compose overrides
`DATABASE_URL` to `db:5432` for containers, because inside the compose network the database is a
service name rather than a published host port. Run one or the other — they both want port 8000.

`make help` lists every target.
</details>

### What you need

| | |
|---|---|
| **Razorpay** | Test-mode key id, key secret, webhook secret. No real money moves |
| **LLM** *(optional)* | One free key — Groq or Google AI Studio. `LLM_ENABLED=false` runs the entire pipeline on deterministic policy and hand-written templates, which is how CI runs |
| **Tunnel** *(for webhooks)* | `cloudflared tunnel --url http://localhost:8000`, registered in the Razorpay dashboard |
| **Email** *(optional)* | A Resend key. Sending is disabled entirely unless `RESEND_TO_OVERRIDE` is set — the default state of the system is "cannot email anyone" |

---

## See it

Three server-rendered screens, at `/ui`:

| Screen | What it shows |
|---|---|
| **`/ui/worklist`** | Open receivables ranked by *recoverable money* — `P(collect) × amount × urgency` — not by age. Every row carries a plain-English reason for its position, so a ten-day-old invoice outranking a 128-day-old one is explainable rather than mysterious |
| **`/ui/audit`** | Every action proposed and what happened to it. Refusals and sends in **one list, not two tabs** — separating them would let a reader take only the flattering half. Filter chips for gate refusals, in-run refusals, approvals and executions; a chain column showing each entry hashed over its predecessor |
| **`/ui/recovery`** | Causal and time-window figures for one run, side by side, with the divergence explained on screen |

Every label, badge, status and control on those screens is explained in plain language in
[**`payvra-docs/ui-guide.md`**](payvra-docs/ui-guide.md).

---

## How it works

```
 invoices ──► score ──► worklist ──► diagnose ──► propose ──► GATE ──► send ──► reconcile
                │                        │           │          │        │          │
             ADR-008                   cause     one of 9    7 checks  Resend   Razorpay
            explainable               inference   tools     (ADR-005)   email    webhook
              rules                    (LLM)      (LLM)    deterministic         (signed)
                                                                │
                                                                ▼
                                                    hash-chained audit log
                                                  (every verdict, pass or fail)
```

### The LLM proposes. Deterministic code disposes.

The model's entire surface is one structured object per invoice: an action from a **closed list of
nine tools**, a tone tier, and a one-line rationale. It has no API access. It cannot create a
payment link, and it cannot send anything.

Everything after that is deterministic Python — is the tool on the list, is it legal from this
invoice's current state, is the JSON well-formed. If any of those fail, the proposal is discarded,
a rules-based policy decides instead, and **the rejection is logged**, so the model can be seen
being overruled. The ranking, the scoring, the gate and the reconciliation contain no model at all.

That ratio is deliberate. An LLM call you can replace with an `if` statement is a liability.

> The system runs end to end with `LLM_ENABLED=false` and with the `litellm` package uninstalled.
> CI proves both.

### The gate — seven ordered checks

Every outbound action passes through `guardrails/gate.py`. **There is no bypass path and no skip
flag.** Every check runs even after an earlier one fails — no short-circuit, no "warn and continue"
— and the full verdict is written to the audit log whether it passed or not.

| # | Check | Refuses when |
|---|---|---|
| 1 | `time_window` | Outside 08:00–19:00 IST (RBI recovery-conduct norms) |
| 2 | `freshness` | The invoice settled between planning and dispatch |
| 3 | `consent` | No consent on file for this channel, or opt-out recorded |
| 4 | `frequency_cap` | This counterparty has been contacted too recently |
| 5 | `value_threshold` | Above the amount an agent may act on without a human |
| 6 | `content_policy` | The draft fails validation — missing opt-out, wrong tier, unsafe content |
| 7 | `stopping_rules` | Disputed, on the exception list, or out of touch budget |

The contact-hours window can be widened by environment variable for an out-of-hours demo — but the
check still executes against the widened value, and **the override itself is written into the audit
log**. Compliant *by record*, not by assertion. A gate with a bypass is not a gate.

### The audit log under-claims, deliberately

`executed` means a message left the building — a confirmed provider acceptance with a message ID
written back against the action. A failed send stays `approved` and claims nothing. The contact
counter moves only on confirmed delivery, because that counter enforces the frequency cap:
inflating it with messages nobody received would suppress real outreach later on the strength of a
fiction.

The log may under-claim. It must never over-claim. That's the one thing an audit trail cannot be
wrong about.

Entries are hash-chained and append-only at the database level — `DELETE` and `UPDATE` are no-ops
by rule, `TRUNCATE` is blocked by trigger.

### Payment links above the ceiling

Razorpay caps a single Payment Link at roughly ₹5L, and the highest-value receivables are exactly
the ones worth chasing. Rather than curating the demo data beneath the limit, links are **capped at
the ceiling with `accept_partial` and collected in tranches** (ADR-006).

Proven live: a ₹14,00,000 invoice, link created at ₹5,00,000, paid ₹1,00,000 then ₹5,00,000,
reconciled through `payment_link.partially_paid` then `payment_link.paid`. ₹6,00,000 recovered,
₹8,00,000 still open, invoice correctly **not** closed — because the figure counts money received,
not invoices closed.

---

## Repo layout

```
api/app/
  agent/          batch runner, tool registry, diagnosis, proposal, run-scoped metrics
  guardrails/     the seven-check gate — the only sanctioned path to sending
  reconciliation/ settlement, revocation on payment, manual/offline reconciliation
  razorpay/       Payment Links client, webhook signature verification
  delivery/       gated sender + Resend email transport
  generation/     LLM abstraction (LiteLLM) and deterministic message templates
  scoring/        explainable weighted ranking (ADR-008)
  ingestion/      CSV/XLSX parsing, header mapping, counterparty matching
  ui/             server-rendered screens (Jinja2)
  routers/        REST API — batches, invoices, worklist, webhooks
  models/         SQLAlchemy 2.0 mappings
api/tests/        563 tests
api/scripts/      run_batch, verify_razorpay, verify_llm, inspect_webhook, create_demo_link
payvra-docs/      requirements, architecture, ADRs, runbooks
```

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · PostgreSQL 16 · Pydantic v2 ·
Jinja2 · APScheduler · LiteLLM · pytest · ruff · mypy

---

## Testing

```bash
make test          # pytest       — 562 passed, 1 skipped
make lint          # ruff check
make typecheck     # mypy -p app  — strict on agent/, guardrails/, reconciliation/
```

CI runs lint, typecheck and the full suite on 3.12, plus **a second job that installs without
`litellm`** and runs the generation tests on templates alone — because ADR-003 requires the app to
work with the LLM package absent, and a claim like that is worthless unless something checks it.

The suite never touches the network. An autouse fixture clears the Resend credentials for every
test, so an unstubbed send raises rather than delivering; tests that need a send opt in explicitly
and stub the transport. Autouse and opt-in are that way round because the cost of forgetting to opt
*out* is a message in a stranger's inbox.

---

## Decisions

Each ADR records the alternatives and why they lost.

| | |
|---|---|
| [ADR-001](payvra-docs/decisions/ADR-001-architecture-style.md) | Guardrailed agent loop, not a free-roaming agent |
| [ADR-002](payvra-docs/decisions/ADR-002-tech-stack.md) | Python/FastAPI backend, React frontend |
| [ADR-003](payvra-docs/decisions/ADR-003-llm-provider.md) | LiteLLM over Groq + Gemini free tiers, no GPU |
| [ADR-004](payvra-docs/decisions/ADR-004-agent-framework.md) | LangGraph over raw tool-calling or CrewAI — *execution split superseded by ADR-009; the node structure stands, and the shipped runner is synchronous Python with no LangGraph dependency* |
| [ADR-005](payvra-docs/decisions/ADR-005-guardrails-and-compliance.md) | Deterministic gate, seven ordered checks |
| [ADR-006](payvra-docs/decisions/ADR-006-razorpay-integration.md) | Payment Links as primary rail; REST over MCP |
| [ADR-007](payvra-docs/decisions/ADR-007-database-and-queue.md) | Postgres + APScheduler, not Celery |
| [ADR-008](payvra-docs/decisions/ADR-008-scoring-engine.md) | Explainable weighted rules over a trained model |
| [ADR-009](payvra-docs/decisions/ADR-009-batch-runner-and-run-scoped-recovery.md) | Synchronous batch runner, run-scoped recovery measurement |

Full documentation index: [`payvra-docs/CLAUDE.md`](payvra-docs/CLAUDE.md). Requirements are in
[`payvra-docs/requirements/`](payvra-docs/requirements/); the demo runbook and script are in
[`payvra-docs/runbooks/`](payvra-docs/runbooks/) and [`payvra-docs/prompts/`](payvra-docs/prompts/).

---

## Known limitations

Deliberate scope cuts, listed so they are not mistaken for oversights. None is load-bearing for the
parts of the system meant to be judged.

### Authentication is a placeholder

**The bearer token is the merchant's UUID.** No signing, no expiry, no user table, no password, no
roles. `Authorization: Bearer <merchant-uuid>` is looked up directly against `merchants.id`. Anyone
who knows or guesses a merchant id can act as that merchant. **This must not be deployed anywhere
real as-is.**

What *is* built and tested is the isolation **shape**, which is the expensive part to retrofit:

- `merchant_id` is resolved once, in a dependency, from the `Authorization` header
- **no endpoint accepts a merchant id from a path, query, or body parameter** — there is no code
  path that reads caller-supplied identity
- every query is scoped by the resolved merchant
- a cross-tenant resource returns **404, not 403** — *"this exists but is not yours"* leaks the
  existence of another tenant's data
- an unknown merchant fails closed with 401 rather than returning an empty result set

Replacing the placeholder with real token verification changes one function and nothing above it.

### Other cuts

| Limitation | Detail |
|---|---|
| **React frontend is a scaffold** | `web/` is a Phase 0 placeholder that renders a splash screen and confirms the toolchain. The real UI is the server-rendered Jinja screens at `/ui` |
| **Email only** | SMS and WhatsApp are refused explicitly by the sender rather than silently dropped, which is why the log has never claimed one |
| **Reply handling is unbuilt** | Inbound replies and promise-break follow-up are designed but not implemented. No judged clause depends on them |
| **LLM column mapping is a stub** | The rule dictionary covers Tally/Zoho/Busy exports; anything else is resolved by the merchant via `POST /batches/{id}/mapping` |
| **Historical DSO is synthetic** | Only the latest `metrics_snapshots` row carries a computed collection period. Reconstructing true as-of-date DSO needs historical balances the seed does not model |
| **Audit log is tamper-*evident*, not tamper-*proof*** | A superuser with `ALTER TABLE` can drop the rules and trigger. The hash chain makes that detectable, not preventable |
| **Original uploads are not retained** | `batch_rows` stores every parsed row instead. Invoice files are merchant PII and keeping them is a liability with no upside |
| **`gate.*` audit entries are not run-scoped** | The gate writes its own entry and has no knowledge of runs, so filtering the audit screen to one run hides the per-check verdicts. Nothing displayed is wrong; the filter is narrower than it looks |
| **Razorpay test mode only** | No real money moves |

---

## Status

Hackathon MVP, Razorpay test mode only. The recovery loop runs end to end and has been exercised
against live Razorpay Payment Links, real signed webhooks, real LLM providers, and real email
delivery.
