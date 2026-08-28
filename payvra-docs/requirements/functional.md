# Functional Requirements

Priority: **P0** = MVP, must ship. **P1** = ship if time. **P2** = roadmap, do not build now.
**POST** = deferred until after submission — see below.

---

## Scope gate — read before building anything here

`requirements/track3-bar.md` is the acceptance checklist, and it outranks the priorities in this
file. A requirement marked P0 below is still deferred if it moves none of the bar's four clauses,
and several now are.

**Every P0 must map to a clause — measured money recovered, compliant escalation, stopping rules,
or audit trail — or it is POST.** This is a submission-scope decision, not a judgement about the
requirement: everything marked POST is still correct, still wanted, and still specified here.

| Area | Status | Why |
|---|---|---|
| FR-16, FR-17 | **P0 — new** | The batch runner and its recovery figures. All four clauses unlock here |
| FR-11 Reply handling | **POST** | No clause depends on inbound replies |
| FR-12 Promise tracking | **POST** | Depends on FR-11 |
| FR-14 Reporting | **trimmed** | Three items serve the bar; the rest are POST |
| FR-15 Human control | **trimmed** | Two items serve the bar; the rest are POST |

**FR-11.3 (free-text and vernacular date extraction) is removed from planned scope entirely**, not
merely deferred. See the note under FR-11.

**This does not touch Hinglish message generation.** That is FR-8.5, it is built, it is
live-verified against a real model as of commit `a9cf753`, and it stays.

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
| FR-9.1 | Create a Razorpay Payment Link for an invoice with `reference_id` = invoice number. An invoice may have **several links over its life** — regenerations (FR-9.4) and ceiling tranches (FR-9.8) — each with a unique `reference_id` via the `-R2`/`-R3` suffix | P0 |
| FR-9.2 | Set `expire_by`, `accept_partial`, `notify`, `reminder_enable` | P0 |
| FR-9.3 | Resend an existing link via Razorpay's notify endpoint | P0 |
| FR-9.4 | Auto-regenerate links approaching expiry while the invoice is still unpaid | P0 |
| FR-9.5 | Cancel outstanding links when an invoice settles | P0 |
| FR-9.8 | **Cap each link at the platform amount ceiling**, `min(outstanding_paise, ceiling)`, with `accept_partial` set. An invoice above the ceiling is collected in tranches, each reconciling through FR-13.4 | **P0 — new** |
| FR-9.9 | Never let an over-ceiling amount reach Razorpay. The cap is applied before the call, so the failure mode is a smaller link, never a `400` mid-run | **P0 — new** |
| FR-9.6 | Offer a split/instalment plan when cause = `cash_crunch` (2 links, partial amounts) | P1 — *see note* |
| FR-9.7 | Smart Collect virtual account per counterparty for large NEFT/RTGS payments | P1 |

**FR-9.8 and FR-9.9 come from ADR-006, option C.** Links above roughly ₹5L are refused, and the
three highest-priority seeded invoices are ₹14.0L, ₹10.7L and ₹9.3L — the top of the worklist. The
ceiling split is **P0 and cause-independent**: it applies to any invoice above the limit, whatever
the diagnosis.

**FR-9.6 stays P1 and is now partly overlapping.** It is the *strategic* split — offered because a
counterparty is cash-crunched and a smaller ask is likelier to be paid. FR-9.8 is the *mechanical*
split — forced by a platform limit regardless of cause. They share machinery and differ in intent.
Whether they merge is open, and is a Phase 6 implementation decision.

## FR-10 — Delivery

| ID | Requirement | Priority |
|---|---|---|
| FR-10.1 | Send via email | P0 |
| FR-10.2 | Send via one second channel — WhatsApp sandbox or SMS | P0 |
| FR-10.3 | Ingest delivery receipts, bounces, opens, link views as engagement signals | P0 |
| FR-10.4 | A bounce marks the contact stale and triggers channel switching | P0 |
| FR-10.5 | Live WhatsApp Business API (requires Meta approval) | P2 |

## FR-11 — Reply handling — **POST-SUBMISSION (Phase 7)**

No clause of the Track 3 bar depends on inbound replies. The bar asks what the system *sent*,
*refused*, and *recovered* — all outbound. Deferred whole; the spec below stands unchanged for
after submission.

| ID | Requirement | Priority |
|---|---|---|
| FR-11.1 | Ingest inbound replies via webhook | POST |
| FR-11.2 | Classify intent: `dispute`, `promise_to_pay`, `query`, `refusal`, `wrong_contact`, `acknowledgment`, `unclear` | POST |
| ~~FR-11.3~~ | ~~Extract a promised date from free-text, including Hinglish~~ | **REMOVED** |
| FR-11.4 | `dispute` → freeze all outreach, flag for human, record reason | POST |
| FR-11.5 | `promise_to_pay` → store PTP, suppress outreach until `promised_date + 1` | POST |
| FR-11.6 | `wrong_contact` → mark contact stale, request AP contact, try alternate channel | POST |
| FR-11.7 | `refusal` or opt-out → permanent stop, move to exception list | POST |
| FR-11.8 | Confidence below threshold → route to the human "needs you" queue, never guess | POST |

