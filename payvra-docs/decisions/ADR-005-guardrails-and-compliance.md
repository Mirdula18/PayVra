# ADR-005 — Deterministic gate, seven ordered checks

**Status:** Accepted
**Date:** 2026-08-23

## Context

PAYVRA contacts real businesses about money. That puts it inside two regulatory perimeters:

- **RBI recovery-conduct norms** — contact only 08:00–19:00, no contact-list scraping, no shaming
  or public disclosure, no third-party disclosure, interactions digitally recorded
- **DPDP** — Rules notified 13 Nov 2025 (G.S.R. 846(E)); enforcement machinery from 13 Nov 2026;
  full substantive compliance from 13 May 2027; max penalty ₹250 crore per instance

Track 3's bar explicitly requires "compliant escalation, stopping rules, and an audit trail."

## Decision

Every action passes through `guardrails.gate()` before execution. Seven checks, fixed order,
all seven always evaluated even after one fails. Every verdict — pass and fail — written to
`audit_log`.

| # | Check | Fails when |
|---|---|---|
| 1 | `time_window` | outside 08:00–19:00 IST |
| 2 | `freshness` | invoice paid since the proposal was made |
| 3 | `consent` | channel not permitted, opted out, or quarantined |
| 4 | `frequency_cap` | >2 touches in a **rolling 7 days (IST)**, or >6 lifetime |
| 5 | `value_threshold` | above merchant threshold, or tier 3+, without approval |
| 6 | `content_policy` | banned phrase, missing amount / invoice no. / link / opt-out, or **no drafted message at all** |
| 7 | `stopping_rules` | settled, disputed, opted out, 3 broken promises, cap reached |

The gate contains **no LLM call** and is not bypassable. A failed check halts the action; there is
no "warn and continue."

**The gate runs after generation, immediately before the send.** Check 6 inspects a drafted
message, so there is nothing to inspect until one exists — an outbound action arriving with no
draft fails check 6 rather than passing vacuously. The per-action sequence is
`create link -> generate -> gate -> send`, interleaved one action at a time rather than batched by
stage, so the gate-to-send gap stays near zero and check 2's freshness re-read is a statement
about the moment of sending. See the data-flow note in `architecture/overview.md`.

**A passing gate writes `outcome = approved`, never `executed`.** Passing the gate is
authorisation, not delivery. If the gate claimed execution, a crash between gate and send would
leave the audit log asserting a message was sent that never was. The `executed` entry is written
by the delivery layer after the transport confirms the send. **The audit log may under-claim; it
may never over-claim.**

## Rationale

**Why deterministic:** a compliance control a language model can be talked out of is not a control.

**Why all seven always run:** a partial verdict is a weaker audit record and a weaker demo. Showing
a judge all seven verdicts on a blocked action is more convincing than showing the first failure.

**Why this order:** cheapest and most consequential first. `time_window` is a pure clock comparison.
`freshness` is one indexed read and prevents the single worst failure mode in the product —
chasing a customer who already paid.

**Why refusals are logged as carefully as sends:** the strongest answer to "what if the AI goes
rogue?" is opening the audit log filtered to `outcome = blocked` and showing every message the
system refused to send, with reasons.

## Alternatives considered

**LLM-based content moderation.** Rejected as the primary control — non-deterministic, and the
failure mode is silent. Acceptable only as an *additional* layer on top of the rule-based check.

**Check only at planning time.** Rejected. Hours pass between planning (01:30) and dispatch. The
invoice may have been paid, the customer may have opted out, the merchant may have paused. The gate
must run immediately before execution.

**Configurable checks that merchants can disable.** Rejected for compliance checks (1, 3, 6, 7) —
these are not preferences. Checks 4 and 5 are configurable within limits: caps can tighten but not
loosen past the ceiling; thresholds can lower but not be removed.

## Consequences

**Good:** Compliance is structural, not aspirational; audit log is complete by construction;
directly satisfies the Track 3 bar; the "refused actions" view is the strongest demo moment

**Bad:** Actions can be blocked for reasons a merchant finds inconvenient; adds latency to dispatch
(negligible — all checks are indexed reads)

**Non-negotiable:** no code path may send an outbound message without a `GateVerdict.passed == True`
in scope. Enforce this in the delivery layer's signature, not just by convention.
