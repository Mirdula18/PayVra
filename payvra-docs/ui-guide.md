# PAYVRA — what everything on the screen means

Plain-language reference for every screen, label, badge and button.
Look up whatever is confusing you; each entry says **what it is, why it exists, and what you do about it.**

---

## 1. The one idea behind the whole product
    
PAYVRA chases unpaid B2B invoices for you. It repeats this loop:

| # | Step | In plain words |
|---|---|---|
| 1 | **Rank** | Which unpaid invoice is most worth chasing today? |
| 2 | **Diagnose** | Why hasn't this one been paid? |
| 3 | **Choose** | What is the single best thing to do about it? |
| 4 | **Check** | Is that thing allowed? (7 rules, always) |
| 5 | **Send** | If allowed, email the customer with a payment link. |
| 6 | **Reconcile** | When they pay, stop chasing them immediately. |
| 7 | **Record** | Write down every decision — including the ones it refused to make. |

**Almost every word in the UI is a label for one of these seven steps.** If a term confuses you, ask
"which step is this about?" and it usually becomes obvious.

> **The most important idea:** PAYVRA is judged on the money it *refuses* to chase as much as the
> money it collects. That's why refusals are shown everywhere instead of hidden.

---

## 2. The screens

| Screen | The question it answers | Go here when |
|---|---|---|
| **Overview** | How is the whole book doing right now? | You just logged in |
| **Revenue at Risk** | Which invoices are unpaid, and which matter most? | You want to find or search an invoice |
| **Recovery Queue** | What needs *my* attention today? | You're doing your daily work |
| **Batches** | What happened each time the agent ran? | You want to check a specific run |
| **Analytics** | Is this actually working? | You're reporting to someone |
| **Audit Log** | What exactly did it do, and why? | Something looks wrong, or you're proving compliance |
| **Policy** | What are the rules it follows? | You want to know what it's allowed to do |

---

## 3. The top bar (the part you asked about)

### Left side

**The client name** — e.g. *Nandi Industrial Supplies Pvt Ltd*. This is **whose invoices you are
currently looking at.** PAYVRA can hold several separate businesses ("clients"); nothing ever
crosses between them. To switch, use the dropdown at the **bottom of the left sidebar**.

### Right side — the Run Recovery controls

These three sit together. **This is how you make the agent do a round of work.**

| Field | What it is | What to put |
|---|---|---|
| **The number box** | How many invoices the agent may work on in this run | Start with **5**. Max 25 |
| **Send for real** | A safety switch. **Off = practice. On = real emails go out.** | Leave it **OFF** unless you mean it |
| **Run recovery** | The button that starts the run | Click when the two above are set |

**What the number actually means.** The agent takes the top *N* invoices from Revenue at Risk —
the most worth chasing — and works only those. It's a cost and safety limit, not a target. Type
5, and it considers exactly 5 accounts.

**What "Send for real" actually does.**

| | Off (default) | On |
|---|---|---|
| Ranks, diagnoses, decides | ✅ Yes | ✅ Yes |
| Runs all 7 rule checks | ✅ Yes | ✅ Yes |
| Writes the full audit trail | ✅ Yes | ✅ Yes |
| **Creates a real payment link** | ❌ No | ✅ Yes |
| **Emails a real customer** | ❌ No | ✅ Yes |
| Can recover money | ❌ No | ✅ Yes |

With it **off**, you see exactly what the agent *would* do, safely. This is called a **Rehearsal**.
Nothing reaches a customer, and nothing can be undone by mistake.

> ⚠️ **Turning it on sends real email to real people and creates real payment links.** There is no
> undo. Only tick it when you genuinely want customers contacted.

**When should you click Run recovery?** Once a day is plenty. The agent won't double-contact anyone
— the frequency cap (§8) stops that — so an extra run is safe, just usually pointless.

---

## 4. Money words

| Term | Plain meaning |
|---|---|
| **Revenue at risk** | Money customers owe you and haven't paid. The problem. |
| **Potential recovery** | What you could still get back from one invoice. |
| **Recovered** | Money that has actually arrived and been confirmed by the bank/Razorpay. Not a guess. |
| **Recovery rate** | Of the money in play, what share came back. |
| **Outstanding** | What's still owed on an invoice right now. Falls as they pay. |
| **Partial** | They paid some but not all. Common on large invoices. |
| **Paid in full** | Nothing left owing. Chasing stops. |

### Two recovery numbers on the Batches and Recovery screens

You'll see **Causal** and **Time-window**. They're different questions:

- **Causal** *(the headline)* — money that came in from invoices **this run actually chased**.
- **Time-window** *(context)* — everything that arrived **while the run was happening**, for any reason.

**We lead with Causal because it's the one we can defend.** If a customer paid on their own while
the agent happened to be running, that's not our win. Time-window counts it; Causal doesn't.

