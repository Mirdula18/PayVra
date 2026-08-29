# Runbook: the Phase 9 demo rehearsal

**Read this before the first rehearsal, not before the last one.** Two constraints below can turn
a working system into a zero on stage, and both are invisible until you hit them.

The script itself is `prompts/demo-script.md`. This runbook is about the conditions the demo runs
under. What is being evidenced is `requirements/track3-bar.md` — four clauses, four artefacts.

---

## 🔴 Constraint 1 — the contact-hours window will refuse your entire run

**Gate check 1 refuses every outbound action outside 08:00–19:00 IST.**

That is correct behaviour, it is a compliance claim made to a judge (NFR-5.1, RBI recovery-conduct
norms), and it stays the default. It also means:

> A rehearsal at 21:00 IST produces a run where **every single action is refused**. A flawless
> demonstration of clause 3 and a zero for clause 1, from the same command.

The failure is silent in the worst way — nothing errors. The run completes, the audit log fills
with refusals, and the recovered figure is ₹0. It looks like the agent decided not to act.

### Mitigation, in order of preference

**1. Rehearse and present inside the window.** The only mitigation with no explaining to do.
Build the schedule around it.

**2. Widen the window by environment variable** (FR-16.8), under three binding conditions:

* **The gate still executes.** There is no bypass path and no skip flag. The window is a value the
  check reads, never a rule it can be told to ignore. A gate with a bypass is not a gate.
* **Widened only by explicit environment variable.** Never a request parameter, never a default,
  never a UI toggle.
* **An active override is written into the audit log.** This is what keeps an out-of-window run
  compliant *by record* — the trail shows the window was widened and when, rather than leaving a
  judge unable to tell that it was.

If a run used an override, **say so before you are asked**, and show the audit entry. A system that
records its own exceptions is making the same argument as its refusal list. Being caught hiding one
undoes every other claim in the pitch.

**Never** demo with the window widened and the override unrecorded. That is not a shortcut, it is
the one thing the audit trail exists to prevent.

---

## ✅ Constraint 2 — the payment link amount ceiling — RESOLVED AND PROVEN LIVE

Links above roughly ₹5L are refused by Razorpay, and the three highest-priority seeded invoices are
₹14.0L, ₹10.7L and ₹9.3L — the top rows of the worklist. This used to mean the most valuable
receivables in the demo were the ones the system could not collect.

**Resolved by ADR-006 option C** — cap each link at the ceiling with `accept_partial` and collect in
tranches — and **exercised end to end against the real API on 29 Aug**:

```
INV-2026-1079        ₹14,00,000 outstanding
link created at      ₹5,00,000     capped, accept_partial: true
reference_id         INV-2026-1079-R2   (collision auto-resolved)
paid                 ₹1,00,000 partial, then ₹5,00,000
reconciled           payment_link.partially_paid, then payment_link.paid
outstanding after    ₹8,00,000     invoice still open
recovery reported    ₹6,00,000     1 partially recovered, 0 paid in full
```

This is no longer a risk to plan around — it is **an asset**. A judge asking about a large invoice
gets a real answer with real evidence, and the ceiling being handled rather than hidden is a
stronger story than never being asked. See Q2.

The tone tier also de-escalated 2 → 1 on the partial payment (FR-13.4): someone who has just paid
part of what they owe gets a gentler next message, not a firmer one. Worth mentioning if the
conversation goes that way.

---

## Known state — work around it, do not fix it before the demo

Recorded so nothing here reads as a surprise on stage.

| State | Why it stays |
|---|---|
| **9 invoices `stopped`, 8 with a null reason** | Pre-fix residue. A reseed clears them **and wipes the ₹3,18,154 and ₹6,00,000 figures with it.** Do not reseed before demoing. The Worklist shows these as `stopped` with no reason text — they are not on any screen the script visits |
| **Outbound messages are approved, never delivered** | No transport (FR-10, Phase 6.5, parked). The audit log says `approved`, never `executed`, for a send. If asked, see Q5 |
| **Phase 7 parked** | Reply handling and promise tracking are unbuilt. No clause depends on them |
| Contact-window override was used on three of the four live runs | Recorded in the audit log for each, and visible on screen as a `widened` pill. Compliant by record — see Q6 |
| **`gate.*` audit entries are not run-scoped** | Found during Phase 9 rehearsal. See below — it changes how the audit screen is demoed |

