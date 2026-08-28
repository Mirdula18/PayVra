# The Track 3 bar — acceptance checklist

**This is the document every remaining phase is measured against.** If a piece of work does not
move one of the four clauses below from red to green, it is out of scope until submission.

The judging bar, verbatim:

> *"Don't just identify the problem. Show measured money recovered across a batch, with compliant
> escalation, stopping rules, and an audit trail."*

Four clauses. Each needs one artefact a judge can look at. Not a passing test, not a module that
exists — a thing on screen with real numbers behind it.

---

## Status at a glance

Verified at commit `a9cf753`. Phases 0–5 complete, 448 tests, ruff and mypy clean.

| # | Clause | Proving artefact | Phase | Status |
|---|---|---|---|---|
| 1 | Measured money recovered | `₹ recovered` + invoice count for one named run | 6 produces, 8 shows | 🔴 **Zero** |
| 2 | Compliant escalation | Attempt 1/2/3 → tone tier, each gate-approved, in one run | 6 | 🔴 **Never executed** |
| 3 | Stopping rules | Gate refusals persisted with reason, visible beside sends | 3 ✅ built | 🟡 **Proven, never run** |
| 4 | Audit trail | Hash-chained log filtered to refusals | 3 ✅ built | 🟡 **Proven, never run** |

🔴 no evidence · 🟡 mechanism proven in isolation, no evidence from a real run · 🟢 evidenced

**All four unlock from Phase 6.** Clauses 3 and 4 are built and tested; what they lack is a
production caller that produces real entries. That is the same missing piece clauses 1 and 2 need.

---

## Clause 1 — "measured money recovered across a batch"

**The artefact:** a single figure — rupees recovered and invoice count — attributable to one
identified run of the batch runner, next to the ranked worklist it acted on.

**Produced by:** Phase 6 (the run and its `recovery_run_id`), surfaced by Phase 8.

**Current evidence:** zero from an agent run.

One invoice *has* settled end to end for real: `INV-2026-1020`, ₹23,134, paid on a live Razorpay
test link, reconciled by a genuinely signed `payment_link.paid` webhook. That proves the rail
works. It does **not** satisfy this clause, for two reasons a judge will spot immediately:

* it was triggered by `scripts/create_demo_link`, not by the agent
* one invoice is not "across a batch"

**What "measured" has to mean here.** Two figures, defined in FR-17:

* **Causal (headline)** — rupees received against invoices this run acted on. The number to say out
  loud, because it survives "how do you know your agent caused that?"
* **Time-window (context)** — everything received during the run's wall-clock window, regardless of
  cause.

Show both. See FR-17 for how to explain a divergence, and lead with the row where the gate refused
contact and the invoice was paid anyway — the case where the system declines to claim money it
could have claimed.

**Measured in rupees received, not invoices settled.** Under ADR-006 the ceiling split collects
large invoices in tranches, so a ₹14L receivable can have ₹10L genuinely recovered while its status
is still `partially_paid`. A settled-invoice-only figure would report that as zero.

**The Razorpay amount ceiling — resolved 2026-08-26.** Links above roughly ₹5L are refused, and the
three highest-priority seeded invoices (₹14.0L, ₹10.7L, ₹9.3L) are all above it. Resolved by
ADR-006 option C: cap each link at the ceiling with `accept_partial` and collect in tranches. The
top of the worklist is collectable again, and the constraint is demonstrated rather than hidden —
it also puts the built-but-otherwise-unused FR-13.4 partial reconciliation path on screen.
Implementation lands in Phases 4 and 6 (FR-9.8, FR-9.9).

---

## Clause 2 — "compliant escalation"

**The artefact:** one counterparty in the run showing attempt 1 at tone tier 1, attempt 2 at tier
2, attempt 3 at tier 3 — each message drafted, each gated, each verdict recorded. Plus at least one
escalation the gate **refused**, with the reason.

**Produced by:** Phase 6.

**Current evidence:** none. The escalation ladder has never run. Tone tiers exist and are
live-verified in Phase 5 drafting; frequency caps, contact hours and stopping rules are
live-verified in Phase 3. Nothing has ever walked an invoice up the ladder.

**"Compliant" is the load-bearing word.** It does not mean "we escalated politely". It means every
escalation passed a gate that could have refused it, and that some were refused. An escalation
sequence with no refusals in it is not evidence of compliance — it is evidence that nothing was
checked. The demo needs both outcomes present.

Phase 6 adds only the **attempt counter** (1/2/3 → tone tier). It does not decide whether attempt N
may fire — Phase 3 already owns that. See the Phase 6 section of `architecture/agent-loop.md`.

---

## Clause 3 — "stopping rules"

**The artefact:** the audit log filtered to `outcome = blocked`, showing real refusals from a real
run — a settled invoice not chased, an out-of-hours send deferred, a frequency cap hit, a
permanently stopped account.

**Produced by:** Phase 3 (built ✅), evidenced by a Phase 6 run, shown by Phase 8.

**Current evidence:** 79 passing tests prove every one of the seven gates blocks correctly, and
`gate()` writes both `approved` and `blocked` to `audit_log` itself, so no caller can execute
ungated and leave no trace. That is strong — but every entry in the database today came from the
test suite.

**The gap is evidential, not functional.** Nothing needs building. A Phase 6 run produces real
refusals as a side effect of doing its job.

---

## Clause 4 — "an audit trail"

**The artefact:** the hash-chained `audit_log`, filtered to refusals, showing what the agent
declined to send and why — beside what it did send.

**Produced by:** Phase 3 (built ✅), evidenced by a Phase 6 run, shown by Phase 8.

**Current evidence:** the chain works and is tamper-evident, with a DB-level TRUNCATE guard.
Every gate verdict, pass or fail, is written. As with clause 3, all existing rows are test data.

**The demo moment is the refusal list, not the send list.** Any tool can show what it did. Showing
what it refused — with the rule that stopped it — is the thing that distinguishes a guardrailed
agent from an unsupervised one. Phase 8 must make that filter one click.

---

## How to use this document

**Before starting any work, ask: which clause does this move, and from what to what?** If there is
no answer, it is post-submission. This applies to features that are already specified elsewhere in
`requirements/` — a P0 in `functional.md` that serves no clause is still deferred, and several now
are (see FR-11, FR-12, and the trimmed FR-14/FR-15).

**Phases that serve the bar:** 6 (all four clauses), 8 (evidences 1, 3, 4), 9 (rehearsal).

**Phases re-scoped as post-submission:** 7 (replies and promise tracking — no clause depends on it).

**Resolved issue that had capped clause 1:** the Razorpay amount ceiling — decided, ADR-006 option
C (tranche collection). Implementation is FR-9.8/FR-9.9 in Phases 4 and 6.

**Demo constraint tracked here because it can zero clause 1:** the contact-hours gate refuses every
outbound action outside 08:00–19:00 IST. A rehearsal at 21:00 produces a run where everything is
refused — a flawless clause-3 demo and a zero clause-1 demo. See the Phase 9 runbook.
