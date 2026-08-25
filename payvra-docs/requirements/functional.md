# Functional Requirements

Priority: **P0** = MVP, must ship. **P1** = ship if time. **P2** = roadmap, do not build now.

---

## FR-1 — Ingestion

| ID | Requirement | Priority |
|---|---|---|
| FR-1.1 | Upload a CSV or XLSX of invoices; parse into the canonical `Invoice` schema | P0 |
| FR-1.2 | Map arbitrary column headers to canonical fields; LLM-assisted for unrecognised headers | P0 |
| FR-1.3 | Reject rows with missing `due_date` or `amount` into a repair queue, surfaced in UI | P0 |
| FR-1.4 | Fuzzy-match counterparties across name variants ("Sharma Ent." = "Sharma Enterprises Pvt Ltd"); exact-match on GSTIN takes precedence | P0 |
| FR-1.5 | Detect and skip duplicate invoices on `(merchant_id, invoice_number)` | P0 |
| FR-1.6 | Import contacts with name, email, phone, role | P0 |
| FR-1.7 | Google Sheets connector | P1 |
| FR-1.8 | Zoho Books OAuth connector | P2 |
| FR-1.9 | Tally XML import | P2 |

## FR-2 — Consent and quarantine

| ID | Requirement | Priority |
|---|---|---|
| FR-2.1 | On import, require the merchant to confirm a consent basis per counterparty | P0 |
| FR-2.2 | Record permitted channels per counterparty (email / SMS / WhatsApp) | P0 |
| FR-2.3 | Counterparties with no consent basis go to **quarantine** and are never contacted | P0 |
| FR-2.4 | Every outbound message carries an opt-out mechanism | P0 |
| FR-2.5 | Opt-out is honoured immediately and permanently across all channels | P0 |
| FR-2.6 | Data-subject erasure endpoint that purges a counterparty's PII while retaining anonymised audit records | P1 |

## FR-3 — Detection and aging

| ID | Requirement | Priority |
|---|---|---|
| FR-3.1 | Compute `days_past_due` and aging bucket for every invoice, refreshed nightly | P0 |
| FR-3.2 | Compute total exposure and concentration risk per counterparty | P0 |
| FR-3.3 | Flag invoices crossing the MSME Act 45-day threshold separately | P0 |
| FR-3.4 | Detect partial payments and track residual outstanding | P0 |

## FR-4 — Scoring and worklist

| ID | Requirement | Priority |
|---|---|---|
| FR-4.1 | Compute a collectability score per invoice from: DPD, invoice value, share of total exposure, counterparty historical average days-to-pay, broken-promise count, engagement rate, dispute flag, lifetime revenue | P0 |
| FR-4.2 | Rank the worklist by `P(collectable) x amount_at_risk x urgency_multiplier` | P0 |
| FR-4.3 | Every ranked row must carry a **plain-English reason** for its position | P0 |
| FR-4.4 | Rescore nightly, folding in the previous day's engagement signals | P0 |
| FR-4.5 | Merchant can manually pin, snooze, or exclude any account from the worklist | P0 |

## FR-5 — Diagnosis

| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | Infer a probable cause per unpaid invoice from behavioural signals | P0 |
| FR-5.2 | Supported causes: `oversight`, `cash_crunch`, `dispute`, `wrong_contact`, `awaiting_docs`, `refusal`, `unknown` | P0 |
| FR-5.3 | Each cause maps to a distinct intervention (see `architecture/agent-loop.md`) | P0 |
| FR-5.4 | Signals used: link opened but unpaid, email bounced, reply intent, partial payment, historical pattern, zero engagement | P0 |

## FR-6 — Decision engine

| ID | Requirement | Priority |
|---|---|---|
| FR-6.1 | The agent proposes exactly one action per eligible account per planning cycle | P0 |
| FR-6.2 | Proposals are constrained to the closed tool registry; anything else is rejected | P0 |
| FR-6.3 | Proposals outside the current recovery state's allowed transitions are rejected | P0 |
| FR-6.4 | On rejection, a deterministic fallback policy selects the action | P0 |
| FR-6.5 | Every proposal records a machine-readable rationale | P0 |

## FR-7 — Guardrails (the gate)

Every action passes all checks in order. Any failure halts the action and logs the reason.

| ID | Requirement | Priority |
|---|---|---|
| FR-7.1 | **Time window** — only 08:00–19:00 IST; otherwise requeue to the next window | P0 |
| FR-7.2 | **Freshness** — re-read invoice payment status from DB; abort if settled | P0 |
| FR-7.3 | **Consent** — channel permitted, opt-out not exercised, not quarantined | P0 |
| FR-7.4 | **Frequency cap** — blocks on the 3rd touch in any rolling 7 days per counterparty, and the 7th in an invoice lifetime | P0 |
| FR-7.5 | **Value threshold** — invoices above a merchant-set amount require human approval | P0 |
| FR-7.6 | **Tone ceiling** — tier 3+ requires human approval regardless of value | P0 |
| FR-7.7 | **Content policy** — no threats, no shaming, no third-party disclosure; must contain the correct amount, invoice number, payment link, and opt-out | P0 |
| FR-7.8 | **Stopping rules** — settled, disputed, opted out, 3 broken promises, or touch cap → permanent stop | P0 |
| FR-7.9 | Every gate verdict, pass or fail, written to `audit_log` | P0 |

## FR-8 — Message generation