> **Don't add the "Recovered" column down the page.** Each batch reports money from the invoices it
> touched, and two batches can have chased the same invoice. Adding them counts the same rupee
> twice. That's why there's no grand total.

---

## 5. Stage — where an invoice is in the chase

This is the **Stage** column, and the Recovery Queue tabs.

| Badge | What it means | Do you need to act? |
|---|---|---|
| **Queued** *(not started)* | Known about, never chased yet | No — the agent will get to it |
| **Recovering** *(nudged / chasing)* | The agent has contacted them and is working it | **No.** This is the healthy state |
| **Promised** | They replied saying they'll pay by a date | No — waiting on that date |
| **Broke promise** | That date passed with no payment | **Yes.** Trust is gone; decide how hard to push |
| **Escalated** | Pushed as far as the agent may go alone | **Yes.** Usually a phone call or legal step |
| **Needs review** | The agent stopped and is **waiting for your permission** | **Yes — this is your main job.** See §6 |
| **Stopped** | Chasing has ended permanently, by rule | Only if you disagree with the reason |
| **Recovered** *(settled)* | Paid. Done. | No |

**"Recovering" is the one people misread.** It doesn't mean money is coming back — it means *the
agent is currently working this account.* Nothing for you to do.

---

## 6. "Needs review" — your actual daily job

The agent stops and asks you when it hits either of these:

| Trigger | Threshold on this book |
|---|---|
| The invoice is large | Over **₹5,00,000** |
| The message would be the firmest tone | **Tier 3** |

**The rule behind it:** the agent may be *gentler* on its own, but must ask permission to be
*firmer*. It can never escalate to a hard demand without a human saying yes.

Open **Recovery Queue → Needs review** and work the list.

---

## 7. Root cause — why the invoice is unpaid

This is the **Root cause** column. The agent infers it from the invoice's history and any replies.
**It changes the strategy** — you don't chase a disputed invoice the way you chase a forgotten one.

| Label | What it means | What the agent does |
|---|---|---|
| **Overlooked** | They simply forgot. Most common, easiest to fix | One gentle reminder + payment link |
| **Cash crunch** | They want to pay but money is tight | Keeps the tone low and makes paying easy |
| **Disputed** | They disagree with the invoice | **Freezes all outreach and routes to a human.** Chasing a dispute makes it worse |
| **Wrong contact** | Replies suggest we're emailing the wrong person | Marks the contact stale and tries again |
| **Awaiting docs** | They're waiting on paperwork before they can pay | Sends the invoice reference they need |
| **Refusing to pay** | They've said outright they won't | **Permanent stop.** Onto the exception list, never contacted again |
| **Undiagnosed** | Not enough information to tell yet | Treats it as a plain reminder |

> **Disputed and Refusing to pay both end the chase, but differently.** Disputed **pauses** and asks
> a person to sort it out — it can resume. Refusing to pay is a **permanent stop**; that customer is
> never contacted again by the agent.

**"Undiagnosed" is not an error.** It means the agent is being honest that it doesn't know rather
than guessing.

---

## 8. The seven rules (the "gate")

Every single outbound action is checked against all seven, every time. **There is no override
switch and no skip flag.** If one fails, nothing is sent — and the refusal is recorded.

| Rule | Blocks a send when | Setting on this book |
|---|---|---|
| **Contact hours** | It's outside business hours in India | **08:00–19:00 IST** |
| **Freshness** | They paid in the last few seconds while we were preparing | — |
| **Consent** | No permission on file, or they opted out | — |
| **Frequency cap** | You've contacted them too often already | **2 per week, 6 lifetime** |
| **Value ceiling** | The invoice is too big to act on alone | **Over ₹5,00,000** |
| **Content policy** | The draft message is missing something required | — |
| **Stopping rules** | Chasing should have stopped already | See §9 |

Seeing lots of refusals is **normal and good**. It means the rules are working. Check **Policy** to
see which rules have fired and how often.

---

## 9. Stop reasons — why chasing ended for good

Shown under a **Stopped** badge.

| Reason | Plain meaning | Reversible? |
|---|---|---|
| **Settled** | They paid. Nothing left to chase | n/a — this is success |
| **Disputed** | They formally disagree with the invoice | Yes, once you resolve the dispute |
| **Opted out** | They clicked "stop emailing me", **or told us outright they won't pay** | **No.** Legally binding |
| **Broken promises exceeded** | They promised to pay and didn't, too many times | Yes, by a human decision |
| **Touch cap reached** | Contacted the maximum allowed times | Yes, next period |
| **No consent** | No permission to contact them at all | Yes, once consent is recorded |
| **Merchant excluded** | *You* put them on a do-not-contact list | Yes — you control this |
| **Written off** | You've given up on the money | Yes |

