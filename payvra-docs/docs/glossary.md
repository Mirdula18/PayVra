# Glossary

Terms used throughout this repo. When a term appears in code, use exactly this spelling.

## Receivables domain

**AR** — Accounts Receivable. Money owed to the business by its customers.

**Aging bucket** — Grouping of overdue invoices by days past due: `0-30`, `31-60`, `61-90`, `90+`.

**Collectability** — Our estimated probability that a given invoice will be paid if chased.
Distinct from creditworthiness; a customer can be creditworthy and still not pay without a nudge.

**Counterparty** — The customer who owes money. We deliberately avoid "debtor" (adversarial) and
"client" (ambiguous). One counterparty may have many invoices and many contacts.

**Days Past Due (DPD)** — `today - due_date`, in days. Negative before the due date.

**DSO** — Days Sales Outstanding. Average days to collect revenue. The headline metric PAYVRA moves.
`DSO = (Accounts Receivable / Total Credit Sales) x Number of Days`

**Dunning** — The process of systematically communicating with customers to collect overdue money.
A *dunning sequence* is the ordered set of touches over an invoice's recovery lifecycle.

**Exception list** — Accounts PAYVRA has permanently stopped chasing, with a recorded reason.
Being on this list is a *feature*, not a failure. Judges will ask to see it.

**MSME Act 45-day rule** — Under the MSMED Act, buyers must pay registered MSME suppliers within
45 days. Crossing it changes the legal tone available to us and is flagged separately.

**PTP** — Promise to Pay. A customer's stated commitment to pay by a specific date. Extracted from
free-text replies. A *broken promise* is a PTP whose date passed without payment.

**Touch** — A single outbound contact attempt (one email, one SMS, one WhatsApp message).
Capped per week and per invoice lifecycle.

**Tone tier** — Escalation level of a message, 1 to 4.
`1` courtesy / pre-due · `2` gentle reminder · `3` firm, cc's AP head · `4` formal notice.
Tier 3+ requires human approval above the value threshold.

**Worklist** — The ranked queue of accounts to act on. Ordered by
`P(collectable) x amount_at_risk x urgency`, never alphabetically or by age alone.

## Payments / Razorpay

**Idempotency key** — A unique client-generated string sent with a write request so that retrying
the same request never creates a duplicate. Required on all Razorpay writes.

**MDR** — Merchant Discount Rate. The fee a merchant pays per transaction.

**Payment Link** — A Razorpay-hosted checkout URL for a specific amount. Our primary collection rail.
Created via `POST /v1/payment_links`.

**reference_id** — Field on a Razorpay Payment Link where we store our invoice number. This is what
makes reconciliation trivial when the webhook fires.

**Smart Collect / Virtual Account (VA)** — A per-customer virtual bank account and VPA. Money sent to
it via NEFT/RTGS/IMPS/UPI auto-reconciles to that customer. Used for large B2B payments.

**Test mode** — Razorpay sandbox. No real money moves. All hackathon work happens here.

**VPA** — Virtual Payment Address. A UPI handle, e.g. `payvra.acme@razorpay`.

**Webhook** — Razorpay's server-to-server callback when a payment event occurs. We consume
`payment_link.paid`, `payment_link.partially_paid`, `payment_link.expired`,
`payment_link.cancelled`, `invoice.paid`, `invoice.partially_paid`, `invoice.expired`.

**X-Razorpay-Signature** — HMAC-SHA256 of the raw request body, keyed with the webhook secret.
Must be verified *before* parsing the body.

## Compliance

**DPDP** — Digital Personal Data Protection Act. Rules notified by MeitY on 13 Nov 2025
(G.S.R. 846(E)). Enforcement machinery from 13 Nov 2026; full substantive compliance
(Sections 3–10) from 13 May 2027. Max penalty ₹250 crore per instance for failure to take
reasonable security safeguards.

**Consent basis** — Our recorded justification for contacting a counterparty: which channels are
permitted, for what purpose, and whether they have opted out.

**PCI-DSS** — Card data security standard. PAYVRA is *out of scope* because Razorpay hosts checkout
and we never see card data. Do not add anything that changes this.

**RBI recovery-conduct norms** — Contact only between 08:00 and 19:00, no contact-list scraping,
no shaming or public disclosure, no third-party disclosure, interactions digitally recorded.

**Quarantine** — Contacts with no lawful consent basis. Never contacted, listed separately from
the exception list.

## System

**Audit log** — Append-only, hash-chained record of every decision, its inputs, the rationale, the
guardrail verdicts, and the outcome. Includes actions that were *refused*.

*What "append-only" actually guarantees, precisely:* **the application cannot tamper with it.**
Entries are INSERT-only and hash-chained per merchant (`entry_hash = sha256(canonical(row) +
prev_hash)`), so any edit to a stored row is detectable by re-walking the chain. At the database
level, UPDATE and DELETE are rewritten to no-ops by two PostgreSQL RULEs, and TRUNCATE — which
RULEs do not cover — raises via a BEFORE TRUNCATE trigger. There is no flag, GUC, or session
variable that disables any of these; the seed rebuilds the schema rather than truncating.

*What it does not guarantee:* this is not tamper-**proof** storage. A superuser, or any role with
ALTER TABLE on `audit_log`, can drop the rules and the trigger and then rewrite rows freely. The
hash chain still makes such tampering *detectable* — an attacker would have to recompute every
subsequent `entry_hash` — but detection is not prevention. A genuine append-only guarantee against
a privileged operator needs external anchoring (periodic chain-head export to WORM storage or a
third party), which is out of scope for the hackathon build.

State this boundary plainly if asked. The honest answer is stronger than an overclaim someone
disproves in the Q&A.

**Gate** — The deterministic guardrail check every proposed action passes through before execution.
See `architecture/agent-loop.md`.

**Recovery state** — Where an invoice sits in the dunning lifecycle. Distinct from payment status.
See `architecture/data-model.md`.

**Tool registry** — The fixed, closed set of actions the agent may propose. Anything outside it is
rejected and the deterministic fallback policy runs.