**FR-11.3 is removed from planned scope, not deferred.** Extracting a date from *"next Tuesday tak
clear kar dunga"* is the hardest correctness problem in the product and the least defensible: a
misread date silently suppresses outreach on a live receivable, and the failure is invisible until
the money is later than it should have been. It serves no clause of the bar. If reply handling is
built later, promised dates should be captured from a structured input the counterparty confirms,
not inferred from prose.

**This has no bearing on Hinglish message generation (FR-8.5)**, which is a different capability in
the opposite direction: *writing* code-mixed text, validated before it is sent, with a
deterministic template fallback. It is built and live-verified. It stays.

## FR-12 — Promise tracking — **POST-SUBMISSION (Phase 7)**

Depends entirely on FR-11. Deferred with it.

Note that **FR-12.3 is already enforced** — the three-broken-promises stopping rule is gate check 7,
built and tested in Phase 3. What is deferred is *recording* promises, not stopping on them.

| ID | Requirement | Priority |
|---|---|---|
| FR-12.1 | Store PTPs with promised date, amount, source reply, confidence | POST |
| FR-12.2 | Daily sweep: promises past date without payment → mark broken → escalate one tier | POST |
| FR-12.3 | Three broken promises → permanent stop, exception list | ✅ built (gate check 7) |
| FR-12.4 | Surface "promises due today" on the dashboard | POST |

## FR-13 — Reconciliation

| ID | Requirement | Priority |
|---|---|---|
| FR-13.1 | Webhook endpoint verifying `X-Razorpay-Signature` over the raw body | P0 |
| FR-13.2 | Dedupe on the `x-razorpay-event-id` header; processing is idempotent | P0 |
| FR-13.3 | On `payment_link.paid`: mark settled, **revoke all scheduled jobs for that invoice**, close open PTP, emit `recovered` event | P0 |
| FR-13.4 | On `payment_link.partially_paid`: reduce outstanding, re-enter loop at a *lower* tone tier | P0 |
| FR-13.5 | On `payment_link.expired`: regenerate if still unpaid and not stopped | P0 |
| FR-13.6 | Manual "mark as paid offline" with reason, for cheque/bank transfer outside Razorpay | P0 |

## FR-14 — Reporting — **trimmed to the bar**

Three items evidence a clause. The rest are correct and wanted, and are POST.

| ID | Requirement | Priority | Clause |
|---|---|---|---|
| FR-14.1 | Run summary: ₹ recovered and invoice count, scoped to one `recovery_run_id` | **P0** | 1 |
| FR-14.4 | Exception list with per-account stop reason | **P0** | 3 |
| FR-14.5 | Audit log, filterable by verdict — refusals beside sends | **P0** | 3, 4 |
| FR-14.2 | DSO before vs after | POST | — |
| FR-14.3 | Promise-kept rate | POST | depends on FR-12 |
| FR-14.6 | Per-account timeline view | POST | — |
| FR-14.7 | Cost per recovery | POST | — |

FR-14.1 is narrowed from "batch summary" to a run-scoped figure; the definition is FR-17. FR-14.5
loses the invoice / counterparty / action-type filters — **filter by verdict is the one that
matters**, because the demo moment is the refusal list.

## FR-15 — Human control — **trimmed to the bar**

| ID | Requirement | Priority | Clause |
|---|---|---|---|
| FR-15.3 | Exclude any account from automation entirely | **P0** ✅ built | 3 |
| FR-15.6 | Configure guardrails: contact hours, frequency caps, value threshold, approval tier | **P0** partial | 2, 3 |
| FR-15.1 | "Review plan" screen | POST | superseded for now by `dry_run` (FR-16.7) |
| FR-15.2 | Edit any drafted message before it sends | POST | — |
| FR-15.4 | "Needs you" queue | POST | depends on FR-11 |
| FR-15.5 | Global pause | POST | a synchronous run is stopped by not running it |

FR-15.6 is partial and stays partial: **only the contact-hours window becomes configurable** in
Phase 6, under the three conditions in FR-16.8. Caps, thresholds and approval tiers keep their
built-in values.

## FR-16 — The batch runner (Phase 6) — **NEW**

The production caller Phases 3, 4 and 5 do not have. Authority: ADR-009 and the Phase 6 section of
`architecture/agent-loop.md`.

