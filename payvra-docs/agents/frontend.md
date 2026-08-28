# Agent: Frontend

**Scope:** `web/src/`
**Prerequisites:** `/CLAUDE.md`, `architecture/api-contracts.md`, `requirements/user-stories.md`

Judges spend most of their time on two screens: **Dashboard** and **Audit Log**. Budget accordingly.

---

## Stack

React 18 + Vite + TypeScript, Tailwind, shadcn/ui, TanStack Query, Recharts, React Router.

Generate types from the OpenAPI schema FastAPI produces. Do not hand-write API types —
they drift, and the drift shows up during a demo.

---

## Phase 8 submission scope — three screens

**Cut to the minimum that evidences `requirements/track3-bar.md`.** Everything else in this
document is POST-SUBMISSION. A screen that does not carry a clause does not ship first.

**Server-rendered is fine.** The React stack below stays specified and stays the target, but the
bar asks for evidence, not for a single-page app. If server-rendered templates get the three
screens done sooner and more reliably, build those — a judge is reading numbers off a page, and
nothing in the bar rewards client-side routing. Decide on time remaining, not on preference.

| # | Screen | Evidences | Must show |
|---|---|---|---|
| 1 | **Ranked worklist** | context for 1, 2 | Invoices ordered by priority with the plain-English reason per row. This is what makes it not an aging report. |
| 2 | **Recovered figure** | clause 1 | ₹ recovered and invoice count for one `recovery_run_id`. **Both figures, labelled** — causal as headline, time-window beside it (FR-17.4). |
| 3 | **Audit log** | clauses 3, 4 | Refusals **beside** sends, with the reason on each refusal. Filter by verdict in one click. |

**Screen 3 is the demo screen and the hardest to fake.** The moment that lands is not what the
agent did — it is opening the log, filtering to `outcome = blocked`, and showing every message the
system refused to send with the rule that stopped it. Build that filter first, not last.

Three details that are easy to drop and expensive to miss:

* **The refusal reason must be human-readable on the row.** "Gate check 4 failed" is not evidence;
  "3rd contact in 7 days — frequency cap" is.
* **Sends and refusals belong in one list, not two tabs.** Separating them lets a viewer see only
  the flattering half, which is the opposite of the argument being made.
* **If a contact-hours override was active, surface it** (FR-16.8). A run that widened the window
  and says so is compliant by record; one that hides it is not.

### POST-SUBMISSION — the full screen set

Specified, wanted, and not built before submission.

| Order | Screen | Purpose | Status |
|---|---|---|---|
| 1 | `Worklist` | The primary screen. Ranked queue. | **Phase 8 — ships** |
| 2 | `Upload` | Batch import, column mapping, repair queue | POST — the API works; seed covers the demo |
| 3 | `Consent` | Per-counterparty consent, quarantine list | POST — enforced by gate check 3 regardless |
| 4 | `Account` | Per-counterparty timeline | POST |
| 5 | `ReviewPlan` | 14-day preview before activation | POST — `dry_run` (FR-16.7) covers this for now |
| 6 | `Dashboard` | Recovered / needs-you / promises / exceptions | **trimmed** — recovered figure only |
| 7 | `AuditLog` | Filterable audit trail — the demo screen | **Phase 8 — ships** |
| 8 | `Settings` | Guardrail configuration | POST — env vars only in Phase 6 |

Screens 3, 4 and 5 depend on capabilities that are themselves POST (FR-11, FR-12) or already
enforced server-side without a UI. Nothing is lost by deferring them; a judge cannot see consent
enforcement on a screen anyway — they see it in the audit log as a refusal, which is screen 3.

---

## Worklist — the primary screen

Default sort is **priority rank**, never date and never alphabetical. This is the whole point;
if a judge sees an aging report sorted by date, the product's core claim evaporates.

Every row shows the `priority_reason` string prominently. Not in a tooltip, not behind a chevron —
in the row. "₹4.2L, 68 days. This customer has paid late twice before but always paid."

Show `inferred_cause` as a labelled chip: `cash crunch`, `dispute`, `wrong contact`.