| ID | Requirement | Priority |
|---|---|---|
| FR-8.1 | Generate a message given invoice facts, interaction history, tone tier, language, channel | P0 |
| FR-8.2 | Output constrained to a JSON schema; validated before use | P0 |
| FR-8.3 | Validator asserts correct amount, invoice number, payment link present, no banned phrases | P0 |
| FR-8.4 | On two validation failures, fall back to a deterministic template | P0 |
| FR-8.5 | Support English and Hinglish; language selected per counterparty preference | P0 |
| FR-8.6 | Regional language support (Tamil, Hindi, Gujarati) | P2 |

## FR-9 — Payment rail

| ID | Requirement | Priority |
|---|---|---|
| FR-9.1 | Create a Razorpay Payment Link per invoice with `reference_id` = invoice number | P0 |
| FR-9.2 | Set `expire_by`, `accept_partial`, `notify`, `reminder_enable` | P0 |
| FR-9.3 | Resend an existing link via Razorpay's notify endpoint | P0 |
| FR-9.4 | Auto-regenerate links approaching expiry while the invoice is still unpaid | P0 |
| FR-9.5 | Cancel outstanding links when an invoice settles | P0 |
| FR-9.6 | Offer a split/instalment plan when cause = `cash_crunch` (2 links, partial amounts) | P1 |
| FR-9.7 | Smart Collect virtual account per counterparty for large NEFT/RTGS payments | P1 |

## FR-10 — Delivery

| ID | Requirement | Priority |
|---|---|---|
| FR-10.1 | Send via email | P0 |
| FR-10.2 | Send via one second channel — WhatsApp sandbox or SMS | P0 |
| FR-10.3 | Ingest delivery receipts, bounces, opens, link views as engagement signals | P0 |
| FR-10.4 | A bounce marks the contact stale and triggers channel switching | P0 |
| FR-10.5 | Live WhatsApp Business API (requires Meta approval) | P2 |

## FR-11 — Reply handling

| ID | Requirement | Priority |
|---|---|---|
| FR-11.1 | Ingest inbound replies via webhook | P0 |
| FR-11.2 | Classify intent: `dispute`, `promise_to_pay`, `query`, `refusal`, `wrong_contact`, `acknowledgment`, `unclear` | P0 |
| FR-11.3 | Extract a promised date from free-text, including Hinglish ("next Tuesday tak clear kar dunga") | P0 |
| FR-11.4 | `dispute` → freeze all outreach, flag for human, record reason | P0 |
| FR-11.5 | `promise_to_pay` → store PTP, suppress outreach until `promised_date + 1` | P0 |
| FR-11.6 | `wrong_contact` → mark contact stale, request AP contact, try alternate channel | P0 |
| FR-11.7 | `refusal` or opt-out → permanent stop, move to exception list | P0 |
| FR-11.8 | Confidence below threshold → route to the human "needs you" queue, never guess | P0 |

## FR-12 — Promise tracking

| ID | Requirement | Priority |
|---|---|---|
| FR-12.1 | Store PTPs with promised date, amount, source reply, confidence | P0 |
| FR-12.2 | Daily sweep: promises past date without payment → mark broken → escalate one tier | P0 |
| FR-12.3 | Three broken promises → permanent stop, exception list | P0 |
| FR-12.4 | Surface "promises due today" on the dashboard | P0 |

## FR-13 — Reconciliation

| ID | Requirement | Priority |
|---|---|---|
| FR-13.1 | Webhook endpoint verifying `X-Razorpay-Signature` over the raw body | P0 |
| FR-13.2 | Dedupe on the `x-razorpay-event-id` header; processing is idempotent | P0 |
| FR-13.3 | On `payment_link.paid`: mark settled, **revoke all scheduled jobs for that invoice**, close open PTP, emit `recovered` event | P0 |
| FR-13.4 | On `payment_link.partially_paid`: reduce outstanding, re-enter loop at a *lower* tone tier | P0 |
| FR-13.5 | On `payment_link.expired`: regenerate if still unpaid and not stopped | P0 |
| FR-13.6 | Manual "mark as paid offline" with reason, for cheque/bank transfer outside Razorpay | P0 |

## FR-14 — Reporting

| ID | Requirement | Priority |
|---|---|---|
| FR-14.1 | Batch summary: total outstanding, ₹ recovered, recovery rate, invoice count by state | P0 |
| FR-14.2 | DSO before vs after, with the delta as the headline number | P0 |
| FR-14.3 | Promise-kept rate | P0 |
| FR-14.4 | Exception list with per-account stop reason | P0 |
| FR-14.5 | Full audit log, filterable by invoice, counterparty, action type, and verdict | P0 |
| FR-14.6 | Per-account timeline view: every touch, reply, promise, and payment in order | P0 |
| FR-14.7 | Cost per recovery (LLM + messaging cost / ₹ recovered) | P1 |

## FR-15 — Human control

| ID | Requirement | Priority |
|---|---|---|
| FR-15.1 | "Review plan" screen: preview every scheduled action for the next 14 days before activating | P0 |
| FR-15.2 | Edit any drafted message before it sends | P0 |
| FR-15.3 | Exclude any account from automation entirely | P0 |
| FR-15.4 | "Needs you" queue: disputes, pending escalations, unclear replies | P0 |
| FR-15.5 | Global pause — stop all outreach immediately | P0 |
| FR-15.6 | Configure guardrails: contact hours, frequency caps, value threshold, approval tier | P0 |
