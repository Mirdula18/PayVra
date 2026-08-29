"""The three Phase 8 screens. Read-only, server-rendered, no client framework.

**Tenant identity is resolved exactly as the API resolves it** -- from a credential, looked up
against ``merchants``, never from a path or query parameter. A browser cannot send an
``Authorization`` header on a plain navigation, so the token is carried in a cookie set by a small
form. That is still a credential the caller presents, resolved in one place, which is the property
``deps.current_merchant_id`` exists to guarantee (agents/backend.md ground rule 2). What is *not*
allowed, and is not done here, is naming a merchant in a URL.

``run`` is a query parameter, but it names a recovery run rather than a tenant, and every lookup
is scoped to the resolved merchant first -- a run belonging to someone else reads as absent, the
same 404-not-403 shape the API uses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.agent import metrics
from app.clock import IST
from app.db import get_db
from app.exceptions import AuthenticationError, NotFoundError
from app.models.audit_log import AuditLog
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.recovery_run import RecoveryRun
from app.money import paise_to_exact

router = APIRouter(prefix="/ui", tags=["ui"])

def _ist(value: datetime | None, fmt: str = "%d %b %H:%M") -> str:
    """Render a timestamp in IST. **Every time shown to a user is IST.**

    Postgres returns ``timestamptz`` in UTC, so formatting one directly showed 14:39 for a run
    started at 20:09 IST -- a five-and-a-half hour lie on a product whose central compliance claim
    is a *contact window in IST*. Nothing here is ever displayed in any other zone.
    """
    if value is None:
        return "—"
    return value.astimezone(IST).strftime(fmt)


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["rupees"] = paise_to_exact
templates.env.filters["ist"] = _ist

#: The cookie carrying the merchant token. Same value the API takes as a bearer token.
SESSION_COOKIE = "payvra_token"

#: Rows per screen. Enough to show the shape of a batch without a pagination control nobody will
#: click during a five-minute demo.
WORKLIST_LIMIT = 40
AUDIT_LIMIT = 200


def ui_merchant(
    db: Annotated[Session, Depends(get_db)],
    payvra_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> uuid.UUID:
    """Resolve the merchant from the session cookie, the same way the API resolves a bearer token.

    Fails closed: a missing, malformed, or unknown token is an authentication error, never a
    fallback to "some merchant". A token naming a merchant that does not exist must not quietly
    return an empty page.
    """
    if not payvra_token:
        raise AuthenticationError("no session")
    try:
        merchant_id = uuid.UUID(payvra_token.strip())
    except ValueError as exc:
        raise AuthenticationError("invalid session token") from exc

    exists = db.execute(select(Merchant.id).where(Merchant.id == merchant_id)).scalar_one_or_none()
    if exists is None:
        raise AuthenticationError("invalid session token")
    return merchant_id


UiMerchant = Annotated[uuid.UUID, Depends(ui_merchant)]
DbDep = Annotated[Session, Depends(get_db)]


def _resolve_run(db: Session, merchant_id: uuid.UUID, run: str | None) -> RecoveryRun | None:
    """The named run, or the merchant's most recent. Always scoped to the merchant first.

    A run id belonging to another merchant resolves to ``None`` rather than raising a distinct
    error -- "this exists but is not yours" leaks the existence of another tenant's data.
    """
    if run:
        try:
            run_id = uuid.UUID(run)
        except ValueError:
            return None
        return db.execute(
            select(RecoveryRun).where(
                RecoveryRun.id == run_id, RecoveryRun.merchant_id == merchant_id
            )
        ).scalar_one_or_none()

    return db.execute(
        select(RecoveryRun)
        .where(RecoveryRun.merchant_id == merchant_id)
        .order_by(desc(RecoveryRun.started_at))
        .limit(1)
    ).scalar_one_or_none()


def _recent_runs(db: Session, merchant_id: uuid.UUID, limit: int = 12) -> list[RecoveryRun]:
    return list(
        db.execute(
            select(RecoveryRun)
            .where(RecoveryRun.merchant_id == merchant_id)
            .order_by(desc(RecoveryRun.started_at))
            .limit(limit)
        ).scalars()
    )


def _merchant(db: Session, merchant_id: uuid.UUID) -> Merchant:
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:  # pragma: no cover - the dependency already proved it exists
        raise NotFoundError("merchant not found")
    return merchant


# --- session -------------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, db: DbDep) -> Any:
    """Paste a merchant token. The token *is* the merchant id -- a Phase 1 placeholder, unchanged.

    The candidate list is a development convenience and shows only what the seed created. It is
    not an auth bypass: the token still has to be presented and is still resolved against the
    database on every request.
    """
    candidates = list(
        db.execute(
            select(Merchant.id, Merchant.name)
            .join(Invoice, Invoice.merchant_id == Merchant.id)
            .group_by(Merchant.id, Merchant.name)
            .limit(5)
        )
    )
    return templates.TemplateResponse(
        request, "login.html", {"candidates": candidates, "title": "Sign in"}
    )


@router.post("/login")
def do_login(token: Annotated[str, Form()]) -> RedirectResponse:
    response = RedirectResponse(url="/ui/audit", status_code=303)
    response.set_cookie(SESSION_COOKIE, token.strip(), httponly=True, samesite="lax")
    return response


@router.get("/")
def index() -> RedirectResponse:
    """The audit log is the landing screen. It is the one that carries the argument."""
    return RedirectResponse(url="/ui/audit", status_code=303)


# --- screen 1: the ranked worklist ------------------------------------------------------------


@router.get("/worklist", response_class=HTMLResponse)
def worklist(request: Request, db: DbDep, merchant_id: UiMerchant) -> Any:
    """Ranked by recoverable money, with the reason for each position (clause 1 context)."""
    rows = list(
        db.execute(
            select(Invoice, Counterparty)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .where(
                Invoice.merchant_id == merchant_id,
                Invoice.outstanding_paise > 0,
            )
            .order_by(desc(Invoice.priority_score).nullslast(), desc(Invoice.days_past_due))
            .limit(WORKLIST_LIMIT)
        )
    )
    total_outstanding = sum(int(invoice.outstanding_paise) for invoice, _ in rows)
    return templates.TemplateResponse(
        request,
        "worklist.html",
        {
            "title": "Worklist",
            "merchant": _merchant(db, merchant_id),
            "rows": rows,
            "total_outstanding": total_outstanding,
            "unscored": sum(1 for invoice, _ in rows if invoice.priority_score is None),
        },
    )


# --- screen 2: the recovery figure ------------------------------------------------------------


@router.get("/recovery", response_class=HTMLResponse)
def recovery(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    run: Annotated[str | None, Query()] = None,
) -> Any:
    """Causal as the headline, time-window beside it, divergence explained on screen (clause 1)."""
    run_row = _resolve_run(db, merchant_id, run)
    figures = metrics.recovery_for_run(db, run_row.id) if run_row is not None else None
    return templates.TemplateResponse(
        request,
        "recovery.html",
        {
            "title": "Recovery",
            "merchant": _merchant(db, merchant_id),
            "run": run_row,
            "figures": figures,
            "runs": _recent_runs(db, merchant_id),
        },
    )


# --- screen 3: the audit log (the priority) ---------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    run: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query()] = None,
    show_scoring: Annotated[bool, Query()] = False,
) -> Any:
    """Refusals beside sends, with the rule that stopped each one (clauses 3 and 4).

    Deliberately **one list, not two tabs**: separating them would let a viewer read only the
    flattering half, which is the opposite of the argument the log exists to make.
    """
    run_row = _resolve_run(db, merchant_id, run) if run else None

    scope = [AuditLog.merchant_id == merchant_id]
    if run_row is not None:
        scope.append(AuditLog.inputs["recovery_run_id"].astext == str(run_row.id))
    if not show_scoring:
        # A rescore writes one entry per invoice (ADR-008 requires the feature vector on record),
        # so 116 scoring rows bury 21 refusals on a seeded book. They are legitimate audit entries
        # and are still reachable, but they are not *actions*, and this screen exists to show what
        # the system did and refused to do. Hiding them by default is the difference between a
        # compliance argument and a log dump.
        scope.append(AuditLog.action_type.not_like("score.%"))

    # Counted over the whole filtered set, not the fetched page. Deriving them from `entries`
    # made every number silently wrong past AUDIT_LIMIT -- a filter chip reading "6" when the
    # real answer was 60 is worse than no chip.
    counts = {
        str(row[0]): int(row[1])
        for row in db.execute(
            select(AuditLog.outcome, func.count())
            .where(*scope)
            .group_by(AuditLog.outcome)
        )
    }
    scoring_total = int(
        db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.merchant_id == merchant_id, AuditLog.action_type.like("score.%"))
        ).scalar_one()
    )

    stmt = select(AuditLog).where(*scope)
    if outcome:
        stmt = stmt.where(AuditLog.outcome == outcome)
    entries = list(db.execute(stmt.order_by(desc(AuditLog.id)).limit(AUDIT_LIMIT)).scalars())

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "title": "Audit log",
            "merchant": _merchant(db, merchant_id),
            "entries": entries,
            "counts": counts,
            "total": sum(counts.values()),
            "scoring_total": scoring_total,
            "show_scoring": show_scoring,
            "truncated": len(entries) >= AUDIT_LIMIT,
            "run": run_row,
            "runs": _recent_runs(db, merchant_id),
            "outcome": outcome,
        },
    )
