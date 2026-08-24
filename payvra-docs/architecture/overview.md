# Architecture Overview

## The shape of the system

PAYVRA is a **guardrailed agent loop**, not a chatbot and not a CRUD app with AI sprinkled on top.

The single most important structural decision: **the LLM never executes a money action.**
It proposes a structured action. A deterministic policy engine validates it against the tool
registry, the state machine, and the guardrail gate. Only then does a tool run.

That separation is what produces the audit trail Track 3 demands, and it is the answer to the
question every judge will ask: *"what stops it from doing something stupid?"*

## Layers

```
L9  Presentation      React dashboard, worklist, timeline, audit viewer
L8  Reconciliation    Webhook receiver, signature verify, idempotent settle
L7  Delivery          Email / SMS / WhatsApp, receipt ingestion
L6  Payment rail      Razorpay Payment Links, Invoices, Smart Collect
L5  Generation        LLM drafting, schema validation, template fallback
L4  Guardrails        Deterministic gate wrapping every tool call
L3  Orchestrator      LangGraph state machine: observe -> diagnose -> plan -> validate -> act
L2  Risk engine       Aging, collectability scoring, ranked worklist
L1  State             Postgres + in-process scheduler
L0  Ingestion         CSV / Sheets / Tally / Zoho -> canonical Invoice
```

## The recovery loop

```
   Ingest  ->  Score  ->  Decide  ->  Guardrails
                                          |
                                          v
   Report  <-  Reconcile  <-  Collect  <-  Send
      |
      +-- feeds back into the next cycle
```

Eight stages. The merchant touches two of them (review plan, approve escalations).
Everything else runs unattended.

## Request paths

There are exactly three ways work enters the system.

### 1. Human action (synchronous, HTTP)
Upload a batch, approve an escalation, edit a message, pause. FastAPI handles these directly.
No LLM calls in a request-response path — ever. If a human action needs generation, it enqueues.

### 2. Scheduled job (asynchronous, APScheduler)
The bulk of the work. Nightly planning, dispatch windows, promise sweeps, link hygiene.
See `architecture/components.md` for the full schedule.

### 3. Inbound event (asynchronous, webhook)
Razorpay payment events and inbound message replies. Handler verifies, acknowledges in
under 200 ms, and enqueues processing. Never process inline — Razorpay retries on slow responses
and you will get duplicate deliveries.

## Data flow: one invoice, end to end

```
CSV row
  -> normalize to Invoice, resolve counterparty, check consent
  -> nightly: compute days_past_due, aging bucket, exposure
  -> nightly: score collectability, rank into worklist
  -> nightly: agent observes state + signals, diagnoses cause,
              proposes {action, channel, tone_tier, rationale}
  -> validate against tool registry + allowed state transitions
  -> queue as a pending Action with scheduled_for timestamp
  -> dispatch window: guardrail gate runs 7 checks in order
       any failure -> log verdict, halt, requeue or stop
  -> generate message (LLM), validate schema + content policy
       two failures -> deterministic template
  -> create Razorpay Payment Link (idempotency key, reference_id = invoice_number)
  -> send via channel, record Action as executed
  -> customer opens / replies / pays
  -> inbound reply -> classify -> PTP / dispute / wrong contact / refusal
  -> webhook payment_link.paid -> verify HMAC -> dedupe -> settle
       -> REVOKE all scheduled jobs for this invoice
       -> close open promise, emit recovered event
  -> dashboard aggregates, audit log records everything
```

## Trust boundaries

| Boundary | What crosses | Control |
|---|---|---|
| Merchant -> API | Invoice data, PII, approvals | Auth, tenant scoping on `merchant_id` |
| API -> LLM provider | Invoice facts, counterparty name, history | No card data. PII minimised. Provider has no-training policy where available. |
| API -> Razorpay | Amount, reference_id, customer contact | Idempotency keys, test mode only |
| Razorpay -> API | Payment events | HMAC-SHA256 over raw body, replay-safe |
| API -> Counterparty | Generated message + payment link | Guardrail gate, content policy, opt-out |

## What is deliberately NOT in the architecture

- **No GPU, no self-hosted model.** Every model call is a hosted inference API call. The backend
  runs in 1 vCPU / 512 MB. See `decisions/ADR-003-llm-provider.md`.
- **No card data path.** Razorpay hosts checkout. This keeps us out of PCI-DSS scope entirely.
- **No Celery for MVP.** APScheduler in-process. See `decisions/ADR-007-database-and-queue.md`.
- **No vector database for MVP.** Under ~10k records, a numpy dot product beats operational overhead.
- **No chat interface.** There is no conversational surface in this product.
- **No free-roaming agent.** The tool registry is closed and the state machine constrains transitions.

## Failure modes and responses

| Failure | Response |
|---|---|
| LLM rate-limited (429) | Backoff with jitter, 3 retries, then deterministic template |
| LLM returns invalid schema | Retry once with a repair prompt, then template |
| LLM proposes an unknown tool | Reject, run deterministic fallback policy, log it |
| Razorpay API down | Backoff, circuit-break after 5 failures, requeue action |
| Payment link expired | `link_hygiene` job regenerates if still unpaid |
| Duplicate webhook delivery | Dedupe on `event.id`, processing is a no-op |
| Payment lands while message queued | Freshness check at gate step 2 aborts the send |
| Email bounces | Contact marked stale, channel switched, AP contact requested |
| Unhandled exception in send path | Fail **closed** — nothing sends, alert raised |
| Everything is broken | Global pause; merchant retains full manual control |

## Deployment topology

```
Vercel            React SPA
   |
   v
Render/Railway    FastAPI + APScheduler (single process)
   |                    |
   v                    v
Neon Postgres      Groq / Gemini (HTTPS)
                   Razorpay API (HTTPS)
                   Resend / MSG91 (HTTPS)

Cloudflare Tunnel -> localhost:8000  (webhooks, local dev only)
```

Single process for MVP. The scheduler runs in-process with the API. This is a deliberate
simplification for the hackathon and is documented as a known scaling limit in ADR-007.
