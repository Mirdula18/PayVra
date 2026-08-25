"""Shared constants and builders for the gate tests.

A plain module rather than a fixture file: ``make_action`` is a builder, not a fixture, and
importing builders across test modules is cleaner than importing fixtures (which shadows them and
trips F811). The four database fixtures live in ``conftest.py`` as ``gate_*``.
"""

from __future__ import annotations

from datetime import datetime

from app.clock import IST
from app.enums import ActionType, ActorType, Channel
from app.models.invoice import Invoice
from app.schemas.gate import DraftMessage, ProposedAction

# A moment comfortably inside the contact window, so tests that are not about time never trip it.
MIDDAY_IST = datetime(2026, 8, 24, 12, 0, tzinfo=IST)
NIGHT_IST = datetime(2026, 8, 24, 3, 0, tzinfo=IST)

LINK = "https://rzp.io/i/testlink"
OPT_OUT = "https://payvra.test/opt-out/tok123"
SENDER = "GateTest Supplies"


def clean_body(invoice: Invoice) -> str:
    """A message that satisfies every required element, so only the override under test differs."""
    rupees = invoice.outstanding_paise // 100
    return (
        f"Hello, invoice {invoice.invoice_number} for INR {rupees:,} is now due.\n"
        f"You can pay here: {LINK}\n"
        f"To stop receiving these, use {OPT_OUT}\n"
        f"— {SENDER}"
    )


def clean_message(invoice: Invoice, **overrides: object) -> DraftMessage:
    payload: dict[str, object] = {
        "channel": Channel.EMAIL,
        "subject": f"Invoice {invoice.invoice_number}",
        "body": clean_body(invoice),
        "quoted_amount_paise": invoice.outstanding_paise,
        "quoted_invoice_number": invoice.invoice_number,
        "payment_link_url": LINK,
        "opt_out_url": OPT_OUT,
        "sender_name": SENDER,
    }
    payload.update(overrides)
    return DraftMessage(**payload)  # type: ignore[arg-type]


def make_action(invoice: Invoice, **overrides: object) -> ProposedAction:
    """A compliant send_message action. Overrides bend exactly one thing at a time."""
    message = overrides.pop("message", clean_message(invoice))
    payload: dict[str, object] = {
        "invoice_id": invoice.id,
        "type": ActionType.SEND_MESSAGE,
        "tone_tier": 1,
        "rationale": "30 days past due, first reminder",
        "proposed_by": ActorType.AGENT,
        "channel": Channel.EMAIL,
        "message": message,
    }
    payload.update(overrides)
    return ProposedAction(**payload)  # type: ignore[arg-type]
