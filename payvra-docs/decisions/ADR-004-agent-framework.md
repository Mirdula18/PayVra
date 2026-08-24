# ADR-004 — LangGraph over raw tool-calling or CrewAI

**Status:** Accepted
**Date:** 2026-08-23

## Context

ADR-001 requires an explicit, inspectable state machine with a validation step between proposal
and execution. We need per-node observability so the audit log can record what the agent saw and
why it chose an action.

## Decision

Use **LangGraph** to define the `observe -> diagnose -> plan -> validate -> queue` graph, with a
`fallback` branch off `validate`.

## Rationale

LangGraph models the loop as an explicit graph with typed state, which is exactly the shape
ADR-001 mandates. Nodes are plain Python functions, so `diagnose` and `validate` contain no LLM
call at all and are unit-testable in isolation.

Its checkpointing gives us per-node state snapshots that map almost directly onto audit log
entries — the framework's trace *is* the audit trail, which saves real implementation time.

Conditional edges express "reject → fallback" natively rather than as ad-hoc control flow.

## Alternatives considered

**Raw LLM tool-calling loop, hand-rolled.** Fewest dependencies. Rejected: we would rebuild
state management, checkpointing, and conditional routing by hand, and the audit-trail plumbing
would be entirely bespoke.

**CrewAI / AutoGen.** Multi-agent orchestration frameworks. Rejected: they optimise for agents
talking to each other. We have one agent making one bounded decision. Wrong abstraction, and their
autonomy defaults fight ADR-001.

**Plain state machine, no LLM framework.** Genuinely tempting. Rejected only because LangGraph's
tracing saves audit-log work. If LangGraph proves heavy in Phase 6, this is the fallback — and the
architecture is deliberately designed so that swap costs a day, not a week.

## Consequences

**Good:** Explicit graph matching the documented architecture; testable nodes; tracing feeds the
audit log; conditional routing is first-class

**Bad:** Dependency weight; LangChain-adjacent APIs churn between versions

**Mitigation:** pin the version. Keep LangGraph confined to `agent/graph.py` and `agent/nodes.py`.
No LangGraph imports anywhere else in the codebase — that boundary is what makes the swap cheap.
