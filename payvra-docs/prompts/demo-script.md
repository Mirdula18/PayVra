# Demo Script

**Format:** 5-minute pitch video plus a public GitHub repo and the architecture.
**Goal:** the judge remembers one thing — *the loop closes and the money is real.*

Rehearsal conditions, the pre-flight checklist and the full Q&A bank live in
`runbooks/demo-rehearsal.md`. This file is the run of show.

---

## Which run leads: `c8e62a04` (₹3,18,154), not the ₹6,00,000

There are two live runs with real money against them, and **the larger number is the wrong one to
lead with.**

| | `c8e62a04` — 19:13 IST | `14060ac6` — 20:38 IST |
|---|---|---|
| Recovered | ₹3,18,154 | **₹6,00,000** |
| Accounts | **20** | 1 |
| Refusals | **12** | 0 |
| Tone tiers exercised | **1, 2 and 3** | one action |
| Bar clauses evidenced | **all four** | clause 1 only |

The bar asks for *"measured money recovered **across a batch**, with compliant escalation, stopping
rules, and an audit trail."* The ₹6,00,000 run is a single account with zero refusals. It is a
bigger number attached to a smaller claim: it shows nothing about batch behaviour, nothing about
escalation, and produces almost no audit trail. Leading with it would answer one clause loudly and
three not at all.

`c8e62a04` evidences all four in one screen: 20 accounts considered, 5 actions completed,
**12 refused across three different rule families**, ₹3,18,154 recovered and attributed.

**The ₹6,00,000 is not discarded — it is the answer to a question.** When a judge asks about a
large invoice, that run is the evidence, and it lands harder as a response than as an opening. See
Q2 in the Q&A.

---

## Pre-computed vs live

**Nothing on stage waits on a third party.** Every model call, payment link and webhook has already
happened; the demo reads what they produced.

| Step | State | Why |
|---|---|---|
| The two live runs | **Pre-computed** | Already in the database. At ~30 RPM a 20-account run takes minutes |
| Payment links | **Pre-computed** | Real, already created and already paid |
| Webhooks / settlement | **Pre-computed** | Already received, verified and reconciled |
| **The delivered email** | **Pre-computed** | Sent 2 Sep, already sitting in the inbox. Opened from a tab, never triggered on stage |
| All three screens | **Live** | Server-rendered reads of local Postgres — no API in the path |
| `--report` in a terminal | **Live, optional** | One local query. Safe |
| A fresh batch run | **NEVER on stage** | Model latency, Razorpay rate limits, link budget — **and it now sends real email** |

The only live things are page loads against your own database. There is no call to Razorpay, Groq,
Gemini or Resend anywhere in the five minutes.

**Sending live is the one addition that would break this property, and it is not worth it.** An
inbox refresh is somewhere between two seconds and a minute, none of which you control, and dead
air on a five-minute video is unrecoverable. The stored send is better evidence anyway: it has a
provider message ID in the log, which a live send would not have until it landed.

**If a judge asks whether the batch was pre-computed, say yes** — it is how any system with rate
limits behaves, the batch genuinely ran, it ran earlier. Honesty costs nothing here and being
caught costs everything.

---

## 0:00–0:40 — The hook

No slides. Open on the **Worklist**.

> "Indian SMEs wait 73 days to get paid. Seven lakh crore rupees are sitting in overdue B2B
> invoices right now. Every accounting system in the country can already tell you *what's* overdue.
> None of them collect it.
>
> That handoff — from knowing to getting paid — is where the money dies. PAYVRA closes it."

Do not say "AI-powered". Do not open the architecture.

## 0:40–1:30 — Screen 1: Worklist → *clause 1 context*

Point at the ordering, then at one reason string.

> "₹2.01 crore of open receivables, worst first. This is not an aging report — it's ranked by
> **recoverable money**: probability of collection, times amount, times urgency.
>
> Look at row four. Meridian Logistics, ₹8.3 lakh, **ten days late**.
>
> Now row seven — Blue Ocean Exports, **128 days** late. And row eleven, Zenith Marketing,
> **₹14 lakh**, 86 days late. Both rank *below* a ten-day-old invoice.
>
> Because a ₹19.9 lakh relationship that always pays is worth more than an old invoice that never
> will. And every row says *why*, in plain English. Not a score. A sentence."

**Do not claim the ranking is smart. Let them read rows four, seven and eleven.**

