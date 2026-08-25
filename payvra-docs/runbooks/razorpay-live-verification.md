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

### 2. `reference_id` may not be reusable

`links.py` sets `reference_id` to `invoice.invoice_number` on **every** link, and
`regenerate_if_needed` (FR-9.4) creates a *second* link for the same invoice — same
`reference_id`, different idempotency key. Razorpay treats `reference_id` as a unique identifier
per account.

If it rejects the duplicate, regeneration has never worked outside the stubs. The
`--skip-dup-check`-able probe in `verify-razorpay` tests exactly this and prints the rework note
if it 4xxs.

---

## What this does not cover

- **Delivery providers** (Resend/MSG91/WhatsApp) — separate keys, separate verification.
- **Settlement against a seeded invoice** — B5 deliberately uses a throwaway reference. Pay a link
  built for a real invoice to exercise `settle_invoice` end to end.
- **The dashboard poll** — `GET /invoices/{id}/reconciliation-status` is built and tested, but
  rendering it is Phase 8 (see `agents/frontend.md`).
