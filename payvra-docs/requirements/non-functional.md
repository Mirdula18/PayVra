# Non-Functional Requirements

---

## NFR-1 — Performance

| ID | Requirement | Target |
|---|---|---|
| NFR-1.1 | Ingest and normalise a 500-invoice batch | < 45 s |
| NFR-1.2 | Rescore a full worklist of 1,000 invoices | < 10 s |
| NFR-1.3 | Worklist API response | < 500 ms p95 |
| NFR-1.4 | Webhook acknowledgement (ack fast, process async) | < 200 ms |
| NFR-1.5 | Dashboard first meaningful paint | < 2 s |
| NFR-1.6 | Message generation per invoice (LLM round-trip) | < 4 s |

**Design note:** webhook handlers must return `200` immediately and enqueue processing.
Razorpay retries on non-2xx; slow handlers cause duplicate deliveries.

## NFR-2 — LLM constraints

| ID | Requirement |
|---|---|
| NFR-2.1 | Every LLM call is a hosted inference API call. **No GPU anywhere in the stack.** |
| NFR-2.2 | Respect provider rate limits: Groq ~30 RPM, Gemini free tier per-project (check AI Studio) |
| NFR-2.3 | Exponential backoff with jitter on 429; max 3 retries |
| NFR-2.4 | Circuit breaker: after 2 consecutive failures for a call type, switch to deterministic template for 5 minutes |
| NFR-2.5 | Every LLM output is schema-validated before use; unvalidated output is never sent |
| NFR-2.6 | Cache generated messages; never regenerate identical content |
| NFR-2.7 | Batch generation runs offline (scheduled), never in a request-response path |

**Demo note:** a 100-invoice batch at 3 touches each is ~300 generation calls. At 30 RPM that is
10+ minutes. Pre-compute batches; make only 3–5 calls live. See `prompts/demo-script.md`.

## NFR-3 — Reliability

| ID | Requirement |
|---|---|
| NFR-3.1 | All Razorpay writes carry an idempotency key |
| NFR-3.2 | Webhook processing dedupes on `event.id`; replaying an event is a no-op |
| NFR-3.3 | Scheduled jobs are idempotent; a double-run must not double-send |
| NFR-3.4 | Razorpay API failures retry with backoff; circuit-break after 5 consecutive failures |
| NFR-3.5 | Any unhandled exception in the send path fails **closed** — no message goes out |
| NFR-3.6 | Race condition: payment lands while a message is queued → freshness check (FR-7.2) catches it |

## NFR-4 — Security

| ID | Requirement |
|---|---|
| NFR-4.1 | **No card data, ever.** No PAN, CVV, expiry, or token stored or logged. Keeps us out of PCI-DSS scope. |
| NFR-4.2 | Webhook signature verified via HMAC-SHA256 over the **raw** body, before JSON parsing |
| NFR-4.3 | Secrets in environment variables only; `.env` gitignored; separate test/live key pairs |
| NFR-4.4 | Razorpay live keys are never used in development or demo |
| NFR-4.5 | Row-level tenant isolation on `merchant_id`; every query scoped |
| NFR-4.6 | PII (phone, email, GSTIN) never written to application logs; redact at the logger |
| NFR-4.7 | TLS everywhere; webhook endpoint HTTPS-only |
| NFR-4.8 | Rate-limit the public API |

## NFR-5 — Compliance

| ID | Requirement | Basis |
|---|---|---|
| NFR-5.1 | Outbound contact restricted to 08:00–19:00 IST | RBI recovery-conduct norms |
| NFR-5.2 | No contact-list scraping; only contacts the merchant supplied | RBI |
| NFR-5.3 | No shaming, threats, or disclosure to third parties | RBI |
| NFR-5.4 | All interactions digitally recorded and retrievable | RBI |
| NFR-5.5 | Consent basis recorded per counterparty before first contact | DPDP |
| NFR-5.6 | Purpose limitation — contact data used only for payment collection | DPDP |
| NFR-5.7 | Opt-out honoured immediately and permanently | DPDP |
| NFR-5.8 | Retention policy with a defined erasure path | DPDP |
| NFR-5.9 | MSME Act 45-day threshold flagged and available to tone selection | MSMED Act |

DPDP context: Rules notified 13 Nov 2025 (G.S.R. 846(E)). Enforcement machinery from 13 Nov 2026;
full substantive compliance (Sections 3–10) from 13 May 2027. Max penalty ₹250 crore per instance.

## NFR-6 — Auditability

| ID | Requirement |
|---|---|
| NFR-6.1 | `audit_log` is append-only; no UPDATE or DELETE, enforced at the DB layer |
| NFR-6.2 | Each entry hash-chains to its predecessor (`prev_hash`), making tampering detectable |
| NFR-6.3 | Every entry records: actor (agent/human/system), action, inputs, rationale, gate verdicts, outcome, timestamp |
| NFR-6.4 | **Refused actions are logged with equal fidelity to executed ones** |
| NFR-6.5 | Audit log is queryable by invoice, counterparty, action type, verdict, and date range |
| NFR-6.6 | Retention: minimum 7 years for financial records (assumption — verify for production) |

## NFR-7 — Observability

| ID | Requirement |
|---|---|
| NFR-7.1 | Structured JSON logs with correlation IDs across the agent loop |
| NFR-7.2 | Every LLM call traced: prompt, model, tokens, latency, cost, validation outcome |
| NFR-7.3 | Metrics: messages sent, gate blocks by reason, ₹ recovered, LLM error rate, webhook lag |
| NFR-7.4 | Health endpoint reporting DB, Redis, Razorpay, and LLM provider reachability |

## NFR-8 — Cost

| ID | Requirement |
|---|---|
| NFR-8.1 | Entire hackathon build runs on free tiers: Neon, Render/Railway, Vercel, Groq, Gemini |
| NFR-8.2 | Zero GPU spend |
| NFR-8.3 | Backend fits in 1 vCPU / 512 MB |
| NFR-8.4 | Target LLM cost < $1 per full 100-invoice demo run |

## NFR-9 — Usability

| ID | Requirement |
|---|---|
| NFR-9.1 | Setup to first ranked worklist: under 10 minutes |
| NFR-9.2 | Daily use: under 3 minutes for a merchant with 300 open invoices |
| NFR-9.3 | Every automated decision is explainable in one plain sentence, no jargon |
| NFR-9.4 | Nothing irreversible happens without an explicit human action |
| NFR-9.5 | Global pause reachable from every screen |

## NFR-10 — Constraints and known limits

| ID | Constraint |
|---|---|
| NFR-10.1 | Razorpay test mode caps standard Payment Links at 30 per business |
| NFR-10.2 | The dedicated UPI payment link (`upi_link: true`) is **live mode only** — demo standard links, which still offer UPI at checkout |
| NFR-10.3 | GST-compliant invoices cannot be created via the Razorpay Invoices API |
| NFR-10.4 | WhatsApp Business API requires Meta approval — use sandbox or simulated send for MVP |
| NFR-10.5 | Payout Links require IP allowlisting (RazorpayX is P2 anyway) |
| NFR-10.6 | Free LLM rate limits are org-level; extra API keys do not multiply quota |