> ⚠️ **Re-read these three rows before recording.** The total and the ordering shift as payments
> land — the ₹2.07 crore in an earlier draft of this script was already stale by ₹6 lakh after the
> tranche run. Numbers spoken aloud have to be checked the morning of, not inherited.
>
> **Do not say "across 120 invoices".** The chip on screen totals the **40 rows displayed**, not
> the book. There are 120 invoices and 115 still owing, and the whole book is ₹3.06 crore — a
> figure that appears nowhere on screen. Verified 2 Sep: the chip reads **₹2,01,48,694**, and rows
> four, seven and eleven are still Meridian (₹8.3L, 10 days), Blue Ocean (128 days) and Zenith
> (₹14L, 86 days).

## 1:30–2:20 — Screen 3: Audit log → *clauses 3 and 4*

**Stay on the default "All runs" view. Do not filter to a run here.**

That is not laziness — `gate.*` entries do not carry a `recovery_run_id` (the gate writes its own
audit entry and has no knowledge of runs), so filtering to a single run hides every per-check
verdict and leaves the `Gate` column reading `—`. The unfiltered view is where the pills, the
`blocked` outcomes and the full picture are. Run-scoping is carried by the **Recovery** screen,
which does it properly. See the known-issues note in `runbooks/demo-rehearsal.md`.

This is the centre of the demo; give it the time.

> "This is the agent's decision log. Every action it proposed, and what happened to it.
>
> **Fifty-nine refused by the gate. Ninety-six refused inside a run.** Forty-four executed. And
> they're all in the same list, not a separate tab, because the refusals are the point."

Click **Refused by gate**, then read three aloud, unhurried, pointing at the red pills:

> "*Value threshold — ₹9.3 lakh is above what the agent may send without a human.* *Time window —
> outside contact hours.* *Stopping rules — this counterparty is on the exception list.*
>
> The agent proposed all of these. A deterministic gate refused them, and wrote down which rule
> stopped each one."

> ⚠️ **Two things to know before you point at a row.**
>
> **The chip counts drift, and they have.** They were 34 / 56 / 37 when this script was first
> written and are 59 / 96 / 44 now, because the Phase 6.5 delivery runs on 2 Sep added their own
> refusals. Re-read the chips the morning of; do not recite any number from memory, including the
> ones above.
>
> **Stay on the 29 Aug and 2 Sep rows.** Of the 59 gate refusals, 30 are from the 29 Aug runs and
> 25 from the 2 Sep delivery runs — all real. Scroll far enough and you reach **four** seeded
> historical entries (31 Jul ×2, 13 Aug, 20 Aug) illustrating `freshness`, `frequency_cap`,
> `time_window` and `stopping_rules` — a realistic book has history, and the seed builds it. They
> are legitimate demo data, **but they did not happen on a live run**, so never point at one and
> imply it did. If a judge asks about a July date, say plainly it is seeded history and scroll
> forward.

Then the chain column:

> "Every entry is hashed over the one before it. Change any row and every link after it breaks.
> The log is append-only at the database level — there's no flag that turns that off."

Hover a chain cell to show the full hash pair.

> "Any tool can show you what it did. This shows what it **refused to do**, and the rule that
> stopped it."

## 2:20–2:40 — The email that actually went out → *clause 4*

**Alt-tab to a tab that already has the email open.** Do not send anything. Do not open an inbox
and refresh. Do not wait for anything to arrive. The tab is loaded before recording starts, and the
whole beat is twenty seconds.

This exists because `executed` is a claim, and until Phase 6.5 it was a claim the log could not
back. Now it can.

> "One thing about that `executed` pill — it doesn't mean drafted, and it doesn't mean queued.
>
> This is the message. A real send through a real provider, with the provider's message ID written
> back into the log next to the action. Tier two, because this is the second attempt on this
> invoice. The link in it is a live Razorpay payment link.
>
> And there's an opt-out at the bottom of every one of these, because a collections message you
> can't unsubscribe from isn't compliant, whatever it says in the body."

Let them read it. Do not recite the message aloud — twenty seconds of a judge reading a real email
is worth more than you narrating it.

> ⚠️ **Two things about this screen that you volunteer rather than get caught on.**
>
> **The `To:` is your own address, not the counterparty's.** Every outbound email is redirected to
> one address by configuration — that is deliberate, the seeded book is realistic-looking addresses
> on a reserved domain, and sending collections mail to them would be indefensible. If the `To:`
> line is visible on screen, say so in one sentence: *"every send is redirected to a single test
> inbox — the counterparty addresses in the seed are fake, and I'm not emailing strangers to
> rehearse a demo."* It reads as care. Being asked about it first reads as a bug.
>
> **Do not click the opt-out link.** It points at a cloudflared quick tunnel that no longer exists,
> because the URL is baked into the body at send time and quick tunnels get a new address on every
> restart. The link being present is the compliance claim; the tunnel being live is not part of the
> demo. See the checklist in `runbooks/demo-rehearsal.md`.