**"Broken promises exceeded"** is the one people ask about. It means: *this customer said "I'll pay
by Friday" and didn't — repeatedly. The agent has stopped believing them and stopped chasing.*
That's a stopping rule doing its job, not a failure.

---

## 10. Tone tiers — how firm the message is

| Tier | Tone | Who approves |
|---|---|---|
| **T1** | Polite reminder | Agent, alone |
| **T2** | Firm follow-up | Agent, alone |
| **T3** | Final notice | **You must approve** |

Tone goes **up** with each attempt. It also goes **down** on its own if they make a part-payment —
someone who just paid you something gets a gentler next message, not a harder one.

---

## 11. Risk score, and "Not yet scored"

**Risk score** ranks which invoice is most worth chasing:

```
score  =  chance they'll pay  ×  amount owed  ×  how urgent it is
```

This is why **the list is not sorted by age.** A ₹8 lakh invoice ten days late from a reliable
customer beats a ₹90,000 invoice four months late from someone who never pays. The **Why this rank**
column explains each row in one sentence.

**"Not yet scored" / "—"** means the ranking hasn't been calculated for that invoice yet. It's not
broken; it just hasn't been through a scoring pass. It will sort to the bottom until it has.

---

## 12. The Batches screen

Each card = **one time the agent ran.**

| Thing you see | What it is |
|---|---|
| **The date and time** | When that run happened (always IST) |
| **Rehearsal** badge | "Send for real" was **off** — a practice run. Nobody was contacted |
| **Live** badge | Real emails went out |
| **The long ID** | A unique reference for that run. Only needed for support or an audit |
| **Window widened** | The contact-hours rule was temporarily changed for this run — and that change is itself recorded |
| **Accounts** | How many invoices it looked at |
| **Executed / Sent** | How many messages actually went out |
| **Approved** | Passed the rules, but the send didn't complete |
| **Refused** | Blocked by one of the seven rules |
| **Recovered** | Money that came back from the invoices this run chased |

A batch with **0 recovered is not a failed batch.** Runs finish in seconds; people pay days later.
The number grows over time.

---

## 13. The account page (click any invoice number)

### AI Diagnosis
Five steps showing the agent's reasoning: **Signal detected → Root cause → Intervention selected →
Policy check → Outcome.** Read top to bottom to see how it thought.

### Recovery Workflow
Everything the agent did to this one invoice.

| Thing you see | What it means |
|---|---|
| **"0 actions"** | The agent has never worked this invoice |
| **"Newest first"** | The most recent event is at the top. Read downward to go back in time |
| **"Not yet worked"** | Same as 0 actions — it hasn't come up in a run yet |
| **"This invoice has not come up in a batch"** | It exists and is ranked, but no run has reached it. **Run recovery from Overview**, or increase the number so it goes deeper down the list |
| **"Read message ▾"** | Click to see the **exact email that was sent** — subject, body, payment link and all |

**Why an invoice might not have been worked yet:** each run only takes the top few. If you run 5
accounts and the invoice is ranked 30th, it won't be touched. Raise the number or run again.

---

## 14. Audit Log words

| Word | Meaning |
|---|---|
| **Sent / executed** | The message genuinely left the building, confirmed by the email provider |
| **Approved** | The rules said yes, but the send did not complete |
| **Refused by gate** | One of the seven rules blocked it |
| **Refused in run** | The agent itself decided not to act |
| **Chain** | Each entry is sealed with a code based on the entry before it |
| **Chain intact** | Nothing has been altered |

**Why "Approved" and "Sent" are different:** the log will never claim a message went out when it
didn't. It would rather under-report than over-report — that's the whole point of an audit trail.

**The Chain, simply:** every entry is locked to the one before it. Change any old row and every row
after it visibly breaks. So the history can't be quietly edited — it's *tamper-evident*.

---

## 15. Quick answers

| You see | Do this |
|---|---|
| A number in the sidebar next to **Recovery Queue** | That many accounts need you. Go work them |
| **Needs review** | Approve or reject — the agent is blocked until you do |
| **Broke promise** | Decide whether to escalate |
| **Stopped** | Read the reason. Act only if you disagree |
| **Recovering** | Nothing. It's working |
| **Refused by gate** | Nothing. A rule did its job |
| **0 actions** on an invoice | Run recovery, or raise the account number |
| **₹0 recovered** on a recent batch | Wait. People pay days later |

---

## 16. Words this product deliberately avoids

| We don't say | Because |
|---|---|
| "Delivered" | We only know the provider accepted it, not that it reached the inbox |
| "Total recovered" across batches | It would double-count |
| "Sent" for an approved-but-unsent message | The log must never over-claim |

If a number looks smaller than you expected, that is usually on purpose.
**PAYVRA reports what it can prove.**
