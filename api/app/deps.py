"""Shared FastAPI dependencies (auth, tenant scoping).

Phase 0 only re-exports the DB session dependency. Real auth and ``merchant_id`` scoping from the
auth token — never from a request parameter — arrive with the routers in a later phase.
"""

from __future__ import annotations

from app.db import get_db

__all__ = ["get_db"]