## 2:40–3:30 — Screen 2: Recovery → *clause 1*

Same run selected. **The second figure on this screen reads ₹0. Explain it before anyone asks.**

> "₹3,18,154 recovered. Real money, on a real Razorpay link, paid by a real payment, reconciled by
> a signed webhook.
>
> There are two figures here, and the second one is zero. That's not a gap in the data — it's the
> reason we lead with the first.
>
> **Time-window** counts everything that arrived while the run was open. This run was open for five
> seconds. Nobody pays an invoice in five seconds, so it counts nothing.
>
> **Causal** counts money received against the invoices this run actually acted on, with no closing
> time — because recovery isn't instant. A counterparty pays hours or days later. That's the
> ₹3,18,154."

Then the point. **This is the moment that separates the pitch.**

> "Which is the thing about a time-window number: it's mostly a measure of how long your batch ran.
> Leave it going overnight and it gets enormous, and none of that extra money is yours.
>
> So we don't lead with it. We lead with the one that can name the invoices — and even that one
> leaves money out. There are accounts in this run where the gate refused to let us make contact,
> and the customer paid anyway. We could count that. We don't, because we didn't earn it.
>
> It's the smaller claim. It's the one that survives the question."

## 3:30–4:20 — Escalation and the ₹14 lakh answer → *clause 2*

Back to the **Audit log**, "All runs", **Refused by gate** filter still on.

> "Compliant escalation, concretely: attempt one goes out at tier one, attempt two at tier two,
> attempt three at tier three — and at tier three the gate stops it and asks a human.
>
> The system is free to be gentler on its own. It has to ask permission to be firmer."

Now switch the Recovery screen to run **`29 Aug 20:38 IST`** — the ₹6,00,000 tranche run.

> "One more. Razorpay caps a single payment link at around ₹5 lakh. This invoice is ₹14 lakh.
>
> So it's collected in tranches. ₹6 lakh has come in — reconciled twice, once partial, once full —
> and ₹8 lakh is still open. The invoice is **not closed**, and we still report the ₹6 lakh,
> because we count money received rather than invoices closed."

## 4:20–5:00 — The close

Back to the Audit log, refusals filter still on.

> "So: it ranks by recoverable money, diagnoses why each invoice is unpaid, proposes one action per
> account from a closed list — and a deterministic gate decides whether it runs. Seven checks,
> every one recorded, pass or fail. What passes gets sent, and what gets sent is what the log calls
> executed. Nothing else earns that word.
>
> Measured money recovered. Compliant escalation. Stopping rules. An audit trail.
>
> Not a dashboard that tells you what's overdue. Something that collects it — and can show you
> every single thing it refused to do on the way."

Leave the refusal list on screen. Stop talking.

---

## Screen order, against the bar

| # | Screen | Clause |
|---|---|---|
| 1 | Worklist | 1 (context) — recoverable money, not age |
| 2 | **Audit log** | **3 and 4** — refusals beside sends, hash chain |
| 3 | **The delivered email** | **4** — `executed` means a message left the building |
| 4 | Recovery | 1 — causal headline, the ₹0 explained |
| 5 | Audit log, refusals filter | 2 — the tier ladder and its approval ceiling |
| 6 | Recovery, tranche run | 1 — the ₹14 lakh answer |

The email sits **immediately after** the audit log and before the money, because it closes the
audit log's own loop: a judge who has just read a column of `executed` pills is holding exactly the
question the email answers, and answering it there costs twenty seconds. Moved later it becomes a
feature tour; moved earlier it is a screenshot with nothing to prove.

**The audit log is second, not last.** It is the strongest screen and the one a judge is least
likely to have seen from another team. Leading with the number invites arithmetic; leading with the
refusals invites trust, and the number then reads as credible rather than as a claim.

Judges should reach the end having *seen* all four clauses without hearing you assert any of them.

---

## Things that will lose it

- Opening with the architecture instead of the problem
- Saying "AI-powered" before showing a refusal
- Running a live batch on stage
- Claiming a clause instead of showing it
- **Not explaining the ₹0 before a judge notices it**
- **Triggering a send on stage and watching an inbox** — the one avoidable source of dead air
- **Being asked about the `To:` address instead of volunteering it**
- Hiding that the batch was pre-computed
