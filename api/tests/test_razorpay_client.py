"""Razorpay client: retry policy, circuit breaker, test-mode guard, idempotency keys.

No network. ``httpx.MockTransport`` stands in for Razorpay so the retry ladder is exercised
deterministically rather than against a live API we would also be rate-limiting.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.razorpay import client as client_mod
from app.razorpay.client import (
    CIRCUIT_FAILURE_THRESHOLD,
    MAX_ATTEMPTS,
    CircuitBreaker,
    RazorpayCircuitOpen,
    RazorpayClient,
    RazorpayClientError,
    RazorpayConfigError,
    RazorpayServerError,
    idempotency_key,
)

TEST_KEY = "rzp_test_abc123"
TEST_SECRET = "secret"


def make_client(handler: object, **kw: object) -> RazorpayClient:
    """A client wired to a mock transport, with retry sleeps removed."""
    client = RazorpayClient(key_id=TEST_KEY, key_secret=TEST_SECRET, **kw)  # type: ignore[arg-type]
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        base_url="https://api.razorpay.test/v1",
        auth=(TEST_KEY, TEST_SECRET),
    )
    # Backoff is real time; the policy under test is *whether* it retries, not how long it waits.
    client._sleep_before_retry = lambda attempt: None  # type: ignore[method-assign]
    return client


# --- test-mode guard ---------------------------------------------------------------------------


def test_a_live_key_is_refused_at_construction() -> None:
    """ADR-006 is test mode only. Refused here, not at first payment: a live key that only fails
    on a real charge is a live key already loaded into a running process."""
    with pytest.raises(RazorpayConfigError, match="test mode only"):
        RazorpayClient(key_id="rzp_live_realkey", key_secret="s")


def test_missing_credentials_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty argument falls back to settings, so the settings have to be empty too."""
    from app.razorpay import client as client_mod

    monkeypatch.setattr(client_mod.settings, "razorpay_key_id", "", raising=False)
    monkeypatch.setattr(client_mod.settings, "razorpay_key_secret", "", raising=False)
    with pytest.raises(RazorpayConfigError, match="must be set"):
        RazorpayClient(key_id="", key_secret="")


def test_a_test_key_is_accepted() -> None:
    assert RazorpayClient(key_id=TEST_KEY, key_secret=TEST_SECRET).key_id == TEST_KEY


# --- retry policy ------------------------------------------------------------------------------


def test_4xx_is_never_retried() -> None:
    """A 4xx means our request is wrong. Retrying cannot fix it and burns rate limit."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "amount is invalid"}}
        )

    client = make_client(handler)
    with pytest.raises(RazorpayClientError) as excinfo:
        client.request("POST", "/payment_links", json={})

    assert len(calls) == 1, "a 4xx must be attempted exactly once"
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "BAD_REQUEST_ERROR"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_no_4xx_status_is_retried(status: int) -> None:
    """A malformed or unauthorised request will be just as malformed on a second attempt.

    **429 is deliberately excluded** — see the test below. This assertion previously included it,
    on the reasoning that "a rate-limit reply is precisely the moment not to send more requests".
    That conflates *not hammering* with *not retrying*: the client backs off exponentially with
    jitter, so a retry lands 0.5s, then 1s, then 2s later rather than immediately. Never retrying
    does not slow anything down, it simply loses the request — and a batch run creating several
    links in succession lost four accounts that way against the live API.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": {"description": "no"}})

    with pytest.raises(RazorpayClientError):
        make_client(handler).request("GET", "/payment_links/x")
    assert len(calls) == 1


def test_429_is_retried_with_backoff() -> None:
    """Rate limiting is transient by definition: the same request succeeds shortly (NFR-3.4)."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"error": {"description": "Too many requests"}})

    with pytest.raises(RazorpayServerError):
        make_client(handler).request("GET", "/payment_links/x")
    assert len(calls) == 3, "429 must exhaust the attempt budget, not fail on the first reply"


def test_a_429_that_clears_succeeds_without_losing_the_account() -> None:
    """The case that matters: one rate-limited attempt must not cost an invoice its turn."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"description": "slow down"}})
        return httpx.Response(200, json={"id": "plink_ok"})

    result = make_client(handler).request("GET", "/payment_links/x")
    assert result["id"] == "plink_ok"
    assert len(calls) == 2


def test_retry_after_is_honoured_when_razorpay_sends_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Razorpay knows its own window better than our backoff curve does."""
    slept: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "2"}, json={"error": {"description": "slow"}}
        )

    with pytest.raises(RazorpayServerError):
        make_client(handler).request("GET", "/payment_links/x")
    assert 2.0 in slept


def test_an_absurd_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch run should requeue rather than block for minutes on a single link."""
    slept: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "600"}, json={"error": {"description": "slow"}}
        )

    with pytest.raises(RazorpayServerError):
        make_client(handler).request("GET", "/payment_links/x")
    assert max(slept) <= client_mod.RETRY_AFTER_CEILING_SECONDS


