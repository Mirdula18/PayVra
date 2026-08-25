"""Inspect the webhook events a REAL Razorpay delivery actually stored.

``scripts.verify_razorpay`` proves the *outbound* half against the live API. It cannot prove the
inbound half, because that requires Razorpay to sign a request and send it to us. This script is
the other half: run it after paying a probe link, and it reports what a genuine signed delivery
carried.

Run:  python -m scripts.inspect_webhook          (from api/, venv active)
      make inspect-webhook

The single most important thing it tells you is implicit in a row existing at all. The endpoint
verifies the signature *before* it inserts (routers/webhooks.py steps 2-3, 5), so a stored row is
proof that ``RAZORPAY_WEBHOOK_SECRET`` verified a payload Razorpay actually signed. If no row
appeared, the reason is in the uvicorn log, not here -- see the runbook.

Everything else it checks is our reading of a real envelope rather than a fixture we invented:

* where the dedupe key came from -- the ``x-razorpay-event-id`` header, or the body-derived
  ``sha256:`` fallback the endpoint degrades to when that header is absent.
* ``reference_id`` on the entity -- the invoice number, the whole reconciliation key.
* ``notes.invoice_id`` -- the belt-and-braces fallback.
* whether ``webhooks.extract`` reads all of the above off the real envelope.
* whether processing ran, and what the settle recorded.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.audit_log import AuditLog
from app.models.webhook_event import WebhookEvent
from app.razorpay import webhooks

DIVIDER = "=" * 78


def _entity_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror of ``webhooks._entity``, kept separate so a bug there cannot hide itself here."""
    container = payload.get("payload")
    if not isinstance(container, dict):
        return {}
    for key in ("payment_link", "invoice", "payment"):
        holder = container.get(key)
        if isinstance(holder, dict) and isinstance(holder.get("entity"), dict):
            return dict(holder["entity"])
    return {}


def report(event: WebhookEvent, *, raw: bool) -> list[str]:
    """Print one event's findings. Returns the labels that failed."""
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if detail:
            print(f"         {detail}")
        if not ok:
            failures.append(label)

    payload = dict(event.raw_payload)

    print(f"\n  received_at : {event.received_at}")
    print(f"  event_type  : {event.event_type}")
    print(f"  processed_at: {event.processed_at or 'NOT PROCESSED'}")
    print(f"  top-level keys: {', '.join(sorted(payload))}")

    container = payload.get("payload")
    if isinstance(container, dict):
        print(f"  payload.* keys: {', '.join(sorted(container))}")
    print()

    # Assumption 4. Implicit in the row existing, and the strongest signal here.
    check(
        bool(event.signature_valid),
        "RAZORPAY_WEBHOOK_SECRET verified a REAL Razorpay-signed payload",
        "the row exists, so verify_signature() returned True before the insert",
    )

    # Where the dedupe key actually came from. A `sha256:` prefix means the header was missing
    # and the endpoint degraded to a body-derived key rather than rejecting the event.
    used_fallback = event.razorpay_event_id.startswith(webhooks.FALLBACK_EVENT_ID_PREFIX)
    check(
        not used_fallback,
        f"dedupe key came from the {webhooks.EVENT_ID_HEADER} header",
        f"stored key {event.razorpay_event_id!r}"
        + (
            "\n         Razorpay did not send the header. The event still processed on a"
            "\n         body-derived key, which dedupes a replay correctly -- but confirm the"
            "\n         header name against the delivery log before assuming it is absent."
            if used_fallback
            else ""
        ),
    )
    # Informational: the body is expected NOT to carry an id. Its presence would be the surprise.
    if payload.get("id"):
        print(f"  [note] envelope also carried a top-level id: {payload['id']!r}")

    entity = _entity_of(payload)
    if not entity:
        check(False, "an entity was found under payload.*", "no payment_link/invoice/payment")
        return failures

    print(f"\n  entity keys: {', '.join(sorted(entity))}\n")

    # Assumption 2: reference_id survives into a real webhook, not just a fetch.
    check(
        bool(entity.get("reference_id")),
        "reference_id survives into the REAL webhook payload",
        f"got {entity.get('reference_id')!r}",
    )

    # Assumption 3: notes survives too.
    raw_notes = entity.get("notes")
    notes: dict[str, Any] = raw_notes if isinstance(raw_notes, dict) else {}
    check(
        bool(notes.get("invoice_id")),
        "notes.invoice_id survives into the REAL webhook payload",
        f"got {notes!r}",
    )

    # And our extractor against that same real envelope.
    facts = webhooks.extract(payload, event_id=event.razorpay_event_id)
    check(
        facts.event_type in webhooks.HANDLED_EVENTS,
        f"webhooks.extract() recognises event_type {facts.event_type!r}",
        f"handled: {', '.join(webhooks.HANDLED_EVENTS)}",
    )
    check(
        facts.reference_id is not None or facts.invoice_id_note is not None,
        "webhooks.extract() found at least one invoice identifier",
        f"reference_id={facts.reference_id!r} notes.invoice_id={facts.invoice_id_note!r}",
    )
    check(
        facts.amount_paid_paise > 0 or facts.event_type not in webhooks.SETTLING_EVENTS,
        "webhooks.extract() read a non-zero amount_paid on a settling event",
        f"amount_paid={facts.amount_paid_paise} amount={facts.amount_paise}",
    )

    if raw:
        print("\n  --raw: full stored payload. CONTAINS COUNTERPARTY PII; do not paste publicly.")
        print("    " + json.dumps(payload, indent=2, default=str).replace("\n", "\n    "))

    return failures


