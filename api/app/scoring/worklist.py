"""Priority ranking and the plain-English reason string (FR-4.2, FR-4.3).

``priority = p_collectable * amount_at_risk * urgency_multiplier``

The reason string is not decoration. FR-4.3 makes it required on every ranked row, and
agents/backend.md is blunt about why: a finance lead will not act on a ranking she cannot justify
to her CFO. It is templated from the top three contributing features, so it is always consistent
with the score that produced it -- if the reason and the ordering ever disagree, one of them is a
bug, and the reason is the one the merchant can see.

Nothing here calls an LLM. The strings are templates over the feature vector.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record as audit_record
from app.clock import today
from app.enums import ActorType
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.money import PAISE_PER_LAKH, paise_to_display
from app.scoring import features as feat
from app.scoring import model

# Scores are persisted as NUMERIC; quantising keeps a rescore of unchanged inputs byte-identical
# rather than flapping in the last float digit, which is what makes the job idempotent.
SCORE_DP = Decimal("0.0001")
PRIORITY_DP = Decimal("0.01")

# How many contributing features the reason string quotes. ADR-008 and backend.md both say three.
REASON_FEATURE_COUNT = 3

# A contribution below this is noise and is not worth a clause in the reason string -- it would
# pad the sentence with "and engagement is average", which tells the merchant nothing.
REASON_MIN_MAGNITUDE = 0.01


def _q(value: float, dp: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(dp, rounding=ROUND_HALF_UP)


def _plural(count: int, noun: str) -> str:
    """ "1 reminder" / "5 reminders". The reason string is read by a person."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True)
class ScoredInvoice:
    invoice_id: uuid.UUID
    features: feat.InvoiceFeatures
    p_collectable: Decimal
    urgency: float
    priority: Decimal
    reason: str


# --- reason phrases ------------------------------------------------------------------------------
# One phrase per feature per direction. Written the way a collections lead would say it out loud;
# they are read by a human deciding whether to act, not by a model.


