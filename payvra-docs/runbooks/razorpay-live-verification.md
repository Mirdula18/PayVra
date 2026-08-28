# Runbook: verifying the Razorpay leg against the real API

Everything Razorpay-facing was built against a stubbed transport. This runbook is the one thing
that talks to the live test-mode API and to a genuinely Razorpay-signed webhook. Run it **before
Phase 5**: a wrong assumption here is Phase 4 rework, and the worst time to discover it is during
a rehearsal.

Four assumptions are under test:

| # | Assumption | Proven by |
|---|---|---|
| 1 | `create_payment_link` returns the field shape `links.py` reads (`id`, `short_url`, `status`) | `make verify-razorpay` |
| 2 | `reference_id` survives the round trip into the webhook payload | `verify-razorpay` (create + fetch), then `inspect-webhook` (real event) |
| 3 | `notes` survives too, with both internal ids | same |
| 4 | The test-mode webhook secret verifies a real Razorpay-signed payload | `make inspect-webhook` |

Assumptions 1 and 3 are provable without a tunnel. Assumption 4 is not provable at all until
Razorpay signs a request and sends it to us, which is what Part B exists for.

> **`make` is not installed on the current dev machine.** Every `make` command below has a raw
> equivalent; run that instead. From the repo root (`D:\PayVra`), PowerShell:
>
> | `make …` | raw equivalent |
> |---|---|
> | `make db-up` | `docker compose up -d db` then `.venv\Scripts\alembic.exe -c api\alembic.ini upgrade head` |
> | `make dev` | `.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000` |
> | `make tunnel` | `cloudflared tunnel --url http://localhost:8000` |
> | `make verify-razorpay` | `cd api; ..\.venv\Scripts\python.exe -m scripts.verify_razorpay` |
> | `make inspect-webhook` | `cd api; ..\.venv\Scripts\python.exe -m scripts.inspect_webhook` |
>
> `ARGS="--keep"` becomes a plain trailing argument: `… -m scripts.verify_razorpay --keep`.
> The `cd api` matters — `scripts` is only importable from there. `app` is importable anywhere
> because the package is installed editable.

---

## Part A — outbound only (no tunnel, ~5 minutes)

### A1. Get test-mode API keys

1. Log in to the Razorpay Dashboard.
2. **Switch the mode toggle to `Test Mode`** (top bar). This is the step people skip, and a live
   key is refused at client construction, so getting it wrong fails loudly rather than quietly.
3. **Account & Settings → API Keys → Generate Test Key**.
4. You get a **Key Id** (`rzp_test_…`) and a **Key Secret**. The secret is shown **once** — copy it
   now.

### A2. Fill in `.env`

Three variables, at the repo root (`D:\PayVra\.env`). Only the first two matter for Part A:

```dotenv
RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxxx     # from A1
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxx  # from A1, shown once
RAZORPAY_WEBHOOK_SECRET=                    # Part B — you invent this, see B3
```

> **`RAZORPAY_WEBHOOK_SECRET` is not the Key Secret.** It is a separate string that *you choose*
> and type into the webhook form in B3. Pasting the API key secret here is the single most common
> cause of every webhook 400ing, and the symptom looks identical to a signing bug.

### A3. Run the outbound probe

```bash
make verify-razorpay
```

It preflights the credentials, then creates **one real payment link** (₹100) and reads it back.
It creates a **second** link only to test whether `reference_id` can be reused, and cancels both
before exiting — so a repeated run costs nothing against the 30-link test-mode cap.

Expected: a `PASS` line per check and `RESULT: all N checks passed`. Any `FAIL` is a real finding —
paste it back.

---

## Part B — the inbound half (tunnel + a real signed webhook)

Order matters. The API must be up before the tunnel, and the webhook must be registered before
you pay anything.

### B1. Start the API

```bash
make db-up     # Docker Desktop must be running
make dev       # uvicorn on :8000 — keep this window open and watch it
```

### B2. Open the tunnel

`cloudflared` is required. On Windows:

```bash
winget install --id Cloudflare.cloudflared
```

Then, in a second window:

```bash
make tunnel
```

It prints a line like:

```
https://random-words-here.trycloudflare.com
```

> **This URL changes every time you restart the tunnel**, and the webhook registration in B3 does
> not follow it. Leave the tunnel running for the whole session, and re-register if you restart
> it. Budget for this on demo day.

### B3. Register the webhook

In the Razorpay Dashboard, still in **Test Mode**:

1. **Account & Settings → Webhooks → Add New Webhook**.
2. **Webhook URL** — the tunnel URL **plus the full API path**:
   ```
   https://random-words-here.trycloudflare.com/api/v1/webhooks/razorpay
   ```
   The `/api/v1` prefix is easy to drop and produces a silent 404 rather than an error you notice.
3. **Secret** — invent one now (any strong string). Put the *same* value in `.env` as
   `RAZORPAY_WEBHOOK_SECRET`, then **restart `make dev`** so settings reload.
4. **Active Events** — tick:
   - `payment_link.paid`
   - `payment_link.partially_paid`
   - `payment_link.expired`
   - `payment_link.cancelled`
5. Save.

### B4. Create a payable link and pay it

```bash
make verify-razorpay ARGS="--keep"
```

`--keep` leaves the probe link payable instead of cancelling it. Open the printed `PAY THIS LINK`
URL and pay with a Razorpay test card:

- **Card** `4111 1111 1111 1111`
- **Expiry** any future date · **CVV** any 3 digits

Watch the `make dev` window as the payment completes.

### B5. Inspect what actually arrived

```bash
make inspect-webhook
```

This reads the stored event and reports assumptions 2, 3 and 4 against a real signed payload.
`ARGS="--raw"` dumps the whole envelope — it contains counterparty PII, so do not paste it
publicly.

