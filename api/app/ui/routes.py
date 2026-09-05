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
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import BigInteger, desc, func, select
from sqlalchemy.orm import Session

from app.agent import metrics
from app.clock import IST
from app.config import settings
from app.db import get_db
from app.enums import RecoveryState
from app.exceptions import AuthenticationError, NotFoundError
from app.guardrails import gate
from app.models.action import Action
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.message import Message
from app.models.payment_link import PaymentLink
from app.models.promise import Promise
from app.models.recovery_run import RecoveryRun
from app.money import paise_to_exact
from app.reconciliation.manual import OFFLINE_METHODS
from app.ui import actions as human

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


#: Runs offered in the pickers. Raised from 12 once the UI could start runs of its own: a user
#: clicking "Run recovery" a few times used to push a named, money-bearing run off the end of the
#: list, and the only way back was a hand-typed ``?run=`` URL.
RUN_PICKER_LIMIT = 25


def _recent_runs(
    db: Session, merchant_id: uuid.UUID, limit: int = RUN_PICKER_LIMIT
) -> list[RecoveryRun]:
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


def _all_merchants(db: Session) -> list[tuple[uuid.UUID, str, int]]:
    """Every merchant with invoices, for the header switcher and the sign-in list.

    **This is a convenience, not an authorisation decision.** Listing the tenants a seeded
    development database happens to contain is not the same as granting access to them: the
    switcher posts to the sign-in endpoint, which sets a cookie, and every data route still
    resolves identity from that cookie through ``ui_merchant``. No screen reads a merchant from a
    path, query or body -- that property is what makes the placeholder auth replaceable in one
    function, and a switcher that bypassed it would quietly cost the whole guarantee.

    Ordered by size, and carrying the invoice count. A development database accumulates tenants
    the test suite created -- several of them named the same thing, holding one invoice each. A
    bare list of names makes those indistinguishable from the real book; the count makes the
    answer obvious without hiding anything that is genuinely there.
    """
    return [
        (row[0], row[1], int(row[2]))
        for row in db.execute(
            select(Merchant.id, Merchant.name, func.count(Invoice.id))
            .join(Invoice, Invoice.merchant_id == Merchant.id)
            .group_by(Merchant.id, Merchant.name)
            .order_by(desc(func.count(Invoice.id)), Merchant.name)
        )
    ]


#: Recovery states grouped into the operational buckets the queue screen works in. Two states
#: share a bucket where the operator's next move is the same: a broken promise and an escalation
#: both mean "a person has to decide", and the distinction is on the row, not in the tab.
QUEUE_BUCKETS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("review", "Needs review", ("human_review",), "warn"),
    ("escalated", "Escalated", ("escalated", "broken_promise"), "stop"),
    ("running", "Agent handling", ("chasing", "nudged", "promised"), "info"),
    ("queued", "Not started", ("not_started",), "mute"),
    ("stopped", "Stopped", ("stopped",), "stop"),
    ("done", "Recovered", ("settled",), "ok"),
)

#: Cause codes as a finance team would say them. The enum is the record; this is the label.
CAUSE_LABELS = {
    "oversight": "Overlooked",
    "cash_crunch": "Cash crunch",
    "dispute": "Disputed",
    "wrong_contact": "Wrong contact",
    "awaiting_docs": "Awaiting docs",
    "refusal": "Refusing to pay",
    "unknown": "Undiagnosed",
}


def _nav_counts(db: Session, merchant_id: uuid.UUID) -> dict[str, int]:
    """The two sidebar badges. Cheap enough to run on every screen."""
    at_risk = int(
        db.execute(
            select(func.count()).select_from(Invoice).where(
                Invoice.merchant_id == merchant_id,
                Invoice.outstanding_paise > 0,
                Invoice.recovery_state != RecoveryState.SETTLED.value,
            )
        ).scalar_one()
    )
    review = int(
        db.execute(
            select(func.count()).select_from(Invoice).where(
                Invoice.merchant_id == merchant_id,
                Invoice.recovery_state == RecoveryState.HUMAN_REVIEW.value,
            )
        ).scalar_one()
    )
    return {"nav_at_risk": at_risk, "nav_review": review}


