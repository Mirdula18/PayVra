# User Stories

Format: **As a** [persona] **I want** [capability] **so that** [outcome].
Each story has explicit acceptance criteria. Do not mark a story done without meeting all of them.

Personas defined in `docs/vision.md`: **Priya** (AR lead), **Rahul** (founder), **Meena** (AR exec).

---

## Epic A — Onboarding

### US-A1 — Connect and import
**As Priya, I want** to upload my invoice export and have PAYVRA understand it **so that** I don't
have to reformat anything.

- [ ] Accepts CSV and XLSX
- [ ] Unrecognised column headers are auto-mapped, with the mapping shown for confirmation
- [ ] Rows missing `due_date` or `amount` land in a visible repair queue, not silently dropped
- [ ] Counterparty variants collapse into one record; GSTIN match wins over name match
- [ ] Summary shown within 45 s for a 500-row file

### US-A2 — Record consent
**As Priya, I want** to confirm which customers I may contact and how **so that** I'm not exposed
to a DPDP or RBI conduct problem.

- [ ] Per-counterparty consent screen listing permitted channels
- [ ] Bulk-confirm option for counterparties with an existing commercial relationship
- [ ] Counterparties left unconfirmed go to **quarantine** and are never contacted
- [ ] Quarantine count is visible on the dashboard, not hidden

### US-A3 — Set guardrails
**As Priya, I want** to set the boundaries before anything sends **so that** I stay in control of
my customer relationships.

- [ ] Configurable: contact hours (default 08:00–19:00 IST), weekly touch cap, lifetime touch cap
- [ ] Configurable: invoice value above which human approval is required
- [ ] Configurable: tone tier above which human approval is required (default 3)
- [ ] Settings are enforced immediately, including on already-scheduled actions

---

## Epic B — The worklist

### US-B1 — See what matters, not what's oldest
**As Priya, I want** a ranked list of who to chase **so that** I stop wasting effort on the wrong
accounts.

- [ ] Ranked by `P(collectable) x amount x urgency`, not by age
- [ ] Each row shows amount, DPD, proposed action, and a **plain-English reason for its rank**
- [ ] Example reason: "₹4.2L, 68 days. This customer has paid late twice before but always paid."
- [ ] Sortable and filterable, but the default view is the ranked one
- [ ] Refreshed nightly with the previous day's engagement signals

### US-B2 — Review before activating
**As Priya, I want** to see exactly what will be sent over the next two weeks **so that** nothing
surprises me or my customers.

- [ ] Preview shows: recipient, channel, send time, tone tier, full message text
- [ ] Any message is editable inline
- [ ] Any account can be excluded from automation entirely
- [ ] Nothing sends until "Activate" is pressed

---

## Epic C — Automated recovery

### US-C1 — Chase without me
**As Rahul, I want** PAYVRA to follow up so I don't have to **so that** I stop losing money to
my own awkwardness about chasing clients.

- [ ] Pre-due courtesy note goes out 3 days before due date
- [ ] Escalating sequence runs automatically after the due date
- [ ] Every message contains a working Razorpay Payment Link
- [ ] Tone escalates by tier, never becomes threatening or shaming
- [ ] Nothing sends outside 08:00–19:00 IST

### US-C2 — Right intervention for the right reason
**As Priya, I want** PAYVRA to treat a cash-crunched customer differently from a disputing one
**so that** we don't damage good relationships.

- [ ] Link opened repeatedly but unpaid → offer instalment split, tone stays low
- [ ] Email bounced → contact marked stale, alternate channel tried, AP contact requested
- [ ] Reply classified as dispute → all outreach freezes, routed to human
- [ ] Historical on-time payer, first slip → single gentle nudge, no escalation
- [ ] The inferred cause is visible on the account timeline

### US-C3 — Track promises
**As Priya, I want** promises to pay recorded and followed up **so that** "I'll pay next week"
stops disappearing into a WhatsApp thread.

