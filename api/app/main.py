"""FastAPI application entrypoint.

Phase 2 exposes ``GET /health`` plus the ingestion and worklist endpoints under ``/api/v1``.
The lifespan starts
the in-process APScheduler so a running process always has a live scheduler (ADR-007), and
``/health`` reports its status.

Every domain exception is translated to the error envelope from architecture/api-contracts.md.
A stack trace must never reach a client (agents/backend.md), so the catch-all handler logs the
exception and returns an opaque ``INTERNAL_ERROR``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.exceptions import (
    AuthenticationError,
    IngestionError,
    NotFoundError,
    PayvraError,
    ValidationError,
)
from app.routers import batches, worklist
from app.scheduler.registry import build_scheduler, health_snapshot, register_jobs

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# Domain exception -> (HTTP status, error envelope code).
_ERROR_MAP: dict[type[PayvraError], tuple[int, str]] = {
    AuthenticationError: (401, "UNAUTHENTICATED"),
    NotFoundError: (404, "NOT_FOUND"),
    ValidationError: (422, "VALIDATION_FAILED"),
    IngestionError: (422, "FILE_UNREADABLE"),
}


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


app = FastAPI(title="PAYVRA API", version="0.1.0", lifespan=lifespan)
app.include_router(batches.router, prefix=API_PREFIX)
app.include_router(worklist.router, prefix=API_PREFIX)


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


@app.exception_handler(PayvraError)
async def payvra_error_handler(request: Request, exc: PayvraError) -> JSONResponse:
    for exc_type, (http_status, code) in _ERROR_MAP.items():
        if isinstance(exc, exc_type):
            return JSONResponse(status_code=http_status, content=_envelope(code, str(exc)))
    logger.exception("unhandled domain error", exc_info=exc)
    return JSONResponse(
        status_code=500, content=_envelope("INTERNAL_ERROR", "an unexpected error occurred")
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own body/query validation, rendered in our envelope rather than its default."""
    return JSONResponse(
        status_code=422,
        content=_envelope(
            "VALIDATION_FAILED",
            "request payload failed validation",
            {"errors": [{k: str(v) for k, v in e.items()} for e in exc.errors()]},
        ),
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500, content=_envelope("INTERNAL_ERROR", "an unexpected error occurred")
    )


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + scheduler status. A dead scheduler must be visible here, not silent."""
    return {
        "status": "ok",
        "env": settings.env,
        "scheduler": health_snapshot(app.state.scheduler),
    }