def _shell(
    db: Session,
    merchant_id: uuid.UUID,
    active: str,
    title: str,
    heading: str | None = None,
    subhead: str | None = None,
) -> dict[str, Any]:
    """Context every signed-in screen needs: who you are, who else there is, where you are."""
    return {
        "title": title,
        "heading": heading or title,
        "subhead": subhead,
        "active": active,
        "merchant": _merchant(db, merchant_id),
        "merchants": _all_merchants(db),
        "cause_labels": CAUSE_LABELS,
        **_nav_counts(db, merchant_id),
    }


# --- session -------------------------------------------------------------------------------------


#: Where a sign-in or a switch may send you afterwards. A redirect target that came straight from
#: a form field is an open-redirect; an allowlist of our own screens is the cheap fix.
SAFE_REDIRECTS = frozenset({"/ui/home", "/ui/worklist", "/ui/audit", "/ui/recovery"})


def _safe_next(value: str | None) -> str:
    return value if value in SAFE_REDIRECTS else "/ui/home"


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, db: DbDep) -> Any:
    """Pick a merchant, or paste a token. The token *is* the merchant id -- Phase 1, unchanged.

    Picking from the list posts the same value to the same endpoint that a pasted token does.
    It removes the copy-paste, not the credential.
    """
    return templates.TemplateResponse(
        request,
        "login.html",
        {"candidates": _all_merchants(db), "title": "Sign in", "active": "login"},
    )


