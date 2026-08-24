# ADR-001 — Guardrailed agent loop, not a free-roaming agent

**Status:** Accepted
**Date:** 2026-08-23

## Context

Track 3 asks for an agent that "detects revenue at risk, determines the right intervention, and
executes a bounded recovery workflow." The word that matters is **bounded**. The judging bar
demands "compliant escalation, stopping rules, and an audit trail."

Meanwhile, the actions this system takes are irreversible and reputational. A badly worded message
to a customer cannot be unsent. Chasing someone who already paid damages a commercial relationship.
Contacting someone outside RBI-permitted hours is a conduct breach.

## Decision

The LLM produces a structured proposal and nothing else. A deterministic policy engine validates
the proposal against a closed tool registry and an explicit state machine, then a guardrail gate
runs seven ordered checks immediately before execution. Only after all of that does a tool run.

The LLM has no ability to call an API, send a message, or move money.

## Alternatives considered

**Free-roaming ReAct agent with tool access.** Rejected. Faster to build, impossible to audit,
and there is no defensible answer to "what stops it doing something stupid?" One hallucinated tool
call on stage ends the demo.

**Pure rules engine, no LLM.** Rejected. Cannot classify Hinglish replies, cannot extract promised
dates from free text, cannot adapt tone. Also fails the "meaningful AI" bar — Track 3 is explicitly
an AI track.

**Human-in-the-loop on every action.** Rejected. Defeats the product. The value proposition is that
Priya spends 3 minutes a day, not 3 hours. Approval is reserved for escalations and high-value
accounts.

## Consequences

**Good:**
- Every action is explainable and logged, including refusals
- The product still functions with every LLM provider down (deterministic fallback)
- Compliance checks cannot be argued away by a model
- Directly answers the judging bar's "bounded / stopping rules / audit trail" language

**Bad:**
- More code than a naive agent
- The state machine must be maintained as the product grows
- Constrains what the agent can invent — deliberately

**Follow-on:** ADR-004 (framework), ADR-005 (the gate).