| ID | Requirement | Priority | Clause |
|---|---|---|---|
| FR-16.1 | One synchronous pass over the ranked worklist, top N, N configurable | P0 | 1 |
| FR-16.2 | Per account: diagnose → propose exactly ONE action from the closed registry → gate | P0 | 2 |
| FR-16.3 | On approval: create Razorpay link, generate message, record executed | P0 | 1, 2 |
| FR-16.4 | On refusal: persist the refusal with its reason and continue — a refusal is a result, not an error | P0 | 3, 4 |
| FR-16.5 | Every run opens a `recovery_runs` row and carries its `recovery_run_id` through actions and audit entries | P0 | 1, 4 |
| FR-16.6 | Per-invoice attempt counter (1/2/3) selects tone tier from the existing Phase 5 templates | P0 | 2 |
| FR-16.7 | `dry_run` mode: diagnose, propose and gate, persist verdicts, create no link and send nothing | P0 | 2, 3 |
| FR-16.8 | Contact-hours window configurable by environment variable; gate always executes; an active override is written to the audit log | P0 | 2 |
| FR-16.9 | Re-running is safe: link idempotency, message cache, and gate freshness prevent double-contact | P0 | 3 |

**FR-16.6 adds a counter and nothing else.** Whether attempt N may fire is decided by gate checks
1, 4, 5 and 7 — built and verified in Phase 3. Do not restate that policy here or in the runner.

**Non-goals for Phase 6, deferred not rejected:** scheduler, async queue, retry layer.

## FR-17 — Run-scoped recovery measurement (Phase 6) — **NEW**

The number clause 1 asks for. Two figures, both scoped to `recovery_run_id`.

**Recovery is measured in rupees received, not in invoices settled.** Under ADR-006 option C, an
invoice above the Razorpay link ceiling is collected in tranches, so a ₹14L receivable can have
₹10L genuinely recovered while its status is still `partially_paid`. A figure defined over settled
invoices would report that as ₹0 — and would make the tranche mechanism *lower* the headline number
it was chosen to raise. Count the money; report the invoice count beside it.

| ID | Requirement | Priority | Clause |
|---|---|---|---|
| FR-17.1 | **Causal (headline):** ₹ received against invoices that received an action in this run — including partial payments | P0 | 1 |
| FR-17.2 | **Time-window (context):** ₹ received between run start and run end, regardless of cause | P0 | 1 |
| FR-17.3 | Report **fully settled** invoice count separately from **partially recovered** count; never merge them | P0 | 1 |
| FR-17.4 | Report both figures, labelled, never one alone | P0 | 1 |
| FR-17.5 | Rupees received derive from reconciled payment events, not from `outstanding_paise` deltas alone | P0 | 1 |

FR-17.5 exists because `outstanding_paise` can also move for reasons that are not recovery — a
manual write-off, a credit note, a correction. Reading the reconciled payment events keeps the
figure defensible when someone asks what it is counting.

### Schema changes

Specified here; **no migration is written by this document.**

| Change | Purpose |
|---|---|
| New `recovery_runs` table | id, merchant_id, started_at, finished_at, account_limit, dry_run, window_override, counts |
| `actions.recovery_run_id` | Causal attribution; "what did this run do?" |
| `audit_log.recovery_run_id` | Filter the trail to one run — the clause 3 and 4 demo |

`invoices.settled_at` already exists and is unchanged. Partial recovery is read from reconciled
payment events (FR-17.5), which also already exist.

**Naming:** `recovery_run_id`, not `batch_id`. `batches` already means *an uploaded invoice file*
in this schema. "The batch runner" is the spoken name; the columns say `recovery_run`. See ADR-009.

### Explaining a divergence between the two figures

The two numbers will differ, and being unable to explain the gap is worse than the gap. Expect to
be asked. Time-window is normally the larger. Each cause has a one-line answer:

| Cause of divergence | Direction | What to say |
|---|---|---|
| **Gate refused the action, invoice was paid anyway** | time-window only | ***"We declined to contact them and they paid regardless. Counting that as recovery would be dishonest."*** |
| Invoice paid during the run but the runner never touched it — a cheque cleared, a customer paid unprompted | time-window higher | *"Real money, not our recovery. That is why the headline is the causal figure."* |
| Runner acted, counterparty paid after the run closed | causal higher on the **next** run | *"Recovery is not instant. A run's causal figure keeps growing after the run ends — this is a snapshot, not a final total."* |
| Runner acted and payment arrived through an offline path (FR-13.6) | both count it | *"Attribution is by action, not by rail. We chased it, they paid — how they paid is not the test."* |
| Invoice above the link ceiling, collected in tranches (ADR-006 C) | both count the ₹, invoice not yet settled | *"₹10L of a ₹14L invoice is recovered. The invoice is still open — we report money received, not invoices closed."* |

**Lead with the first row.** It is the one worth volunteering before being asked, because it is the
only one where the system declines to claim money it could have claimed. A figure that excludes
payments the agent had nothing to do with is making the same argument as the refusal list — that
the number can be trusted precisely because it was not maximised. Clause 4 is about exactly that,
and this is clause 4 expressed as a number instead of a log.