@router.post("/login")
def do_login(
    token: Annotated[str, Form()],
    next: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Set the session cookie. Also the switch-merchant endpoint -- switching *is* signing in.

    ``next`` returns you to the screen you were reading, so changing client mid-demo does not
    also lose your place.
    """
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    response.set_cookie(SESSION_COOKIE, token.strip(), httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/ui/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/ui/home", status_code=303)


# --- home: what the system is, and the evidence that it works ----------------------------------


def _kpis(db: Session, merchant_id: uuid.UUID) -> dict[str, Any]:
    """The six numbers the command centre leads with.

    ``recovered`` is the sum of reconciled settlements on this book -- the money that actually
    arrived. It is deliberately *not* a sum of per-run causal figures: causal is unbounded above,
    so two runs that both chased an invoice both report what it later paid, and adding those
    columns would count the same rupee twice. Per-run attribution lives on the batch screen; this
    is the book-level total, and the two answer different questions.
    """
    at_risk_rows, at_risk_amount, customers = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Invoice.outstanding_paise), 0),
            func.count(func.distinct(Invoice.counterparty_id)),
        ).where(
            Invoice.merchant_id == merchant_id,
            Invoice.outstanding_paise > 0,
            Invoice.recovery_state != RecoveryState.SETTLED.value,
        )
    ).one()

    recovered = int(
        db.execute(
            select(
                func.coalesce(
                    func.sum(AuditLog.inputs["amount_paise"].astext.cast(BigInteger)), 0
                )
            ).where(
                AuditLog.merchant_id == merchant_id,
                AuditLog.action_type == metrics.SETTLE_ACTION_TYPE,
            )
        ).scalar_one()
    )

    active = int(
        db.execute(
            select(func.count()).select_from(Invoice).where(
                Invoice.merchant_id == merchant_id,
                Invoice.recovery_state.in_(
                    [
                        RecoveryState.NUDGED.value,
                        RecoveryState.CHASING.value,
                        RecoveryState.PROMISED.value,
                        RecoveryState.ESCALATED.value,
                    ]
                ),
            )
        ).scalar_one()
    )

    # Recovery rate on money, not invoice count: a tranche collection is a partial success and
    # counting closed invoices would score it zero.
    addressable = int(at_risk_amount) + recovered
    rate = (recovered / addressable * 100) if addressable else 0.0

    return {
        "at_risk_amount": int(at_risk_amount),
        "at_risk_rows": int(at_risk_rows),
        "customers": int(customers),
        "recovered": recovered,
        "active": active,
        "rate": rate,
    }


def _risk_breakdown(db: Session, merchant_id: uuid.UUID) -> list[dict[str, Any]]:
    """Money at risk by diagnosed cause -- the "why is it happening" panel."""
    rows = db.execute(
        select(
            Invoice.inferred_cause,
            func.count(),
            func.coalesce(func.sum(Invoice.outstanding_paise), 0),
        )
        .where(
            Invoice.merchant_id == merchant_id,
            Invoice.outstanding_paise > 0,
            Invoice.recovery_state != RecoveryState.SETTLED.value,
        )
        .group_by(Invoice.inferred_cause)
        .order_by(desc(func.coalesce(func.sum(Invoice.outstanding_paise), 0)))
    ).all()
    total = sum(int(r[2]) for r in rows) or 1
    return [
        {
            "cause": str(r[0]),
            "label": CAUSE_LABELS.get(str(r[0]), str(r[0]).replace("_", " ").title()),
            "count": int(r[1]),
            "amount": int(r[2]),
            "pct": int(r[2]) / total * 100,
        }
        for r in rows
    ]


@router.get("/home", response_class=HTMLResponse)
def home(request: Request, db: DbDep, merchant_id: UiMerchant) -> Any:
    """The landing screen: what this does, and the four numbers that prove it.

    Every figure here is a link to the screen that shows its working. The point of the page is
    that someone who has never seen the product can read it top to bottom and understand what the
    system claims and where to go to check the claim.
    """
    runs = _recent_runs(db, merchant_id)

    # Causal recovery per run, newest first -- **not** a single "best" figure.
    #
    # A headline that picked the largest causal number would pick a one-account run over a
    # twenty-account one, because the tranche invoice alone is worth more than a whole batch.
    # That is a bigger number attached to a smaller claim, and putting it on the landing screen
    # would make the product argue against itself.
    #
    # Causal is also unbounded above, so two runs that both acted on an invoice both report the
    # money it later paid. Per-run attribution is correct; **the column does not add up**, which
    # is exactly why it is shown as a list of runs rather than reduced to one total.
    #
    # Dry runs are excluded here and counted instead. They contacted nobody and claim nothing by
    # definition, so on a landing screen they are rehearsal noise -- on this book they are more
    # than half the rows, all of them zeroes. The audit and recovery screens still list them.
    #
    # Bounded by _recent_runs (12), a few small queries each.
    real_runs = [r for r in runs if not r.dry_run]
    run_figures = [(r, metrics.recovery_for_run(db, r.id)) for r in real_runs]

    outcome_counts = {
        str(row[0]): int(row[1])
        for row in db.execute(
            select(AuditLog.outcome, func.count())
            .where(
                AuditLog.merchant_id == merchant_id,
                AuditLog.action_type.not_like("score.%"),
            )
            .group_by(AuditLog.outcome)
        )
    }

    book = db.execute(
        select(func.count(), func.coalesce(func.sum(Invoice.outstanding_paise), 0)).where(
            Invoice.merchant_id == merchant_id, Invoice.outstanding_paise > 0
        )
    ).one()

    # Distinct gate checks that actually refused something. "Five of the seven fired" is a
    # stronger and more honest line than "seven checks exist", and it is only true if counted.
    # Reduced in Python rather than SQL: jsonb_array_elements is set-returning and does not
    # compose with count(distinct ...), and the blocked set is small enough that it does not
    # matter which side of the wire does the work.
    fired: set[str] = set()
    for (verdicts,) in db.execute(
        select(AuditLog.gate_verdicts).where(
            AuditLog.merchant_id == merchant_id, AuditLog.outcome == "blocked"
        )
    ):
        for verdict in verdicts or []:
            if isinstance(verdict, dict) and verdict.get("passed") is False:
                fired.add(str(verdict.get("check")))

    # The activity feed: what the agent has been doing, newest first.
    feed = list(
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.merchant_id == merchant_id,
                AuditLog.action_type.not_like("score.%"),
            )
            .order_by(desc(AuditLog.id))
            .limit(12)
        ).scalars()
    )

    return templates.TemplateResponse(
        request,
        "home.html",
        _shell(
            db,
            merchant_id,
            "home",
            "Overview",
            subhead=f"{merchant.name}" if (merchant := _merchant(db, merchant_id)) else None,
        )
        | _kpis(db, merchant_id)
        | {
            "run_figures": run_figures,
            "dry_runs_hidden": len(runs) - len(real_runs),
            "counts": outcome_counts,
            "audit_total": sum(outcome_counts.values()),
            "open_invoices": int(book[0]),
            "outstanding": int(book[1]),
            "rules_fired": sorted(fired),
            "gate_check_total": len(gate.CHECKS),
            "breakdown": _risk_breakdown(db, merchant_id),
            "feed": feed,
        },
    )


# --- recovery queue: the operations console ----------------------------------------------------


@router.get("/queue", response_class=HTMLResponse)
def queue(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    bucket: Annotated[str | None, Query()] = None,
) -> Any:
    """Work grouped by what the operator has to do about it, not by database state."""
    counts: dict[str, tuple[int, int]] = {}
    for key, _label, states, _tone in QUEUE_BUCKETS:
        row = db.execute(
            select(func.count(), func.coalesce(func.sum(Invoice.outstanding_paise), 0)).where(
                Invoice.merchant_id == merchant_id,
                Invoice.recovery_state.in_(list(states)),
            )
        ).one()
        counts[key] = (int(row[0]), int(row[1]))

    selected = bucket if any(b[0] == bucket for b in QUEUE_BUCKETS) else QUEUE_BUCKETS[0][0]
    states = next(b[2] for b in QUEUE_BUCKETS if b[0] == selected)

    rows = list(
        db.execute(
            select(Invoice, Counterparty)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .where(
                Invoice.merchant_id == merchant_id,
                Invoice.recovery_state.in_(list(states)),
            )
            .order_by(desc(Invoice.outstanding_paise))
            .limit(WORKLIST_LIMIT)
        )
    )

    return templates.TemplateResponse(
        request,
        "queue.html",
        _shell(db, merchant_id, "queue", "Recovery Queue")
        | {"buckets": QUEUE_BUCKETS, "counts": counts, "selected": selected, "rows": rows},
    )


# --- batches: measured money recovered, per run ------------------------------------------------


@router.get("/batches", response_class=HTMLResponse)
def batches(request: Request, db: DbDep, merchant_id: UiMerchant) -> Any:
    """Every batch the agent has run, with what each one recovered."""
    runs = _recent_runs(db, merchant_id)
    rows = [(r, metrics.recovery_for_run(db, r.id)) for r in runs]
    return templates.TemplateResponse(
        request,
        "batches.html",
        _shell(db, merchant_id, "batches", "Batches")
        | {"rows": rows, "gate_check_total": len(gate.CHECKS)},
    )


# --- analytics ---------------------------------------------------------------------------------


@router.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request, db: DbDep, merchant_id: UiMerchant) -> Any:
    """Business impact, and the shape of what the gate refused."""
    runs = _recent_runs(db, merchant_id)
    real = [r for r in runs if not r.dry_run]
    figures = [metrics.recovery_for_run(db, r.id) for r in real]

    refusals: dict[str, int] = {}
    for (verdicts,) in db.execute(
        select(AuditLog.gate_verdicts).where(
            AuditLog.merchant_id == merchant_id, AuditLog.outcome == "blocked"
        )
    ):
        for v in verdicts or []:
            if isinstance(v, dict) and v.get("passed") is False:
                refusals[str(v.get("check"))] = refusals.get(str(v.get("check")), 0) + 1

    settlements = list(
        db.execute(
            select(AuditLog.created_at, AuditLog.inputs["amount_paise"].astext.cast(BigInteger))
            .where(
                AuditLog.merchant_id == merchant_id,
                AuditLog.action_type == metrics.SETTLE_ACTION_TYPE,
            )
            .order_by(AuditLog.created_at)
        )
    )

    outcome_counts = {
        str(row[0]): int(row[1])
        for row in db.execute(
            select(AuditLog.outcome, func.count())
            .where(
                AuditLog.merchant_id == merchant_id,
                AuditLog.action_type.not_like("score.%"),
            )
            .group_by(AuditLog.outcome)
        )
    }

    return templates.TemplateResponse(
        request,
        "analytics.html",
        _shell(db, merchant_id, "analytics", "Analytics")
        | _kpis(db, merchant_id)
        | {
            "runs_total": len(runs),
            "runs_real": len(real),
            "batch_accounts": sum(f.accounts_considered for f in figures),
            "batch_executed": sum(f.actions_executed for f in figures),
            "batch_refused": sum(f.actions_refused for f in figures),
            "refusals": sorted(refusals.items(), key=lambda kv: -kv[1]),
            "refusals_max": max(refusals.values()) if refusals else 1,
            "settlements": settlements,
            "breakdown": _risk_breakdown(db, merchant_id),
            "counts": outcome_counts,
        },
    )


# --- policy: the rules, visible ----------------------------------------------------------------


@router.get("/policy", response_class=HTMLResponse)
def policy(request: Request, db: DbDep, merchant_id: UiMerchant) -> Any:
    """The stopping and escalation rules as cards, with how often each has fired.

    Compliance claims are worth nothing filed under a paragraph in a README. The seven checks are
    the product's central argument, so they get a screen where each one states its rule, its
    current setting, and its hit count on this book.
    """
    merchant = _merchant(db, merchant_id)
    fired: dict[str, int] = {}
    for (verdicts,) in db.execute(
        select(AuditLog.gate_verdicts).where(
            AuditLog.merchant_id == merchant_id, AuditLog.outcome == "blocked"
        )
    ):
        for v in verdicts or []:
            if isinstance(v, dict) and v.get("passed") is False:
                fired[str(v.get("check"))] = fired.get(str(v.get("check")), 0) + 1

    return templates.TemplateResponse(
        request,
        "policy.html",
        _shell(db, merchant_id, "policy", "Policy")
        | {
            "merchant": merchant,
            "fired": fired,
            "checks": [c.__name__.replace("check_", "") for c in gate.CHECKS],
            "settings": settings,
        },
    )


# --- screen 1: the ranked worklist ------------------------------------------------------------


@router.get("/worklist", response_class=HTMLResponse)
def worklist(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    q: Annotated[str | None, Query()] = None,
    cause: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
) -> Any:
    """Ranked by recoverable money, with the reason for each position (clause 1 context).

    ``q``, ``cause`` and ``state`` narrow the list. They filter *rows*, never tenancy -- the
    merchant scope is applied first and unconditionally, so no combination of parameters can widen
    what a caller sees beyond their own book.
    """
    scope = [Invoice.merchant_id == merchant_id, Invoice.outstanding_paise > 0]
    if cause:
        scope.append(Invoice.inferred_cause == cause)
    if state:
        scope.append(Invoice.recovery_state == state)
    if q:
        like = f"%{q.strip()}%"
        scope.append(Invoice.invoice_number.ilike(like) | Counterparty.name.ilike(like))

    rows = list(
        db.execute(
            select(Invoice, Counterparty)
            .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
            .where(*scope)
            .order_by(desc(Invoice.priority_score).nullslast(), desc(Invoice.days_past_due))
            .limit(WORKLIST_LIMIT)
        )
    )
    # Totals over the whole filtered set, never over the page. The rows are capped at
    # WORKLIST_LIMIT, so summing them produced a headline that disagreed with the Overview KPI --
    # two screens quoting different "at risk" figures for the same book reads as a bug whichever
    # one is right.
    matched, matched_value = db.execute(
        select(func.count(), func.coalesce(func.sum(Invoice.outstanding_paise), 0))
        .select_from(Invoice)
        .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
        .where(*scope)
    ).one()
    causes = db.execute(
        select(Invoice.inferred_cause, func.count())
        .where(Invoice.merchant_id == merchant_id, Invoice.outstanding_paise > 0)
        .group_by(Invoice.inferred_cause)
        .order_by(desc(func.count()))
    ).all()
    total_outstanding = sum(int(invoice.outstanding_paise) for invoice, _ in rows)
    total_open = int(
        db.execute(
            select(func.count()).select_from(Invoice).where(
                Invoice.merchant_id == merchant_id, Invoice.outstanding_paise > 0
            )
        ).scalar_one()
    )
    return templates.TemplateResponse(
        request,
        "worklist.html",
        _shell(db, merchant_id, "worklist", "Revenue at Risk")
        | {
            "rows": rows,
            "total_outstanding": total_outstanding,
            "total_open": total_open,
            "matched": int(matched),
            "matched_value": int(matched_value),
            "limit": WORKLIST_LIMIT,
            "unscored": sum(1 for invoice, _ in rows if invoice.priority_score is None),
            "q": q or "",
            "cause": cause,
            "state": state,
            "causes": causes,
            "states": [s.value for s in RecoveryState],
        },
    )


# --- one account: the screen you actually work in ----------------------------------------------


@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    ok: Annotated[str | None, Query()] = None,
    err: Annotated[str | None, Query()] = None,
) -> Any:
    """Everything about one receivable on one page, including **the messages themselves**.

    The three list screens answer "what happened across the book". None of them answered "what is
    going on with *this* account", which is the question anyone actually chasing money asks -- and
    the drafted and delivered message text, which is the product's entire visible output, appeared
    on no screen at all.

    Scoped to the merchant first, so another tenant's invoice reads as absent rather than
    forbidden -- the same 404-not-403 shape the API uses.
    """
    row = db.execute(
        select(Invoice, Counterparty)
        .join(Counterparty, Counterparty.id == Invoice.counterparty_id)
        .where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
    ).one_or_none()
    if row is None:
        raise NotFoundError("invoice not found")
    invoice, counterparty = row

    actions = list(
        db.execute(
            select(Action)
            .where(Action.invoice_id == invoice.id, Action.merchant_id == merchant_id)
            .order_by(desc(Action.created_at))
        ).scalars()
    )
    messages = {
        m.action_id: m
        for m in db.execute(
            select(Message).where(
                Message.action_id.in_([a.id for a in actions] or [uuid.uuid4()])
            )
        ).scalars()
    }
    links = list(
        db.execute(
            select(PaymentLink)
            .where(PaymentLink.invoice_id == invoice.id)
            .order_by(desc(PaymentLink.created_at))
        ).scalars()
    )
    contacts = list(
        db.execute(
            select(Contact)
            .where(Contact.counterparty_id == counterparty.id)
            .order_by(desc(Contact.is_primary))
        ).scalars()
    )
    promises = list(
        db.execute(
            select(Promise)
            .where(Promise.invoice_id == invoice.id)
            .order_by(desc(Promise.created_at))
        ).scalars()
    )

    return templates.TemplateResponse(
        request,
        "invoice.html",
        _shell(db, merchant_id, "worklist", invoice.invoice_number)
        | {
            "invoice": invoice,
            "counterparty": counterparty,
            "actions": actions,
            "messages": messages,
            "links": links,
            "contacts": contacts,
            "promises": promises,
            "flash_ok": ok,
            "flash_err": err,
            "draft": human.preview(db, invoice.id, merchant_id),
            "offline_methods": OFFLINE_METHODS,
            "needs_approval": invoice.recovery_state == RecoveryState.HUMAN_REVIEW.value,
            "is_disputed": invoice.stop_reason == "disputed",
            "is_stopped": invoice.recovery_state == RecoveryState.STOPPED.value,
            "is_settled": invoice.recovery_state == RecoveryState.SETTLED.value,
        },
    )


# --- demo mode: the whole loop, one account, one step at a time --------------------------------


@router.get("/demo", response_class=HTMLResponse)
def demo(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    invoice: Annotated[str | None, Query()] = None,
    ok: Annotated[str | None, Query()] = None,
    err: Annotated[str | None, Query()] = None,
) -> Any:
    """A scripted run of the real loop on **one** account, for recording.

    **Nothing here is simulated and no step is faked.** Each one calls the same code the batch
    runner calls, and every step's status is *derived from the database* rather than stored in a
    wizard -- so the board cannot drift from what actually happened, and reloading mid-demo shows
    the truth rather than a remembered position. Pay the link on a phone and the next refresh
    shows the webhook landing, because the webhook is what moved the row.

    Scoped to one invoice on purpose: a payment link is real money against a finite test-mode
    budget, so a demo that created five would cost four more than the story needs.
    """
    # Poll Razorpay on every render of a chosen account. That is what makes the waiting step
    # resolve on its own after a payment, with no webhook tunnel in the picture.
    chosen = human.demo_state(db, merchant_id, invoice, poll=bool(invoice))
    return templates.TemplateResponse(
        request,
        "demo.html",
        _shell(db, merchant_id, "demo", "Demo Mode")
        | {
            "state": chosen,
            "candidates": human.demo_candidates(db, merchant_id),
            "flash_ok": ok,
            "flash_err": err,
        },
    )


def _demo_back(invoice_id: uuid.UUID, outcome: human.Outcome) -> RedirectResponse:
    flag = "ok" if outcome.ok else "err"
    return RedirectResponse(
        url=f"/ui/demo?invoice={invoice_id}&{flag}={quote(outcome.message)}", status_code=303
    )


@router.post("/demo/{invoice_id}/link")
def demo_link(db: DbDep, merchant_id: UiMerchant, invoice_id: uuid.UUID) -> RedirectResponse:
    """Step 2. Create the real Razorpay payment link for this one invoice."""
    return _demo_back(invoice_id, human.demo_create_link(db, invoice_id, merchant_id))


@router.post("/demo/{invoice_id}/send")
def demo_send(
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    body: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Step 3-4. Draft, gate, send. The approval is recorded; the gate still decides."""
    return _demo_back(
        invoice_id,
        human.approve_and_send(
            db, invoice_id, merchant_id, actor_id=UI_ACTOR, body_override=body
        ),
    )


@router.post("/demo/{invoice_id}/check")
def demo_check(db: DbDep, merchant_id: UiMerchant, invoice_id: uuid.UUID) -> RedirectResponse:
    """Ask Razorpay for this link's status now, rather than waiting for the next refresh."""
    result = human.check_payment(db, invoice_id, merchant_id)
    if result.error:
        outcome = human.Outcome(False, f"Could not reach Razorpay: {result.error}")
    elif result.changed:
        outcome = human.Outcome(
            True,
            f"Payment found — {paise_to_exact(result.applied_paise)} settled and outreach "
            f"called off." if result.fully_settled else
            f"Part payment found — {paise_to_exact(result.applied_paise)} settled.",
        )
    else:
        outcome = human.Outcome(
            False, f"No new payment yet (Razorpay says the link is {result.link_status})."
        )
    return _demo_back(invoice_id, outcome)


@router.post("/demo/{invoice_id}/escalate")
def demo_escalate(db: DbDep, merchant_id: UiMerchant, invoice_id: uuid.UUID) -> RedirectResponse:
    """Step 6. They did not pay in time -- raise the tone tier for the next attempt."""
    return _demo_back(invoice_id, human.escalate(db, invoice_id, merchant_id, actor_id=UI_ACTOR))


# --- human-in-the-loop: one account at a time --------------------------------------------------

#: Who the audit trail records for a UI action. The placeholder auth carries no user identity
#: (the token *is* the merchant id), so naming the surface is the honest maximum -- "merchant" on
#: its own would imply a person the system cannot actually identify.
UI_ACTOR = "merchant:ui"


def _back(invoice_id: uuid.UUID, outcome: human.Outcome) -> RedirectResponse:
    """Back to the account, carrying the result as a flash message.

    The banner is a query parameter rather than a session flash because these screens hold no
    server-side session at all. It survives one redirect and no more, which is the whole life a
    flash needs.
    """
    flag = "ok" if outcome.ok else "err"
    return RedirectResponse(
        url=f"/ui/invoice/{invoice_id}?{flag}={quote(outcome.message)}", status_code=303
    )


@router.post("/invoice/{invoice_id}/send")
def act_send(
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    body: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Approve the reminder and send it. **Re-gates first; the approval is not a bypass.**"""
    return _back(
        invoice_id,
        human.approve_and_send(
            db, invoice_id, merchant_id, actor_id=UI_ACTOR, body_override=body
        ),
    )


@router.post("/invoice/{invoice_id}/contact")
def act_contact(
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    email_address: Annotated[str, Form()],
    name: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    return _back(
        invoice_id,
        human.update_contact(
            db, invoice_id, merchant_id, email_address=email_address, name=name, actor_id=UI_ACTOR
        ),
    )


@router.post("/invoice/{invoice_id}/dispute")
def act_dispute(
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    reason: Annotated[str, Form()] = "",
) -> RedirectResponse:
    return _back(
        invoice_id,
        human.mark_disputed(db, invoice_id, merchant_id, reason=reason, actor_id=UI_ACTOR),
    )


@router.post("/invoice/{invoice_id}/resolve")
def act_resolve(db: DbDep, merchant_id: UiMerchant, invoice_id: uuid.UUID) -> RedirectResponse:
    return _back(
        invoice_id, human.resolve_dispute(db, invoice_id, merchant_id, actor_id=UI_ACTOR)
    )


@router.post("/invoice/{invoice_id}/stop")
def act_stop(
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    reason: Annotated[str, Form()] = "",
) -> RedirectResponse:
    return _back(
        invoice_id,
        human.stop_chasing(db, invoice_id, merchant_id, reason=reason, actor_id=UI_ACTOR),
    )


@router.post("/invoice/{invoice_id}/paid")
def act_paid(
    db: DbDep,
    merchant_id: UiMerchant,
    invoice_id: uuid.UUID,
    amount: Annotated[str, Form()],
    method: Annotated[str, Form()] = "neft",
    reference: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    return _back(
        invoice_id,
        human.mark_paid(
            db,
            invoice_id,
            merchant_id,
            amount_rupees=amount,
            method=method,
            reference=reference,
            actor_id=UI_ACTOR,
        ),
    )


# --- doing something, from the UI --------------------------------------------------------------


@router.post("/run")
def start_run(
    db: DbDep,
    merchant_id: UiMerchant,
    limit: Annotated[int, Form()] = 5,
    live: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Run the agent over the worklist, from a button instead of a terminal.

    **Dry by default, and live only on an explicit tick.** A live run creates real Razorpay
    payment links against a finite test-mode budget and sends real email; a dry run walks the
    identical code path, records the identical audit trail, and contacts nobody. Making the safe
    one the default is the difference between a control someone can explore and one they have to
    be briefed on first.

    Synchronous, like the CLI it replaces (ADR-009). A run over a handful of accounts takes
    seconds without a model, so a job queue would add moving parts and a status-polling screen to
    buy nothing a user would notice.
    """
    from app.agent import runner

    result = runner.run(
        db, merchant_id, limit=max(1, min(limit, 25)), dry_run=live is None
    )
    return RedirectResponse(url=f"/ui/recovery?run={result.recovery_run_id}", status_code=303)


# --- screen 2: the recovery figure ------------------------------------------------------------


@router.get("/recovery", response_class=HTMLResponse)
def recovery(
    request: Request,
    db: DbDep,
    merchant_id: UiMerchant,
    run: Annotated[str | None, Query()] = None,
) -> Any:
    """Causal as the headline, time-window beside it, divergence explained on screen (clause 1)."""
    runs = _recent_runs(db, merchant_id)

    # Figures for every run in the picker, so an option can say what it collected. Choosing a run
    # by timestamp alone is guesswork on a book with a dozen of them.
    figures_by_run = {r.id: metrics.recovery_for_run(db, r.id) for r in runs}

    if run:
        run_row = _resolve_run(db, merchant_id, run)
        # A run older than the picker window still has to appear in it, or selecting it from a
        # link renders a page whose own dropdown disagrees with what is on screen.
        if run_row is not None and run_row.id not in figures_by_run:
            runs = [*runs, run_row]
            figures_by_run[run_row.id] = metrics.recovery_for_run(db, run_row.id)
    else:
        # **Default to the most recent run that actually collected something**, not simply the
        # most recent. Runs are cheap and most of them recover nothing -- a rehearsal, a dry run,
        # a batch whose counterparties have not paid yet -- so "latest" reliably lands on a screen
        # of zeroes while the book's real recovery sits two rows down. The picker still lists
        # every run; this only decides where you start.
        earned = [r for r in runs if figures_by_run[r.id].causal.rupees_paise > 0]
        run_row = earned[0] if earned else _resolve_run(db, merchant_id, None)

    figures = figures_by_run.get(run_row.id) if run_row is not None else None

    return templates.TemplateResponse(
        request,
        "recovery.html",
        _shell(db, merchant_id, "recovery", "Recovery")
        | {
            "run": run_row,
            "figures": figures,
            "runs": runs,
            "figures_by_run": figures_by_run,
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
        _shell(db, merchant_id, "audit", "Audit log")
        | {
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
