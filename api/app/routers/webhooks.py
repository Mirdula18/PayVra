"""``POST /webhooks/razorpay`` — the mandatory 7-step order (agents/razorpay-integration.md).

No auth header: this endpoint is authenticated by ``X-Razorpay-Signature`` and nothing else
(api-contracts.md). It is also the only unauthenticated write in the application, which is why
the order below is not negotiable:

1. read the **raw** bytes, not a parsed body
2. verify the signature over those bytes
3. invalid -> 400, log, stop. **Never parse an unverified body**
4. only now parse the JSON, and take the event id from the ``x-razorpay-event-id`` **header**
5. insert-first dedupe on the unique constraint; ``IntegrityError`` means duplicate
6. enqueue processing asynchronously
7. return 200, under 200 ms

Razorpay retries on non-2xx *and* on slow responses, so a handler that reconciles inline turns
one payment into a storm of duplicate deliveries. Everything past step 5 happens off this path.

**Duplicates are normal, not an anomaly.** Razorpay documents that the same event may be
delivered more than once *by design* -- at-least-once delivery, not a malfunction on either side.
So a ``{"status": "duplicate"}`` response is a healthy outcome and must never page anyone, drive
an alert, or be counted as an error rate. The unique constraint absorbing a redelivery is the
system working exactly as intended.

**The documented SLA is a 2XX within 5 seconds.** Our 200 ms target is a self-imposed margin,
roughly 25x stricter, and it stays -- it is what keeps the async design honest. But 200 ms is
*not* the hard requirement, so do not sacrifice correctness to defend it, and do not read a
280 ms p99 as an outage. Five seconds is the real cliff.

**The event id is a header, never a body field.** The envelope is
``{entity, account_id, event, contains, payload, created_at}`` with no top-level ``id``. A
verified request that lacks the header is still genuinely from Razorpay, so it is processed under
a body-derived fallback key rather than rejected -- 400-ing a valid event is precisely what
creates an infinite retry loop.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy.exc import IntegrityError

from app.clock import now_utc
from app.config import settings
from app.db import SessionLocal
from app.models.webhook_event import WebhookEvent
from app.razorpay import webhooks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request, background: BackgroundTasks, response: Response
) -> dict[str, str]:
    """Verify, record, acknowledge. Reconciliation happens in the background."""
    # 1. RAW bytes. Parsing first and re-serialising would change the byte sequence that was
    #    signed, so the signature could never be checked against what Razorpay actually sent.
    raw = await request.body()
    signature = request.headers.get(webhooks.SIGNATURE_HEADER, "")

    # 2 + 3. Verify before parsing. An unverified body is untrusted input and is never fed to a
    #        parser, let alone to the database.
    if not webhooks.verify_signature(raw, signature, settings.razorpay_webhook_secret):
        logger.warning("invalid webhook signature; rejecting")
        response.status_code = 400
        return {"status": "invalid"}

    # 4. Only now.
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("signed webhook body was not valid JSON")
        response.status_code = 400
        return {"status": "malformed"}

    # 4b. The dedupe key comes from the `x-razorpay-event-id` HEADER. The envelope has no
    #     top-level `id`, so reading one from the body would be empty on every genuine event.
    event_id, from_header = webhooks.resolve_event_id(request.headers, raw)
    if not from_header:
        # Signature already verified, so this genuinely came from Razorpay. Rejecting it would
        # buy nothing and guarantee an infinite retry loop; degrade to a body-derived key, which
        # still dedupes a replay because Razorpay redelivers identical bytes.
        logger.warning(
            "verified webhook carried no %s header; falling back to a body-derived key",
            webhooks.EVENT_ID_HEADER,
        )

    facts = webhooks.extract(payload, event_id=event_id)

    # 5. Insert-first dedupe. The unique constraint on razorpay_event_id **is** the mechanism --
    #    a SELECT-then-INSERT check loses to a concurrent redelivery, and Razorpay redelivers.
    db = SessionLocal()
    try:
        db.add(
            WebhookEvent(
                razorpay_event_id=facts.event_id,
                event_type=facts.event_type,
                raw_payload=payload,
                signature_valid=True,
                received_at=now_utc(),
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        # Already seen. A no-op 200: Razorpay has done nothing wrong and must not retry.
        logger.info("duplicate webhook %s", webhooks.safe_log_fields(facts))
        return {"status": "duplicate"}
    finally:
        db.close()

    # 6. Async. Returns immediately; FastAPI runs this after the response is sent.
    background.add_task(_process, facts.event_id)

    # 7. Acknowledge. Note the log line carries the event id and type only -- never the payload,
    #    which holds counterparty names, emails and phone numbers.
    logger.info("accepted webhook %s", webhooks.safe_log_fields(facts))
    return {"status": "ok"}


def _process(event_id: str) -> None:
    """Background entry point. Isolated so a processing failure can never affect the 200."""
    from app.reconciliation.processor import process_event

    process_event(event_id)