def test_an_unparseable_retry_after_falls_back_to_our_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP-date form is legal but needs clock-skew handling; guessing wrong is worse."""
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            json={"error": {"description": "slow"}},
        )

    with pytest.raises(RazorpayServerError):
        make_client(handler).request("GET", "/payment_links/x")


def test_5xx_is_retried_to_the_attempt_limit() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="upstream unavailable")

    with pytest.raises(RazorpayServerError):
        make_client(handler).request("GET", "/payment_links/x")
    assert len(calls) == MAX_ATTEMPTS


def test_a_connection_error_is_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RazorpayServerError, match="connection error"):
        make_client(handler).request("GET", "/payment_links/x")
    assert len(calls) == MAX_ATTEMPTS


def test_a_5xx_that_recovers_succeeds_without_raising() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"id": "plink_ok"})

    result = make_client(handler).request("GET", "/payment_links/x")
    assert result == {"id": "plink_ok"}
    assert len(calls) == 2


# --- circuit breaker ---------------------------------------------------------------------------


def test_the_circuit_opens_after_five_consecutive_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    client = make_client(handler)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(RazorpayServerError):
            client.request("GET", "/payment_links/x")

    assert client.breaker.consecutive_failures == CIRCUIT_FAILURE_THRESHOLD
    with pytest.raises(RazorpayCircuitOpen, match="requeue"):
        client.request("GET", "/payment_links/x")


def test_an_open_circuit_makes_no_request_at_all() -> None:
    """The point is to stop calling, not to fail faster while still calling."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    client = make_client(handler)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(RazorpayServerError):
            client.request("GET", "/x")
    before = len(calls)

    with pytest.raises(RazorpayCircuitOpen):
        client.request("GET", "/x")
    assert len(calls) == before, "an open circuit must not reach the transport"


def test_a_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker()
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    assert not breaker.is_open


def test_a_4xx_does_not_count_toward_the_breaker() -> None:
    """The API is healthy; we are the ones sending something wrong. Opening on our own bad
    requests would take out a working integration."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"description": "bad"}})

    client = make_client(handler)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD + 2):
        with pytest.raises(RazorpayClientError):
            client.request("GET", "/x")
    assert client.breaker.consecutive_failures == 0
    assert not client.breaker.is_open


def test_the_circuit_half_opens_after_the_cooldown() -> None:
    breaker = CircuitBreaker(threshold=2, reset_after=0.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.opened_at is not None
    assert not breaker.is_open, "a zero cooldown should admit a probe immediately"


# --- idempotency keys --------------------------------------------------------------------------


def test_the_key_is_stable_for_the_same_inputs() -> None:
    invoice_id = uuid.uuid4()
    assert idempotency_key(invoice_id, 100, "collection") == idempotency_key(
        invoice_id, 100, "collection"
    )


@pytest.mark.parametrize(
    ("amount", "purpose"),
    [(200, "collection"), (100, "regeneration"), (200, "instalment")],
)
def test_the_key_changes_when_any_input_changes(amount: int, purpose: str) -> None:
    """Change the amount or the purpose and you legitimately want a different link."""
    invoice_id = uuid.uuid4()
    assert idempotency_key(invoice_id, 100, "collection") != idempotency_key(
        invoice_id, amount, purpose
    )


def test_the_key_differs_across_invoices() -> None:
    assert idempotency_key(uuid.uuid4(), 100, "collection") != idempotency_key(
        uuid.uuid4(), 100, "collection"
    )


# --- no card data ------------------------------------------------------------------------------


def test_no_card_data_field_exists_anywhere_in_the_money_modules() -> None:
    """PCI-DSS scope is avoided by never having a place to put card data (hard rule 2).

    Checked against declared **names** -- variables, dataclass and Pydantic fields, function
    parameters -- via the AST, not against raw text. A grep would fire on the docstrings that
    explain why card data is absent, which is the fastest way to get a guard switched off.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    banned = {"card", "cvv", "pan", "card_number", "card_token", "expiry_month", "expiry_year"}

    offenders: list[str] = []
    for folder in ("razorpay", "reconciliation", "schemas"):
        for path in (root / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                elif isinstance(node, ast.Assign):
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
                elif isinstance(node, ast.arg):
                    names.add(node.arg)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
            hits = {n for n in names if n.lower() in banned}
            offenders += [f"{path.relative_to(root)}: {n}" for n in sorted(hits)]

    assert not offenders, f"card data fields found: {offenders}"