def show_settle(db: Session) -> None:
    """What reconciliation did with it, if anything."""
    print()
    print(DIVIDER)
    print("RESULTING SETTLEMENT (audit_log: reconcile.settle)")
    print(DIVIDER)
    entry = db.execute(
        select(AuditLog)
        .where(AuditLog.action_type == "reconcile.settle")
        .order_by(desc(AuditLog.id))
        .limit(1)
    ).scalar_one_or_none()
    if entry is None:
        print("  none yet -- the webhook may not have matched an invoice.")
        print("  A probe link carries a reference_id no seeded invoice has, so 'unmatched' is the")
        print("  CORRECT outcome for a probe. Pay a link made for a real seeded invoice to see a")
        print("  settlement.")
        return
    inputs = entry.inputs or {}
    print(f"  subject_id      : {entry.subject_id}")
    print(f"  source          : {inputs.get('source')}")
    print(f"  revoked_actions : {inputs.get('revoked_actions')}   <-- the demo's number")
    print(f"  promises_closed : {inputs.get('promises_closed')}")
    print(f"  rationale       : {entry.rationale}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect real stored Razorpay webhook events.")
    parser.add_argument("--limit", type=int, default=1, help="how many recent events (default 1)")
    parser.add_argument("--raw", action="store_true", help="dump the full payload (has PII)")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        events = list(
            db.execute(
                select(WebhookEvent).order_by(desc(WebhookEvent.received_at)).limit(args.limit)
            ).scalars()
        )

        print(DIVIDER)
        print(f"STORED WEBHOOK EVENTS (most recent {args.limit})")
        print(DIVIDER)

        if not events:
            print("\n  No webhook events stored.")
            print("\n  That is NOT necessarily a signature failure. The endpoint rejects before")
            print("  it inserts, so read the uvicorn log to tell the cases apart:")
            print("    'invalid webhook signature; rejecting' -> wrong RAZORPAY_WEBHOOK_SECRET")
            print("    'signed webhook body was not valid JSON' -> reached us, body is not JSON")
            print("    nothing at all                         -> never reached us; check tunnel")
            print("\n  A missing event-id header no longer stops anything: a verified payload is")
            print("  processed under a body-derived key, so it would appear here regardless.")
            return 1

        failures: list[str] = []
        for event in events:
            failures += report(event, raw=args.raw)

        show_settle(db)

        print()
        print(DIVIDER)
        if failures:
            print(f"RESULT: {len(failures)} CHECK(S) FAILED -- this is Phase 4 rework")
            for label in failures:
                print(f"  - {label}")
            return 1
        print("RESULT: a real signed Razorpay payload verified and parsed cleanly")
        print(DIVIDER)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