### `gate.*` entries carry no `recovery_run_id`

`gate()` writes its own audit entry and has no knowledge of runs, so only the runner's
`run.account` entries carry the id. Filtering the audit screen to one run therefore shows:

* ✅ every account the run touched, with a readable reason naming the rule that stopped it
* ❌ **no per-check verdicts** — the `Gate` column reads `—` for every row, because
  `run.account` entries have an empty `gate_verdicts` array

The pills, the `blocked` outcomes and the seven-check breakdown all live on the `gate.*` entries,
which the run filter excludes.

**Demo consequence: use the unfiltered "All runs" view for the audit beat.** It is the default
landing view, it is richer, and it is where the per-check evidence is. Run-scoping is carried by
the Recovery screen, which does it correctly — clause 1 needs the scope, clauses 3 and 4 need the
verdicts, and they are on different screens.

**Not fixed before the demo, deliberately.** The fix is to thread `recovery_run_id` into the gate's
audit entry, which is feature code in a phase scoped to docs and rehearsal. Nothing on screen is
wrong — every refusal still names its rule in the `Why` column. The information is complete; it is
the *filter* that is narrower than it looks. Worth fixing after submission.

## The state the demo actually reads

Verified 29 Aug. Everything below is already in the database; the demo creates nothing.

```
merchant   Nandi Industrial Supplies Pvt Ltd
           c2d79cf5-7f9f-fff3-e0b0-c708a30f6f20
invoices   120   (₹2.07 crore outstanding, 12 with money recovered against them)
audit_log  291 entries, hash chain verified
links      4 real Razorpay links, 3 of them paid
budget     17 of 30 used — 13 remaining
```

**Live runs available:**

| Run | IST | Accounts | Acted | Refused | Recovered |
|---|---|---|---|---|---|
| `c8e62a04` | 29 Aug **19:13** | 20 | 5 | **12** | **₹3,18,154** ← lead with this |
| `14060ac6` | 29 Aug **20:38** | 1 | 1 | 0 | ₹6,00,000 (tranche) ← the ₹14L answer |
| `bb6670f4` | 29 Aug 20:28 | 10 | 2 | 8 | ₹0 |
| `b4e43e2d` | 29 Aug 19:07 | 20 | 11 | 9 | ₹0 |

The reasoning for that ordering is in `prompts/demo-script.md`.

## Pre-demo checklist

**The demo needs the database and the web server. Nothing else.** No tunnel, no Razorpay, no model
— every external call already happened. This is the shortest possible surface to get wrong.

### Required

```powershell
cd D:\PayVra
docker compose up -d db
.venv\Scripts\alembic.exe -c api\alembic.ini current      # 0007 (head)
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --app-dir api
```

- [ ] Postgres up, migrations at `0007 (head)`
- [ ] uvicorn on :8000 — wait for `Application startup complete.`
- [ ] Browser at **http://localhost:8000/ui/**, signed in with
      `c2d79cf5-7f9f-fff3-e0b0-c708a30f6f20`
- [ ] **Scores populated.** An unscored worklist ranks arbitrarily and the "not an aging report"
      claim collapses on the first screen:
      ```powershell
      docker exec payvra-db psql -U payvra -d payvra -c "select count(*) from invoices where priority_score is null and outstanding_paise > 0;"
      ```
      Anything but a small number means run the rescore before going on.

### Environment — clear these, do not set them

| Variable | Demo value | Why |
|---|---|---|
| `LLM_ENABLED` | **unset or `false`** | No model call happens on stage. Leaving it on risks nothing but proves nothing either |
| `CONTACT_WINDOW_OVERRIDE_START` / `_END` | **unset** | The demo runs no batch. An override left set would widen a later run silently |

