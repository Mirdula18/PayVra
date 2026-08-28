# ADR-006 — Payment Links as primary rail; REST over MCP

**Status:** Accepted
**Date:** 2026-08-23

## Context

PAYVRA's core differentiator is that the payment rail lives *inside* the recovery loop. Competitors
(Kapittx, Growfin, CredFlow, Recordent) chase and then hand off; we chase, collect, and reconcile
in one system. The choice of Razorpay product determines how well that works.

## Decision

**Primary rail: Razorpay Payment Links** (`POST /v1/payment_links`), with `reference_id` set to the
merchant's invoice number.

**Integration method: direct REST API calls** from our tool functions, not the Razorpay MCP server.

**Test mode only** for the entire hackathon.

Roadmap (P1): Smart Collect virtual accounts for large NEFT/RTGS payments; Invoices API for
merchants without their own invoice numbering.

## Rationale

**Why Payment Links:**
- Works in test mode, so the whole loop is demoable
- Hosted checkout means no card data touches us — PCI-DSS scope avoided entirely
- `reference_id` carries our invoice number through to the webhook, making reconciliation a
  single indexed lookup rather than a matching problem
- Supports `accept_partial`, which the `cash_crunch` instalment intervention needs
- Supports `expire_by`, which creates the link-hygiene lifecycle
- One URL drops cleanly into email, SMS, and WhatsApp alike

**Why REST over MCP:** the Razorpay MCP server is a legitimate and elegant option, and mirrors how
Razorpay's own agentic stack works. But for a hackathon it adds a moving part between the agent and
the money, and our architecture (ADR-001) deliberately does *not* let the LLM call payment tools
directly — which is most of MCP's value. Direct REST from a Python tool function is fewer failure
modes on stage. Mention MCP as a roadmap item in the pitch; do not depend on it for the demo.

## The payment link amount ceiling — RESOLVED: collect in tranches

**Status:** decided 2026-08-26. **Decision: option C** — cap each link at the ceiling and set
`accept_partial`, collecting the balance across successive links. **Implemented in:** Phase 4
(ceiling check) and Phase 6 (the runner creating tranche links).

Razorpay refuses payment links above a maximum amount. Measured against the live test account on
2026-08-26:

| Amount | Result |
|---|---|
| ₹23,134 | ✅ created, paid, settled end to end |
| ₹5,00,000 | ✅ created |
| ₹14,00,000 | ❌ `400 BAD_REQUEST_ERROR: amount exceeds maximum amount allowed` |

The exact ceiling sits between ₹5L and ₹14L and was not bisected further, because every additional
probe that *succeeds* consumes test-mode link budget.

**Why this is a blocker and not a note.** The three highest-priority seeded invoices are ₹14.0L,
₹10.7L and ₹9.3L. They are the top three rows of the ranked worklist — the first thing a judge
looks at, and the rows the scoring engine exists to surface. **None of them can currently receive a
payment link**, so none can be recovered, so the headline figure in clause 1 is capped at whatever
the smaller invoices add up to. The most valuable receivables in the demo are the ones the system
cannot collect.

`create_link` passes `outstanding_paise` straight through with no ceiling check, so this fails at
dispatch, live, on the highest-value account in the run.

### Decision: C — cap at the ceiling, collect in tranches

Each link is created at `min(outstanding_paise, LINK_AMOUNT_CEILING)` with `accept_partial` set.
An invoice above the ceiling is collected across successive links, each one reconciling through the
FR-13.4 `payment_link.partially_paid` path that already exists.

**Rationale — it demonstrates the constraint rather than hiding it.** Options A and B both make the
ceiling disappear from view: A by removing it, B by arranging the data so it is never met. C is the
only one where a judge sees a real external limit being handled. *"Razorpay caps a link at roughly
₹5L, so a ₹14L receivable is collected in tranches and reconciled per tranche"* is a stronger answer
than never being asked, because the question a judge is actually probing is whether the system
copes with the real world.

**And it surfaces built code that otherwise never runs.** FR-13.4 partial reconciliation is
implemented and unit-tested, and under A or B it would never execute once — a whole reconciliation
path present in the codebase and absent from the demo. C makes it load-bearing.

**Not an option, under any route:** leaving `create_link` to fail at dispatch. The ceiling check is
required regardless, so that an over-ceiling amount is capped predictably rather than producing a
400 in the middle of a run on the highest-value account.

### Alternatives rejected

