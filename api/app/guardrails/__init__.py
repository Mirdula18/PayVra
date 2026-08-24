"""Guardrails: the gate. Deterministic, ordered, exhaustively logged.

Never contains an LLM call. Never bypassable. Never "warn and continue" — a failed check halts the
action. Every verdict, pass or fail, is written to audit_log by the caller.
"""
