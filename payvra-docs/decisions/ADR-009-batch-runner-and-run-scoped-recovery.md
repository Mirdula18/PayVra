# ADR-009 — A synchronous batch runner, and run-scoped recovery measurement

**Status:** Accepted
**Date:** 2026-08-26
**Partially supersedes:** ADR-004 (the execution split only — the node structure stands)
**Defers:** ADR-007's APScheduler as the execution trigger, for Phase 6 only

## Context

Phases 3, 4 and 5 are complete and independently verified against live services: the seven-check
gate blocks correctly (79 tests), the Razorpay rail creates links and reconciles a real signed
webhook end to end, and drafting produces validated messages from a live model (16/16 checks).

None of them has a production caller. Only scripts and the test suite invoke them. Nothing has been
recovered by the system acting on its own.

`requirements/track3-bar.md` is the constraint that matters now. Of the bar's four clauses, two are
built but unevidenced and two are at zero. All four unlock from the same missing piece: something
that walks a ranked worklist and actually acts.

The architecture as documented does not produce that in one step. `architecture/agent-loop.md` and
ADR-004 specify a two-stage execution model: `plan_day` runs the graph and **queues** actions, and
a separate `dispatch_window` job claims them later with `SELECT ... FOR UPDATE SKIP LOCKED` and
executes them behind the gate. ADR-007 makes APScheduler the trigger for both.

That model is correct for production and wrong for the deadline. It requires a scheduler, a claim
protocol, a retry layer and a dispatch window to all be right before a single rupee moves, and it
makes a demo depend on wall-clock timing. It also cannot be verified by hand in one command, which
breaks the project's working agreement — finish a phase, make it runnable, verify by hand, commit.

## Decision

**Phase 6 delivers a synchronous batch runner: one pass, one command, one run identifier.**

For each account in the ranked worklist (top N, configurable), in a single process, in order:

```
diagnose -> propose exactly ONE action from the closed registry
         -> submit to the Phase 3 gate
         -> if approved: create Razorpay link, generate message, record
         -> if refused: persist the refusal with its reason, continue
```

The run is identified by **`recovery_run_id`**, stored in a new `recovery_runs` table. Actions and
audit entries produced by a run carry that id, which is what makes the bar's "across a batch"
measurable.

**Explicit non-goals for Phase 6:** a scheduler, an async queue, a retry layer. Each is deferred,
not rejected.

**Recovery is measured two ways**, both scoped to `recovery_run_id`:

* **Causal (headline)** — invoices that received an action in this run and subsequently settled.
* **Time-window (context)** — invoices that settled within the run's wall-clock window.

**The contact-hours window becomes configurable**, defaulting unchanged to 08:00–19:00 IST, with
three conditions in the decision itself: the gate still executes with no bypass path, the window
widens only by explicit environment variable, and an active override is written into the audit log.

## Rationale

**Why synchronous.** The queue-then-dispatch split exists to survive partial failure across
scheduled runs at scale. Phase 6's job is different: prove the loop closes, with evidence, in a
form a person can run and inspect. A synchronous pass is inspectable by construction — the run
either produced results or it did not, and the output is the audit trail. There is no window to
wait for and no queue state to reason about when something looks wrong on stage.

**Why `recovery_run_id` and not `batch_id`.** `batches` already exists and means *an uploaded
invoice file* — `filename`, `column_mapping`, `row_count`. Reusing `batch_id` for a recovery run
would give one term two meanings in the same schema, and the ambiguity would surface in exactly the
place it hurts most: a judge asking what a number is scoped to. "The batch runner" remains the
spoken name; `recovery_run_id` is what the columns say.

**Why two recovery figures.** The honest number and the impressive number are usually not the same,
and being unable to explain the gap is worse than the gap. Causal attribution answers "how do you
know your agent caused that?" — the question this bar invites. Time-window catches money that
arrived during the run for unrelated reasons, which is real recovery but not *our* recovery.
Reporting only the larger figure invites a challenge that cannot be answered; reporting only the
smaller one understates a working system. Report both, name which is which.