**A. Request a higher limit on the test account.**
*For:* real invoice values, top worklist rows collectable in one link, nothing arranged for the
camera. *Rejected:* depends on an external party answering before submission — no control over
timing, no guarantee of approval, and nothing to plan around. It also hides the constraint rather
than handling it, so it teaches the system nothing.

**B. Curate demo data beneath the ceiling.**
*For:* entirely within our control, immediate, removes the failure mode from the demo path.
*Rejected:* it shrinks the headline recovered figure by construction, and "₹50L of receivables
under management" is a weaker opening than the real distribution. Worse, it is the option most
likely to be *noticed* — a seeded book where no invoice happens to exceed a platform limit invites
exactly the question it was arranged to avoid.

### Downstream consequences

Four places assumed one full-value link per invoice. All are documentation changes; **no code is
written by this ADR.**

| Where | Assumption | Now |
|---|---|---|
| FR-9.1 | "a Payment Link **per invoice**" | Per invoice *per tranche*; `reference_id` already suffixes (`next_reference_id`) |
| FR-9.6 | Instalment split, P1, `cash_crunch` only | Overlaps this mechanism. The *ceiling* split is P0 and cause-independent; the *strategic* split stays P1 |
| FR-17 | Recovery counts **settled invoices** | **Must count rupees received.** See below — this is the consequential one |
| `agents/razorpay-integration.md` | `"amount": invoice.outstanding_paise` | `min(outstanding_paise, ceiling)` |

**FR-17 is the one that would have quietly broken the headline number.** A ₹14L invoice collected in
₹5L tranches is `PARTIALLY_PAID`, not `settled`, until the final tranche lands. A recovery figure
defined over *settled invoices* would count that invoice as ₹0 recovered while ₹10L had actually
arrived — so option C, chosen to raise the recovered figure, would have lowered it. FR-17 is amended
to measure rupees received, with invoice count reported separately.

`offer_installment` remains in the closed tool registry with its status **open**, to be decided at
Phase 6 implementation: the ceiling split may make it redundant, or the two may merge.

---

## Known constraints

| Constraint | Impact |
|---|---|
| **Payment link maximum amount** (between ₹5L and ₹14L) | **Blocker — see above.** Caps the recovered figure; top three worklist rows cannot be collected |
| Test mode caps standard Payment Links at 30 per business | Seed data must reuse links or stay under the cap; plan demo accordingly |
| `reference_id` must be unique per link | Regenerations carry a `-R2`, `-R3` suffix (`next_reference_id`); reconciliation strips it. Found live 2026-08-26 |
| Dedicated UPI payment link (`upi_link: true`) is **live mode only** | Demo standard links — they still offer UPI at checkout |
| GST-compliant invoices cannot be created via the Invoices API | Merchants keep issuing their own invoices; we only collect |
| RazorpayX Payout Links require IP allowlisting | RazorpayX is P2; not in MVP |

## Implementation requirements

- Every write carries an idempotency key: `sha256(invoice_id + amount_paise + purpose)`
- `reference_id` = `invoice_number`, always
- Webhook signature verified as HMAC-SHA256 over the **raw** body, before any JSON parsing
- Webhook events deduped on the `x-razorpay-event-id` header via a unique constraint,
  insert-first-process-second (the envelope carries no top-level `id`)
- Handler acknowledges in under 200 ms and processes asynchronously — Razorpay retries slow handlers
- Links approaching `expire_by` while unpaid are regenerated by `link_hygiene`
- Links on settled invoices are cancelled

## Alternatives considered

**Smart Collect virtual accounts as the primary rail.** Excellent auto-reconciliation and the
natural fit for large B2B NEFT/RTGS. Rejected as *primary* for MVP: heavier setup per counterparty,
and a bank transfer is a worse demo than a link you can tap on stage. Kept as P1.

**Razorpay Invoices API as primary.** Rejected: GST-compliant invoices cannot be created via API,
and our merchants already have invoice numbering. We are the collection layer, not the billing layer.

**RazorpayX payouts.** Only relevant for refunds and over-payment adjustments. P2.

## Consequences

**Good:** Full loop demoable in test mode; PCI scope avoided; reconciliation is trivial via
`reference_id`; instalments supported natively

**Bad:** 30-link test cap constrains batch demos; no UPI-specific link in test mode; dependent on
webhook delivery for reconciliation (mitigated by a manual "mark paid offline" path)
