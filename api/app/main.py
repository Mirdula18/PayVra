"""FastAPI application entrypoint.

Phase 4 exposes ``GET /health`` plus the ingestion, worklist, reconciliation and webhook
endpoints under ``/api/v1``.
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
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.exceptions import (
    AuthenticationError,
    IngestionError,
    NotFoundError,
    PayvraError,
    ValidationError,
)
from app.generation import llm
from app.routers import batches, invoices, webhooks, worklist
from app.scheduler.registry import build_scheduler, health_snapshot, register_jobs
from app.ui import routes as ui_routes

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


@app.middleware("http")
async def _mark_request_path(request: Request, call_next: Any) -> Any:
    """Mark request handling so ``generation.llm`` can refuse to run inside it.

    agents/agent-engine.md forbids an LLM call in a request-response path: a multi-second model
    call holds a worker, and inside the webhook handler it would blow the 200 ms acknowledgement
    budget and turn one payment into a Razorpay retry storm. This makes that rule enforceable in
    code rather than a comment someone has to remember -- the same reasoning as delivery/sender.py
    taking its gate verdict as a required argument.
    """
    with llm.request_path():
        return await call_next(request)


# Server-rendered Phase 8 screens. No API_PREFIX: these are pages, not the versioned JSON API,
# and they must stay separable from it — a UI route is never a contract anyone integrates against.
app.include_router(ui_routes.router)

app.include_router(batches.router, prefix=API_PREFIX)
app.include_router(worklist.router, prefix=API_PREFIX)
app.include_router(invoices.router, prefix=API_PREFIX)
# Webhooks carry no auth header; they authenticate by signature (api-contracts.md).
app.include_router(webhooks.router, prefix=API_PREFIX)


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


@app.exception_handler(PayvraError)
async def payvra_error_handler(request: Request, exc: PayvraError) -> Any:
    # A browser navigating to a page with no session should be shown the sign-in form, not a JSON
    # error envelope. Only the UI surface behaves this way; the API still fails with 401 as its
    # contract says, which is what the tenant-isolation tests pin down.
    if isinstance(exc, AuthenticationError) and request.url.path.startswith("/ui"):
        return RedirectResponse(url="/ui/login", status_code=303)

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
