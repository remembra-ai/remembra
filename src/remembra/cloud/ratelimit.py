"""Plan-aware rate limits (recall / relay bursts, signup limits).

slowapi's decorator limits are static strings per route; plans need per-account
limits (Free recalls 20/min, Solo 60/min, ...) and signup needs limits keyed by
network (/24) and email domain. This module runs those checks on the SAME
storage backend as slowapi: ``REMEMBRA_RATE_LIMIT_STORAGE`` = ``memory`` (the
default, per process) or ``redis://host:port/db`` (shared across workers and
restarts; requires the ``redis`` package, shipped in the ``cloud`` extra).
"""

from __future__ import annotations

import ipaddress

import structlog
from limits import parse
from limits.storage import Storage, storage_from_string
from limits.strategies import MovingWindowRateLimiter

log = structlog.get_logger(__name__)


def storage_uri_from_setting(value: str | None) -> str:
    """Map the ``rate_limit_storage`` setting to a ``limits`` storage URI."""
    value = (value or "memory").strip()
    if value in ("memory", "memory://"):
        return "memory://"
    return value


class CloudRateLimiter:
    """Moving-window limiter over a ``limits`` storage backend."""

    def __init__(self, storage_uri: str = "memory://") -> None:
        self.storage_uri = storage_uri
        # Raises limits.errors.ConfigurationError when e.g. redis is not installed.
        storage = storage_from_string(storage_uri)
        if not isinstance(storage, Storage):
            raise ValueError(f"Rate limit storage {storage_uri!r} is async-only; use memory:// or redis://")
        self._storage = storage
        self._limiter = MovingWindowRateLimiter(self._storage)

    def hit(self, namespace: str, key: str, limit: str) -> bool:
        """Consume one unit of ``limit`` (e.g. "20/minute") for ``key``. False when exhausted."""
        return bool(self._limiter.hit(parse(limit), namespace, key))

    def reset(self) -> None:
        self._storage.reset()


_limiter: CloudRateLimiter | None = None


def get_cloud_rate_limiter() -> CloudRateLimiter:
    global _limiter
    if _limiter is None:
        from remembra.config import get_settings

        _limiter = CloudRateLimiter(storage_uri_from_setting(get_settings().rate_limit_storage))
        log.info("cloud_rate_limiter_ready", backend=_limiter.storage_uri.split(":", 1)[0])
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
