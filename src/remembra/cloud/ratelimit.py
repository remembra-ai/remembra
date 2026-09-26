"""Plan-aware rate limits (recall / relay bursts, signup limits).

slowapi's decorator limits are static strings per route; plans need per-account
limits (Free recalls 20/min, Solo 60/min, ...) and signup needs limits keyed by
network (/24) and email domain. This module runs those checks on the SAME
storage backend as slowapi: ``REMEMBRA_RATE_LIMIT_STORAGE`` = ``memory`` (the
default, per process) or ``redis://host:port/db`` (shared across workers and
restarts; requires the ``redis`` package, shipped in the ``cloud`` extra).

A shared backend that stops answering (Redis down, network split) must not
turn every rate-limited route into a 500: :class:`CloudRateLimiter` then keeps
counting in a per-process memory store, retries the backend every
``retry_seconds``, and reports the outage through :meth:`status`
(``/health/ready`` shows it as the ``rate_limit`` component).
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog
from limits import parse
from limits.storage import MemoryStorage, Storage, storage_from_string
from limits.strategies import MovingWindowRateLimiter

log = structlog.get_logger(__name__)


# A shared backend that black-holes packets must not stall the event loop:
# ``limits`` storage calls are synchronous, so bound every Redis round trip.
REDIS_TIMEOUT_SECONDS = 1.0


def storage_options_for(storage_uri: str) -> dict[str, Any]:
    """Driver options for ``storage_uri`` (short socket timeouts for Redis)."""
    if storage_uri.split(":", 1)[0] in ("redis", "rediss", "redis+unix", "redis+sentinel", "redis+cluster"):
        return {"socket_connect_timeout": REDIS_TIMEOUT_SECONDS, "socket_timeout": REDIS_TIMEOUT_SECONDS}
    return {}


def storage_uri_from_setting(value: str | None) -> str:
    """Map the ``rate_limit_storage`` setting to a ``limits`` storage URI."""
    value = (value or "memory").strip()
    if value in ("memory", "memory://"):
        return "memory://"
    return value


class CloudRateLimiter:
    """Moving-window limiter over a ``limits`` storage backend, with a memory fallback."""

    def __init__(
        self,
        storage_uri: str = "memory://",
        *,
        retry_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.storage_uri = storage_uri
        # Raises limits.errors.ConfigurationError when e.g. redis is not installed.
        storage = storage_from_string(storage_uri, **storage_options_for(storage_uri))
        if not isinstance(storage, Storage):
            raise ValueError(f"Rate limit storage {storage_uri!r} is async-only; use memory:// or redis://")
        self._storage = storage
        self._limiter = MovingWindowRateLimiter(self._storage)
        self._fallback_storage = MemoryStorage()
        self._fallback = MovingWindowRateLimiter(self._fallback_storage)
        self.retry_seconds = retry_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._down_until: float | None = None
        self._last_error: str | None = None

    @property
    def backend(self) -> str:
        return self.storage_uri.split(":", 1)[0]

    def hit(self, namespace: str, key: str, limit: str) -> bool:
        """Consume one unit of ``limit`` (e.g. "20/minute") for ``key``. False when exhausted.

        While the shared backend is unreachable the hit is counted in a
        per-process memory store instead (limits still apply per worker).
        """
        item = parse(limit)
        if not self._backend_down():
            try:
                allowed = bool(self._limiter.hit(item, namespace, key))
            except Exception as e:  # ConnectionError, TimeoutError, redis errors
                self._mark_down(e)
            else:
                self._mark_up()
                return allowed
        return bool(self._fallback.hit(item, namespace, key))

    def check_backend(self) -> bool:
        """Ping the backend (blocking). True when it answers; updates :meth:`status`."""
        try:
            reachable = bool(self._storage.check())
        except Exception as e:
            self._mark_down(e)
            return False
        if reachable:
            self._mark_up()
        else:
            self._mark_down(None)
        return reachable

    def status(self) -> dict[str, Any]:
        with self._lock:
            down = self._down_until is not None
            return {
                "backend": self.backend,
                "reachable": not down,
                "fallback": "memory" if down else None,
                "last_error": self._last_error if down else None,
            }

    def reset(self) -> None:
        self._storage.reset()
        self._fallback_storage.reset()

    def _backend_down(self) -> bool:
        with self._lock:
            return self._down_until is not None and self._clock() < self._down_until

    def _mark_down(self, error: Exception | None) -> None:
        with self._lock:
            first = self._down_until is None
            self._down_until = self._clock() + self.retry_seconds
            self._last_error = type(error).__name__ if error is not None else "unreachable"
        if first:
            log.warning(
                "rate_limit_storage_unreachable",
                backend=self.backend,
                error_type=self._last_error,
                fallback="memory",
            )

    def _mark_up(self) -> None:
        with self._lock:
            if self._down_until is None:
                return
            self._down_until = None
            self._last_error = None
        log.info("rate_limit_storage_recovered", backend=self.backend)


_limiter: CloudRateLimiter | None = None


def get_cloud_rate_limiter() -> CloudRateLimiter:
    global _limiter
    if _limiter is None:
        from remembra.config import get_settings

        _limiter = CloudRateLimiter(storage_uri_from_setting(get_settings().rate_limit_storage))
        log.info("cloud_rate_limiter_ready", backend=_limiter.backend)
    return _limiter


def set_cloud_rate_limiter(limiter: CloudRateLimiter | None) -> None:
    global _limiter
    _limiter = limiter


def rate_limits_enabled() -> bool:
    from remembra.config import get_settings

    return bool(get_settings().rate_limit_enabled)


def network_key(ip: str) -> str:
    """The signup-limit bucket for a client IP: its /24 (IPv4) or /56 (IPv6)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"raw:{ip}"
    prefix = 24 if addr.version == 4 else 56
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
