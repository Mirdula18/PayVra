"""FastAPI application entrypoint.

Phase 0 exposes only ``GET /health``. The lifespan starts the in-process APScheduler so a running
process always has a live scheduler (ADR-007), and ``/health`` reports its status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.scheduler.registry import build_scheduler, health_snapshot, register_jobs


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    scheduler = build_scheduler()
    register_jobs(scheduler)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="PAYVRA API", version="0.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + scheduler status. A dead scheduler must be visible here, not silent."""
    return {"status": "ok", "scheduler": health_snapshot(app.state.scheduler)}
