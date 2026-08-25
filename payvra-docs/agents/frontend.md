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

## Screens, in build order

| Order | Screen | Purpose |
|---|---|---|
| 1 | `Worklist` | The primary screen. Ranked queue. |
| 2 | `Upload` | Batch import, column mapping, repair queue |
| 3 | `Consent` | Per-counterparty consent, quarantine list |
| 4 | `Account` | Per-counterparty timeline |
| 5 | `ReviewPlan` | 14-day preview before activation |
| 6 | `Dashboard` | Recovered / needs-you / promises / exceptions |
| 7 | `AuditLog` | Filterable audit trail — the demo screen |
| 8 | `Settings` | Guardrail configuration |

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
