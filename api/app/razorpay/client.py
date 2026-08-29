"""Razorpay REST client. This module handles money — be paranoid (agents/razorpay-integration.md).

Direct REST rather than the Razorpay MCP server (ADR-006): our architecture deliberately does not
let the LLM call payment tools, which is most of MCP's value, and a hackathon demo wants fewer
moving parts between the agent and the money.

Three properties matter more than anything else here:

* **4xx is never retried.** A 4xx means *our* request is wrong. Retrying cannot fix it, and it
  burns rate limit we will want during the demo. Only 5xx and connection errors retry.
* **The circuit breaker fails closed.** After five consecutive failures the client stops calling
  out entirely and the caller requeues, rather than hammering a struggling API.
* **Test mode only.** A live key is refused at construction, not at call time.

**No card data. Ever.** No PAN, CVV, expiry, or token appears in any model, log, or request built
here — that is what keeps PAYVRA out of PCI-DSS scope. The hosted checkout owns all of it.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings
from app.exceptions import PayvraError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.razorpay.com/v1"

# ADR-006: test mode for the entire hackathon. A live key id starts with `rzp_live_`.
TEST_KEY_PREFIX = "rzp_test_"

# Retry policy. Deliberately short: this runs inside a dispatch window with a gate verdict that
# expires in five minutes, so a long retry ladder would fail the send on staleness anyway.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5

# Rate limiting. A 4xx by status, a "try again shortly" by meaning.
TOO_MANY_REQUESTS = 429

# Razorpay could in principle ask us to wait a very long time; a batch run should give up and
# requeue rather than block on it.
RETRY_AFTER_CEILING_SECONDS = 10.0
BACKOFF_MAX_SECONDS = 4.0
REQUEST_TIMEOUT_SECONDS = 10.0

# Consecutive failures before the breaker opens, and how long it stays open.
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_RESET_SECONDS = 60.0


class RazorpayError(PayvraError):
    """Base for every Razorpay failure."""


class RazorpayConfigError(RazorpayError):
    """Credentials are missing or are not test-mode credentials."""


class RazorpayClientError(RazorpayError):
    """A 4xx. Our request was wrong; retrying it is pointless and costs rate limit."""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(f"Razorpay {status_code}: {message}")
        self.status_code = status_code
        self.code = code


class RazorpayServerError(RazorpayError):
    """A 5xx or a connection failure. Retried with backoff."""


class RazorpayCircuitOpen(RazorpayError):
    """The breaker is open. The caller should requeue rather than wait."""


def idempotency_key(invoice_id: object, amount_paise: int, purpose: str) -> str:
    """``sha256(invoice_id:amount_paise:purpose)`` — ADR-006's key.

    Deterministic on exactly the three things that make a link *the same* link. Change the amount
    or the purpose and you legitimately want a new link; ask twice for the same one and you must
    get the original back.
    """
    return hashlib.sha256(f"{invoice_id}:{amount_paise}:{purpose}".encode()).hexdigest()


@dataclass
class CircuitBreaker:
    """Trips after N consecutive failures; refuses calls until a cooldown elapses.

    Counts *consecutive* failures, so a single blip inside otherwise healthy traffic never opens
    it — the point is to stop calling an API that is genuinely down, not to react to noise.
    """

    threshold: int = CIRCUIT_FAILURE_THRESHOLD
    reset_after: float = CIRCUIT_RESET_SECONDS
    consecutive_failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_after:
            # Half-open: allow one probe through. A failure re-opens immediately.
            self.opened_at = None
            self.consecutive_failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.opened_at = time.monotonic()
            logger.error(
                "razorpay circuit opened after %d consecutive failures",
                self.consecutive_failures,
            )


@dataclass
class RazorpayClient:
    """Thin, synchronous REST client. One instance per process is fine; it holds no per-call state
    beyond the breaker."""

    key_id: str = ""
    key_secret: str = ""
    base_url: str = BASE_URL
    timeout: float = REQUEST_TIMEOUT_SECONDS
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    _client: httpx.Client | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.key_id = self.key_id or settings.razorpay_key_id
        self.key_secret = self.key_secret or settings.razorpay_key_secret
        if not self.key_id or not self.key_secret:
            raise RazorpayConfigError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set")
        if not self.key_id.startswith(TEST_KEY_PREFIX):
            # Refused here rather than at call time: a live key that only fails on the first
            # payment attempt is a live key that has already been loaded into a running process.
            raise RazorpayConfigError(
                f"refusing a non-test key ({self.key_id[:12]}...): ADR-006 is test mode only"
            )

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                auth=(self.key_id, self.key_secret),
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- the one place a request is actually made ---------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency: str | None = None,
    ) -> dict[str, Any]:
        """Issue one Razorpay call, with retry on 5xx/connection errors and never on 4xx.

        Raises :class:`RazorpayCircuitOpen` without calling out when the breaker is open, so a
        caller can requeue immediately instead of waiting on a timeout.
        """
        if self.breaker.is_open:
            raise RazorpayCircuitOpen(
                f"circuit open after {self.breaker.consecutive_failures} consecutive failures; "
                "requeue rather than retry"
            )

        headers = {"X-Razorpay-Idempotency-Key": idempotency} if idempotency else None
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.request(method, path, json=json, headers=headers)
            except httpx.RequestError as exc:
                # Connection-level: the request may never have reached Razorpay. Retryable.
                last_error = RazorpayServerError(f"connection error calling {path}: {exc}")
                logger.warning(
                    "razorpay connection error path=%s attempt=%d/%d", path, attempt, MAX_ATTEMPTS
                )
                self._sleep_before_retry(attempt)
                continue

            if response.status_code < 400:
                self.breaker.record_success()
                return dict(response.json()) if response.content else {}

            if response.status_code == TOO_MANY_REQUESTS:
                # 429 is a 4xx but is emphatically NOT "our request is malformed" -- it is "slow
                # down", and the same request will succeed shortly. Retrying is the documented
                # behaviour (NFR-3.4) and the batch runner depends on it: a run creating several
                # links in quick succession hits this, and treating it as a permanent failure
                # loses those accounts for the whole run.
                #
                # Honour Retry-After when Razorpay sends one; it knows its own window better than
                # our backoff curve does.
                retry_after = _retry_after_seconds(response)
                last_error = RazorpayServerError(f"Razorpay 429 (rate limited) on {path}")
                logger.warning(
                    "razorpay 429 path=%s attempt=%d/%d retry_after=%s",
                    path,
                    attempt,
                    MAX_ATTEMPTS,
                    retry_after,
                )
                if retry_after is not None:
                    time.sleep(min(retry_after, RETRY_AFTER_CEILING_SECONDS))
                else:
                    self._sleep_before_retry(attempt)
                continue

            if response.status_code < 500:
                # 4xx. Our fault. Do not retry, and do not count toward the breaker: the API is
                # healthy, we are the ones sending something wrong.
                self.breaker.record_success()
                error = _extract_error(response)
                logger.warning(
                    "razorpay %d path=%s code=%s", response.status_code, path, error.get("code")
                )
                raise RazorpayClientError(
                    response.status_code,
                    str(error.get("description", response.text[:200])),
                    code=error.get("code"),
                )

            last_error = RazorpayServerError(f"Razorpay {response.status_code} on {path}")
            logger.warning(
                "razorpay %d path=%s attempt=%d/%d",
                response.status_code,
                path,
                attempt,
                MAX_ATTEMPTS,
            )
            self._sleep_before_retry(attempt)

        self.breaker.record_failure()
        raise last_error or RazorpayServerError(f"{path} failed after {MAX_ATTEMPTS} attempts")

    def _sleep_before_retry(self, attempt: int) -> None:
        """Exponential backoff with jitter. No sleep after the final attempt."""
        if attempt >= MAX_ATTEMPTS:
            return
        delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
        time.sleep(delay * (0.5 + random.random() / 2))  # noqa: S311 - jitter, not crypto

    # --- payment link endpoints ----------------------------------------------------------------

    def create_payment_link(self, payload: dict[str, Any], *, idempotency: str) -> dict[str, Any]:
        return self.request("POST", "/payment_links", json=payload, idempotency=idempotency)

    def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
        return self.request("GET", f"/payment_links/{link_id}")

    def notify(self, link_id: str, medium: str) -> dict[str, Any]:
        """Resend an existing link through Razorpay (FR-9.3).

        Only ever called explicitly, by us, after the gate has approved the touch. It is not the
        same thing as Razorpay's automatic reminders, which stay disabled — see links.py.
        """
        if medium not in ("sms", "email"):
            raise ValueError(f"notify medium must be sms or email, got {medium!r}")
        return self.request("POST", f"/payment_links/{link_id}/notify_by/{medium}")

    def cancel(self, link_id: str) -> dict[str, Any]:
        return self.request("POST", f"/payment_links/{link_id}/cancel")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """``Retry-After`` in seconds, if Razorpay sent a usable one.

    Only the delta-seconds form is honoured. The HTTP-date form is legal but needs clock-skew
    handling to be safe, and guessing wrong here means either hammering a rate limit or stalling
    a run -- so an unparseable value falls back to our own backoff instead.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _extract_error(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return dict(error) if isinstance(error, dict) else {}
