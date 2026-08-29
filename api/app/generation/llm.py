"""The LLM wrapper. **The only module in this repository that imports litellm.**

agents/agent-engine.md: *"All calls go through one wrapper in ``generation/llm.py``. No direct
LiteLLM calls elsewhere."* One wrapper is what makes the ADR-003 mitigations enforceable rather
than aspirational -- a second call site is a second place the circuit breaker, the cache, the cost
log and the kill switch would all have to be reimplemented, and the one that gets missed is the
one that burns the free-tier quota during the pitch.

Five properties, each mandated by ADR-003's "required mitigations":

* **``LLM_ENABLED=false`` is absolute.** Off means no import, no key read, no call. The full
  pipeline runs on templates alone, and CI runs exactly that way.
* **Exponential backoff with jitter, max 3 attempts.** Free tiers rate-limit; a fixed retry
  interval from a batch loop is a thundering herd against a 30 RPM ceiling.
* **Circuit breaker per job type: 2 consecutive failures -> templates for 5 minutes.** Per job,
  because Groq rate-limiting classification says nothing about whether Gemini can draft.
* **Token, latency and cost are logged for every call.** "Full demo run well under $1" is a claim
  ADR-003 makes to judges; a claim nobody measures is a guess.
* **Never in a request-response path.** Enforced, not documented -- see :func:`complete`.

**litellm is imported lazily, inside the call.** That is deliberate: with the kill switch off,
nothing in this file needs the dependency to exist, so template generation, the test suite and CI
all run with litellm uninstalled. A fallback that quietly depends on the thing it is a fallback
for is not a fallback.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config import settings
from app.exceptions import PayvraError

logger = logging.getLogger(__name__)

# --- retry / breaker policy (ADR-003) ------------------------------------------------------------

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0
REQUEST_TIMEOUT_SECONDS = 30.0

# Two, not five. A drafting model that has failed twice in a row is rate-limited or down, and the
# template is already good enough to send -- so the cheap, correct move is to stop asking.
CIRCUIT_FAILURE_THRESHOLD = 2
CIRCUIT_RESET_SECONDS = 300.0  # 5 minutes, per ADR-003


class LLMJob(StrEnum):
    """The four call types ADR-003 names. Routing and breaker state are both per job."""

    DRAFTING = "drafting"
    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"
    PROPOSAL = "proposal"


# ADR-003's routing table. Splitting by job is the whole point -- classification needs speed and
# structured output (Groq), drafting needs Hinglish fluency (Gemini).
#
# **Model ids go stale, and a stale id is indistinguishable from an outage at the call site.**
# The originals here (llama-3.3-70b-versatile, gemini-2.0-flash) were both retired by their
# providers; every drafting call fell back to a template, which is exactly the degradation the
# fallback is designed to hide. Verified working by scripts/verify_llm -- re-run it when a
# provider deprecates something, and prefer whatever the provider's own 404 recommends.
_GROQ = "groq/openai/gpt-oss-20b"
_GEMINI = "gemini/gemini-3.6-flash"
_OPENROUTER = "openrouter/meta-llama/llama-3.3-70b-instruct:free"

# Each job falls back across *providers*, not just to OpenRouter. One provider having a bad
# afternoon is the common case, and a Groq outage should not cost us classification when a working
# Gemini key is sitting right there. OpenRouter stays last: free tier, slowest, most rate-limited.
MODEL_ROUTES: dict[LLMJob, tuple[str, ...]] = {
    LLMJob.DRAFTING: (_GEMINI, _GROQ, _OPENROUTER),
    LLMJob.CLASSIFICATION: (_GROQ, _GEMINI, _OPENROUTER),
    LLMJob.EXTRACTION: (_GROQ, _GEMINI, _OPENROUTER),
    LLMJob.PROPOSAL: (_GROQ, _GEMINI, _OPENROUTER),
}

# litellm reads provider credentials from os.environ, but ours live in .env and are loaded by
# pydantic-settings into `settings` -- they never reach the environment. Passing api_key
# explicitly is what closes that gap; without it every call is an AuthenticationError no matter
# how valid the key in .env is.
_PROVIDER_KEYS: tuple[tuple[str, str], ...] = (
    ("groq/", "groq_api_key"),
    ("gemini/", "gemini_api_key"),
    ("openrouter/", "openrouter_api_key"),
)


# Current flagship models are reasoning models, and they spend the token budget on internal
# thinking *before* emitting anything. gemini-3.6-flash given max_tokens=1080 returned
# finish_reason='length' with completion_tokens=1076 and content=None -- an empty draft that
# looked exactly like a provider outage and fell back to a template every single time.
#
# Every job here is short structured output, not a reasoning problem: draft a dunning message,
# classify a reply, extract a date. Turning thinking off makes the same call return clean JSON in
# ~200 tokens instead of burning 1076 on deliberation. Providers that do not understand the
# parameter drop it (see DROP_UNSUPPORTED_PARAMS) rather than erroring.
DEFAULT_REASONING_EFFORT = "none"

# ...but providers disagree on how to say "as little as possible", and the disagreement is a hard
# 400 rather than a shrug. Groq accepts only low|medium|high and rejects "none" outright; Gemini
# accepts "none" and needs it, because at "low" it still spends the budget thinking.
#
# Sending "none" everywhere made every Groq call fail three times before falling through to
# Gemini -- so Groq was dead for classification, extraction and proposal, the three jobs ADR-003
# routes to it *first*. It went unnoticed because verify_llm only exercised drafting, which is
# Gemini-first. drop_params does not help: litellm forwards the parameter happily, and it is the
# *value* the provider rejects.
_MINIMAL_REASONING: tuple[tuple[str, str], ...] = (
    ("gemini/", "none"),
    ("groq/", "low"),
    # OpenRouter proxies many models; "low" is the value the OpenAI-compatible surface accepts.
    ("openrouter/", "low"),
)


def reasoning_effort_for(model: str, requested: str | None) -> str | None:
    """Translate "minimal thinking" into the value this provider actually accepts.

    Only the "none" request is translated. An explicit ``low``/``medium``/``high`` is a caller
    asking for real deliberation and is passed through untouched.
    """
    if requested != DEFAULT_REASONING_EFFORT:
        return requested
    for prefix, value in _MINIMAL_REASONING:
        if model.startswith(prefix):
            return value
    return requested

# litellm raises on a parameter a provider does not support. Our routes span three providers with
# different capabilities, so a param meant for one must not break the fallback to another.
DROP_UNSUPPORTED_PARAMS = True


def api_key_for(model: str) -> str | None:
    """The configured key for this model's provider, or None if it is a placeholder.

    Returning None rather than the placeholder lets the route be skipped cleanly instead of
    burning three retries on a credential that was never going to work.
    """
    for prefix, attribute in _PROVIDER_KEYS:
        if model.startswith(prefix):
            key = str(getattr(settings, attribute, "") or "")
            return key if key and not key.startswith("dummy") else None
    return None


class LLMUnavailable(PayvraError):
    """No usable model response. **Always recoverable: the caller falls back to a template.**

    Raised when the kill switch is off, the breaker is open, litellm is not installed, no API key
    is configured, or every attempt against every route failed. Callers must not propagate this to
    a user -- there is always a template.
    """


class LLMInRequestPath(PayvraError):
    """An LLM call was attempted while handling an HTTP request. Always a bug."""


# --- "never in a request-response path", enforced ------------------------------------------------
#
# agents/agent-engine.md: "Never call an LLM inside a request-response path. Never call one inside
# the gate. Never call one in the ranking path." A four-second model call inside a request holds a
# worker, and inside the webhook handler it would blow the 200ms acknowledgement budget and turn
# one payment into a Razorpay retry storm.
#
# Enforced the same way delivery/sender.py enforces its gate verdict -- in the signature of the
# thing, not in a comment. main.py's middleware marks request handling; complete() refuses.

_IN_REQUEST_PATH: ContextVar[bool] = ContextVar("payvra_in_request_path", default=False)


@contextmanager
def request_path() -> Iterator[None]:
    """Mark the enclosed block as HTTP request handling. Set by middleware in ``main.py``."""
    token = _IN_REQUEST_PATH.set(True)
    try:
        yield
    finally:
        _IN_REQUEST_PATH.reset(token)


@contextmanager
def worker_path() -> Iterator[None]:
    """Clear the request marker for scheduler and worker code.

    Starlette runs ``BackgroundTasks`` inside the request's context, so a background task inherits
    the marker. Genuine off-request work wraps itself in this to say so explicitly, which keeps
    the guard honest instead of forcing it to be lenient.
    """
    token = _IN_REQUEST_PATH.set(False)
    try:
        yield
    finally:
        _IN_REQUEST_PATH.reset(token)


def in_request_path() -> bool:
    return _IN_REQUEST_PATH.get()


# --- circuit breaker -----------------------------------------------------------------------------


@dataclass
class JobBreaker:
    """Consecutive-failure breaker for one job type."""

    threshold: int = CIRCUIT_FAILURE_THRESHOLD
    reset_after: float = CIRCUIT_RESET_SECONDS
    consecutive_failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_after:
            # Half-open: let one probe through. A failure re-opens immediately, because
            # consecutive_failures is left one below the threshold rather than reset to zero.
            self.opened_at = None
            self.consecutive_failures = self.threshold - 1
            return False
        return True

    @property
    def opens_in_seconds(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.reset_after - (time.monotonic() - self.opened_at))

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.opened_at = time.monotonic()
            logger.warning(
                "llm circuit open job=%s after %d consecutive failures; templates for %.0fs",
                getattr(self, "job", "?"),
                self.consecutive_failures,
                self.reset_after,
            )


@dataclass
class BreakerRegistry:
    """One breaker per job type. Groq failing must not disable Gemini."""

    breakers: dict[LLMJob, JobBreaker] = field(default_factory=dict)

    def for_job(self, job: LLMJob) -> JobBreaker:
        breaker = self.breakers.get(job)
        if breaker is None:
            breaker = JobBreaker()
            breaker.job = job.value  # type: ignore[attr-defined]  # for the log line only
            self.breakers[job] = breaker
        return breaker

    def reset(self) -> None:
        self.breakers.clear()


BREAKERS = BreakerRegistry()


# --- usage accounting ----------------------------------------------------------------------------


@dataclass
class UsageStats:
    """Running totals for the process. Surfaced on the health endpoint and in the demo."""

    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_latency_ms": round(self.total_latency_ms / self.calls, 1) if self.calls else 0.0,
        }


USAGE = UsageStats()


@dataclass(frozen=True)
class LLMResponse:
    """One successful completion, plus what it cost."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float


