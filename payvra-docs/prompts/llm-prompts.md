# LLM Prompts

Every prompt used in PAYVRA. Keep them here, not scattered in code — they are product surface and
need reviewing as a set.

**Rules for all prompts:**
- Request JSON only, no prose, no markdown fences
- Include the schema in the prompt
- Keep them short; these are narrow tasks, not open-ended reasoning
- Never include card data, full GSTIN, or more PII than the task needs
- Every output is schema-validated before use

---

## 1. Action proposal (`agent/nodes.py::plan`)

Model: Groq `llama-3.3-70b-versatile`

```
You are the decision engine for a B2B receivables recovery system in India.
Propose exactly ONE next action for the invoice below.

INVOICE
  Number: {invoice_number}
  Outstanding: ₹{outstanding_display}
  Days past due: {days_past_due}
  Recovery state: {recovery_state}
  Current tone tier: {current_tone_tier}
  Touches so far: {touch_count} of {lifetime_cap}
  Crosses MSME 45-day rule: {crosses_msme_45}

COUNTERPARTY
  Name: {counterparty_name}
  Average days to pay historically: {avg_days_to_pay}
  Broken promises: {broken_promise_count}
  Relationship value: ₹{lifetime_revenue_display}

DIAGNOSED CAUSE: {inferred_cause}

RECENT HISTORY
{history_lines}

SIGNALS
{signal_lines}

ALLOWED ACTIONS (you may not propose anything else):
{tool_registry_lines}

ALLOWED TRANSITIONS from state "{recovery_state}":
{allowed_transitions}

PERMITTED CHANNELS: {permitted_channels}
TOUCHES REMAINING THIS WEEK: {weekly_remaining}

GUIDANCE — right intervention per cause:
  oversight     -> single gentle nudge, tier 1-2, do not escalate
  cash_crunch   -> offer instalment split, keep tone LOW, extend link expiry
  dispute       -> mark_disputed, freeze, route to human
  wrong_contact -> switch_channel, request AP contact
  awaiting_docs -> send documents, tier 1
  refusal       -> stop
  unknown       -> continue standard sequence at current tier

Escalating tone is expensive and damages relationships. Prefer the gentlest action that
could plausibly work. Stopping is always available and never needs permission.

Respond with JSON only:
{
  "action": "<one of the allowed actions>",
  "channel": "<email|sms|whatsapp|null>",
  "tone_tier": <1-4>,
  "inferred_cause": "<cause>",
  "rationale": "<one sentence, plain English, shown to the merchant>",
  "confidence": <0.0-1.0>
}
```

**Repair prompt on parse failure:**
```
Your previous response failed validation: {validation_error}
Respond again with valid JSON matching the schema exactly. JSON only, no other text.
```

---

## 2. Message drafting (`generation/drafter.py`)

Model: Gemini Flash

```
Write a payment reminder from {merchant_name} to {counterparty_name} in India.

CONTEXT
  Invoice: {invoice_number}
  Amount outstanding: ₹{outstanding_display}
  Due date: {due_date}
  Days overdue: {days_past_due}
  Payment link: {payment_link}
  Channel: {channel}
  Language: {language}
  Tone tier: {tone_tier}
  Prior contact: {touch_count} previous message(s)
  {promise_context}

TONE TIER {tone_tier} MEANS:
  1 - Courtesy. Friendly, assumes oversight. No pressure.
  2 - Gentle reminder. Warm but clear about the overdue status.
  3 - Firm. Professional, direct, states the business impact. Not aggressive.
  4 - Formal notice. Businesslike, references terms, states next steps factually.

{language_instruction}

MUST INCLUDE
  - The exact amount ₹{outstanding_display}
  - The invoice number {invoice_number}
  - The payment link {payment_link}
  - The opt-out line: "{opt_out_line}"
  - Sender identification as {merchant_name}

MUST NOT INCLUDE
  - Legal threats, or any claim of legal action not actually being taken
  - Credit rating or blacklist threats
  - References to personal assets, family, or anything outside the business relationship
  - Shaming language, or mention of the debt to any third party
  - ALL CAPS demands
  - "Final notice" unless tone tier is 4
  - Invented facts. Use only what is above.

Keep it under {max_words} words. {channel_format_note}

Respond with JSON only:
{
  "subject": "<subject line, or null for sms/whatsapp>",
  "body": "<message text>"
}
```

`language_instruction` when `hinglish`:
```
Write in natural Hinglish - conversational Hindi-English code-mixing in Latin script,
the way Indian business people actually write on WhatsApp. Not formal Hindi, not
translated English. Keep the amount, invoice number, and link in English/numerals.
```

