"""Create a REAL Razorpay payment link against a REAL seeded invoice, so the loop can be proven.

``scripts/verify_razorpay`` proves the API contract, but it deliberately uses synthetic probe ids
and never touches the database -- so paying its link settles nothing. This script is the missing
half: it creates a link bound to an actual invoice row, which is what makes ``payment_link.paid``
resolve to something and drive reconciliation.

**Why this exists as a script rather than an endpoint.** ``create_payment_link`` is a Phase 6 agent
tool (architecture/agent-loop.md): the agent proposes it, ``guardrails.gate`` approves it, and only
then does it run. ``api/architecture/api-contracts.md`` deliberately specifies no link-creation
route, and ``link_hygiene`` only *regenerates* a link that already exists. That leaves Phase 4's
own acceptance test -- "pay a test link, watch the invoice auto-settle" -- unreachable until Phase 6
lands, which would mean signing off a phase on unverified code.

So this is a verification harness, not a shortcut around the design. It calls the real
``links.create_link``, writes a real ``payment_links`` row, and burns real test-mode budget. Every
line of the path a payment actually travels is production code; only the trigger is manual, and
Phase 6 replaces the trigger.

Run:  python -m scripts.create_demo_link            (from api/, venv active)
      python -m scripts.create_demo_link --invoice INV-2026-1052
      python -m scripts.create_demo_link --amount 10000     # partial-payment testing

Creates ONE link against the test-mode budget of 30 and leaves it payable -- unlike the probe
scripts, the whole point here is that someone pays it.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.enums import PaymentStatus, RecoveryState
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.money import paise_to_exact
from app.razorpay.client import RazorpayClient, RazorpayError
from app.razorpay.links import LINK_BUDGET, LinkBudgetExceeded, create_link, links_used

DIVIDER = "=" * 78

# Razorpay's DOMESTIC test card. The widely-quoted 4111 1111 1111 1111 is an international Visa
# and a test account configured for domestic-only payments refuses it with "International cards
# are not supported" -- which reads like a broken link rather than a wrong card.
TEST_CARD = "5267 3181 8797 5449"

# Netbanking is the most reliable test-mode route: no card details, and the simulator has an
# explicit Success button. Prefer it when walking someone through a payment.
TEST_UPI = "success@razorpay"


def _force_utf8_stdout() -> None:
    """Print rupee amounts without dying on a cp1252 console.

    A Windows terminal defaults to cp1252, which has no code point for U+20B9, so the first
    ``paise_to_exact`` call raises UnicodeEncodeError and takes the script with it -- after the
    Razorpay link may already have been created. Reconfiguring is better than dropping the symbol:
    this is an Indian receivables tool and every amount it reports is in rupees.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def resolve_merchant(db: Session, merchant_id: str | None) -> Merchant:
    """The merchant to work against. Defaults to whichever has the most invoices.

    The dev database accumulates single-invoice merchants from the API test suite, so "the one
    with 120 invoices" identifies the seeded merchant far more reliably than a name match.
    """
    if merchant_id is not None:
        merchant = db.get(Merchant, uuid.UUID(merchant_id))
        if merchant is None:
            raise SystemExit(f"no merchant with id {merchant_id}")
        return merchant

    row = db.execute(
        select(Merchant)
        .join(Invoice, Invoice.merchant_id == Merchant.id)
        .group_by(Merchant.id)
        .order_by(func.count(Invoice.id).desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise SystemExit("no merchant has any invoices -- run `python -m app.seed` first")
    return row


def resolve_invoice(db: Session, merchant: Merchant, invoice_number: str | None) -> Invoice:
    """The invoice to collect. Defaults to the highest-priority unpaid one.

    Highest-priority rather than arbitrary, because this is also the link a demo would use, and
    the worklist's top row is the one a judge will already be looking at.
    """
    if invoice_number is not None:
        invoice = db.execute(
            select(Invoice).where(
                Invoice.merchant_id == merchant.id,
                Invoice.invoice_number == invoice_number,
            )
        ).scalar_one_or_none()
        if invoice is None:
            raise SystemExit(f"{merchant.name} has no invoice {invoice_number}")
        return invoice

    invoice = db.execute(
        select(Invoice)
        .where(
            Invoice.merchant_id == merchant.id,
            Invoice.payment_status.in_(
                (PaymentStatus.UNPAID.value, PaymentStatus.PARTIALLY_PAID.value)
            ),
            Invoice.recovery_state.not_in(
                (RecoveryState.SETTLED.value, RecoveryState.STOPPED.value)
            ),
            Invoice.outstanding_paise > 0,
        )
        .order_by(Invoice.priority_score.desc().nullslast())
        .limit(1)
    ).scalar_one_or_none()
    if invoice is None:
        raise SystemExit(f"{merchant.name} has no collectable invoice")
    return invoice


def describe(invoice: Invoice, merchant: Merchant, amount: int) -> None:
    print()
    print(DIVIDER)
    print("INVOICE")
    print(DIVIDER)
    print(f"  merchant        : {merchant.name}")
    print(f"  invoice         : {invoice.invoice_number}")
    print(f"  invoice_id      : {invoice.id}")
    print(f"  outstanding     : {paise_to_exact(invoice.outstanding_paise)}")
    print(f"  link amount     : {paise_to_exact(amount)}")
    print(f"  days past due   : {invoice.days_past_due}")
    print(f"  payment_status  : {invoice.payment_status}")
    print(f"  recovery_state  : {invoice.recovery_state}")
    if amount < invoice.outstanding_paise:
        print("  NOTE: amount is below outstanding -- expect partially_paid, not a full settle.")


def next_steps(invoice: Invoice, short_url: str, full: bool) -> None:
    print()
    print(DIVIDER)
    print("PAY IT")
    print(DIVIDER)
    print(f"  {short_url}")
    print()
    print("  Easiest: Netbanking -> any bank -> Success on the simulator page.")
    print(f"  Or card {TEST_CARD} (domestic), any future expiry, any CVV.")
    print(f"  Or UPI {TEST_UPI}.")
    print()
    print("  Requirements for the webhook to come back:")
    print("    * uvicorn running on :8000")
    print("    * cloudflared tunnel running and its URL registered in the Razorpay dashboard")
    print("      (Settings -> Webhooks, TEST mode) with payment_link.paid subscribed")
    print()
    print(DIVIDER)
    print("THEN VERIFY")
    print(DIVIDER)
    print("  python -m scripts.inspect_webhook")
    print()
    print("  Expected afterwards:")
    outcome = "paid / settled" if full else "partially_paid"
    print(f"    * invoices.payment_status  -> {outcome}")
    if full:
        print("    * invoices.settled_at      -> set")
        print("    * every pending action for the invoice revoked in the same transaction")
    print("    * an audit_log 'reconcile.settle' row naming this invoice")
    print()
    print("  SQL check:")
    print(
        "    select invoice_number, payment_status, settled_at from invoices\n"
        f"     where id = '{invoice.id}';"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a real Razorpay payment link for a real seeded invoice."
    )
    parser.add_argument("--merchant", help="merchant UUID (default: the one with most invoices)")
    parser.add_argument("--invoice", help="invoice number (default: highest priority unpaid)")
    parser.add_argument(
        "--amount",
        type=int,
        help="amount in paise (default: the full outstanding). Below outstanding to test partials.",
    )
    parser.add_argument(
        "--accept-partial", action="store_true", help="let the payer send less than the full amount"
    )
    args = parser.parse_args(argv)
    _force_utf8_stdout()

    db = SessionLocal()
    try:
        merchant = resolve_merchant(db, args.merchant)
        invoice = resolve_invoice(db, merchant, args.invoice)
        amount = args.amount if args.amount is not None else invoice.outstanding_paise

        if amount <= 0:
            print(f"cannot create a link for {amount} paise", file=sys.stderr)
            return 1

        describe(invoice, merchant, amount)

        used = links_used(db, merchant.id)
        print()
        print(f"  link budget     : {used}/{LINK_BUDGET} used")

        try:
            client = RazorpayClient()
        except RazorpayError as exc:
            print(f"\nRazorpay client unavailable: {exc}", file=sys.stderr)
            print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env.", file=sys.stderr)
            return 1

        try:
            result = create_link(
                db, client, invoice, amount_paise=amount, accept_partial=args.accept_partial
            )
        except LinkBudgetExceeded as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        except RazorpayError as exc:
            print(f"\nRazorpay refused the link: {exc}", file=sys.stderr)
            return 1

        # create_link only flushes; nothing is durable until this commit. Without it the row
        # vanishes and the webhook has no link to resolve -- the exact failure this script exists
        # to rule out.
        db.commit()

        link = result.link
        print()
        print(DIVIDER)
        print("LINK" + ("" if result.created else "  (reused an existing one)"))
        print(DIVIDER)
        print(f"  razorpay_link_id: {link.razorpay_link_id}")
        print(f"  reference_id    : {link.reference_id}")
        print(f"  status          : {link.status}")
        print(f"  expires         : {link.expire_by.isoformat()}")

        next_steps(invoice, link.short_url, full=amount >= invoice.outstanding_paise)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
