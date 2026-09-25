"""Guard rails for the extraction / consolidation LLM clients (REL-7).

Before: the OpenAI SDK retried every failure twice (3 calls, each up to 30s)
— burning quota during a quota outage — and the extractor/consolidator
silently fell back (sentence split / ADD) with no trace in the stored memory.

Now:

* :func:`make_llm_client` builds the ``AsyncOpenAI`` client with a low
  ``max_retries`` and a bounded timeout, and routes every HTTP request through
  :class:`BreakerTransport`, which feeds the shared ``"llm"`` circuit breaker
  (quota -> open immediately with the long reset; 5xx/timeout/429 counted) and
  fails fast while it is open — no request leaves the process.
* :func:`classify_llm_exception` maps OpenAI/Anthropic SDK exceptions to the
  same :class:`ProviderErrorKind` values the embedding path uses.
* :func:`mark_llm_fallback` records that an LLM step degraded to its non-LLM
  default. Callers that want to persist this (e.g. ``metadata.extraction =
  "fallback"`` for later reprocessing) open :func:`llm_fallback_scope` around
  the work and read the returned dict.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
import structlog

from remembra.core.circuit_breaker import CircuitBreaker, CircuitOpenError, get_breaker
from remembra.core.metrics import LLM_ERRORS, LLM_FALLBACKS
from remembra.core.provider_errors import ProviderErrorKind, classify_http_error, retry_after_from_headers

log = structlog.get_logger(__name__)

LLM_BREAKER_NAME = "llm"
DEFAULT_LLM_TIMEOUT = 20.0
DEFAULT_LLM_MAX_RETRIES = 1


def _settings_value(name: str, default: Any) -> Any:
    try:
        from remembra.config import get_settings

        value = getattr(get_settings(), name, default)
    except Exception:
        return default
    return value if isinstance(value, type(default)) and not isinstance(value, bool) else default


def get_llm_breaker() -> CircuitBreaker:
    """Process-wide breaker shared by every LLM client built here."""
    return get_breaker(
        LLM_BREAKER_NAME,
        failure_threshold=5,
        reset_timeout=30.0,
        quota_reset_timeout=float(_settings_value("provider_quota_reset_seconds", 900.0)),
    )


class LLMProviderError(RuntimeError):
    """Typed LLM HTTP failure (used internally to feed the breaker)."""

    def __init__(self, message: str, kind: ProviderErrorKind, status_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after: float | None = None


class LLMCircuitOpenError(httpx.TransportError):
    """Raised by BreakerTransport while the LLM circuit is open.

    Subclasses ``httpx.TransportError`` so the SDK treats it as a connection
    failure (no response to parse); with ``max_retries`` low it is cheap.
    """

    def __init__(self, message: str, kind: str | None, retry_after: float | None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class BreakerTransport(httpx.AsyncBaseTransport):
    """httpx transport that gates requests on, and reports outcomes to, a breaker."""

    def __init__(self, breaker: CircuitBreaker, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.breaker = breaker
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            is_probe = self.breaker.acquire()
        except CircuitOpenError as e:
            raise LLMCircuitOpenError(f"LLM circuit open ({e.last_error_kind})", e.last_error_kind, e.retry_after) from None
        try:
            response = await self.inner.handle_async_request(request)
        except httpx.TimeoutException as e:
            self.breaker.record_failure(TimeoutError(str(e)), is_probe=is_probe)
            LLM_ERRORS.inc(component="http", kind=ProviderErrorKind.UNAVAILABLE.value)
            raise
        except httpx.TransportError as e:
            self.breaker.record_failure(ConnectionError(str(e)), is_probe=is_probe)
            LLM_ERRORS.inc(component="http", kind=ProviderErrorKind.UNAVAILABLE.value)
            raise
        except BaseException:
            self.breaker.release(is_probe)
            raise

        if response.status_code >= 400:
            await response.aread()
            try:
                body: Any = response.json()
            except Exception:
                body = response.text[:4000]
            kind = classify_http_error(response.status_code, body)
            error = LLMProviderError(f"LLM HTTP {response.status_code}", kind, response.status_code)
            error.retry_after = retry_after_from_headers(response.headers)
            self.breaker.record_failure(error, is_probe=is_probe)
            LLM_ERRORS.inc(component="http", kind=kind.value)
        else:
            self.breaker.record_success(is_probe=is_probe)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def make_llm_client(
    api_key: str | None,
    *,
    base_url: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    inner_transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    """Build an ``AsyncOpenAI`` client wired to the shared LLM breaker."""
    from openai import AsyncOpenAI

    t = timeout if timeout is not None else float(_settings_value("llm_timeout_seconds", DEFAULT_LLM_TIMEOUT))
    retries = max_retries if max_retries is not None else int(_settings_value("llm_max_retries", DEFAULT_LLM_MAX_RETRIES))
    http_client = httpx.AsyncClient(
        transport=BreakerTransport(get_llm_breaker(), inner_transport),
        timeout=httpx.Timeout(t, connect=min(5.0, t)),
    )
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=t, max_retries=retries, http_client=http_client)


def classify_llm_exception(exc: BaseException) -> str:
    """Map an OpenAI/Anthropic SDK (or transport) exception to a ProviderErrorKind value."""
    from remembra.core.ai_spend import SpendBudgetExceeded

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SpendBudgetExceeded):
            return "credit_budget"
        if isinstance(current, LLMCircuitOpenError):
            return current.kind or ProviderErrorKind.UNAVAILABLE.value
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return classify_http_error(status, getattr(current, "body", None)).value
        if isinstance(current, TimeoutError | httpx.TimeoutException | ConnectionError | httpx.TransportError):
            return ProviderErrorKind.UNAVAILABLE.value
        name = type(current).__name__
        if name in ("APITimeoutError", "APIConnectionError"):
            nested = current.__cause__ or current.__context__
            if isinstance(nested, LLMCircuitOpenError):
                return nested.kind or ProviderErrorKind.UNAVAILABLE.value
            return ProviderErrorKind.UNAVAILABLE.value
        current = current.__cause__ or current.__context__
    return "internal"


_fallbacks: ContextVar[dict[str, str] | None] = ContextVar("remembra_llm_fallbacks", default=None)


@contextmanager
def llm_fallback_scope() -> Iterator[dict[str, str]]:
    """Collect ``{component: kind}`` for every LLM step that fell back inside the block.

    The dict is shared with tasks spawned inside the block (they inherit the
    context), so background sub-steps are captured too.
    """
    collected: dict[str, str] = {}
    token = _fallbacks.set(collected)
    try:
        yield collected
    finally:
        _fallbacks.reset(token)


def mark_llm_fallback(component: str, kind: str | None) -> None:
    """Record that ``component`` (extraction, consolidation, ...) fell back to its default."""
    kind = kind or "internal"
    LLM_FALLBACKS.inc(component=component)
    LLM_ERRORS.inc(component=component, kind=kind)
    collected = _fallbacks.get()
    if collected is not None:
        collected[component] = kind
    log.warning("llm_fallback", component=component, kind=kind)
