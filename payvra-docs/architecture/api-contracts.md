# API Contracts

FastAPI. Base path `/api/v1`. All responses JSON. All money in **paise**, integer.
All endpoints except `/webhooks/*` and `/health` require auth and are scoped to `merchant_id`
derived from the token — never from a request parameter.

Error envelope:
```json
{ "error": { "code": "INVOICE_NOT_FOUND", "message": "…", "details": {} } }
```

---

## Ingestion

### `POST /batches`
Upload an invoice file. Multipart.

Request: `file` (CSV/XLSX), `name` (string, optional)

```json
{
  "batch_id": "uuid",
  "created": 372,
  "updated": 8,
  "duplicates": 3,
  "repair_queue": 8,
  "counterparties_matched": 96,
  "counterparties_quarantined": 3,
  "column_mapping": { "Bill No": "invoice_number", "Party": "counterparty_name" },
  "total_outstanding_paise": 14200000000
}
```

### `GET /batches/{batch_id}/repairs`
Rows that failed validation, with the reason.

### `POST /batches/{batch_id}/repairs/{row_id}`
Submit corrected values for one row.

### `POST /batches/{batch_id}/mapping`
Override the auto-detected column mapping and re-parse.

---

## Consent

### `GET /counterparties?consent_status=pending`
```json
{
  "items": [
    { "id": "uuid", "name": "Acme Distributors Pvt Ltd", "gstin": "…",
      "open_invoices": 4, "outstanding_paise": 820000000,
      "consent": { "email": null, "sms": null, "whatsapp": null } }
  ],
  "total": 12
}
```

### `POST /counterparties/{id}/consent`
```json
{ "channels": ["email", "whatsapp"], "basis": "existing_commercial_relationship" }
```

### `POST /counterparties/consent/bulk`
```json
{ "counterparty_ids": ["uuid", "uuid"], "channels": ["email"], "basis": "existing_commercial_relationship" }
```

### `POST /counterparties/{id}/opt-out`
Immediate, permanent, all channels. Also reachable unauthenticated via the opt-out token
embedded in every message: `POST /public/opt-out/{token}`.

---

## Worklist

### `GET /worklist?limit=50&state=chasing`
The primary screen. Ranked, never alphabetical.

```json
{
  "items": [
    {
      "invoice_id": "uuid",
      "invoice_number": "INV-4471",
      "counterparty": { "id": "uuid", "name": "Acme Distributors Pvt Ltd" },
      "outstanding_paise": 42000000,
      "days_past_due": 68,
      "aging_bucket": "61-90",
      "crosses_msme_45": true,
      "recovery_state": "chasing",
      "inferred_cause": "cash_crunch",
      "collectability_score": 0.74,
      "priority_score": 3108.0,
      "priority_reason": "₹4.2L, 68 days. This customer has paid late twice before but always paid.",
      "current_tone_tier": 2,
      "touch_count": 3,
      "proposed_action": {
        "type": "send_message",
        "channel": "whatsapp",
        "tone_tier": 3,
        "scheduled_for": "2026-08-24T09:15:00+05:30",
        "requires_approval": true
      }
    }
  ],
  "total": 61,
  "summary": {
    "total_outstanding_paise": 14200000000,
    "overdue_count": 61,
    "high_risk_count": 14
  }
}
```

### `POST /worklist/{invoice_id}/pin`
### `POST /worklist/{invoice_id}/snooze` — body: `{ "until": "2026-09-01" }`
### `POST /worklist/{invoice_id}/exclude` — removes from automation entirely

---

## Plan review

### `GET /plan?days=14`
Everything scheduled, before activation.

```json
{
  "activated": false,
  "actions": [
    {
      "action_id": "uuid",
      "invoice_number": "INV-4471",
      "counterparty_name": "Acme Distributors Pvt Ltd",
      "type": "send_message",
      "channel": "email",
      "tone_tier": 2,
      "scheduled_for": "2026-08-24T09:15:00+05:30",
      "requires_approval": false,
      "message_preview": { "subject": "…", "body": "…" },
      "rationale": "Link opened twice without payment; cause inferred as cash_crunch. Offering split."
    }
  ],
  "counts_by_tier": { "1": 12, "2": 34, "3": 9, "4": 2 },
  "counts_by_channel": { "email": 41, "whatsapp": 16 }
}
```

### `PATCH /plan/actions/{action_id}`
Edit the drafted message before it sends.
```json
{ "subject": "…", "body": "…" }
```
Re-runs content validation. Returns `422` with the failing rule if the edit breaks policy
(e.g. removes the payment link or the opt-out).