```powershell
$env:LLM_ENABLED=$null
$env:CONTACT_WINDOW_OVERRIDE_START=$null; $env:CONTACT_WINDOW_OVERRIDE_END=$null
```

The historical runs already recorded their own override, which stays visible as a `widened` pill —
that is by design and is the honest record, not something to clear.

### Not required, but do not break

- [ ] **Do not reseed.** It wipes ₹3,18,154 and ₹6,00,000
- [ ] **Do not run a batch.** Every clause is already evidenced by a stored run
- [ ] Link budget is **13 of 30 remaining** — the demo spends none. Any rehearsal that creates a
      link is spending real budget for no benefit
- [ ] Tunnel and Razorpay credentials are irrelevant to the five minutes. Only needed if you choose
      to take a live payment as an encore, which the script does not

### The audit log mixes seeded history with live runs

Of the 34 gate refusals, **30 are real** — 29 Aug, from the live runs. **Four are seeded history**:

| Date | Check illustrated |
|---|---|
| 31 Jul | `frequency_cap`, `time_window` |
| 13 Aug | `freshness` — *"invoice settled after planning and before dispatch; send aborted"* |
| 20 Aug | `stopping_rules` — three broken promises |

The seed builds a realistic book, and a realistic book has history. These are legitimate demo data.
**But they did not happen on a live run**, so never point at one while saying "watch what it just
did". If a judge asks about a July date, say plainly that it is seeded history and scroll to 29 Aug.

The `freshness` one is the most tempting to misuse — an invoice paid between planning and dispatch
is the product's best story, and it is the one entry here that was *not* produced live. Resist it.

### Evidence — confirm each on screen before recording

- [ ] **Clause 1** — Recovery, run `19:13 IST`: `₹3,18,154` causal headline, time-window beside it
- [ ] **Clause 2** — Audit log, same run, refusals: tiers 1, 2 **and** 3 present, with tier-3
      approval refusals
- [ ] **Clause 3** — refusals across at least three rule families (`merchant_excluded`, value
      threshold, tone tier)
- [ ] **Clause 4** — refusals and approvals in one list, chain column populated, hover shows the
      full hash pair
- [ ] **The ₹14L answer** — Recovery, run `20:38 IST`: `₹6,00,000`, `1 partially recovered`

### Fallback

- [ ] A recorded run-through, made under good conditions, kept ready. Browsers, servers and laptops
      fail at inconvenient moments and none of it says anything about whether the system works

---

## Rehearse these answers

They will be asked, and each has a documented answer. **Say them out loud in rehearsal** — Q0
especially, because it only works if it sounds unrehearsed.

Ordered by how much they matter rather than how likely they are: Q0 is the one that wins the room,
Q4 is the one most easily lost badly, and Q5 is the one where a bluff would undo everything else.

### Q0 — volunteer this one, do not wait to be asked

**Pointing at the gap between the two recovery figures:**

> *"These two numbers don't match, and I want to explain why before you ask.*
>
> *The larger one is everything that came in while the run was going. The smaller one — the one we
> lead with — is only what we actually chased.*
>
> *The difference is mostly this: there are invoices here where the gate refused to let us make
> contact. Frequency cap, outside contact hours, no consent on file. Some of those customers paid
> anyway. We could count that money. We don't, because we didn't earn it.*
>
> *The number is smaller because it's the one we can stand behind."*

That is clause 4 expressed as a number instead of a log, and it lands harder than the audit screen
because nobody expects a team to argue their own figure down. **Rehearse it until it is natural.**

### Q2 — "What about a ₹14 lakh invoice?"

You now have real evidence. **Switch the Recovery screen to the `20:38 IST` run while answering.**

> *"Good question, because Razorpay caps a single payment link at around ₹5 lakh — so a ₹14 lakh
> receivable can't just be sent as one link.*
>
> *We collect it in tranches. Here's that exact invoice. The agent created a link capped at the
> ceiling, and it's come in twice — one partial payment, then the rest. Six lakh recovered, eight
> lakh still open.*
>
> *And notice the invoice is still open. We report it as six lakh recovered anyway, because we
> count money received, not invoices closed. If we counted closed invoices this would read zero —
> which would be a lie in the flattering direction."*

