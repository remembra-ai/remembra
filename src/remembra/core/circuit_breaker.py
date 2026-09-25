"""
Circuit Breaker Pattern.

Prevents cascading failures (and quota burn) when an upstream provider is
down or out of credits. Wraps embedding calls (``EmbeddingService``) and the
extraction/consolidation LLM clients (``core.llm_guard``).

States:
- CLOSED: normal operation, requests pass through
- OPEN: provider is failing, requests fail fast with ``CircuitOpenError``
- HALF_OPEN: reset timeout elapsed; exactly ONE probe request is let through.
  Probe success closes the circuit, probe failure re-opens it.

What counts as a failure (everything else is ignored):
- provider errors whose ``.kind`` is ``unavailable`` (5xx/timeout/connection),
  ``rate_limited`` (429), or ``quota_exhausted``;
- ``TimeoutError`` / ``ConnectionError`` / ``OSError``.
4xx input errors, auth errors, ``ValueError`` etc. never count — one bad input
must not take the provider offline for every tenant.

``quota_exhausted`` opens the circuit immediately with a long reset timeout:
retrying an account that is out of credits only burns requests.

State is guarded by a ``threading.Lock`` (not ``asyncio.Lock``) so the breaker
is safe across event loops and worker threads; the critical sections never
await.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import wraps
from threading import Lock
from typing import Any, TypeVar, cast

import structlog

from remembra.core.metrics import BREAKER_OPENS, BREAKER_STATE
from remembra.core.provider_errors import BREAKER_COUNTED_KINDS, ProviderErrorKind
from remembra.core.time import utcnow

log = structlog.get_logger(__name__)

T = TypeVar("T")


class CircuitState(StrEnum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_STATE_GAUGE = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}


class Verdict(StrEnum):
    IGNORE = "ignore"  # not a provider-health signal
    COUNT = "count"  # counts toward failure_threshold
    OPEN_NOW = "open_now"  # open immediately with the long reset timeout


@dataclass
class CircuitStats:
    """Statistics for a circuit breaker."""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    last_failure: datetime | None = None
    last_success: datetime | None = None
    state_changes: int = 0


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    reset_timeout: float = 30.0
    quota_reset_timeout: float = 900.0
    call_timeout: float | None = None
    max_open_seconds: float = 3600.0


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open and rejecting calls."""

    def __init__(
        self,
        message: str,
        name: str = "",
        retry_after: float | None = None,
        last_error_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.retry_after = retry_after
        self.last_error_kind = last_error_kind


def error_kind_of(exc: BaseException) -> str | None:
    """Return the provider error kind attached to an exception, if any."""
    kind = getattr(exc, "kind", None)
    if kind is None:
        return None
    return str(kind.value if isinstance(kind, ProviderErrorKind) else kind)


def default_classifier(exc: BaseException) -> Verdict:
    """Decide whether an exception is a provider-health signal."""
    if isinstance(exc, CircuitOpenError):
        return Verdict.IGNORE
    kind = error_kind_of(exc)
    if kind is not None:
        if kind == ProviderErrorKind.QUOTA_EXHAUSTED:
            return Verdict.OPEN_NOW
        if kind in {k.value for k in BREAKER_COUNTED_KINDS}:
            return Verdict.COUNT
        return Verdict.IGNORE
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return Verdict.COUNT
    return Verdict.IGNORE


StateListener = Callable[["CircuitBreaker", CircuitState, CircuitState], None]
_listeners: list[StateListener] = []


def register_state_listener(fn: StateListener) -> None:
    """Call ``fn(breaker, old_state, new_state)`` on every transition of any breaker."""
    if fn not in _listeners:
        _listeners.append(fn)


def unregister_state_listener(fn: StateListener) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


class CircuitBreaker:
    """
    Circuit breaker for external service calls.

    Usage::

        breaker = CircuitBreaker("openai", failure_threshold=5)

        result = await breaker.call(client.embed, "hello")

        # Or manually, when the call site needs custom handling:
        is_probe = breaker.acquire()          # raises CircuitOpenError
        try:
            result = await do_call()
        except BaseException as exc:
            breaker.record_failure(exc, is_probe=is_probe)
            raise
        breaker.record_success(is_probe=is_probe)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 1,  # kept for signature compatibility; a single probe closes
        reset_timeout: float = 30.0,
        call_timeout: float | None = None,
        quota_reset_timeout: float = 900.0,
        classifier: Callable[[BaseException], Verdict] = default_classifier,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        del success_threshold
        self.name = name
        self.config = CircuitBreakerConfig(
            failure_threshold=max(1, failure_threshold),
            reset_timeout=reset_timeout,
            quota_reset_timeout=quota_reset_timeout,
            call_timeout=call_timeout,
        )
        self._classify = classifier
        self._clock = clock
        self._lock = Lock()

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._open_for: float = reset_timeout
        self._probe_in_flight = False

        self.last_error_kind: str | None = None
        self.last_error_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.opened_by_kind: str | None = None

        self.stats = CircuitStats()
        BREAKER_STATE.set(0, name=name)

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def _refresh_locked(self) -> list[tuple[CircuitState, CircuitState]]:
        """OPEN -> HALF_OPEN once the reset timeout elapses. Caller holds lock."""
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self._open_for:
                return [self._transition_locked(CircuitState.HALF_OPEN)]
        return []

    @property
    def state(self) -> CircuitState:
        with self._lock:
            changes = self._refresh_locked()
            state = self._state
        self._notify(changes)
        return state

    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    def allows_requests(self) -> bool:
        """Non-consuming check: would ``acquire()`` succeed right now?"""
        with self._lock:
            changes = self._refresh_locked()
            ok = self._state == CircuitState.CLOSED or (self._state == CircuitState.HALF_OPEN and not self._probe_in_flight)
        self._notify(changes)
        return ok

    def retry_after(self) -> float | None:
        """Seconds until the circuit will let a probe through (None if not open)."""
        with self._lock:
            if self._state != CircuitState.OPEN or self._opened_at is None:
                return None
            return max(0.0, self._open_for - (self._clock() - self._opened_at))

    # ------------------------------------------------------------------
    # Core protocol
    # ------------------------------------------------------------------

    def acquire(self) -> bool:
        """Reserve permission to call. Returns True if this call is the half-open probe.

        Raises CircuitOpenError when the circuit is open, or half-open with a
        probe already in flight.
        """
        with self._lock:
            changes = self._refresh_locked()
            self.stats.total_calls += 1
            state = self._state
            if state == CircuitState.CLOSED:
                is_probe = False
                rejected = False
            elif state == CircuitState.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                is_probe = True
                rejected = False
            else:
                self.stats.rejected_calls += 1
                rejected = True
                is_probe = False
            retry = (
                max(0.0, self._open_for - (self._clock() - self._opened_at))
                if state == CircuitState.OPEN and self._opened_at is not None
                else 1.0
            )
            kind = self.opened_by_kind or self.last_error_kind
        self._notify(changes)
        if rejected:
            log.debug("circuit_breaker_rejected", name=self.name, state=state.value)
            raise CircuitOpenError(
                f"Circuit '{self.name}' is {state.value}",
                name=self.name,
                retry_after=retry,
                last_error_kind=kind,
            )
        return is_probe

    def record_success(self, is_probe: bool = False) -> None:
        with self._lock:
            self.stats.successful_calls += 1
            now = utcnow()
            self.stats.last_success = now
            self.last_success_at = now
            changes: list[tuple[CircuitState, CircuitState]] = []
            if is_probe:
                self._probe_in_flight = False
            if self._state == CircuitState.HALF_OPEN:
                changes.append(self._transition_locked(CircuitState.CLOSED))
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0
        self._notify(changes)

    def record_failure(self, exc: BaseException, is_probe: bool = False) -> None:
        verdict = self._classify(exc)
        kind = error_kind_of(exc) or (ProviderErrorKind.UNAVAILABLE.value if verdict != Verdict.IGNORE else None)
        changes: list[tuple[CircuitState, CircuitState]] = []
        with self._lock:
            if is_probe:
                self._probe_in_flight = False
            if verdict == Verdict.IGNORE:
                return
            self.stats.failed_calls += 1
            now = utcnow()
            self.stats.last_failure = now
            self.last_error_at = now
            self.last_error_kind = kind

            upstream_hint = getattr(exc, "retry_after", None)
            if verdict == Verdict.OPEN_NOW:
                changes.append(self._open_locked(self.config.quota_reset_timeout, kind))
            elif self._state == CircuitState.HALF_OPEN:
                changes.append(self._open_locked(self._open_duration(upstream_hint), kind))
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    changes.append(self._open_locked(self._open_duration(upstream_hint), kind))
            # Already OPEN (a call admitted before the trip finished): no change.
        log.debug(
            "circuit_breaker_failure",
            name=self.name,
            kind=kind,
            verdict=verdict.value,
            failure_count=self._failure_count,
        )
        self._notify(changes)

    def release(self, is_probe: bool) -> None:
        """Give back a probe slot without recording an outcome (e.g. cancellation)."""
        if is_probe:
            with self._lock:
                self._probe_in_flight = False

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute ``await func(*args, **kwargs)`` under breaker protection."""
        import asyncio

        is_probe = self.acquire()
        try:
            if self.config.call_timeout:
                result = await asyncio.wait_for(func(*args, **kwargs), timeout=self.config.call_timeout)
            else:
                result = await func(*args, **kwargs)
        except asyncio.CancelledError:
            self.release(is_probe)
            raise
        except BaseException as exc:
            self.record_failure(exc, is_probe=is_probe)
            raise
        self.record_success(is_probe=is_probe)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_duration(self, upstream_hint: Any) -> float:
        base = self.config.reset_timeout
        if isinstance(upstream_hint, int | float) and upstream_hint > base:
            return min(float(upstream_hint), self.config.max_open_seconds)
        return base

    def _open_locked(self, duration: float, kind: str | None) -> tuple[CircuitState, CircuitState]:
        self._opened_at = self._clock()
        self._open_for = duration
        self.opened_by_kind = kind
        change = self._transition_locked(CircuitState.OPEN)
        BREAKER_OPENS.inc(name=self.name, kind=kind or "unknown")
        return change

    def _transition_locked(self, new_state: CircuitState) -> tuple[CircuitState, CircuitState]:
        old_state = self._state
        self._state = new_state
        if old_state != new_state:
            self.stats.state_changes += 1
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False
            self.opened_by_kind = None
        elif new_state == CircuitState.HALF_OPEN:
            self._probe_in_flight = False
        BREAKER_STATE.set(_STATE_GAUGE[new_state], name=self.name)
        return (old_state, new_state)

    def _notify(self, changes: list[tuple[CircuitState, CircuitState]]) -> None:
        for old, new in changes:
            if old == new:
                continue
            log.info(
                "circuit_breaker_state_change",
                name=self.name,
                from_state=old.value,
                to_state=new.value,
                kind=self.opened_by_kind if new == CircuitState.OPEN else None,
            )
            for fn in list(_listeners):
                try:
                    fn(self, old, new)
                except Exception as e:  # listeners must never break the caller
                    log.warning("circuit_breaker_listener_failed", name=self.name, error=str(e))

    def protect(self, func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """Decorator form of :meth:`call`."""

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await self.call(func, *args, **kwargs)

        return cast(Callable[..., Awaitable[T]], wrapper)

    def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        with self._lock:
            change = self._transition_locked(CircuitState.CLOSED)
        self._notify([change])
        log.info("circuit_breaker_reset", name=self.name)

    def get_status(self) -> dict[str, Any]:
        state = self.state
        return {
            "name": self.name,
            "state": state.value,
            "failure_count": self._failure_count,
            "retry_after_seconds": self.retry_after(),
            "opened_by_kind": self.opened_by_kind,
            "last_error_kind": self.last_error_kind,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "reset_timeout": self.config.reset_timeout,
                "quota_reset_timeout": self.config.quota_reset_timeout,
                "call_timeout": self.config.call_timeout,
            },
            "stats": {
                "total_calls": self.stats.total_calls,
                "successful_calls": self.stats.successful_calls,
                "failed_calls": self.stats.failed_calls,
                "rejected_calls": self.stats.rejected_calls,
                "state_changes": self.stats.state_changes,
                "last_failure": self.stats.last_failure.isoformat() if self.stats.last_failure else None,
                "last_success": self.stats.last_success.isoformat() if self.stats.last_success else None,
            },
        }


# ============================================================================
# Named breakers
# ============================================================================

_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = Lock()


def get_breaker(
    name: str,
    failure_threshold: int = 5,
    reset_timeout: float = 30.0,
    quota_reset_timeout: float = 900.0,
) -> CircuitBreaker:
    """Get or create a process-wide named circuit breaker."""
    with _registry_lock:
        breaker = _breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                reset_timeout=reset_timeout,
                quota_reset_timeout=quota_reset_timeout,
            )
            _breakers[name] = breaker
        return breaker


def all_breakers() -> dict[str, CircuitBreaker]:
    with _registry_lock:
        return dict(_breakers)


def circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    reset_timeout: float = 30.0,
) -> Callable[..., Any]:
    """Decorator protecting an async function with a named breaker."""
    return get_breaker(name, failure_threshold, reset_timeout).protect
