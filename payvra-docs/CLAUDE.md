# CLAUDE.md — PAYVRA

**Read this file first. Every session. Before touching any code.**

---

## What we are building

PAYVRA is an autonomous B2B receivables-recovery agent built on Razorpay's payment rails.
Tagline: **Pay. Recover. Grow.**

Indian SMEs wait ~73 days to get paid. Existing tools tell you *what* is overdue. PAYVRA
*recovers* it: it ranks invoices by recoverable money, diagnoses why each one is unpaid,
runs a guardrailed multi-channel dunning sequence with a live Razorpay Payment Link in every
message, tracks promises to pay, and reconciles the instant a webhook confirms payment.

Built for **Razorpay AI Buildathon, Track 3 — AI Revenue Recovery**.
The judging bar, verbatim: *"Don't just identify the problem. Show measured money recovered
across a batch, with compliant escalation, stopping rules, and an audit trail."*

Every design decision traces back to that sentence.

---

## The bar is a standing constraint

**Read `requirements/track3-bar.md` before planning any work.** It breaks the sentence above into
four clauses, names the artefact that proves each one to a judge, and tracks current evidence
status. It is the acceptance checklist, and it outranks priorities everywhere else in the repo.

**The rule: new work must map to one of the four clauses, or it is deferred.**

| # | Clause | Status at `a9cf753` |
|---|---|---|
| 1 | Measured money recovered | 🔴 zero from an agent run |
| 2 | Compliant escalation | 🔴 never executed |
| 3 | Stopping rules | 🟡 built and tested, never run for real |
| 4 | Audit trail | 🟡 built and tested, never run for real |

Before starting anything, answer: **which clause does this move, and from what to what?** If there
is no answer, it is post-submission — including things marked P0 in `requirements/functional.md`,
several of which are now deferred for exactly this reason (FR-11, FR-12, most of FR-14 and FR-15).

**The structural fact driving everything:** guardrails (Phase 3), the payment rail (Phase 4) and
message generation (Phase 5) are all complete and independently verified against live services —
and **none of them has a production caller.** Only scripts and the test suite invoke them. All four
clauses unlock from the same missing piece: the Phase 6 batch runner.

Two issues affecting clause 1:

* **The Razorpay amount ceiling** — links above roughly ₹5L are refused, and the top three worklist
  invoices are all above it. **Resolved:** ADR-006 option C — cap each link at the ceiling with
  `accept_partial` and collect in tranches, which also puts the built-but-unused FR-13.4 partial
  reconciliation path on screen. Implementation is FR-9.8/FR-9.9. **Consequence: recovery is
  measured in rupees received, not invoices settled** (FR-17).
* **The contact-hours gate** — refuses every outbound action outside 08:00–19:00 IST, so an
  out-of-window run recovers ₹0. Demo constraint. `runbooks/demo-rehearsal.md`.

---

## Non-negotiable invariants

Violating any of these is a bug, no matter how well the code works.

1. **The LLM never executes a money action directly.** It *proposes* a structured action.
   A deterministic policy engine validates it. Only then does a tool run. No exceptions.
2. **Every action is gated before execution** by `guardrails.gate()`. Every gate decision —
   pass or fail — is written to `audit_log`. Refusals are as important as sends.
3. **Nothing sends outside 08:00–19:00 IST.** Hard-coded window, configurable ceiling only.
4. **Re-read invoice status immediately before sending.** If it was paid in the interim, abort.
   Chasing someone who already paid is the single worst failure mode this product has.
5. **All Razorpay writes carry an idempotency key.** All webhook handling dedupes on the `x-razorpay-event-id` header (the body has no id).
6. **Webhook signature is verified over the RAW request body**, before any JSON parsing.
7. **No card data ever touches our servers.** This keeps us out of PCI-DSS scope. Never add
   a field that would store a PAN, CVV, or expiry.
8. **Stopping rules are absolute.** Settled, disputed, opted out, 3 broken promises, or touch
   cap reached → permanent stop, move to exception list, never contact again.
9. **The demo must survive an LLM outage.** Every generation path has a deterministic template
   fallback. Test this path deliberately.

---

## Where to find things

| I need to know... | Read |
|---|---|
| **What we are judged on, and what still lacks evidence** | **`requirements/track3-bar.md`** |
| **What Phase 6 builds** | **`architecture/agent-loop.md` → "Phase 6 deliverable"**, FR-16/FR-17, ADR-009 |
| **What could sink the demo** | **`runbooks/demo-rehearsal.md`**, ADR-006 blocker |
| Why this product exists, who it's for | `docs/vision.md` |
| What a word means (DSO, PTP, dunning, VA) | `docs/glossary.md` |
| What must be built | `requirements/functional.md` |
| Performance, security, compliance limits | `requirements/non-functional.md` |
| Concrete user flows | `requirements/user-stories.md` |
| How the system fits together | `architecture/overview.md` |
| What each service does | `architecture/components.md` |
| Tables, columns, enums, states | `architecture/data-model.md` |
| REST endpoints and payloads | `architecture/api-contracts.md` |
| The agent state machine and tool registry | `architecture/agent-loop.md` |
| Why we chose X over Y | `decisions/ADR-*.md` |
| What to do when working on a specific area | `agents/*.md` |
| How to write code here | `prompts/coding-standards.md` |
| LLM prompt templates | `prompts/llm-prompts.md` |
| The 5-minute pitch | `prompts/demo-script.md` |