**Do not hide the ceiling.** A real external constraint being handled is a stronger answer than
never being asked, and it is exactly why ADR-006 chose tranche collection over quietly curating the
demo data beneath the limit. Curated data would have been the option most likely to be *noticed*.

### Q3 — "Where is the AI, actually?"

The trap is answering with a model name. Answer with the boundary.

> *"It proposes. It doesn't decide, and it can't act.*
>
> *For each invoice, the model gets the facts and returns one structured object — an action from a
> closed list of nine tools, a tone tier, and a one-line rationale. That's the whole surface. It
> has no API access, it can't create a payment link, it can't send anything.*
>
> *Then deterministic Python takes over: is that tool on the list, is it legal from this invoice's
> current state, is the JSON well-formed. If any of those fail, the proposal is discarded and a
> rules-based policy decides instead — and the rejection is logged, so you can see the model being
> overruled.*
>
> *Everything after that — the ranking, the scoring, the gate, the reconciliation — has no model in
> it at all. That ratio is deliberate. An LLM call you can replace with an if-statement is a
> liability."*

If they want to see it: `agent/registry.py` is the closed list, `guardrails/gate.py` is the gate.
**But show the refusal list first** — the argument is stronger from the log than from the code.

If they ask whether it works without the model: yes, and it is tested that way. `LLM_ENABLED=false`
runs the entire pipeline on deterministic policy and hand-written templates. That is how CI runs.

### Q4 — "Your recovery number is smaller than theirs"

Expect this. **Do not get defensive and do not disparage the comparison** — argue the definition.

> *"It probably is, and I'd want to know how they're counting before I compare.*
>
> *Ours counts money received against invoices this specific run acted on and the gate approved.
> It excludes anything that arrived during the run that we didn't chase — and it excludes accounts
> where the gate refused to let us make contact and the customer paid anyway.*
>
> *We show the larger figure too. It's right there beside it. We just don't lead with it, because
> the moment someone asks 'how do you know your agent caused that?', the larger number has no
> answer and the smaller one does.*
>
> *A collections number you can't defend under one question isn't worth more than a smaller one you
> can."*

Then, if it's still live: *"and this is one run over twenty accounts, not a quarter's book."*

### Q5 — "Did the customer actually receive a message?"

Do not bluff this one.

> *"Not on this run — there's no email or SMS transport wired in yet, and the audit log says so.
> Look at the outcome column: it reads `approved`, never `executed`, for a message.*
>
> *What did happen is real: the agent created a live Razorpay payment link against that invoice,
> drafted the message, and the gate approved it. The money came in through that link and reconciled
> through a signed webhook.*
>
> *The log deliberately under-claims. It'll never tell you something was sent when it wasn't —
> that's the one thing an audit trail can't be wrong about."*

### Q6 — "You changed the contact hours?"

Only if they spot the `widened` pill. Volunteer it rather than being caught.

> *"Yes, on that run — we were rehearsing at eight in the evening and the gate refuses all outbound
> contact outside 08:00 to 19:00 IST.*
>
> *Two things about that. The gate still ran — all seven checks, on every action; the window is a
> value the check reads, not a rule it can skip. And the override itself is written into the audit
> log, so you can see it happened. That's the point: it's compliant by record, not by me telling
> you so."*

### Q1 — "How do you know your agent caused that recovery?"

That is what the two figures are for. The headline is causal — invoices this run acted on, and the
money received against them. Time-window sits beside it and is usually larger, because money
arrives for reasons that have nothing to do with us. Full divergence table in FR-17.

### Q7 — "Was the batch pre-computed?"

Yes. It is how any real system with rate limits behaves. The batch genuinely ran; it ran earlier.
Honesty here costs nothing and being caught costs everything.

---

## Have a recorded fallback

A recorded run of the full demo, made under good conditions, kept ready. Tunnel drops, provider
rate limits and dashboard sessions all fail at inconvenient moments, and none of them says anything
about whether the system works.
