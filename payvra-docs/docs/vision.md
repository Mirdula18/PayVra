# Vision

## The problem in one sentence

Indian B2B sellers know exactly what they are owed and have almost no systematic way to collect it.

## The numbers

| Fact | Source |
|---|---|
| ₹7.34 lakh crore locked in delayed MSME receivables (inflation-adjusted, as of March 2024) | GAME / FISME / C2FO, *Delayed Payments Report 3.0* |
| Indian SMEs take an average of **73 days** to pay invoices | Recordent, *Indian SME Receivables Report 2026* |
| Average SME carries ~₹3.83 crore unpaid beyond 360 days | Recordent, 2026 |
| 82.6% of invoices are issued on 0–30 day terms — so 73 days means chronic overrun | Recordent, 2026 |
| 52% of B2B payments in major Indian cities are overdue past 90 days | Recordent via IBS Intelligence |
| Indian mid-market DSO is 60–90 days vs ~47 days globally | PwC 2025 Working Capital Survey |
| A 20-day DSO cut on ₹150 cr revenue frees ~₹8.2 cr of working capital | Kapittx |

MSMEs affected: 6.4 crore. The delayed-payment pool represents over 4.6% of India's GVA.

## Why now

Razorpay launched **Agent Studio** at FTX'26 (Bengaluru, 11–12 March 2026), built on Anthropic's
Claude Agent SDK. Its production agents cover **Subscription Recovery, Abandoned Cart Conversion,
Dispute Responder, and Cashflow Forecaster**.

It does **not** ship a B2B receivables agent — even though Razorpay's own Track 3 brief lists
"B2B receivables chaser" and "Promise-to-pay tracker" as example directions.

That is the white space. Three of the four Track 3 leaks are already Razorpay products.
Building another subscription-retry bot means demoing something the judges' own company does
better. B2B receivables is the one direction where a team can build something genuinely new,
on rails Razorpay already owns.

## Why existing tools don't solve it

Kapittx, Growfin, CredFlow, Recordent, OptimAR, Global PayEX all do AR aging and dunning well.
They share one fatal shape: **they bolt onto an ERP and hand off the actual payment step.**

The sequence they support is: detect overdue → send reminder → *customer goes somewhere else to pay*
→ finance team manually reconciles.

PAYVRA's sequence is: detect → diagnose → decide → **generate the payment rail inside the message**
→ customer taps and pays → webhook reconciles automatically → outreach stops.

Nobody in this market *is* the payment rail. Razorpay is. That is our unfair advantage.

## Who we are for

**Primary — Priya, AR/Finance lead at a ₹50–150 cr B2B manufacturer or distributor.**
Runs collections from Tally plus spreadsheets plus WhatsApp. Chases 200–800 open invoices with
no prioritisation. Forgets follow-ups. DSO 70+ days. Measured on cash collected, blamed for
working capital gaps she can't close alone.

**Secondary — Rahul, founder of a bootstrapped B2B services or SaaS firm.**
No finance team. Personally sends "gentle reminder" emails. Finds chasing clients socially
awkward, so he under-chases, lets invoices slip 60–90 days, and takes working-capital loans to
bridge a gap he could have collected.

**Tertiary — Meena, AR executive at a mid-market firm on SAP or NetSuite.**
Has an ERP that records receivables but doesn't produce a daily prioritised worklist or automate
multi-channel dunning. Does cash application by hand.

## What we are NOT building

- A generic accounts-receivable dashboard
- A chatbot
- A credit bureau or risk-scoring bureau
- A debt-collection agency with human agents
- Anything that touches card data
- A lending product (invoice financing is a roadmap item, not MVP)

## The one-year picture

Ship as an agent inside Razorpay Agent Studio, filling the missing B2B-AR slot. Add native
Tally and Zoho connectors. Monetise as SaaS plus a success fee on recovered amounts — incentives
aligned, because we only win when the merchant gets paid. Long term, collectability scores become
underwriting signal for Razorpay Capital invoice financing.
