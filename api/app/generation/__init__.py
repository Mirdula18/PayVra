"""Generation: produce message text. Schema-constrained LLM with a deterministic template fallback.

Never returns unvalidated LLM output. Never runs in a request-response path. litellm imports are
confined to generation/llm.py.

The public entry point is :func:`app.generation.drafter.generate`. Templates come first and are
always available; the LLM path is an optimisation on top of them, never a dependency — with
``LLM_ENABLED=false`` the whole pipeline runs on templates alone, with litellm uninstalled.
"""
