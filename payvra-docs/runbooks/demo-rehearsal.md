# Runbook: the Phase 9 demo rehearsal

**Read this before the first rehearsal, not before the last one.** Two constraints below can turn
a working system into a zero on stage, and both are invisible until you hit them.

The script itself is `prompts/demo-script.md`. This runbook is about the conditions the demo runs
under. What is being evidenced is `requirements/track3-bar.md` — four clauses, four artefacts.

---

## 🔴 Constraint 1 — the contact-hours window will refuse your entire run

**Gate check 1 refuses every outbound action outside 08:00–19:00 IST.**

That is correct behaviour, it is a compliance claim made to a judge (NFR-5.1, RBI recovery-conduct
norms), and it stays the default. It also means:

> A rehearsal at 21:00 IST produces a run where **every single action is refused**. A flawless
> demonstration of clause 3 and a zero for clause 1, from the same command.

The failure is silent in the worst way — nothing errors. The run completes, the audit log fills
with refusals, and the recovered figure is ₹0. It looks like the agent decided not to act.

### Mitigation, in order of preference

**1. Rehearse and present inside the window.** The only mitigation with no explaining to do.
Build the schedule around it.

**2. Widen the window by environment variable** (FR-16.8), under three binding conditions:

* **The gate still executes.** There is no bypass path and no skip flag. The window is a value the
  check reads, never a rule it can be told to ignore. A gate with a bypass is not a gate.
* **Widened only by explicit environment variable.** Never a request parameter, never a default,
  never a UI toggle.
* **An active override is written into the audit log.** This is what keeps an out-of-window run
  compliant *by record* — the trail shows the window was widened and when, rather than leaving a
  judge unable to tell that it was.

If a run used an override, **say so before you are asked**, and show the audit entry. A system that
records its own exceptions is making the same argument as its refusal list. Being caught hiding one
undoes every other claim in the pitch.

**Never** demo with the window widened and the override unrecorded. That is not a shortcut, it is
the one thing the audit trail exists to prevent.

---

## 🔴 Constraint 2 — the payment link amount ceiling

Links above roughly ₹5L are refused (ADR-006, open blocker). The three highest-priority seeded
invoices are ₹14.0L, ₹10.7L and ₹9.3L — the top three rows of the worklist, and the first thing a
judge looks at.

**The most valuable receivables in the demo are the ones the system cannot collect**, which caps
the clause 1 figure directly.

Resolve this before rehearsal, not during. The options and their tradeoffs are in ADR-006; the
choice is the project owner's. Whichever is taken, rehearse against the resolved state — a demo
tuned to data that later changes is a demo tuned twice.

---

## Pre-rehearsal checklist

Environment:

- [ ] `docker compose up -d db`, migrations at head
- [ ] Seed loaded, and **scores populated** — an unscored worklist ranks arbitrarily and the
      "not an aging report" claim collapses on screen
- [ ] `uvicorn` on :8000
- [ ] `cloudflared` tunnel up, **URL registered in the Razorpay dashboard** — the URL is random per
      restart on the free tier, so a restarted tunnel means re-registering and updating
      `PUBLIC_BASE_URL`
- [ ] `make verify-razorpay` — all checks pass
- [ ] `make verify-llm` — all checks pass
- [ ] Link budget checked: 25 of 30 test-mode links, and each rehearsal consumes some

Timing and data:

- [ ] Rehearsal scheduled **inside 08:00–19:00 IST**, or the override understood and recorded
- [ ] Amount ceiling resolved (ADR-006)
- [ ] Batch **pre-computed** — at ~30 RPM a full pass takes minutes; never run the whole batch live

Evidence — one artefact per clause, each confirmed on screen:

- [ ] Clause 1: ₹ recovered and invoice count for one `recovery_run_id`, **both figures labelled**
- [ ] Clause 2: one counterparty walked 1 → 2 → 3, with at least one refused escalation
- [ ] Clause 3: audit log filtered to `blocked`, reasons human-readable
- [ ] Clause 4: the same log showing refusals **beside** sends

---

## Rehearse these answers

They will be asked, and each has a documented answer already. Say them out loud in rehearsal — the
first one especially, because it works only if it sounds unrehearsed.

### Volunteer this one — do not wait to be asked

**Pointing at the gap between the two recovery figures:**

> *"These two numbers don't match, and I want to explain why before you ask.*
>
> *The larger one is everything that came in while the run was going. The smaller one — the one we
> lead with — is only what we actually chased.*
>
> *The difference is mostly this: there are invoices here where the gate refused to let us make
> contact. Frequency cap, outside contact hours, no consent on file. Some of those customers paid
> anyway. We could count that money. We don't, because we didn't earn it.*
>
> *The number is smaller because it's the one we can stand behind."*

That is clause 4 expressed as a number instead of a log, and it lands harder than the audit screen
because nobody expects a team to argue their own figure down. **Rehearse it until it is natural.**

### Expect these three

**"How do you know your agent caused that recovery?"**
That is what the two figures are for. The headline is causal — invoices this run acted on, and the
money that came in against them. The time-window figure sits beside it and is usually larger,
because money arrives for reasons that have nothing to do with us. Full divergence table in FR-17.

**"Was the batch pre-computed?"**
Yes. It is how any real system with rate limits behaves. The batch genuinely ran; it ran earlier.
Honesty here costs nothing and being caught costs everything.

**"What stops it going rogue?"**
Open `agent/registry.py` and `guardrails/gate.py` on screen. The LLM proposes a structured object
from a closed list; deterministic Python decides. Then show the refusal list — the argument is
stronger from the log than from the code.

### If asked about the big invoices

**"That ₹14L invoice is still open — did it fail?"**

> *"No — Razorpay caps a single payment link at around ₹5L, so we collect a receivable that size in
> tranches. ₹10L of it has come in and reconciled. The invoice stays open until the last tranche
> lands, which is why we report money recovered rather than invoices closed."*

Do not hide the ceiling. A real external constraint being handled is a better answer than never
being asked, and it is the reason option C was chosen over curating the data beneath it (ADR-006).

---

## Have a recorded fallback

A recorded run of the full demo, made under good conditions, kept ready. Tunnel drops, provider
rate limits and dashboard sessions all fail at inconvenient moments, and none of them says anything
about whether the system works.