def _phrase_payment_reliability(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    if f.raw_avg_days_to_pay is None:
        return None  # no history; NO_EVIDENCE contributes, but there is nothing honest to say
    avg = round(f.raw_avg_days_to_pay)
    if positive:
        if avg <= f.raw_terms_days:
            return f"always pays, typically in {avg} days against {f.raw_terms_days}-day terms"
        return f"has paid late before but always pays, averaging {avg} days"
    return f"consistently pays late, averaging {avg} days against {f.raw_terms_days}-day terms"


def _phrase_broken_promises(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    count = f.raw_broken_promises
    if count <= 0:
        return None
    if count == 1:
        return "has broken a payment promise once"
    return f"has broken {count} payment promises"


def _phrase_engagement(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    if f.raw_messages_sent == 0:
        # No messages means no engagement *evidence*, which is why the feature scores
        # NO_EVIDENCE. Saying "has not been contacted yet" would state a non-reason as a reason,
        # and it contradicted the touch-count clause whenever an outreach existed without a
        # recorded message. Stay silent and let a real signal take the slot.
        return None
    if positive:
        return f"opens and clicks the reminders ({f.raw_engagement_events} responses so far)"
    if f.raw_engagement_events == 0:
        return f"has ignored all {_plural(f.raw_messages_sent, 'reminder')} so far"
    return f"barely engages ({f.raw_engagement_events} responses to {f.raw_messages_sent} sends)"


def _phrase_dispute(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    if f.has_dispute < 1.0:
        return None
    return "the invoice is disputed, so this is a commercial problem, not a collections one"


def _phrase_days_past_due(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    dpd = f.raw_days_past_due
    if dpd <= 0:
        return None
    if dpd >= 120:
        return f"{dpd} days in arrears, so recovery odds are thin"
    return f"{dpd} days in arrears"


def _phrase_lifetime_revenue(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    if f.raw_lifetime_revenue_paise <= 0:
        return None
    display = paise_to_display(f.raw_lifetime_revenue_paise)
    if positive:
        return f"a {display} lifetime relationship worth protecting"
    return None


def _phrase_touch_count(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    touches = f.raw_touch_count
    if touches <= 0:
        return None
    if touches == 1:
        return "contacted once already"
    return f"already contacted {touches} times, so returns are diminishing"


def _phrase_amount(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    return None  # the amount already leads the sentence; repeating it adds nothing


def _phrase_exposure(f: feat.InvoiceFeatures, positive: bool) -> str | None:
    share = f.raw_exposure_share
    if share < 0.10:
        return None
    return f"this customer is {share:.0%} of everything outstanding"


_PHRASES = {
    "payment_reliability": _phrase_payment_reliability,
    "broken_promise_count": _phrase_broken_promises,
    "engagement_rate": _phrase_engagement,
    "has_dispute": _phrase_dispute,
    "days_past_due": _phrase_days_past_due,
    "lifetime_revenue": _phrase_lifetime_revenue,
    "touch_count": _phrase_touch_count,
    "amount_at_risk": _phrase_amount,
    "exposure_share": _phrase_exposure,
}


def _headline(f: feat.InvoiceFeatures) -> str:
    """Money and age, always first -- the two things a merchant triages on."""
    money = paise_to_display(f.raw_outstanding_paise)
    dpd = f.raw_days_past_due
    if dpd > 0:
        return f"{money}, {dpd} days"
    if dpd == 0:
        return f"{money}, due today"
    return f"{money}, due in {abs(dpd)} days"


def build_reason(f: feat.InvoiceFeatures, urgency: float) -> str:
    """Plain-English justification, templated from the top three contributing features.

    Never returns an empty or null string: FR-4.3 requires a reason on *every* row, so a row with
    no notable feature still gets an honest fallback rather than a blank the UI has to special-case.
    """
    clauses: list[str] = []
    for contribution in model.contributions(f):
        if len(clauses) >= REASON_FEATURE_COUNT:
            break
        if contribution.magnitude < REASON_MIN_MAGNITUDE:
            continue
        phrase = _PHRASES[contribution.feature](f, contribution.value > 0)
        if phrase:
            clauses.append(phrase)

    # Urgency is not a feature, but it is the single most actionable thing on the row when it
    # applies, so it is stated explicitly rather than left implicit in the ordering.
    if f.has_open_promise_not_yet_due:
        clauses.insert(0, "a promise to pay is open and not yet due, so hold off")
    elif f.crosses_msme_45:
        clauses.insert(0, "past the MSME Act 45-day limit, which carries statutory interest")

    if not clauses:
        clauses.append("no strong signal either way; ranked on value and age")

    body = "; ".join(clauses[:REASON_FEATURE_COUNT])
    return f"{_headline(f)}. {body[0].upper()}{body[1:]}."


def score_invoice(f: feat.InvoiceFeatures) -> ScoredInvoice:
    """Score one invoice. Pure: same features in, same score and same reason out, always."""
    probability = model.p_collectable(f)
    urgency = model.urgency_multiplier(f)
    # FR-4.2: P(collectable) x amount_at_risk x urgency. Amount is in rupees (paise / 100) so the
    # figure reads on a human scale rather than as a hundred-times-larger integer.
    amount_rupees = f.raw_outstanding_paise / 100
    priority = probability * amount_rupees * urgency
    return ScoredInvoice(
        invoice_id=f.invoice_id,
        features=f,
        p_collectable=_q(probability, SCORE_DP),
        urgency=urgency,
        priority=_q(priority, PRIORITY_DP),
        reason=build_reason(f, urgency),
    )


def score_book(
    db: Session, merchant_id: uuid.UUID, *, as_of: date | None = None
) -> list[ScoredInvoice]:
    """Score every open invoice for a merchant. Read-only; no writes, no audit entries."""
    as_of = as_of or today()
    rows = feat.load_scoring_rows(db, merchant_id, as_of=as_of)
    if not rows:
        return []
    merchant = db.get(Merchant, merchant_id)
    context = feat.build_context(
        rows,
        merchant_id=merchant_id,
        as_of=as_of,
        lifetime_touch_cap=merchant.lifetime_touch_cap if merchant else feat.DEFAULT_TOUCH_CAP,
    )
    vectors = feat.extract_all(rows, context)
    return [score_invoice(vector) for vector in vectors]


def rescore(db: Session, merchant_id: uuid.UUID, *, as_of: date | None = None) -> int:
    """Persist scores and log every feature vector to audit_log. Returns rows changed.

    **Idempotent.** An invoice whose score, priority and reason are all unchanged is skipped --
    no UPDATE and, importantly, no audit entry. Writing an audit row per invoice per nightly run
    regardless would bloat the chain and, worse, fill the ADR-008 training set with duplicate
    observations of the same unchanged state.

    The audit entry is the point of this function as much as the score is: ADR-008 says to log
    every score with its feature vector *from day one* so a training set exists when real payment
    outcomes arrive. That log is the only reason a LightGBM migration is possible later.
    """
    scored = score_book(db, merchant_id, as_of=as_of)
    if not scored:
        return 0

    invoices = {
        invoice.id: invoice
        for invoice in db.execute(
            select(Invoice).where(Invoice.id.in_([s.invoice_id for s in scored]))
        ).scalars()
    }

    changed = 0
    for item in scored:
        invoice = invoices.get(item.invoice_id)
        if invoice is None:  # pragma: no cover - the ids come from the same transaction
            continue
        if (
            invoice.collectability_score == item.p_collectable
            and invoice.priority_score == item.priority
            and invoice.priority_reason == item.reason
        ):
            continue

        invoice.collectability_score = item.p_collectable
        invoice.priority_score = item.priority
        invoice.priority_reason = item.reason
        changed += 1

        audit_record(
            db,
            merchant_id=merchant_id,
            actor=ActorType.SYSTEM,
            actor_id="scoring",
            action_type="score.invoice",
            subject_type="invoice",
            subject_id=item.invoice_id,
            outcome="scored",
            rationale=item.reason,
            inputs={
                "features": item.features.as_audit_payload(),
                "weights": model.WEIGHTS,
                "logit_intercept": model.LOGIT_INTERCEPT,
                "p_collectable": float(item.p_collectable),
                "urgency_multiplier": item.urgency,
                "priority_score": float(item.priority),
            },
        )

    db.flush()
    return changed


def lakhs(paise: int) -> Decimal:
    """Rupees in lakhs, for callers rendering money outside the standard display helper."""
    return (Decimal(paise) / Decimal(PAISE_PER_LAKH)).quantize(Decimal("0.1"))