---

## Stack (locked — see ADR-002)

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic
- **DB:** PostgreSQL 16 (Neon free tier)
- **Scheduler:** APScheduler in-process (ADR-007 — deliberately NOT Celery for MVP)
- **Agent:** LangGraph state machine
- **LLM:** LiteLLM abstraction → Groq (classification) + Google Gemini (drafting), OpenRouter fallback
- **Payments:** Razorpay REST API, test mode
- **Frontend:** React 18, Vite, TypeScript, Tailwind, shadcn/ui, TanStack Query, Recharts
- **Deploy:** Render/Railway (API), Vercel (web), Neon (DB)

---

## Build order

Do not build out of order. Each phase must run end-to-end before the next starts.

| Phase | Deliverable | Done when | Clauses |
|---|---|---|---|
| 0 ✅ | Repo skeleton, DB schema, migrations, seed script | `make seed` loads 120 synthetic invoices | — |
| 1 ✅ | Ingest + normalize + aging | Upload CSV → invoices in DB with correct `days_past_due` | — |
| 2 ✅ | Scoring engine + worklist API | `GET /worklist` returns a sensibly ranked list | context for 1, 2 |
| 3 ✅ | Guardrail engine + audit log | Unit tests prove every gate blocks correctly | 3, 4 |
| 4 ✅ | Razorpay Payment Links + webhook + reconciliation | Pay a test link → invoice auto-settles | 1 |
| 5 ✅ | LLM drafting + template fallback | Message generated, validated, fallback proven | 2 |
| **6** | **The batch runner** — one synchronous pass over the ranked worklist | **A run produces real recovery, real escalation, real refusals — all carrying one `recovery_run_id`** | **all four** |
| 7 | ~~Reply classification + PTP tracking~~ | **POST-SUBMISSION** — no clause depends on it | none |
| 8 | Three screens: ranked worklist, recovered figure, audit log | Each of the four clauses has an artefact on screen | 1, 3, 4 |
| 9 | Demo data tuning + rehearsal | 5-min run-through with no dead air | all four |

**Phases 3 and 4 are the ones judges will probe. Do not rush them.**
Both are now built and live-verified — what they lack is evidence from a real run, which is Phase 6.

**Phase 6 is the whole game.** It is the production caller Phases 3, 4 and 5 do not have, and every
clause of the bar unlocks from it. Its spec is the "Phase 6 deliverable" section of
`architecture/agent-loop.md`, with FR-16 and FR-17, under ADR-009.

* One synchronous pass: `diagnose → propose one action → gate → execute or persist the refusal`
* Every run carries a `recovery_run_id` (**not** `batch_id` — `batches` already means an uploaded
  invoice file)
* Escalation is a per-invoice attempt counter (1/2/3 → tone tier) and nothing more; Phase 3 already
  decides whether attempt N may fire
* **Explicit non-goals: scheduler, async queue, retry layer.** Deferred, not rejected

**Phase 7 is deferred whole**, and FR-11.3 — free-text and vernacular date extraction from replies
— is **removed from scope**, not merely postponed. This does not touch Hinglish message
*generation* (FR-8.5), which is built, live-verified, and stays.

---

## Working agreements

- **Ask before inventing.** If a requirement is ambiguous, check `requirements/` first, then ask.
  Do not silently pick an interpretation.
- **Write the test for guardrails and reconciliation.** Everything else can be manually verified;
  these two cannot.
- **Small commits, conventional format:** `feat(agent): add promise-to-pay extraction`
- **Never commit secrets.** `.env` is gitignored. `.env.example` lists every required key with
  a dummy value.
- **Update the ADR** if you change a locked decision. Don't just change the code.
- **Keep the seed data realistic.** Judges will look at it. Real-sounding Indian company names,
  plausible amounts in lakhs, believable payment histories.

## Things that will lose us the hackathon

- A dashboard that only *shows* overdue invoices. Track 3 explicitly penalises detection-only tools.
- A chatbot wrapper. There is no chat interface in this product.
- Bolted-on AI. Every LLM call must be justified in `architecture/agent-loop.md`.
- An agent that can't explain why it did something.
- A demo that hangs on a rate limit. Pre-compute the batch. See `prompts/demo-script.md`.
