"""A shared rate-limit backend (Redis) that stops answering must not 500 the API.

The limits stay enforced from per-process memory while the backend is down,
the backend is retried, and ``/health/ready`` reports the outage as the
``rate_limit`` component. The storage below raises the same ``ConnectionError``
the redis client raises when nothing listens on the port.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

import remembra.config as config_module
from remembra.api.v1 import auth
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter, storage_options_for
from remembra.core.limiter import limiter
from remembra.core.readiness import ReadinessChecker
from tests.security_harness import make_settings, secure_app


class DownStorage(MemoryStorage):
    """A storage whose backend is unreachable until ``up`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.up = False
        self.calls = 0

    def _guard(self) -> None:
        self.calls += 1
        if not self.up:
            raise ConnectionError("Error 61 connecting to 127.0.0.1:6399. Connection refused.")

    def acquire_entry(self, *args: Any, **kwargs: Any) -> bool:
        self._guard()
        return bool(super().acquire_entry(*args, **kwargs))

    def get_moving_window(self, *args: Any, **kwargs: Any) -> tuple[float, int]:
        self._guard()
        return super().get_moving_window(*args, **kwargs)

    def incr(self, *args: Any, **kwargs: Any) -> int:
        self._guard()
        return int(super().incr(*args, **kwargs))

    def check(self) -> bool:
        return self.up


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _down_limiter(clock: Clock) -> tuple[CloudRateLimiter, DownStorage]:
    cloud = CloudRateLimiter("memory://", retry_seconds=30, clock=clock)
    down = DownStorage()
    cloud._storage = down
    cloud._limiter = MovingWindowRateLimiter(down)
    return cloud, down


def test_cloud_limiter_falls_back_to_memory_and_recovers() -> None:
    clock = Clock()
    cloud, down = _down_limiter(clock)
    # Backend down: no exception, and the limit is still enforced from memory.
    assert [cloud.hit("recall", "u1", "2/minute") for _ in range(3)] == [True, True, False]
    assert cloud.status() == {"backend": "memory", "reachable": False, "fallback": "memory", "last_error": "ConnectionError"}
    # Only the first hit tried the dead backend; the rest wait for the retry window.
    assert down.calls == 1

    down.up = True
    assert cloud.hit("recall", "u2", "2/minute") is True
    assert down.calls == 1  # still inside the 30 s back-off
    clock.now += 31
    assert cloud.hit("recall", "u3", "2/minute") is True
    assert down.calls == 2
    assert cloud.status()["reachable"] is True and cloud.status()["fallback"] is None


def test_cloud_limiter_check_backend_marks_up_and_down() -> None:
    cloud, down = _down_limiter(Clock())
    assert cloud.check_backend() is False
    assert cloud.status()["reachable"] is False
    down.up = True
    assert cloud.check_backend() is True
    assert cloud.status() == {"backend": "memory", "reachable": True, "fallback": None, "last_error": None}


def test_redis_storage_gets_short_socket_timeouts() -> None:
    assert storage_options_for("redis://cache:6379/0") == {"socket_connect_timeout": 1.0, "socket_timeout": 1.0}
    assert storage_options_for("rediss://cache:6380/0")["socket_timeout"] == 1.0
    assert storage_options_for("memory://") == {}


@pytest.mark.skipif(importlib.util.find_spec("redis") is None, reason="redis package not installed")
def test_real_redis_uri_with_nothing_listening_falls_back() -> None:
    cloud = CloudRateLimiter("redis://127.0.0.1:1/0")
    assert [cloud.hit("recall", "u", "2/minute") for _ in range(3)] == [True, True, False]
    assert cloud.status()["reachable"] is False
    assert cloud.check_backend() is False


async def test_readiness_reports_rate_limit_backend() -> None:
    settings = make_settings(rate_limit_enabled=True)
    cloud, down = _down_limiter(Clock())
    checker = ReadinessChecker(settings=settings, rate_limiter=cloud)
    degraded = await checker._check_rate_limit()
    assert degraded["status"] == "degraded"
    assert degraded["reachable"] is False and degraded["fallback"] == "memory"
    down.up = True
    healthy = await checker._check_rate_limit()
    assert healthy["status"] == "ok" and healthy["reachable"] is True

    memory = await ReadinessChecker(settings=settings, rate_limiter=CloudRateLimiter())._check_rate_limit()
    assert memory == {"enabled": True, "backend": "memory", "reachable": True, "fallback": None, "status": "ok"}
    assert await ReadinessChecker(settings=make_settings())._check_rate_limit() == {"status": "ok", "enabled": False}
    assert await ReadinessChecker(settings=settings)._check_rate_limit() == {"status": "ok", "enabled": False}


async def test_rate_limited_routes_survive_backend_outage(tmp_path) -> None:
    """slowapi falls back to memory: no 500, and the login limit still bites at 10/minute."""
    down = DownStorage()
    saved = (limiter._storage, limiter._limiter, limiter._storage_dead, limiter.enabled)
    limiter._storage = down
    limiter._limiter = MovingWindowRateLimiter(down)
    limiter._storage_dead = False
    limiter.enabled = True
    limiter._fallback_storage.reset()  # type: ignore[attr-defined]
    try:
        async with secure_app(tmp_path, [auth.router]) as h:
            codes = []
            for i in range(11):
                login = {"email": f"nobody{i}@example.com", "password": "wrong-pass-1"}
                codes.append((await h.client.post("/api/v1/auth/login", json=login)).status_code)
        assert 500 not in codes, codes
        assert codes == [401] * 10 + [429], codes
        assert limiter._storage_dead is True
    finally:
        limiter._fallback_storage.reset()  # type: ignore[attr-defined]
        limiter._storage, limiter._limiter, limiter._storage_dead, limiter.enabled = saved


def test_plan_burst_limit_survives_backend_outage() -> None:
    """The plan-aware limiter behind recall / relay bursts never raises when Redis is down."""
    from fastapi import HTTPException

    from remembra.cloud.limits import _burst_or_429

    cloud, _down = _down_limiter(Clock())
    previous = config_module._settings
    config_module._settings = make_settings(rate_limit_enabled=True)
    set_cloud_rate_limiter(cloud)
    try:
        for _ in range(20):
            _burst_or_429("recall", "user-down", 20, "Recall")
        with pytest.raises(HTTPException) as exc:
            _burst_or_429("recall", "user-down", 20, "Recall")
        assert exc.value.status_code == 429
    finally:
        set_cloud_rate_limiter(None)
        config_module._settings = previous