Rows needing approval get a distinct visual treatment and are reachable in one click.

Money renders as `₹4.2L` / `₹1.4Cr`, not `₹420000`. Indian numbering, not Western.

---

## Dashboard — the judge screen

Four blocks, in this order:

1. **Recovered** — the headline number, large. Below it, DSO delta as `−14.6 days`.
2. **Needs you** — disputes, pending escalations, unclear replies. Empty state should read
   "Nothing needs you today", not a blank panel.
3. **Promises due today** — counterparty, amount, promised date.
4. **Exception list** — who we stopped chasing and why.

The recovered figure must be visibly derived, not asserted. Clicking it opens the list of settled
invoices behind it. A judge who asks "where does that number come from?" should get an answer in
one click, not a verbal explanation.

Use Recharts for the recovered-over-time series. One chart. Resist adding more.

---

## Audit Log — the demo screen

This wins or loses the "what if the AI goes rogue?" question.

**Requirement: filtering to `outcome = blocked` must be one click.** A prominent toggle or segmented
control, not a dropdown inside a filter panel.

Each entry expands to show all seven gate verdicts with pass/fail state and reason. Build a
`GateVerdictBadge` component — green tick, red cross, reason text.

Show actor (`agent` / `human` / `system`), rationale, timestamp in IST.

Include the `GET /audit/verify` hash-chain check as a small "chain verified" indicator. Cheap to
build, disproportionately credible.

---

## Account timeline

Vertical timeline, newest last so it reads as a story. Distinct icons per event kind: message sent,
link opened, reply received, promise made, promise broken, payment received, actions revoked.

Show the raw reply text for `reply_received` events, with the classified intent beside it. Seeing
"next Tuesday tak clear kar dunga" correctly parsed into a promise dated 11 Aug is a genuine
demo moment. Make it visible.

---

## Global elements

- **Pause** button in the header, reachable from every screen. Unmistakable when active.
- Loading states on every query. TanStack Query's `isPending`, not a bare spinner blocking the page.
- Empty states written as sentences, never blank panels.
- Errors from the API error envelope shown inline near the relevant control.

---

## Design notes

Restrained and financial. This is a tool a CFO will look at. Avoid gradients, avoid playful
illustration, avoid a purple SaaS-landing-page aesthetic.

Two type sizes for data, one for headings. Tabular numerals for money columns — misaligned
digits in a financial table look amateurish immediately.

Colour carries meaning only: green for recovered, amber for needs-attention, red for blocked
or disputed, neutral grey for everything else. If a colour does not encode state, do not use it.

Dark mode is not required. Skip it; spend the time on the audit log.

---

## Testing priorities

Manual is fine here, but verify:

1. Worklist default sort is priority, confirmed against the API response order
2. Audit log `outcome = blocked` filter works and shows verdicts
3. Money formatting across ₹1,200 / ₹4.2L / ₹1.4Cr
4. Pause visibly halts and is obvious while active
5. The whole demo path works at 1920×1080 on a projector — test the actual resolution

---

## Phase 8 requirement: the reconciliation poll

**`GET /invoices/{id}/reconciliation-status` exists and the Dashboard must poll it.** Built in
Phase 4 specifically so this is not discovered during rehearsal.

```ts
// Poll from the moment a payment link is opened, not from payment completion:
// the webhook can arrive before an operator clicks anything.
useQuery({
  queryKey: ["reconciliation", invoiceId],
  queryFn: () => api.get(`/invoices/${invoiceId}/reconciliation-status`),
  refetchInterval: (data) => (data?.settled ? false : 1500),  // stop once settled
})
```

Render `revoked_actions` prominently the moment it becomes non-zero — it is the demo's central
number and the single most persuasive thing on the screen. `settled_at` tells you when to stop
polling; `payment_status` distinguishes a partial payment from an unsettled one.

**Do not expect this number from the webhook response.** `POST /webhooks/razorpay` returns
`{"status": "ok"}` and cannot do otherwise: it acknowledges in under 200 ms with reconciliation
deferred, so the count does not exist when it replies. The gap between the payment confirmation
and the number appearing is the asynchronous processing, and it is correct.
