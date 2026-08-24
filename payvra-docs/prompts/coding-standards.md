# Coding Standards

---

## Python

**Formatting:** `ruff format`, line length 100. **Linting:** `ruff check`. **Types:** `mypy --strict`
on `agent/`, `guardrails/`, `reconciliation/`. Best-effort elsewhere.

**Type hints on every function signature.** No bare `dict` or `list` — parameterise them.

**Pydantic for every boundary.** Anything crossing an API, LLM, or webhook boundary is a Pydantic
model, not a dict. This is not optional; it is how the LLM-output validation guarantee is enforced.

**Money:**
```python
amount_paise: int          # yes
amount: float              # NEVER
amount: Decimal            # not in the DB layer
```
Convert at the presentation boundary only. Helper: `paise_to_display(1420000) -> "₹14.2L"`.

**Time:**
```python
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

IST = ZoneInfo("Asia/Kolkata")
```
Store UTC. Convert to IST only for display and for the `time_window` guardrail check.
Never use naive datetimes. Never use `datetime.utcnow()` — it returns a naive object.

**Exceptions:** define domain exceptions in `app/exceptions.py`. Never raise bare `Exception`.
Never `except:` without a type. Never swallow an exception in the send path — fail closed.

**Logging:** structured, with a correlation id.
```python
log.info("action.gated", extra={"action_id": str(a.id), "passed": v.passed,
                                "failed_check": v.first_failure})
```
**Never log PII.** No phone, email, GSTIN, or full webhook payloads. Redact at the logger,
not at each call site — call-site redaction gets forgotten.

**Imports:** absolute from `app.`. No relative imports beyond one level. No `import *`.

**Module boundaries that must not leak:**
- `langgraph` / `langchain` → only `agent/graph.py`, `agent/nodes.py` (ADR-004)
- `litellm` → only `generation/llm.py` (ADR-003)
- `razorpay` HTTP calls → only `razorpay/client.py` (ADR-006)

---

## TypeScript

**Formatting:** Prettier, 100 cols. **Linting:** ESLint with `@typescript-eslint/recommended`.

**No `any`.** Use `unknown` and narrow. API types are generated from the OpenAPI schema — never
hand-written.

Functional components, hooks only. TanStack Query for all server state; never `useEffect` + `fetch`.
Local UI state in `useState`. No global state library — this app does not need one.

Money formatting lives in one place, `utils/money.ts`. Indian numbering (`₹4.2L`, `₹1.4Cr`),
never Western.

---

## SQL and migrations

One Alembic migration per logical change. Never edit an applied migration. Every migration has a
working `downgrade()`.

Every query scoped to `merchant_id`. If you write a query without it, that is a security bug,
not a style issue.

Use `SELECT ... FOR UPDATE SKIP LOCKED` when claiming work from a queue table.

Any query on a table expected to exceed 10k rows needs an index. Verify with `EXPLAIN`, do not assume.

---

## Testing

Pytest. `tests/` mirrors `app/`.

**Must be tested:**
- Every guardrail check, in isolation
- Reconciliation, especially the revoke-on-settle path
- Webhook signature verification and dedupe
- Idempotency of every scheduled job
- Tenant isolation
- Deterministic fallback with `LLM_ENABLED=false`
- Date parsing, including the ambiguous `DD/MM` vs `MM/DD` case

**Need not be tested:** UI components, LLM output quality (validate structure, not prose),
third-party clients (mock them).

Never call a real LLM or the real Razorpay API in a test. Fixtures for both.

---

## Git

Conventional commits:
```
feat(agent): add promise-to-pay extraction
fix(razorpay): verify signature before parsing body
docs(adr): supersede ADR-007 with Celery decision
test(guardrails): cover frequency cap boundary
```

Scopes: `agent`, `guardrails`, `razorpay`, `ingestion`, `scoring`, `web`, `db`, `adr`, `seed`

Small commits. If the message needs "and", it is two commits.

**Never commit:** `.env`, real API keys, real customer data, `__pycache__`, `node_modules`.
`.env.example` lists every required variable with a dummy value.

---

## Documentation

**If you change a locked decision, update the ADR.** Changing the code alone leaves the repo
lying about itself, and judges read the docs.

Docstrings on public functions. Skip them on obvious private helpers — a docstring restating the
function name is noise.

Comment *why*, never *what*:
```python
# notify=False because PAYVRA owns the messaging sequence; Razorpay reminders
# would bypass the guardrail gate and the audit log entirely
payload["notify"] = {"sms": False, "email": False}
```

---

## Security checklist

Before any commit touching the send path, webhook path, or auth:

- [ ] No secrets in code or logs
- [ ] Query scoped to `merchant_id`
- [ ] Webhook signature verified over the raw body, before parsing
- [ ] `hmac.compare_digest`, not `==`, for signature comparison
- [ ] No card data anywhere in the change
- [ ] No PII in log statements
- [ ] Idempotency key on any Razorpay write
- [ ] `GateVerdict` present and passed before any outbound send