### `DELETE /plan/actions/{action_id}`
Cancel a single scheduled action.

### `POST /plan/activate`
Arms the scheduler. Nothing sends before this.

---

## Approvals

### `GET /approvals`
The "needs you" queue.
```json
{
  "items": [
    { "action_id": "uuid", "kind": "escalation", "invoice_number": "INV-4471",
      "counterparty_name": "Acme Distributors Pvt Ltd", "outstanding_paise": 42000000,
      "tone_tier": 3, "reason": "Promise broken on 2026-08-18; second escalation.",
      "message_preview": { "subject": "…", "body": "…" },
      "history_summary": "3 touches, 1 broken promise, link opened 2x" },
    { "reply_id": "uuid", "kind": "dispute", "raw_text": "…", "confidence": 0.91 },
    { "reply_id": "uuid", "kind": "unclear_reply", "raw_text": "…", "confidence": 0.42 }
  ],
  "total": 4
}
```

### `POST /approvals/{action_id}/approve`
### `POST /approvals/{action_id}/reject` — body: `{ "reason": "…" }`
Rejection returns the invoice to the agent, which selects a lower-tier alternative.

---

## Accounts

### `GET /counterparties/{id}/timeline`
Every event on the account, in order. Backs the account detail screen.

```json
{
  "counterparty": { "id": "uuid", "name": "…", "avg_days_to_pay": 71.4, "broken_promise_count": 1 },
  "events": [
    { "at": "2026-08-01T09:02:00+05:30", "kind": "message_sent", "channel": "email",
      "tone_tier": 1, "summary": "Pre-due courtesy note", "source": "llm" },
    { "at": "2026-08-03T14:22:00+05:30", "kind": "link_opened" },
    { "at": "2026-08-06T11:40:00+05:30", "kind": "reply_received", "intent": "promise_to_pay",
      "extracted_date": "2026-08-11", "raw_text": "next Tuesday tak clear kar dunga" },
    { "at": "2026-08-06T11:40:05+05:30", "kind": "outreach_suppressed", "until": "2026-08-12" },
    { "at": "2026-08-12T09:00:00+05:30", "kind": "promise_broken" },
    { "at": "2026-08-15T10:31:00+05:30", "kind": "payment_received", "amount_paise": 42000000 },
    { "at": "2026-08-15T10:31:02+05:30", "kind": "actions_revoked", "count": 4 }
  ]
}
```

---

## Dashboard

### `GET /metrics?from=2026-08-01&to=2026-08-23`
```json
{
  "recovered_paise": 3840000000,
  "outstanding_paise": 10360000000,
  "recovery_rate": 0.27,
  "dso_before_days": 73.2,
  "dso_after_days": 58.6,
  "dso_delta_days": -14.6,
  "promise_kept_rate": 0.68,
  "invoices_by_state": { "chasing": 28, "promised": 9, "escalated": 4, "settled": 17, "stopped": 3 },
  "recovered_series": [ { "date": "2026-08-01", "paise": 0 }, { "date": "2026-08-02", "paise": 120000000 } ]
}
```

`dso_delta_days` is the headline number in the demo. Compute it, never assert it.

### `GET /exceptions`
```json
{
  "items": [
    { "invoice_number": "INV-4102", "counterparty_name": "…", "outstanding_paise": 190000000,
      "stop_reason": "broken_promises_exceeded", "stopped_at": "2026-08-14T09:00:00+05:30",
      "detail": "3 promises broken: 2026-07-22, 2026-08-02, 2026-08-13" }
  ],
  "total": 3
}
```

### `POST /exceptions/{invoice_id}/take-over`
Merchant assumes manual handling. Invoice leaves automation permanently.

---

## Audit

### `GET /audit?invoice_id=&counterparty_id=&action_type=&outcome=&from=&to=&limit=100`
```json
{
  "items": [
    {
      "id": 88213,
      "at": "2026-08-12T19:04:00+05:30",
      "actor": "agent",
      "action_type": "send_message",
      "subject": { "type": "invoice", "id": "uuid", "invoice_number": "INV-4471" },
      "rationale": "Promise broken 2026-08-12. Escalating to tier 3.",
      "gate_verdicts": [
        { "check": "time_window", "passed": false, "reason": "19:04 IST is outside 08:00-19:00" },
        { "check": "freshness", "passed": true },
        { "check": "consent", "passed": true },
        { "check": "frequency_cap", "passed": true },
        { "check": "value_threshold", "passed": false, "reason": "₹4.2L exceeds ₹5L? no — tier 3 requires approval" },
        { "check": "content_policy", "passed": true },
        { "check": "stopping_rules", "passed": true }
      ],
      "outcome": "blocked",
      "entry_hash": "…", "prev_hash": "…"
    }
  ],
  "total": 1247
}
```