def is_enabled() -> bool:
    """The kill switch. Read at call time, not import time, so tests can flip it."""
    return bool(settings.llm_enabled)


def available(job: LLMJob) -> bool:
    """Whether an LLM call would even be attempted. Cheap; safe to call in a loop."""
    return is_enabled() and not BREAKERS.for_job(job).is_open


def _sleep_before_retry(attempt: int) -> None:
    """Exponential backoff with jitter. No sleep after the final attempt."""
    if attempt >= MAX_ATTEMPTS:
        return
    delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
    time.sleep(delay * (0.5 + random.random() / 2))  # noqa: S311 - jitter, not crypto


def _completion_cost(response: Any) -> float:
    """Best-effort cost. Never let accounting break a working generation."""
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:  # noqa: BLE001 - unknown model, missing pricing table, anything
        return 0.0


def complete(
    prompt: str,
    *,
    job: LLMJob,
    system: str | None = None,
    temperature: float = 0.4,
    max_tokens: int = 800,
    response_format: dict[str, Any] | None = None,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
) -> LLMResponse:
    """One completion, with routing, retry, breaker and accounting.

    Raises :class:`LLMUnavailable` for every failure mode. That is the contract: callers do not
    inspect *why* the model was unusable, they fall back to a template.
    """
    if in_request_path():
        raise LLMInRequestPath(
            f"LLM call ({job.value}) attempted inside an HTTP request. Generation belongs on the "
            "scheduler/dispatch path; wrap genuine off-request work in llm.worker_path()."
        )

    if not is_enabled():
        raise LLMUnavailable("LLM_ENABLED=false")

    breaker = BREAKERS.for_job(job)
    if breaker.is_open:
        raise LLMUnavailable(
            f"circuit open for {job.value}; {breaker.opens_in_seconds:.0f}s until retry"
        )

    try:
        import litellm
    except ModuleNotFoundError as exc:
        # Not a failure worth tripping the breaker: the package will not appear on a retry.
        raise LLMUnavailable(f"litellm is not installed: {exc}") from exc

    litellm.drop_params = DROP_UNSUPPORTED_PARAMS

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error: Exception | None = None
    for model in MODEL_ROUTES[job]:
        key = api_key_for(model)
        if key is None:
            # No usable credential. Skipping is not a failure worth retrying or tripping the
            # breaker over -- a missing key will not appear between attempts.
            logger.debug("skipping %s for %s: no API key configured", model, job.value)
            continue
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    api_key=key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    **({"response_format": response_format} if response_format else {}),
                    **(
                        {"reasoning_effort": effort}
                        if (effort := reasoning_effort_for(model, reasoning_effort))
                        else {}
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - litellm raises a wide provider-specific tree
                last_error = exc
                logger.warning(
                    "llm call failed job=%s model=%s attempt=%d/%d: %s",
                    job.value,
                    model,
                    attempt,
                    MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                _sleep_before_retry(attempt)
                continue

            latency_ms = (time.perf_counter() - started) * 1000
            text = _extract_text(response)
            if not text:
                last_error = LLMUnavailable(f"{model} returned an empty completion")
                _sleep_before_retry(attempt)
                continue

            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            cost = _completion_cost(response)

            breaker.record_success()
            USAGE.calls += 1
            USAGE.prompt_tokens += prompt_tokens
            USAGE.completion_tokens += completion_tokens
            USAGE.total_latency_ms += latency_ms
            USAGE.total_cost_usd += cost

            # The accounting line ADR-003's cost claim rests on. Never the prompt or the
            # completion -- both carry counterparty names and amounts.
            logger.info(
                "llm ok job=%s model=%s latency_ms=%.0f prompt_tokens=%d "
                "completion_tokens=%d cost_usd=%.6f",
                job.value,
                model,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                cost,
            )
            return LLMResponse(
                text=text,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                cost_usd=cost,
            )

    breaker.record_failure()
    USAGE.failures += 1
    raise LLMUnavailable(
        f"all routes failed for {job.value}: {type(last_error).__name__ if last_error else 'none'}"
    ) from last_error


def _extract_text(response: Any) -> str:
    """Pull the message text out of a litellm response without assuming its exact class."""
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return ""
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    return str(content).strip() if content else ""


def reset() -> None:
    """Clear breaker state and usage totals. For tests and for the demo reset button."""
    BREAKERS.reset()
    USAGE.calls = 0
    USAGE.failures = 0
    USAGE.prompt_tokens = 0
    USAGE.completion_tokens = 0
    USAGE.total_latency_ms = 0.0
    USAGE.total_cost_usd = 0.0


__all__ = [
    "BREAKERS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_RESET_SECONDS",
    "MAX_ATTEMPTS",
    "MODEL_ROUTES",
    "USAGE",
    "BreakerRegistry",
    "JobBreaker",
    "LLMInRequestPath",
    "LLMJob",
    "LLMResponse",
    "LLMUnavailable",
    "available",
    "complete",
    "in_request_path",
    "is_enabled",
    "request_path",
    "reset",
    "worker_path",
]
