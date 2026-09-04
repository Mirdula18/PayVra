# Runbook: the Phase 9 demo rehearsal

**Read this before the first rehearsal, not before the last one.** Three constraints below can turn
a working system into a zero on stage — or into a confident recitation of something untrue — and
all three are invisible until you hit them.

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

## 🔴 Constraint 3 — time-window reads ₹0 on every run, and the screen says it shouldn't

**Found 2 Sep, before the stopwatch rehearsal. This changes what you say at 2:40.**

Every run in the database reports a time-window figure of **₹0**:

| Run | Causal | Time-window |
|---|---|---|
| `c8e62a04` | ₹3,18,154 | **₹0** |
| `14060ac6` | ₹6,00,000 | **₹0** |
| `b4e43e2d` | ₹6,00,000 | **₹0** |
| every other run | ₹0 | ₹0 |

This is the metric working correctly. Time-window closes at `finished_at`, and these runs finished
in between 0.6 and 5 seconds. Nobody pays an invoice inside five seconds, so nothing lands in the
window. A time-window figure is largely a measure of *how long the batch ran* — which is the
argument for not leading with it, and it is now an argument the data makes for you rather than one
you assert.

**But the script used to say the opposite.** It described time-window as the larger figure and
causal as "the smaller one we can stand behind". On screen the headline is ₹3,18,154 and the
context figure is ₹0 — the headline is *larger*. Reciting the old line in front of that screen is a
factual error about your own product, made while claiming rigour. The script's 2:40 beat and Q0 are
rewritten; **use the new wording**.

> ✅ **The on-screen copy said the opposite too. Fixed 2 Sep.** `recovery.html` used to end its
> divergence note with *"The headline is smaller because it is the one that can be stood behind"*
> — which, with causal ₹3,18,154 against time-window ₹0, contradicted the table directly above it.
>
> The replacement paragraph asserts no direction at all, because neither figure bounds the other:
> time-window runs ahead when unrelated money lands mid-run, causal runs ahead when a run finishes
> in seconds and the payments it caused arrive later. Causal is the headline either way, on the
> grounds that it is the only one that can name the invoices it counted. **The camera can rest on
> that paragraph now** — it says the same thing you do at 2:40.

**Do not sum run figures.** Causal is unbounded above, so `b4e43e2d` (19:07) and `14060ac6` (20:38)
*both* report the same ₹6,00,000 — both acted on `INV-2026-1079` with the gate's approval, and the
tranche money arrived after both. That is correct per-run attribution and it is meaningless when
added up. Never show two runs and total them.

---

## Known state — work around it, do not fix it before the demo

Recorded so nothing here reads as a surprise on stage.

| State | Why it stays |
|---|---|
| **9 invoices `stopped`, 8 with a null reason** | Pre-fix residue. A reseed clears them **and wipes the ₹3,18,154 and ₹6,00,000 figures with it.** Do not reseed before demoing. The Worklist shows these as `stopped` with no reason text — they are not on any screen the script visits |
| **`INV-2026-1066` is paid on Razorpay and `unpaid` here** | ₹3,77,772, paid while uvicorn and cloudflared were both down, so the `payment_link.paid` webhook had nowhere to land. **Leave it that way** — see the decision below |
| **The opt-out URL in the delivered email is dead** | `PUBLIC_BASE_URL` is baked into the body at send time and pointed at a cloudflared quick tunnel, which gets a new address on every restart. The link is present, which is the compliance claim; it is not reachable. Do not click it on stage |
| **Four `GateTest` merchants with `re_abc123` message rows** | Test-suite residue in the dev database from before the `_no_real_email` fixture landed. Separate tenants — invisible to every demo screen, which all filter by `merchant_id`. Cosmetic; clean up after submission |
| **Phase 7 parked** | Reply handling and promise tracking are unbuilt. No clause depends on them |
| Contact-window override was used on three of the four live runs | Recorded in the audit log for each, and visible on screen as a `widened` pill. Compliant by record — see Q6 |
| **`gate.*` audit entries are not run-scoped** | Found during Phase 9 rehearsal. See below — it changes how the audit screen is demoed |
| **Time-window is ₹0 everywhere** | Constraint 3 above. Not a defect; the script now explains it |

### Decision: `INV-2026-1066` stays unpaid. Do not run `mark_paid_offline`.

The invoice is genuinely paid on Razorpay's side and genuinely `unpaid` here, and
`mark_paid_offline` would reconcile it through the identical settle path the webhook uses. It is a
legitimate operation. **It is also the single fastest way to invalidate every number in the
script.**

