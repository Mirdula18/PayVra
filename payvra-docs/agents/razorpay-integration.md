# Agent: Razorpay Integration

**Scope:** `api/app/razorpay/`, `reconciliation/`
**Prerequisites:** `/CLAUDE.md`, `decisions/ADR-006-razorpay-integration.md`

This module handles money. Be paranoid.

---

## Hard rules

1. **Test mode only.** Live keys never appear in this repo, in `.env.example`, or in any demo.
2. **No card data. Ever.** No PAN, CVV, expiry, token — not stored, not logged, not in a Pydantic
   model. This is what keeps PAYVRA out of PCI-DSS scope.
3. **Every write carries an idempotency key.**
4. **Webhook signature is verified over the RAW body**, before any JSON parsing.
5. **Never log a full webhook payload** — it contains counterparty PII. Log the event id and type.

---

## Client

```python
class RazorpayClient:
    def create_payment_link(self, *, invoice: Invoice, expire_by: datetime,
                            accept_partial: bool, idempotency_key: str) -> PaymentLink: ...
    def notify(self, link_id: str, medium: Literal["sms", "email"]) -> None: ...
    def cancel(self, link_id: str) -> None: ...
    def fetch_link(self, link_id: str) -> PaymentLink: ...
```

Idempotency key: `sha256(f"{invoice_id}:{amount_paise}:{purpose}")`. Same invoice, same amount,
same purpose → same key → Razorpay returns the existing link instead of creating a duplicate.

Retry with exponential backoff on 5xx and on connection errors. **Never retry a 4xx** — it means
our request is wrong, and retrying just burns rate limit. Circuit-break after 5 consecutive
failures and requeue the action.

---

## Payment link creation

```python
payload = {
    "amount": invoice.outstanding_paise,
    "currency": "INR",
    "accept_partial": accept_partial,
    "reference_id": invoice.invoice_number,        # THE reconciliation key
    "description": f"Invoice {invoice.invoice_number}",
    "customer": {
        "name": contact.name,
        "email": contact.email,
        "contact": contact.phone,
    },
    "notify": {"sms": False, "email": False},      # we send, not Razorpay
    "reminder_enable": False,                       # we control the sequence
    "expire_by": int(expire_by.timestamp()),
    "notes": {"invoice_id": str(invoice.id), "merchant_id": str(invoice.merchant_id)},
}
```

**`notify` and `reminder_enable` are both False deliberately.** PAYVRA owns the messaging sequence
and the guardrail gate. If Razorpay also sends reminders, they bypass our time-window check, our
frequency cap, and our audit log — which breaks the compliance story entirely.

`reference_id` = `invoice_number`, always. This turns reconciliation into one indexed lookup.

`notes` carries our internal IDs as a belt-and-braces path if `reference_id` is ever missing.

---

## Webhook handler — order is mandatory

```python
@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()                      # 1. RAW bytes, not parsed

    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_signature(raw, signature, secret):  # 2. verify BEFORE parsing
        log.warning("invalid webhook signature")
        return JSONResponse({"status": "invalid"}, status_code=400)   # 3.

    payload = json.loads(raw)                       # 4. only now
    event_id = payload["id"]

    try:                                            # 5. insert-first dedupe
        db.execute(insert(WebhookEvent).values(
            razorpay_event_id=event_id,
            event_type=payload["event"],
            raw_payload=payload,
            signature_valid=True,
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}              # already seen, no-op

    enqueue_webhook_processing(event_id)            # 6. async
    return {"status": "ok"}                         # 7. under 200ms
```

Signature verification:
```python
def verify_signature(raw: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)   # constant-time, not ==
```

The unique constraint on `webhook_events.razorpay_event_id` **is** the dedupe mechanism.
Do not implement dedupe in application logic — race conditions will defeat it.

Razorpay retries on non-2xx and on slow responses. Acknowledge fast, process async, always.

---

## Reconciliation — the most important code path

```python
def settle_invoice(db, invoice_id: UUID, amount_paise: int, source: str) -> None:
    with db.begin():                               # ONE transaction
        inv = db.query(Invoice).with_for_update().get(invoice_id)

        inv.outstanding_paise -= amount_paise
        if inv.outstanding_paise <= 0:
            inv.payment_status = "paid"
            inv.recovery_state = "settled"
            inv.settled_at = now_utc()
        else:
            inv.payment_status = "partially_paid"
            inv.current_tone_tier = max(1, inv.current_tone_tier - 1)   # de-escalate

        # THE critical step
        revoked = db.query(Action).filter(
            Action.invoice_id == invoice_id,
            Action.status.in_(["proposed", "awaiting_approval", "gated_pass"]),
        ).update({"status": "revoked", "revoked_at": now_utc()})

        db.query(Promise).filter(
            Promise.invoice_id == invoice_id, Promise.status == "open"
        ).update({"status": "kept", "resolved_at": now_utc()})

        cancel_outstanding_links(inv)
        audit.record(actor="system", action_type="settle", subject_id=invoice_id,
                     inputs={"amount_paise": amount_paise, "source": source},
                     outcome="executed",
                     rationale=f"Payment received. Revoked {revoked} pending actions.")
```

**The revoke step is the single most important line in the product.** An invoice that settles must
have every pending action cancelled in the same transaction. Miss it and PAYVRA emails a customer
who paid three hours ago — the exact failure that destroys merchant trust.

`mark_paid_offline` calls this identical function with `source="manual"`. One settle path, not two.

---

## Event handling

| Event | Action |
|---|---|
| `payment_link.paid` | `settle_invoice(full)` |
| `payment_link.partially_paid` | `settle_invoice(partial)`, de-escalate tone, re-enter loop |
| `payment_link.expired` | If unpaid and not stopped, regenerate. Else no-op. |
| `payment_link.cancelled` | Mark link cancelled. No state change on the invoice. |
| `invoice.paid` / `invoice.partially_paid` / `invoice.expired` | Same handling as the link equivalents |

Unknown event types: log and return 200. Never 4xx an unrecognised event — Razorpay will retry
it forever.

---

## Known constraints

- Test mode caps standard Payment Links at **30 per business**. Seed data and demo flow must
  respect this. Reuse links where possible; do not create one per synthetic invoice.
- `upi_link: true` is **live mode only**. Use standard links — UPI is still offered at checkout.
- GST-compliant invoices cannot be created via the Invoices API. We collect; merchants bill.

---

## Local webhook development

```bash
cloudflared tunnel --url http://localhost:8000
# register the printed https URL in Razorpay Dashboard -> Webhooks (test mode)
```

Test-mode and live-mode webhook secrets are **different**. Using the wrong one produces signature
failures that look like an attack. Check this first when webhooks stop verifying.

---

## Testing priorities

1. Signature verification: valid passes, tampered body fails, wrong secret fails
2. Duplicate `event.id` is a no-op returning 200
3. `settle_invoice` revokes every pending action, in one transaction
4. Partial payment reduces outstanding and lowers tone tier
5. Expired link regenerates only when unpaid and not stopped
6. Idempotency key prevents duplicate link creation for the same invoice+amount+purpose
7. Handler responds under 200 ms with processing enqueued