**Why the node structure survives.** ADR-004's `observe -> diagnose -> plan -> validate` sequence is
about the discipline that the LLM proposes and deterministic code disposes (ADR-001). That is
untouched here and remains non-negotiable. What changes is only what happens after `validate`:
instead of writing a queued action for a later dispatcher, the runner proceeds straight to the gate
and, if approved, to execution. The graph keeps its shape; the edge after it is shorter.

**Why the contact-hours window becomes configurable rather than staying fixed.** Gate check 1
refuses every outbound action outside 08:00–19:00 IST. That is correct behaviour and stays the
default. But it means a rehearsal at 21:00 IST produces a run where every action is refused: a
flawless demonstration of clause 3 and a zero for clause 1, from the same command. The alternative
to configuration is a bypass path, and a gate with a bypass is not a gate. Making the window a
value the gate reads — rather than a rule the gate can skip — keeps the check in force at all
times, and the audit entry means an out-of-window run is compliant *by record*: the log shows the
window was widened, by whom, and when. A judge can see the override rather than being unable to
tell one happened.

## Alternatives considered

**Build the scheduler and dispatcher as specified (ADR-004 + ADR-007).** The right end state.
Rejected for Phase 6 because it front-loads infrastructure that no clause of the bar asks for, and
because a demo whose central moment depends on a cron window firing correctly is a demo with an
avoidable failure mode. Deferred, not abandoned — the runner is written as a plain callable taking
`merchant_id` and a limit, which is what a scheduled job would call anyway.

**Reuse `batches.id` for recovery runs.** Fewer tables. Rejected: it overloads a term that already
means something else, and ingestion batches and recovery runs have genuinely different lifecycles —
a file is imported once, a worklist is run against repeatedly.

**Time-window attribution only.** Simplest, one query, no join through actions. Rejected as the
headline: it credits the agent with every payment that happened to land during the run, which is
indefensible under the one question this bar most invites. Kept as the secondary figure.

**Causal attribution only.** Most defensible. Rejected as the sole figure: it silently discards
real recovered money and would make the system look weaker than it is, with no way to show why.

**A fixed contact-hours window with a documented "rehearse before 19:00" rule.** No code change,
no override to explain. Genuinely tempting, and it is still the recommended path — rehearse in
window. Rejected as the *only* mitigation because it makes a hard external constraint (when the
judging session happens) into a single point of failure with no recovery.

## Consequences

**Good**

* All four bar clauses become evidenceable from one command
* The run is inspectable by construction; the audit trail is the output, not a side channel
* `recovery_run_id` makes "across a batch" a real scope rather than a phrase
* Phase 3, 4 and 5 finally get a production caller, which is what turns "built" into "proven"
* The runner ports to a scheduled job unchanged — it is a plain callable

**Bad**

* A long run is a long-running process with no resumption; interrupting it leaves work undone
  (idempotency makes re-running safe, but progress is not checkpointed)
* No retry layer means a transient provider failure costs that account this run
* Two recovery figures need explaining rather than one, which is a slide, not a bug
* The configurable window is a knob that can be set wrongly; the audit entry is the mitigation

**Neutral**

* ADR-004's node structure and ADR-007's APScheduler remain the documented end state. Neither is
  rejected. Phase 6 simply does not require them, and the bar does not ask for them.

## Schema implications

Three changes, specified in FR-16 and FR-17. **Documentation only — no migration is written by this
ADR.**

| Change | Why |
|---|---|
| New `recovery_runs` table | The run itself: id, merchant, started/finished, limit, window override, counts |
| `actions.recovery_run_id` | Causal attribution, and "what did this run do?" |
| `audit_log.recovery_run_id` | Filter the trail to one run — the clause 3 and 4 demo |

`invoices.settled_at` already exists and needs no change; it is what both recovery figures read.