Causal has no upper time bound, and `c8e62a04` acted on `INV-2026-1066` with the gate's approval
(`gated_pass`, `gate_failure_reason` null — so it is in that run's touched set). A settle recorded
today therefore flows straight into the run you are demoing:

| Figure | Now | After `mark_paid_offline` |
|---|---|---|
| `c8e62a04` causal — **the headline** | **₹3,18,154** | **₹6,95,926** |
| `c8e62a04` time-window | ₹0 | ₹0 — the window closed 29 Aug 19:13:09 |
| `c8e62a04` paid in full | 1 | 2 |
| `1aee5af0` causal (the delivery run) | ₹0 | ₹3,77,772 |
| Worklist total | ₹2,01,48,694 | ≈₹1,97,70,922 |
| Worklist rows 4, 7 and 11 | unchanged | unchanged — `INV-2026-1066` is row 18 |

Three reasons to leave it:

1. **₹3,18,154 is spoken aloud in the script, printed in this runbook, and is the figure you said
   you want to demo.** Changing it means re-reading and re-recording every beat that names it.
2. **The added money would carry `source=manual, actor=human` in the audit trail.** On a clause-1
   claim, the one row a judge zooms in on is the one where a person attested to a payment instead
   of a signed webhook. It is defensible, but it is defending rather than demonstrating, and it
   spends the credibility that the ₹3,18,154 row earns for free.
3. **It buys nothing.** The delivery claim is already evidenced by run `1aee5af0` — executed
   action, `messages` row, Resend message ID, `touch_count` 2. Settlement is already evidenced
   three times over by real webhooks.

If you want it reconciled after the submission, the payment is not lost — Razorpay retains the
event and the invoice is still open locally. It is a post-demo cleanup, not a pre-demo task.

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

**Re-verified 2 Sep after Phase 6.5.** Everything below is already in the database; the demo
creates nothing.

```
merchant     Nandi Industrial Supplies Pvt Ltd
             c2d79cf5-7f9f-fff3-e0b0-c708a30f6f20
invoices     120  (115 still owing; ₹3.06 crore on the book)
worklist     ₹2,01,48,694 across the top 40 rows — this is the "₹2.01 crore" the script says
audit_log    390 entries for this merchant, hash chain verified
             non-scoring outcomes: 96 refused, 65 approved, 59 blocked, 44 executed, 6 applied, 4 stopped
links        4 real Razorpay links — 1 paid, 1 partially paid, 2 created
budget       4 of LINK_BUDGET 25 consumed locally — 21 remaining
messages     1 real delivered email (2 Sep, INV-2026-1066, Resend id 76b4febf…)
runs         13 for this merchant; the run picker shows 12
```

> The worklist total is the sum of the **40 rows on screen**, not the whole book. ₹2.01 crore on
> screen and ₹3.06 crore in the database are both correct and are different quantities. Do not
> quote the ₹3.06 crore — it is not the number a judge can see.

> **Link budget has two counters and they disagree.** `LINK_BUDGET = 25` in
> `api/app/razorpay/links.py` is checked against `payment_links` rows for this merchant — 4 used,
> 21 left. Razorpay's own test-mode cap is 30 per business and counts every link ever created
> against the account, including ones a reseed deleted locally. **The Razorpay-side count is the
> real ceiling and only the dashboard knows it.** The earlier "17 of 30" in this runbook was that
> tally, not the local one. Check the dashboard before assuming you have 21.

**Live runs available:**

| Run | IST | Accounts | Executed | Refused | Causal | Time-window |
|---|---|---|---|---|---|---|
| `c8e62a04` | 29 Aug **19:13** | 20 | 5 | **12** | **₹3,18,154** ← lead with this | ₹0 |
| `14060ac6` | 29 Aug **20:38** | 1 | 0 (1 approved) | 0 | ₹6,00,000 (tranche) ← the ₹14L answer | ₹0 |
| `b4e43e2d` | 29 Aug 19:07 | 20 | 11 | 9 | ₹6,00,000 — *the same ₹6L as above* | ₹0 |
| `bb6670f4` | 29 Aug 20:28 | 10 | 2 | 8 | ₹0 | ₹0 |
| `1aee5af0` | 2 Sep **20:57** | 12 | 4 | 8 | ₹0 ← **the delivery run** | ₹0 |
| `7c67f9c9` | 2 Sep 20:55 | 12 | 3 | 9 | ₹0 | ₹0 |

`b4e43e2d` was recorded here as ₹0 and is not — causal is unbounded above, so it picked up the
tranche money retroactively when `INV-2026-1079` settled. See Constraint 3: **do not add run
figures together.**

`1aee5af0` recovers nothing and is still the run that proves clause 4's hardest sentence. It is
where the email came from.

The reasoning for the ordering is in `prompts/demo-script.md`.

> ⚠️ **`c8e62a04` is the twelfth row in a twelve-row run picker.** `_recent_runs()` returns 12,
> ordered by `started_at` descending, and the ₹3,18,154 run is now last. **One more batch run of
> any kind — including a dry run — pushes it off the list**, and the Recovery screen offers no way
> to reach it but a hand-typed `?run=` URL. This is the sharpest single reason not to run a batch
> before recording.
>
> Keep the direct URL to hand either way:
> `http://localhost:8000/ui/recovery?run=c8e62a04-8cb9-4695-8c56-b377f4d3d03c`

## Pre-demo checklist

**The demo as scripted needs the database and the web server. Nothing else.** No tunnel, no
Razorpay, no model, no Resend — every external call already happened. That is the shortest possible
surface to get wrong, and every item below is about keeping it that short.

### A. Required — the demo as scripted

**One command.** Postgres, the migrations and the API are all containers:

```powershell
cd D:\PayVra
docker compose up -d --build --wait
```

`--wait` returns only once `payvra-api` reports healthy, and that healthcheck fetches
`/ui/login` — a real request through Postgres. A clean exit means the whole path is up, not that
a port is open. Roughly 45 seconds cold, a few seconds warm.

- [ ] `docker compose ps` shows `payvra-db` and `payvra-api` both `healthy`
- [ ] `docker inspect payvra-migrate --format '{{.State.ExitCode}}'` is `0`

<details>
<summary>Host <code>.venv</code> alternative, if Docker is unavailable</summary>

```powershell
docker compose up -d db
.venv\Scripts\alembic.exe -c api\alembic.ini current      # 0007 (head)
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --app-dir api
```

Wait for `Application startup complete.` Both paths want port 8000, so run one or the other.
</details>

> ⚠️ **Never `docker compose down -v`.** `down` alone stops the containers and keeps the volume;
> `-v` deletes `payvra_pgdata` and with it the ₹3,18,154 run, the ₹6,00,000 tranche, four real
> Razorpay links, the delivered email and a 390-entry hash chain. A reseed does not restore them
> — it builds a *different* book, and this one's money was collected against links that already
> exist at Razorpay. Verified: a full `down` then `up` cycle leaves all of it intact.
- [ ] Browser at **http://localhost:8000/ui/**, signed in with
      `c2d79cf5-7f9f-fff3-e0b0-c708a30f6f20`
- [ ] **Scores populated.** An unscored worklist ranks arbitrarily and the "not an aging report"
      claim collapses on the first screen:
      ```powershell
      docker exec payvra-db psql -U payvra -d payvra -c "select count(*) from invoices where priority_score is null and outstanding_paise > 0;"
      ```
      Anything but a small number means run the rescore before going on.
- [ ] **`c8e62a04` is still in the run picker.** Open the Recovery screen and check the dropdown.
      It is the twelfth and last row. If it is missing, someone ran a batch — use the direct URL.
- [ ] **The email tab is already open**, on the message received 2 Sep, scrolled so the body is
      visible. Opened before recording starts, never during.

### B. 🔴 Before anyone touches a payment link — the API *and* cloudflared, in that order

**The script has nobody clicking a link. If that changes, this section is not optional.** A paid
link with no webhook receiver is money that moved in the world and did not move in the database,
and there is no undo — `INV-2026-1066` is exactly that, and it happened because both processes
were down when the payment went through.

Sequence matters. The tunnel must point at a server that is already listening:

```powershell
# 1. the API first — the tunnel needs something to forward to
docker compose up -d --build --wait

# 2. tunnel second, in its own terminal. The URL is new every restart.
cloudflared tunnel --url http://localhost:8000
```

cloudflared stays on the host: it forwards to the published port 8000, so it does not care
whether the thing behind it is a container or a host process.

- [ ] `docker compose ps` shows `payvra-api` **healthy** (or uvicorn said `Application startup
      complete.` on the host path)
- [ ] cloudflared printed a `https://<something>.trycloudflare.com` URL — **copy it**
- [ ] That URL is set as the Razorpay webhook endpoint **in the Razorpay dashboard**, at
      `<url>/webhooks/razorpay`. A stale endpoint from a previous tunnel is the failure mode; it
      does not error, the webhook simply never arrives
- [ ] `PUBLIC_BASE_URL` in `.env` matches the same tunnel — it is currently a **dead** URL
      (`sustainability-shelter-executives-rpg…`). Any message sent while it is stale ships an
      opt-out link that 404s, which is a compliance claim that does not survive being clicked.
      **Editing `.env` is not enough on the Docker path** — compose injects the environment when
      the container starts, so run `docker compose up -d api` afterwards to pick it up
- [ ] Webhook path confirmed reachable end to end **before** any link is opened, not after

**If you cannot get all four green, do not open a payment link.** Showing a stored, already-paid,
already-reconciled link costs nothing and proves the same thing.

### C. Delivery state — Resend, and where mail would go

The demo sends nothing. These checks exist so that an accidental run cannot email a stranger.

```powershell
Select-String -Path .env -Pattern '^(RESEND_FROM|RESEND_TO_OVERRIDE|RESEND_API_KEY)='
```

- [ ] `RESEND_FROM=onboarding@resend.dev` — Resend's shared sender, no domain to verify
- [ ] `RESEND_TO_OVERRIDE` is **your own address**, spelled correctly, and is the address the
      Resend account is registered under. A typo here does not fail loudly; Resend returns a 403
      naming the address it expected, which is how the last one was caught
- [ ] You understand which way the override fails: **empty disables sending entirely**, it does not
      unlock real recipients. `is_configured()` requires both a key and an override
- [ ] `RESEND_API_KEY` is a send-only key. It should not be able to read anything

**No counterparty in the seed can receive mail even if all of that is wrong** — `recipient_for()`
ignores the contact address by construction and returns the override. The checks above are the
second lock, not the first.

### D. Contact repoint — reverted, and verifiable

The Phase 6.5 delivery test repointed one seeded contact at a real inbox. **It has been reverted.**
The risk is not delivery — the override handles that — it is a real personal email address being
visible on camera.

```powershell
docker exec payvra-db psql -U payvra -d payvra -c "select c.name, ct.email from contacts ct join counterparties c on c.id=ct.counterparty_id where ct.email not like '%example%' and ct.email not like '%invalid-mx%';"
```

- [ ] **Zero rows.** Anything returned is a real address in the seed — repoint it back before
      recording
- [ ] Expected known exceptions, which are deliberate and stay: `maple.retail.ventures.0@` and
      `sterling.components.0@invalid-mx.co.in` (a domain with no MX record, on purpose)
- [ ] `INV-2026-1066`'s primary contact reads `Ramesh Iyer / highland.ceramics.0@example.co.in`

### E. Link budget — know the number before you need it

- [ ] Local: **4 of 25 consumed, 21 remaining.**
      ```powershell
      docker exec payvra-db psql -U payvra -d payvra -c "select count(*) from payment_links pl join invoices i on i.id=pl.invoice_id where i.merchant_id='c2d79cf5-7f9f-fff3-e0b0-c708a30f6f20';"
      ```
- [ ] Razorpay-side: **check the dashboard.** The test-mode cap is 30 per business and counts links
      a local reseed deleted. This is the number that actually stops you
- [ ] **The demo spends zero.** Any link created during rehearsal is real budget bought for no
      benefit, and `create_link` already reuses an existing link for the same invoice and purpose —
      so a re-run of a rehearsed action costs nothing, but a *new* invoice costs one

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
- [ ] **Do not run a batch.** Every clause is already evidenced by a stored run. A batch now costs
      three things it did not before: it can push `c8e62a04` out of the twelve-row run picker, it
      spends link budget, and **it sends real email**
- [ ] Link budget: see section E. The demo spends none
- [ ] Tunnel and Razorpay credentials are irrelevant to the five minutes as scripted. They become
      mandatory the moment anyone opens a payment link — see section B

### The audit log mixes seeded history with live runs

Of the 59 gate refusals, **55 are real** — 30 from the 29 Aug runs, 25 from the 2 Sep delivery
runs. **Four are seeded history**:

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

- [ ] **Clause 1** — Recovery, run `19:13 IST`: `₹3,18,154` causal headline, time-window `₹0`
      beside it, **and you can say why the ₹0 is there without reading it off a page**
- [ ] **Clause 2** — Audit log, same run, refusals: tiers 1, 2 **and** 3 present, with tier-3
      approval refusals
- [ ] **Clause 3** — refusals across at least three rule families (`merchant_excluded`, value
      threshold, tone tier)
- [ ] **Clause 4** — refusals and approvals in one list, chain column populated, hover shows the
      full hash pair
- [ ] **Clause 4, the send** — the 2 Sep email open in its own tab, and the matching `executed`
      row in the audit log. One is a message; the two together are a record
- [ ] **The ₹14L answer** — Recovery, run `20:38 IST`: `₹6,00,000`, `1 partially recovered`

### Fallback

- [ ] A recorded run-through, made under good conditions, kept ready. Browsers, servers and laptops
      fail at inconvenient moments and none of it says anything about whether the system works

---

## Rehearse these answers

They will be asked, and each has a documented answer. **Say them out loud in rehearsal** — Q0
especially, because it only works if it sounds unrehearsed.

Ordered by how much they matter rather than how likely they are: Q0 is the one that wins the room,
Q4 is the one most easily lost badly, and Q5 — which used to be the one where a bluff would undo
everything else — is now answerable with evidence. Q8 is the new one to not get caught by.

### Q0 — volunteer this one, do not wait to be asked

**Rewritten 2 Sep. The old version described time-window as the larger figure; it is ₹0. See
Constraint 3 — reciting the old wording in front of the screen is a factual error about your own
product, delivered in the middle of a rigour claim.**

**Pointing at the ₹0 sitting under the headline:**

> *"That second number is zero, and I want to explain it before you ask.*
>
> *It's the naive way to measure a collections agent — total everything that came in while the run
> was going. This run finished in five seconds. Nobody pays an invoice in five seconds, so it counts
> nothing.*
>
> *Which is exactly the problem with it. That number is really a measure of how long your batch
> ran. Leave it going overnight and it looks enormous, and none of that extra money is yours.*
>
> *The one we lead with names the invoices we actually acted on. And it still leaves money out —
> there are accounts here where the gate refused to let us make contact and the customer paid
> anyway. We could count that. We don't, because we didn't earn it.*
>
> *It's the smaller claim. It's the one that survives the question."*

That is clause 4 expressed as a number instead of a log, and it lands harder than the audit screen
because nobody expects a team to argue their own figure down. **Rehearse it until it is natural.**

The ₹0 makes this beat *stronger*, not weaker: you are not explaining a gap between two numbers you
chose, you are explaining why the industry-standard number is useless and yours isn't. But it only
works if you get there first. A judge who notices a ₹0 before you mention it has found a hole, and
nothing you say afterwards un-finds it.

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

**Rewritten 2 Sep. This used to be a concession. It is now the answer with the strongest evidence
behind it, so do not deliver it apologetically out of habit.**

Show the email tab.

> *"Yes. This one went out on the second of September — a real send through a real provider, and
> the provider's message ID is written back into the log next to the action.*
>
> *That's what `executed` means in this system, and it's the only thing it means. If the send
> fails, the action stays `approved` and nothing is claimed — we don't mark something sent because
> we asked for it to be sent. And the contact counter only moves on a confirmed delivery, because
> that counter is what enforces the frequency cap. Inflating it with messages nobody received would
> suppress real outreach later on the strength of a fiction."*

**Then volunteer the recipient, before they read the `To:` line:**

> *"One thing you'll notice — every outbound email is redirected to a single test inbox. The
> counterparties in this book are seeded, their addresses are on a reserved domain, and I'm not
> emailing strangers to rehearse a demo. The redirect is in the transport itself, not a flag: it
> ignores the stored address by construction, so there is no configuration that sends this to a
> real counterparty."*

If pressed on scale: email is the only channel implemented. SMS and WhatsApp are refused
explicitly by the sender rather than silently dropped, which is why the log has never claimed one.

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

### Q8 — "Does that opt-out link work?"

Only if someone clicks it, which is why the script says not to. The honest answer, immediately:

> *"Not right now, and that's an environment thing rather than a product thing. The opt-out URL is
> written into the message body at send time from a public base URL, and for local development
> that's a temporary tunnel that gets a new address every time it restarts. This message was sent
> under a tunnel that's since gone.*
>
> *The link is generated per recipient, it's unique, and the route that serves it is in the app. In
> a deployment with a real domain it resolves. Here it points at an address that no longer exists."*

**Do not claim it works.** It takes one click to disprove, and it is the cheapest possible thing to
be caught on — the whole pitch rests on the log not over-claiming.

### Q7 — "Was the batch pre-computed?"

Yes. It is how any real system with rate limits behaves. The batch genuinely ran; it ran earlier.
Honesty here costs nothing and being caught costs everything.

---

## Have a recorded fallback

A recorded run of the full demo, made under good conditions, kept ready. Tunnel drops, provider
rate limits and dashboard sessions all fail at inconvenient moments, and none of them says anything
about whether the system works.
