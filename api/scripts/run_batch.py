"""Run the batch runner over a merchant's ranked worklist. The Phase 6 entry point.

One synchronous pass: diagnose, propose one action, gate it, and either execute or record the
refusal. Every run carries a ``recovery_run_id``, which is the scope its recovery figures are
measured in (ADR-009).

Run:  python -m scripts.run_batch --dry-run          (from api/, venv active)
      python -m scripts.run_batch --limit 3
      python -m scripts.run_batch --report <recovery_run_id>

**Start with --dry-run.** It diagnoses, proposes and gates for real, and persists every verdict,
but creates no payment link and contacts nobody. That is how the escalation ladder and the refusal
list get rehearsed without spending test-mode link budget, of which there are 25.

A live run creates real Razorpay links against real invoices. The default limit is small on
purpose; the link budget is the binding constraint, not runtime.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import metrics, runner
from app.config import settings
from app.db import SessionLocal
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.money import paise_to_exact

DIVIDER = "=" * 78


def _force_utf8_stdout() -> None:
    """A cp1252 console cannot encode U+20B9, and every amount here is in rupees."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def resolve_merchant(db: Session, merchant_id: str | None) -> Merchant:
    """The merchant to run against; defaults to whichever has the most invoices.

    The dev database accumulates single-invoice merchants from the API test suite, so "the one
    with 120 invoices" identifies the seeded merchant far more reliably than a name match.
    """
    if merchant_id is not None:
        found = db.get(Merchant, uuid.UUID(merchant_id))
        if found is None:
            raise SystemExit(f"no merchant with id {merchant_id}")
        return found

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


def print_run(result: runner.RunResult, merchant: Merchant) -> None:
    print()
    print(DIVIDER)
    print("RECOVERY RUN" + ("  (DRY RUN -- nothing was sent)" if result.dry_run else ""))
    print(DIVIDER)
    print(f"  recovery_run_id : {result.recovery_run_id}")
    print(f"  merchant        : {merchant.name}")
    start, end = result.contact_window
    print(f"  contact window  : {start:02d}:00-{end:02d}:00 IST")
    if result.window_overridden:
        print("                    ^ WIDENED BY OVERRIDE -- recorded in the audit log")
    print(f"  accounts        : {len(result.accounts)}")
    print(f"  executed        : {result.executed}   (state changes that completed)")
    print(f"  approved        : {result.approved}   (link + draft + gate pass, not delivered)")
    print(f"  refused         : {result.refused}")
    if result.errored:
        print(f"  errors          : {result.errored}")

    print()
    print(DIVIDER)
    print("PER ACCOUNT")
    print(DIVIDER)
    for account in result.accounts:
        mark = {
            runner.OUTCOME_EXECUTED: "EXEC",
            runner.OUTCOME_APPROVED: "OKAY",
            runner.OUTCOME_REFUSED: "STOP",
            runner.OUTCOME_SKIPPED: "SKIP",
            runner.OUTCOME_ERROR: "ERR ",
        }.get(account.outcome, "????")
        tier = f"t{account.tone_tier}" if account.tone_tier else "--"
        attempt = f"#{account.attempt}" if account.attempt else "--"
        print(
            f"  [{mark}] {account.invoice_number:<16} {attempt:<3} {tier:<3} "
            f"{account.action_type or '-':<20} cause={account.cause or '-'}"
        )
        if account.reason:
            print(f"         {account.reason}")
        if account.payment_link_url:
            print(f"         {account.payment_link_url}")


def print_recovery(rec: metrics.RunRecovery) -> None:
    print()
    print(DIVIDER)
    print("RECOVERY (FR-17)")
    print(DIVIDER)
    for figure in (rec.causal, rec.time_window):
        headline = "HEADLINE" if figure is rec.causal else "context "
        print(
            f"  {headline}  {figure.label:<12} {paise_to_exact(figure.rupees_paise):>16}  "
            f"({figure.invoices_paid_in_full} paid in full, "
            f"{figure.invoices_partially_recovered} partially recovered)"
        )
    print()
    if rec.diverges:
        print("  The two figures differ. Causal counts only invoices this run acted on;")
        print("  time-window counts everything that arrived while the run was open.")
        print("  See the divergence table in requirements/functional.md (FR-17).")
    else:
        print("  Both figures agree.")

    if rec.dry_run:
        print()
        print("  This was a DRY RUN: no link was created and nothing was sent, so any")
        print("  money below arrived independently of it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the PAYVRA batch runner.")
    parser.add_argument("--merchant", help="merchant UUID (default: the one with most invoices)")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"accounts to process (default {settings.batch_run_default_limit})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="diagnose, propose and gate; create no link and send nothing",
    )
    parser.add_argument(
        "--report", help="print the recovery figures for an existing recovery_run_id and exit"
    )
    args = parser.parse_args(argv)
    _force_utf8_stdout()

    db = SessionLocal()
    try:
        if args.report:
            print_recovery(metrics.recovery_for_run(db, uuid.UUID(args.report)))
            return 0

        merchant = resolve_merchant(db, args.merchant)
        result = runner.run(db, merchant.id, limit=args.limit, dry_run=args.dry_run)
        print_run(result, merchant)
        print_recovery(metrics.recovery_for_run(db, result.recovery_run_id))

        print()
        print(DIVIDER)
        print("AUDIT TRAIL")
        print(DIVIDER)
        print("  Every verdict above -- approved and refused -- is in audit_log, hash-chained.")
        print("  Filter this run:")
        print(
            f"    select action_type, outcome, rationale from audit_log\n"
            f"     where inputs->>'recovery_run_id' = '{result.recovery_run_id}'\n"
            f"     order by id;"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
