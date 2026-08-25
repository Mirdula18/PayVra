"""Guardrails: the gate. Deterministic, ordered, exhaustively logged.

Never contains an LLM call. Never bypassable. Never "warn and continue" — a failed check halts the
action. Every verdict, pass or fail, is written to audit_log by ``gate()`` itself.

This package deliberately re-exports nothing. ``from app.guardrails.gate import gate`` is the
import. Binding the ``gate`` *function* at package level would shadow the ``gate`` *module* of
the same name, so ``from app.guardrails import gate`` would mean different things depending on
import order — a trap in a package whose entire job is to be unambiguous.
"""
