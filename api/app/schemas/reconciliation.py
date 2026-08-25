"""Request/response models for the manual reconciliation endpoints (api-contracts.md).

**No card data anywhere in this module.** Not a PAN, not a CVV, not an expiry, not a token, not
even an optional field for one. An offline payment is attested by method and reference (a UTR, a
cheque number); a card payment would have come through Razorpay's hosted checkout, which is the
whole reason PAYVRA stays outside PCI-DSS scope.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.reconciliation.manual import OFFLINE_METHODS


class MarkPaidOfflineRequest(BaseModel):
    """``POST /invoices/{id}/mark-paid-offline``."""

    amount_paise: int = Field(gt=0, description="Amount received, in paise.")
    method: str = Field(description=f"One of: {', '.join(OFFLINE_METHODS)}")
    reference: str | None = Field(
        default=None, description="UTR, cheque number, or bank reference. Never a card number."
    )
    paid_on: date | None = Field(default=None, description="IST business date of receipt.")


class SettleResponse(BaseModel):
    """What a settlement did.

    ``revoked_actions`` is deliberately at the top level rather than buried in a detail object:
    it is the number that answers "did you stop chasing someone who paid?", and it is the moment
    the demo turns on.
    """

    invoice_id: uuid.UUID
    amount_applied_paise: int
    outstanding_before_paise: int
    outstanding_after_paise: int
    fully_settled: bool
    revoked_actions: int
    promises_closed: int
    tone_tier_before: int
    tone_tier_after: int
    source: str
    already_settled: bool = False
    links_cancelled: int = 0


class MarkDisputedRequest(BaseModel):
    """``POST /invoices/{id}/mark-disputed``."""

    reason: str = Field(min_length=1, description="Why the counterparty disputes this invoice.")


class MarkDisputedResponse(BaseModel):
    invoice_id: uuid.UUID
    recovery_state: str
    stop_reason: str | None
    inferred_cause: str
    revoked_actions: int


class ReconciliationStatusResponse(BaseModel):
    """``GET /invoices/{id}/reconciliation-status`` — what the dashboard polls after a webhook.

    The webhook's own 200 cannot carry these numbers: it has to acknowledge in under 200 ms with
    reconciliation deferred, so at the moment of acknowledgement the revocation has not happened
    yet. Reconciling inline to populate the response is exactly what turns one payment into a
    Razorpay retry storm.

    So the count reaches the screen by polling instead. ``revoked_actions`` is the demo's central
    number — the answer to "did you stop chasing someone who paid?" — and it is read from the
    ``reconcile.settle`` audit entry rather than recounted, so the figure on screen is the one the
    audit trail will show a judge.
    """

    invoice_id: uuid.UUID
    settled: bool
    settled_at: datetime | None = None
    revoked_actions: int = 0
    promises_closed: int = 0
    # Not in the original contract, but a poller needs to distinguish "not settled yet" from
    # "partially paid and still owing" without a second request.
    payment_status: str
    outstanding_paise: int
