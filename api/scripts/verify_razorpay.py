"""Verify the four Razorpay assumptions against the REAL test-mode API.

Everything Razorpay-facing in Phase 4 was built against a stubbed transport. This script is the
one thing that talks to the live API, and it exists to catch a wrong assumption now rather than
during a rehearsal.

Run:  python -m scripts.verify_razorpay          (from the repo root, venv active)
      make verify-razorpay

It asserts, against real responses:

1. ``create_payment_link`` returns the field shape ``links.py`` reads -- specifically ``id``,
   which is a hard requirement (a ``KeyError`` if absent), plus ``short_url`` and ``status``.
2. ``reference_id`` survives the round trip and comes back on a fetch.
3. ``notes`` survives too, with both of our internal ids intact.
4. ``X-Razorpay-Idempotency-Key`` is at minimum harmless -- our own database is the real
   idempotency control, but a rejected header would be a 4xx on every create.

It also prints the exact webhook payload shape a real event carries, which is the fifth thing to
confirm: that ``webhooks.extract`` reads a real envelope, not the one our fixtures invent.

**Creates exactly one payment link**, against the test-mode budget of 30. It cancels it again at
the end unless ``--keep`` is passed, so a repeated run does not eat the budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Any

from app.clock import now_utc
from app.config import settings
from app.razorpay.client import (
    RazorpayClient,
    RazorpayClientError,
    RazorpayError,
    idempotency_key,
)
from app.razorpay.webhooks import extract

DIVIDER = "=" * 78

# A throwaway reference that cannot collide with a seeded invoice number.
PROBE_REFERENCE = "PAYVRA-PROBE-0001"
PROBE_INVOICE_ID = "00000000-0000-0000-0000-0000000000aa"
PROBE_MERCHANT_ID = "00000000-0000-0000-0000-0000000000bb"


class Checks:
    """Collects results so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.results: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.results.append((ok, label, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}")
        if detail:
            print(f"         {detail}")
        return ok

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [r for r in self.results if not r[0]]


def preflight(checks: Checks) -> bool:
    print(DIVIDER)
    print("PREFLIGHT -- credentials")
    print(DIVIDER)
    key = settings.razorpay_key_id
    ok = checks.check(bool(key), "RAZORPAY_KEY_ID is set", key[:14] + "..." if key else "empty")
    ok &= checks.check(bool(settings.razorpay_key_secret), "RAZORPAY_KEY_SECRET is set")
    ok &= checks.check(
        key.startswith("rzp_test_"), "key is TEST mode", "live keys are refused by the client"
    )
    ok &= checks.check(
        key != "rzp_test_dummy",
        "key is not the .env.example placeholder",
        "replace rzp_test_dummy with a real test key from the Razorpay dashboard",
    )
    ok &= checks.check(
        bool(settings.razorpay_webhook_secret)
        and settings.razorpay_webhook_secret != "dummy-webhook-secret",
        "RAZORPAY_WEBHOOK_SECRET is set to something real",
        "needed for the webhook half; the API half runs without it",
    )
    return ok


def verify_create(client: RazorpayClient, checks: Checks) -> dict[str, Any] | None:
    print()
    print(DIVIDER)
    print("1 + 2 + 3 -- create_payment_link: field shape, reference_id, notes")
    print(DIVIDER)

    amount = 100_00  # Rs 100. Small on purpose; this is a real link on a real account.
    payload = {
        "amount": amount,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": PROBE_REFERENCE,
        "description": "PAYVRA integration probe -- safe to cancel",
        "customer": {"name": "PAYVRA Probe"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": int((now_utc() + timedelta(hours=2)).timestamp()),
        "notes": {"invoice_id": PROBE_INVOICE_ID, "merchant_id": PROBE_MERCHANT_ID},
    }
    key = idempotency_key(PROBE_INVOICE_ID, amount, "probe")

    try:
        response = client.create_payment_link(payload, idempotency=key)
    except RazorpayError as exc:
        checks.check(False, "create_payment_link succeeded", str(exc))
        checks.check(
            False,
            "X-Razorpay-Idempotency-Key is accepted",
            "if the error mentions the header, remove it from client.create_payment_link",
        )
        return None

    checks.check(True, "X-Razorpay-Idempotency-Key did not cause a 4xx")

    print("\n  raw response keys:", ", ".join(sorted(response)))
    print("  full response:")
    print("    " + json.dumps(response, indent=2)[:1400].replace("\n", "\n    "))
    print()

    # 1. field shape links.py depends on
    checks.check(
        "id" in response,
        "response['id'] present (links.py hard-requires it)",
        str(response.get("id")),
    )
    checks.check(
        "short_url" in response, "response['short_url'] present", str(response.get("short_url"))
    )
    checks.check("status" in response, "response['status'] present", str(response.get("status")))

    # 2. reference_id survives
    checks.check(
        response.get("reference_id") == PROBE_REFERENCE,
        "reference_id survives the create round trip",
        f"sent {PROBE_REFERENCE!r}, got {response.get('reference_id')!r}",
    )

    # 3. notes survives
    notes = response.get("notes") or {}
    checks.check(
        notes.get("invoice_id") == PROBE_INVOICE_ID
        and notes.get("merchant_id") == PROBE_MERCHANT_ID,
        "notes survives with both internal ids intact",
        f"got {notes!r}",
    )

    # And the two settings that carry the compliance story.
    checks.check(
        response.get("reminder_enable") in (False, None),
        "reminder_enable is not silently turned on by Razorpay",
        f"got {response.get('reminder_enable')!r}",
    )
    return response


def verify_fetch(client: RazorpayClient, link_id: str, checks: Checks) -> None:
    print()
    print(DIVIDER)
    print("2b -- reference_id and notes survive a FETCH (what a webhook mirrors)")
    print(DIVIDER)
    try:
        fetched = client.fetch_payment_link(link_id)
    except RazorpayError as exc:
        checks.check(False, "fetch_payment_link succeeded", str(exc))
        return

    checks.check(
        fetched.get("reference_id") == PROBE_REFERENCE,
        "reference_id present on fetch",
        f"got {fetched.get('reference_id')!r}",
    )
    checks.check(
        (fetched.get("notes") or {}).get("invoice_id") == PROBE_INVOICE_ID,
        "notes present on fetch",
        f"got {fetched.get('notes')!r}",
    )

    # The webhook entity is the same shape as a fetched entity, so our extractor should read it.
    # No top-level "id": the real envelope has none, and the event id arrives as a header.
    simulated = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": fetched}},
    }
    facts = extract(simulated, event_id="evt_probe")
    checks.check(
        facts.reference_id == PROBE_REFERENCE,
        "webhooks.extract() reads reference_id off a REAL entity",
        f"got {facts.reference_id!r}",
    )
    checks.check(
        facts.invoice_id_note == PROBE_INVOICE_ID,
        "webhooks.extract() reads notes.invoice_id off a REAL entity",
        f"got {facts.invoice_id_note!r}",
    )
    checks.check(
        facts.razorpay_link_id == link_id,
        "webhooks.extract() reads the link id off a REAL entity",
        f"got {facts.razorpay_link_id!r}",
    )
    print("\n  Paste this link into a browser and pay it with test card 4111 1111 1111 1111")
    print("  (any future expiry, any CVV) to make Razorpay send a real payment_link.paid.")


def verify_duplicate_reference(client: RazorpayClient, checks: Checks) -> None:
    """Can two links carry the same ``reference_id``? FR-9.4 silently depends on yes.

    ``links.py`` sets ``reference_id`` to ``invoice.invoice_number`` on *every* link, and
    ``regenerate_if_needed`` creates a **second** link for the same invoice when the first nears
    expiry. Different idempotency key, same reference_id. If Razorpay enforces reference_id
    uniqueness per account -- and it is documented as a unique identifier -- then regeneration
    4xxs against the real API and FR-9.4 has never worked outside our stubs.

    A 4xx here is a genuine finding, not a script failure. It creates a second link only if
    Razorpay permits one; that link is cancelled immediately either way.
    """
    print()
    print(DIVIDER)
    print("4 -- is reference_id reusable? (FR-9.4 link regeneration depends on it)")
    print(DIVIDER)

    amount = 101_00  # differs from the first probe, so only reference_id is being tested
    payload = {
        "amount": amount,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": PROBE_REFERENCE,  # deliberately the SAME as the first link
        "description": "PAYVRA regeneration probe -- safe to cancel",
        "customer": {"name": "PAYVRA Probe"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": int((now_utc() + timedelta(hours=2)).timestamp()),
        "notes": {"invoice_id": PROBE_INVOICE_ID, "merchant_id": PROBE_MERCHANT_ID},
    }

    try:
        response = client.create_payment_link(
            payload, idempotency=idempotency_key(PROBE_INVOICE_ID, amount, "regeneration")
        )
    except RazorpayClientError as exc:
        checks.check(
            False,
            "a second link may reuse an existing reference_id",
            f"Razorpay {exc.status_code} code={exc.code}: {exc}\n"
            "         REWORK: regenerate_if_needed() cannot reuse invoice_number. Suffix it\n"
            "         (e.g. INV-001-R2) and match the webhook on the prefix, or drop back to\n"
            "         notes.invoice_id as the reconciliation key for regenerated links.",
        )
        return
    except RazorpayError as exc:
        checks.check(False, "duplicate-reference probe completed", str(exc))
        return

    checks.check(
        True,
        "a second link may reuse an existing reference_id",
        f"regeneration is safe; second link {response.get('id')}",
    )
    if response.get("id"):
        try:
            client.cancel(str(response["id"]))
            print("  regeneration probe link cancelled.")
        except RazorpayError as exc:
            print(f"  could not cancel regeneration probe link: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Razorpay assumptions against the live API."
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not cancel the probe link (leave it payable to trigger a real webhook)",
    )
    parser.add_argument(
        "--skip-dup-check",
        action="store_true",
        help="skip the reference_id reuse probe (saves one link against the 30 budget)",
    )
    args = parser.parse_args(argv)

    checks = Checks()
    if not preflight(checks):
        print("\nPreflight failed. Fix .env before running the API checks.")
        return 2

    try:
        client = RazorpayClient()
    except RazorpayError as exc:
        print(f"\nCould not construct the client: {exc}")
        return 2

    response = verify_create(client, checks)
    if response and response.get("id"):
        link_id = str(response["id"])
        verify_fetch(client, link_id, checks)
        if not args.skip_dup_check:
            verify_duplicate_reference(client, checks)
        print(f"\n  PAY THIS LINK: {response.get('short_url')}")

        if args.keep:
            print("  --keep set: link left open so a real webhook can be triggered.")
        else:
            try:
                client.cancel(link_id)
                print("  probe link cancelled (pass --keep to leave it payable).")
            except RazorpayError as exc:
                print(f"  could not cancel probe link {link_id}: {exc}")

    print()
    print(DIVIDER)
    if checks.failed:
        print(f"RESULT: {len(checks.failed)} CHECK(S) FAILED -- this is Phase 4 rework")
        for _ok, label, detail in checks.failed:
            print(f"  - {label}{(': ' + detail) if detail else ''}")
        return 1
    print(f"RESULT: all {len(checks.results)} checks passed against the live test-mode API")
    print(DIVIDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