A probe link carries a `reference_id` no seeded invoice has, so **`unmatched` is the correct
outcome here**. Assumption 4 is still proven: the row only exists because the signature verified.
To see a real settlement, create a link for a genuinely seeded invoice and pay that.

---

## Diagnostics: nothing arrived

The endpoint rejects *before* it stores anything, so an empty `webhook_events` table is ambiguous.
The `make dev` log disambiguates it:

| Log line in `make dev` | Meaning | Fix |
|---|---|---|
| `invalid webhook signature; rejecting` | Reached us, signature failed | `RAZORPAY_WEBHOOK_SECRET` ≠ the secret typed in B3. Check you did not paste the API key secret. Restart `make dev` after editing `.env`. |
| `signed webhook body was not valid JSON` | Reached us, signature passed, body unparseable | Rare; capture the raw body from Razorpay's delivery log. |
| `verified webhook carried no x-razorpay-event-id header…` | Processed fine on a fallback key | Not an error. Confirm the header name against the delivery log. |
| `accepted webhook …` | Everything worked | Run `make inspect-webhook`. |
| Nothing at all | Never reached us | Tunnel died, URL changed, or the `/api/v1` prefix is missing from the registration. |

Razorpay's dashboard also keeps a per-webhook delivery log with the response code it received —
check there to confirm whether it thinks it delivered.

---

## Predicted findings

Two things are likelier than the rest to come back `FAIL`. Both are cheap to fix once confirmed,
and both are invisible to the stubs.

### 1. ~~The event id may not be in the body~~ — CONFIRMED AND FIXED

Resolved from Razorpay's official docs; no live call was needed. The envelope is
`{entity, account_id, event, contains, payload, created_at}` with **no top-level `id`**, and the
dedupe value is the **`x-razorpay-event-id` header**. The old `payload["id"]` read would have
400'd every genuine event into an infinite retry loop.

The handler now reads the header case-insensitively and, when it is absent on an
already-verified payload, degrades to a body-derived `sha256:` key rather than rejecting.
`make inspect-webhook` reports which of the two was used — a `sha256:` key on a real delivery
means the header did not arrive and is worth checking against Razorpay's delivery log.

### 2. `reference_id` may not be reusable — ✅ CONFIRMED AND FIXED (2026-08-26)

Razorpay **does** enforce `reference_id` uniqueness per account and rejects a reuse with
`400 BAD_REQUEST_ERROR`. Every FR-9.4 regeneration would have failed in production, and
`link_hygiene` with it — its inner handler catches only `LinkBudgetExceeded`, so a
`RazorpayClientError` would have rolled the whole job back, losing cancellations that had already
succeeded.

Fixed in commit `7c830db`: `next_reference_id()` keeps the clean invoice number for the first link
and suffixes regenerations `-R2`, `-R3`; `base_reference_id()` strips the suffix on reconciliation's
fallback route. Check 4 of `verify-razorpay` now asserts both halves — a duplicate must be refused,
a suffixed reference must be accepted — so it stays a regression guard.

**The stubbed transport accepted the duplicate happily. Only a real call caught it.** That is the
entire argument for this runbook.

---

## Settlement verified end to end — ✅ 2026-08-26

`INV-2026-1020`, ₹23,134, on a link created by `scripts/create_demo_link` against a real seeded
invoice. Paid by card in test mode; a genuinely Razorpay-signed `payment_link.paid` arrived,
verified, deduped on the `X-Razorpay-Event-Id` header, and settled the invoice:
`payment_status → paid`, `recovery_state → settled`, `outstanding_paise → 0`, `settled_at` set.

All four assumptions at the top of this runbook are now proven.

---

## 🔴 Still blocked: live revocation-on-settle

**Scheduled, not assumed.** This is the one part of Phase 4 reconciliation that has **never
executed against real data**, and it is the part the docs call the most important line in the
product: when an invoice settles, every pending `Action` for it must be revoked in the same
transaction. Chasing someone who has already paid is the worst failure mode this product has.

The settlement above reported `revoked_actions: 0`. Not a failure — there was nothing to revoke.
The seed carries only `executed` and `gated_fail` actions; **pending actions are created by the
Phase 6 batch runner**, which does not exist yet. There is currently no way to produce the
precondition.

| | |
|---|---|
| **Blocked on** | Phase 6 — the batch runner creating `proposed` / `approved` actions |
| **Unblocks in** | Phase 6, immediately: run the batch, then pay a link for an invoice it just acted on |
| **Covered today by** | 4 unit tests, including transactional atomicity (`test_reconciliation.py`) |
| **Evidences** | Track 3 bar clause 3 (stopping rules) — a settled invoice not chased |

### The verification, to run in Phase 6

1. `make demo-link ARGS="--invoice <one the run just touched>"` — an invoice with pending actions
2. Confirm pending actions exist for it before paying
3. Pay the link
4. `make inspect-webhook` — **`revoked_actions` must be greater than zero**
5. Confirm every revoked row carries `revoked_at`, and that no further outreach is scheduled

Until step 4 shows a non-zero count against real data, treat revocation as unit-tested only and say
so. A `revoked_actions: 0` reading is not evidence of anything.

---

## What this does not cover

- **Delivery providers** (Resend/MSG91/WhatsApp) — separate keys, separate verification.
- **Live revocation-on-settle** — blocked on Phase 6; see above.
- **The dashboard poll** — `GET /invoices/{id}/reconciliation-status` is built and tested, but
  rendering it is Phase 8 (see `agents/frontend.md`).
- **The payment link amount ceiling** — links above roughly ₹5L are refused, so the three
  highest-value seeded invoices cannot be collected. Blocker; see ADR-006.