- [ ] Free-text replies parsed for a promised date, including Hinglish
- [ ] Outreach suppressed until `promised_date + 1`
- [ ] Promise broken → escalate one tier, log the break
- [ ] Three broken promises → permanent stop, exception list
- [ ] "Promises due today" visible on the dashboard

### US-C4 — Stop the moment I'm paid
**As Priya, I want** chasing to stop instantly when money arrives **so that** I never embarrass
myself in front of a customer who already paid.

- [ ] `payment_link.paid` webhook marks the invoice settled
- [ ] **All scheduled jobs for that invoice are revoked**
- [ ] Any open promise is closed
- [ ] Recovered total updates on the dashboard
- [ ] Manual "mark paid offline" available for cheque and direct bank transfer

---

## Epic D — Human control

### US-D1 — The 3-minute morning
**As Priya, I want** a single screen that tells me what needs me **so that** collections stops
eating my mornings.

- [ ] Four blocks: recovered since yesterday, needs you, promises due today, exception list
- [ ] "Needs you" contains only genuinely actionable items: disputes, pending escalations, unclear replies
- [ ] Approving an escalation takes one click
- [ ] Whole review completable in under 3 minutes for 300 open invoices

### US-D2 — Approve escalations
**As Priya, I want** to sign off before anything firm goes to a customer **so that** I control
the relationship risk.

- [ ] Tier 3+ actions queue for approval instead of sending
- [ ] Invoices above the value threshold queue for approval regardless of tier
- [ ] Approval screen shows the full message, the history, and why the agent escalated
- [ ] Reject sends it back to the agent, which selects a lower-tier alternative

### US-D3 — Pull the plug
**As Priya, I want** a global pause **so that** I can stop everything instantly if something
looks wrong.

- [ ] Reachable from every screen
- [ ] Halts all outbound within one dispatch window
- [ ] Clearly indicated while active

---

## Epic E — Proof

### US-E1 — See the money
**As Priya, I want** hard numbers **so that** I can show my CFO this is working.

- [ ] ₹ recovered, recovery rate, invoice counts by state
- [ ] DSO before vs after, delta as the headline
- [ ] Promise-kept rate
- [ ] All figures scoped to a batch and a date range

### US-E2 — Show my work
**As Meena, I want** a complete record of every action **so that** I can answer an auditor
or an unhappy customer.

- [ ] Per-account timeline: every touch, reply, promise, payment, in order
- [ ] Full audit log filterable by invoice, counterparty, action type, and verdict
- [ ] **Refused actions are shown alongside executed ones, with the reason**
- [ ] Each entry shows actor, inputs, rationale, gate verdicts, outcome

### US-E3 — Honest exceptions
**As Priya, I want** to see who PAYVRA gave up on and why **so that** I can decide whether to
intervene personally.

- [ ] Exception list with a per-account stop reason
- [ ] Reasons: settled, disputed, opted out, 3 broken promises, touch cap, no consent
- [ ] One-click "take this over myself"

---

## Judge stories

Not user stories, but the questions to build for. These decide the outcome.

### JS-1 — "Show me the money"
- [ ] A single number on screen: ₹ recovered across the batch, with the invoices behind it
- [ ] DSO delta, computed and displayed, not asserted

### JS-2 — "What if the AI goes rogue?"
- [ ] Open the audit log, show actions the system **refused** to take and why
- [ ] Explain the closed tool registry and the state-transition rejection path
- [ ] Show the deterministic fallback firing when the LLM is disabled

### JS-3 — "What if something breaks?"
- [ ] Live demo of an expired link being auto-regenerated
- [ ] Explain webhook idempotency and the freshness check
- [ ] Show the circuit breaker and template fallback

### JS-4 — "Is this legal?"
- [ ] Point to the time-window gate, consent ledger, opt-out, quarantine list
- [ ] Explain PCI-DSS non-applicability (no card data)
- [ ] Explain DPDP posture and the MSME Act flag
