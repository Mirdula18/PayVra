# PAYVRA

**Pay. Recover. Grow.**

An autonomous B2B receivables-recovery agent for Indian businesses, built on Razorpay.

Indian SMEs wait an average of 73 days to get paid, with several lakh crore rupees locked in
overdue invoices. The problem isn't visibility — every accounting system produces an aging
report. The problem is that recovery is manual, un-prioritised, emotionally awkward, and
disconnected from the act of actually collecting money.

PAYVRA closes that loop.

## What it does

1. **Ingests** a batch of open invoices (CSV, Google Sheet, Tally export, Zoho Books)
2. **Ranks** them by recoverable money — `P(collectable) x amount x urgency` — not by age
3. **Diagnoses** why each invoice is unpaid: forgotten, disputed, cash-crunched, wrong contact
4. **Executes** a bounded multi-channel dunning sequence, with a live Razorpay Payment Link
   embedded in every message
5. **Tracks** promises to pay and auto-follows-up when they break
6. **Escalates** within hard guardrails — RBI contact hours, consent checks, frequency caps,
   human approval above a value threshold
7. **Reconciles** automatically on webhook, cancelling all pending outreach the moment money lands
8. **Reports** rupees recovered, DSO reduction, and a complete audit trail of every action taken
   *and every action refused*

## Built for

Razorpay AI Buildathon — **Track 3: AI Revenue Recovery**

## Quick start

```bash
cp .env.example .env        # fill in Razorpay test keys + one free LLM key
make install
make db-up                  # migrations
make seed                   # 120 synthetic invoices
make dev                    # API on :8000, web on :5173
```

You need, at minimum:
- Razorpay **test mode** key id + secret + webhook secret
- One free LLM key: Groq or Google AI Studio (see `decisions/ADR-003-llm-provider.md`)
- A tunnel for webhooks: `cloudflared tunnel --url http://localhost:8000`

## Documentation

Start at [`CLAUDE.md`](./CLAUDE.md). It routes to everything else.

## Status

Hackathon MVP. Razorpay test mode only. No real money moves.
