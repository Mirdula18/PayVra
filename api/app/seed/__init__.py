"""Deterministic seed data (see agents/data-and-seed.md).

Run as ``python -m app.seed`` (or ``--reset`` / ``--demo``). Fixed ``RANDOM_SEED`` fixes the shape;
all dates anchor to ``today()`` so the batch is always correctly aged.
"""

from app.seed.builder import run

__all__ = ["run"]