**Blocked entries are the ones judges care about.** Make sure the UI surfaces them, not just sends.

### `GET /audit/verify`
Walks the hash chain and reports whether it is intact. One endpoint, big credibility payoff.

---

## Settings

### `GET /settings` · `PATCH /settings`
```json
{
  "contact_hour_start": 8,
  "contact_hour_end": 19,
  "weekly_touch_cap": 2,
  "lifetime_touch_cap": 6,
  "approval_value_threshold_paise": 50000000,
  "approval_tone_tier": 3,
  "is_paused": false
}
```

### `POST /settings/pause` · `POST /settings/resume`
Global kill switch. Must halt all outbound within one dispatch window.

---

## Webhooks

### `POST /webhooks/razorpay`
No auth header. Verified by `X-Razorpay-Signature`.

**Handler order is mandatory:**
1. Read the **raw** body as bytes
2. Verify HMAC-SHA256 against the webhook secret
3. Invalid → `400`, log, stop. Never parse an unverified body.
4. Parse, and take the event id from the **`x-razorpay-event-id` header** (case-insensitive).
   The envelope has no top-level `id`.
5. Insert into `webhook_events` on `razorpay_event_id`; on conflict → `200`, stop
6. Enqueue for processing
7. Return `200` — target under 200 ms

Handled: `payment_link.paid`, `payment_link.partially_paid`, `payment_link.expired`,
`payment_link.cancelled`, `invoice.paid`, `invoice.partially_paid`, `invoice.expired`

**Never 4xx a verified event.** Razorpay retries a non-2xx indefinitely, so rejecting a genuine
event is an infinite loop. A verified payload missing the id header is processed under a
body-derived fallback key (`sha256:<hex>`), which still dedupes because a redelivery carries
identical bytes.

**Repeat deliveries are expected behaviour**, documented by Razorpay — at-least-once, not a
malfunction. `{"status": "duplicate"}` is a healthy response and must not raise an alert.

**SLA: a 2XX within 5 seconds.** The 200 ms target above is our own stricter margin, not the
documented ceiling.

### `POST /webhooks/inbound/{channel}`
Inbound replies from email/SMS/WhatsApp providers. Provider-specific signature verification,
then the same insert-first-process-second pattern.

---

## Manual actions

### `POST /invoices/{id}/mark-paid-offline`
```json
{ "amount_paise": 42000000, "method": "neft", "reference": "UTR123456", "paid_on": "2026-08-15" }
```
Runs the identical settle path as a webhook: mark settled, **revoke all pending actions**,
close open promises, write audit entry with `actor: human`.

### `GET /invoices/{id}/reconciliation-status`
Polled by the Dashboard after a payment lands. Cheap read, safe on a short interval.

```json
{
  "invoice_id": "uuid",
  "settled": true,
  "settled_at": "2026-08-24T14:22:09+05:30",
  "revoked_actions": 3,
  "promises_closed": 1,
  "payment_status": "paid",
  "outstanding_paise": 0
}
```

**Why this exists.** `revoked_actions` is the demo's central number — the answer to "did you stop
chasing someone who paid?" — but it cannot be returned by `POST /webhooks/razorpay`. That handler
must acknowledge in under 200 ms with reconciliation deferred (ADR-006), so at the moment it
replies the revocation has not happened yet. Reconciling inline to populate the response is
exactly what turns one payment into a Razorpay retry storm.

So the count reaches the screen by polling. It is read from the `reconcile.settle` audit entry
rather than recounted from `actions`, because a recount would include actions revoked for
unrelated reasons (a dispute, a merchant exclusion) and report a number the audit trail does not
support. The figure on screen is the figure a judge will find in the log.

`payment_status` and `outstanding_paise` are included so a poller can distinguish "not settled
yet" from "partially paid and still owing" without a second request.

### `POST /invoices/{id}/mark-disputed`
```json
{ "reason": "Customer claims short delivery on PO-8821" }
```
Freezes all outreach immediately.

---

## Health

### `GET /health`
```json
{
  "status": "ok",
  "checks": { "database": "ok", "razorpay": "ok", "llm_primary": "ok", "llm_fallback": "ok" },
  "scheduler": { "running": true, "next_dispatch": "2026-08-23T09:15:00+05:30" }
}
```
