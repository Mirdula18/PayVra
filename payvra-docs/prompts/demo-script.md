# Demo Script

**Format:** 5-minute pitch video plus a public GitHub repo and the architecture.
**Goal:** the judge remembers one thing — *the loop closes and the money is real.*

---

## Before you record

- [ ] `make seed-demo` — deterministic, fixed seed, pinned `DEMO_DATE`
- [ ] **Batch pre-computed.** At 30 RPM, 300 generation calls take 10+ minutes. Never run a full
      batch live. Pre-generate messages; make only 3–5 calls live.
- [ ] Razorpay test-mode links verified working; stay under the 30-link cap
- [ ] Webhook tunnel up and verified (`cloudflared tunnel --url http://localhost:8000`)
- [ ] Test payment rehearsed end to end at least three times
- [ ] Browser at 1920×1080, zoom 100%, tabs pre-opened, notifications off
- [ ] A recorded fallback video exists in case the live demo fails

**If a judge asks whether the batch was pre-computed, say yes.** It is how any real system with
rate limits behaves. The batch genuinely ran; it ran earlier. Honesty here costs nothing and
dishonesty caught on stage costs everything.

---

## Minute 0:00–0:30 — The hook

Open on the messy invoice batch. No slides yet.

> "Indian SMEs wait 73 days to get paid. Seven lakh crore rupees are locked in overdue B2B
> invoices right now. Every accounting system in the country can already tell you *what's*
> overdue. None of them collect it. That handoff — from knowing to getting paid — is where
> the money dies. PAYVRA closes it."

Do not explain the architecture yet. Do not say "AI-powered." Show the problem.

---

## Minute 0:30–1:30 — Detection and ranking

Upload the batch. Let it process on camera — it takes seconds and looks credible.

> "380 invoices. ₹1.42 crore outstanding. 61 past due."

Cut to the worklist.

> "This is not an aging report. It's ranked by *recoverable money* — probability of collection,
> times amount, times urgency. The oldest invoice isn't first, because oldest is rarely most
> collectable."

Point at a specific `priority_reason`:

> "₹4.2 lakh, 68 days. This customer has paid late twice before but always paid. That's why
> it's ranked third — and PAYVRA will tell you that in plain English for every row."

Note the quarantine count in passing:

> "Three counterparties are quarantined. No consent on file, so we never contact them."

---

## Minute 1:30–3:00 — Diagnosis and the guardrails

Open one account timeline. Point to the Hinglish reply in the seed data.

> "Six days ago this customer wrote: *'bhai next Tuesday tak clear kar dunga.'* PAYVRA read
> that, logged a promise to pay on the 11th, and suppressed all outreach until the 12th.
> No regex parses that sentence."

Now the agent decision. Show the rationale, then pivot to the gate — **this is the segment that
wins the "what if the AI goes rogue" question, so do not rush it.**

> "Here's what matters. The model doesn't send anything. It *proposes* — a JSON object with an
> action, a channel, a tone tier, and a reason. Then seven deterministic checks run before
> anything leaves the system."

Walk the seven verdicts on screen. Then:

> "Contact hours. RBI conduct norms say 8am to 7pm. It's hardcoded, and here" —

**Open the audit log, filter to `blocked`.**

> "— is every message PAYVRA *refused* to send. Blocked for being outside contact hours. Blocked
> for hitting the frequency cap. Blocked because the customer had already paid between planning
> and dispatch. The audit trail records refusals with the same fidelity as sends. That's the
> compliance story, and it's structural, not aspirational."

---

## Minute 3:00–4:15 — The killer moment

Live. Three to five real LLM calls, no more.

1. Agent proposes an action for a chosen account — rationale visible
2. Message drafts live in Hinglish — read one line aloud
3. Razorpay Payment Link generates — show the `reference_id` matching the invoice number
4. Message sends

Then **pay the link on your phone, on camera.**

> "I'm paying this as the customer would."

Cut back to the dashboard. The webhook fires.

> "Webhook verified, invoice settled, **four pending actions revoked**, promise closed,
> recovered total updated. PAYVRA will never message this customer again about this invoice —
> because the loop closed the moment the money landed."

**That revoke count on screen is the moment.** Make sure it renders visibly. If the UI does not
show "4 actions revoked", build that before you record.

Then show one graceful failure:

> "And here's an expired link being auto-regenerated. Things break; the agent handles it."

---

## Minute 4:15–5:00 — The numbers and the close

Dashboard.

> "Across this batch: **₹38.4 lakh recovered. DSO down 14.6 days.** 68% of promises kept.
> And an honest exception list — three accounts PAYVRA stopped chasing, with the reason for
> each. It doesn't pretend it recovers everything."

Then the two claims that land with a payments-company judge:

> "Two things. First — Razorpay's Agent Studio ships agents for subscription recovery, cart
> abandonment, disputes and forecasting. It doesn't ship one for B2B receivables. That's the
> gap we built into.
>
> Second — this runs on a five-hundred-rupee container with zero GPU spend. The intelligence is
> in the policy design and the guardrails, not in burning compute."

Close:

> "PAYVRA. Pay. Recover. Grow."

---

## Judge Q&A — rehearse these

**"How do I know the AI won't do something harmful?"**
Open `agent/registry.py` — closed tool registry. Open `guardrails/gate.py` — seven checks.
Show a rejected hallucination in the audit log. Say: the model proposes, deterministic code disposes.

**"Is the money real?"**
Razorpay test mode. The rails, the webhook, the signature verification, and the reconciliation are
production code paths. Only the money is sandboxed.

**"Is the data real?"**
Synthetic, deliberately. Modelled on the Recordent 2026 SME receivables report — 73-day average
collection, 82.6% of invoices on 0–30 day terms. Say this plainly; the track expects synthetic batches.

**"What's actually AI here?"**
Four things: reply classification, Hinglish date extraction, action proposal, message drafting.
Everything else — aging, scoring, ranking, gating, reconciliation — is deterministic. That ratio
is the design achievement.

**"What if the LLM is down?"**
Demo it. `LLM_ENABLED=false`, run the batch, templates carry it. The product degrades, it does not stop.

**"Isn't this just dunning software?"**
Kapittx, Growfin, CredFlow all chase and then hand off the payment. None of them *is* the payment
rail. We generate the link, we get the webhook, we reconcile, we stop. Nobody else closes that loop.

**"How does it make money?"**
SaaS plus a success fee on recovered amounts. Incentives aligned — we only win when the merchant
gets paid.

**"Does this need GPUs?"**
No. Every model call is a hosted inference API call. 1 vCPU, 512 MB.

---

## Things that will lose it

- Running the full batch live and standing in silence for ten minutes
- Leading with architecture instead of the problem
- Saying "AI-powered platform" — say what the AI does
- A demo that shows detection but not recovery
- Not showing the blocked-actions view
- The revoke count not being visible when the payment lands
- Claiming a DSO improvement without showing it computed