`channel_format_note`:
- email: `Use short paragraphs. No markdown.`
- sms: `Single paragraph, under 300 characters.`
- whatsapp: `Conversational, 2-3 short lines. No formal letter structure.`

---

## 3. Reply intent classification (`replies/classifier.py`)

Model: Groq

```
Classify this reply from a business customer about an unpaid invoice.
The message may be in English, Hindi, or Hinglish (Hindi written in Latin script).

MESSAGE
{raw_text}

INVOICE CONTEXT
  Number: {invoice_number}
  Outstanding: ₹{outstanding_display}
  Days overdue: {days_past_due}

CATEGORIES
  dispute        - disagrees with the invoice: wrong amount, goods not received,
                   quality issue, already paid, invoice not received
  promise_to_pay - commits to paying, with or without a specific date
  query          - asks a question: needs invoice copy, PO reference, GST details,
                   bank details, clarification
  refusal        - refuses to pay, or asks not to be contacted again
  wrong_contact  - says this is not the right person or department
  acknowledgment - acknowledges without committing to anything
  unclear        - cannot be confidently classified

If the message could be a dispute, classify it as dispute. A missed dispute means we
keep chasing someone with a legitimate complaint, which is the worst possible outcome.
Prefer "unclear" over a confident wrong answer.

Respond with JSON only:
{
  "intent": "<category>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one short sentence>"
}
```

---

## 4. Promised-date extraction (`replies/extractor.py`)

Model: Groq. Only runs when intent is `promise_to_pay`.

```
Extract the promised payment date from this message.
Today is {today_ist} (IST), a {today_weekday}.
The message may be in English, Hindi, or Hinglish.

MESSAGE
{raw_text}

EXAMPLES OF RELATIVE DATES
  "next Tuesday tak"     -> the Tuesday of next week
  "agle hafte"           -> approximately 7 days from today
  "month end tak"        -> the last day of the current month
  "15 tareekh ko"        -> the 15th of the current month, or next month if already past
  "kal"                  -> tomorrow
  "2-3 din mein"         -> 3 days from today (take the later bound)
  "after Diwali"         -> unresolvable without a festival calendar; return null

If a partial amount is mentioned, extract it. If no amount is mentioned, return null
(meaning the full outstanding).

If no date can be determined with reasonable confidence, return null for the date.
Do not guess. An invented date suppresses outreach for no reason.

Respond with JSON only:
{
  "promised_date": "<YYYY-MM-DD or null>",
  "promised_amount_paise": <integer or null>,
  "confidence": <0.0-1.0>,
  "interpretation": "<what phrase you resolved and how>"
}
```

---

## 5. Column mapping (`ingestion/mapper.py`)

Model: Groq. **Only for headers that failed rule-based matching.** One call per batch, never per column.

```
Map these spreadsheet column headers to canonical invoice fields.
This is an Indian B2B accounting export, possibly from Tally, Zoho Books, or Excel.

UNMATCHED HEADERS
{headers}

SAMPLE ROW VALUES
{sample_values}

CANONICAL FIELDS
  invoice_number, counterparty_name, gstin, amount, outstanding,
  issue_date, due_date, terms_days, po_ref, contact_email, contact_phone

Return null for any header that does not map to a canonical field.
Do not force a mapping. An unmapped column is fine; a wrong mapping corrupts the data.

Respond with JSON only:
{ "<header>": "<canonical_field or null>", ... }
```

---

## 6. Ambiguous cause diagnosis (`agent/diagnosis.py`)

Model: Groq. **Only when the rules table returns `unknown` and a reply exists.**
Most diagnosis is a lookup — see `architecture/agent-loop.md`.

```
Why is this invoice unpaid? Use only the evidence below.

INVOICE: {invoice_number}, ₹{outstanding_display}, {days_past_due} days overdue
LAST REPLY: {last_reply_text}
SIGNALS: {signal_lines}

CAUSES
  oversight     - simply forgot
  cash_crunch   - wants to pay, cannot right now
  dispute       - disagrees with the invoice
  wrong_contact - reached the wrong person
  awaiting_docs - needs a document from us before they can process payment
  refusal       - will not pay
  unknown       - insufficient evidence

Answer "unknown" rather than guessing. A wrong cause selects a wrong intervention.

Respond with JSON only:
{ "cause": "<cause>", "confidence": <0.0-1.0>, "evidence": "<one sentence>" }
```

---

## Prompt maintenance

- Version prompts alongside code. A prompt change is a product change.
- Log the prompt hash with every LLM call so a behaviour regression is traceable.
- When a prompt is changed, re-run the seed batch and eyeball the output before committing.
- If a prompt needs more than ~40 lines, the task is too broad. Split it.
