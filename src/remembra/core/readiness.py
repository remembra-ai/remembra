"""Readiness (``GET /health/ready``) — REL-2.

``/health`` stays a cheap liveness probe. ``/health/ready`` answers "can this
instance actually store and recall right now?":

* **sqlite**     — ``SELECT 1`` + schema version.
* **qdrant**     — reachability over the transport in use + collection
  dimension check (REL-6).
* **embeddings** — passive state (circuit breaker, last error kind, missing
  credentials) plus an *active* probe (one real embedding call) that is
  cached and single-flight: at most one probe per ``probe_interval`` seconds
  no matter how often readiness is polled, and never while the breaker is
  open (that would just burn quota).
* **llm**        — passive state of the extraction/consolidation breaker.
* **reranker**   — whether the configured reranker can load.
* **pending_embeddings** — queue depth / dead letters.
* **rate_limit** — whether the shared rate-limit backend (``redis://``)
  answers; while it does not, limits fall back to per-process memory.

Always returns HTTP 200: ``status`` is ``ok`` or ``degraded``. Returning 5xx
from a readiness endpoint that an orchestrator also uses for restarts turns
a provider outage into a restart loop.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger(__name__)

OK = "ok"
DEGRADED = "degraded"
WARN = "warn"


class ReadinessChecker:
    def __init__(
        self,
        *,
        settings: Any,
        db: Any = None,
        qdrant: Any = None,
        embeddings: Any = None,
        pending_queue: Any = None,
        reranker: Any = None,
        rate_limiter: Any = None,
        probe_interval: float = 300.0,
        probe_timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.db = db
        self.qdrant = qdrant
        self.embeddings = embeddings
        self.pending_queue = pending_queue
        # The recall service's CrossEncoderReranker (RET-4): reports whether the
        # model actually loaded, not just whether the package is importable.
        self.reranker = reranker
        # The plan-aware CloudRateLimiter (same storage URI as slowapi's).
        self.rate_limiter = rate_limiter
        self.probe_interval = probe_interval
        self.probe_timeout = probe_timeout
        self._clock = clock
        self._probe_task: asyncio.Task[dict[str, Any]] | None = None
        self._last_probe: dict[str, Any] | None = None
        self._last_probe_at: float | None = None
        self.probe_count = 0

    async def check(self) -> dict[str, Any]:
        components: dict[str, dict[str, Any]] = {
            "sqlite": await self._check_sqlite(),
            "qdrant": await self._check_qdrant(),
            "embeddings": await self._check_embeddings(),
            "llm": self._check_llm(),
            "reranker": self._check_reranker(),
            "pending_embeddings": await self._check_pending(),
            "rate_limit": await self._check_rate_limit(),
        }
        degraded = [name for name, c in components.items() if c.get("status") == DEGRADED]
        return {
            "status": DEGRADED if degraded else OK,
            "degraded_components": degraded,
            "components": components,
        }

    # ------------------------------------------------------------------

    async def _check_sqlite(self) -> dict[str, Any]:
        if self.db is None:
            return {"status": DEGRADED, "reason": "not_initialized"}
        try:
            cursor = await asyncio.wait_for(self.db.conn.execute("SELECT 1"), timeout=2.0)
            await cursor.fetchone()
            version = await self.db.get_schema_version() if hasattr(self.db, "get_schema_version") else None
            return {"status": OK, "schema_version": version}
        except Exception as e:
            return {"status": DEGRADED, "reason": "unreachable", "error_type": type(e).__name__}

    async def _check_qdrant(self) -> dict[str, Any]:
        if self.qdrant is None:
            return {"status": DEGRADED, "reason": "not_initialized"}
        reachable = await self.qdrant.health_check()
        result: dict[str, Any] = {"status": OK if reachable else DEGRADED}
        if not reachable:
            result["reason"] = "unreachable"
        dim = getattr(self.qdrant, "dimension_status", None)
        if isinstance(dim, dict):
            result["collection"] = dim.get("collection")
            result["dimensions"] = {"collection": dim.get("actual"), "embedding": dim.get("expected")}
            if not dim.get("ok", True):
                result["status"] = DEGRADED
                result["reason"] = "dimension_mismatch"
        return result

    async def _check_embeddings(self) -> dict[str, Any]:
        if self.embeddings is None:
            return {"status": DEGRADED, "reason": "not_initialized"}
        result: dict[str, Any] = {
            "provider": getattr(self.embeddings, "provider", None),
            "model": getattr(self.embeddings, "model", None),
        }
        problem = self.embeddings.config_problem()
        if problem:
            return {**result, "status": DEGRADED, "reason": "missing_credentials", "detail": problem}

        breaker = self.embeddings.breaker
        result["breaker"] = {
            "state": breaker.state.value,
            "opened_by_kind": breaker.opened_by_kind,
            "retry_after_seconds": breaker.retry_after(),
            "last_error_kind": breaker.last_error_kind,
            "last_error_at": breaker.last_error_at.isoformat() if breaker.last_error_at else None,
            "last_success_at": breaker.last_success_at.isoformat() if breaker.last_success_at else None,
        }
        if breaker.state.value == "open":
            return {
                **result,
                "status": DEGRADED,
                "reason": breaker.opened_by_kind or "circuit_open",
                "probe": self._probe_view(),
            }

        probe = await self._probe()
        result["probe"] = self._probe_view()
        if probe is not None and not probe.get("ok"):
            result["status"] = DEGRADED
            result["reason"] = probe.get("kind") or "probe_failed"
            return result
        expected = getattr(self.qdrant, "dimension_status", None) or {}
        if probe and probe.get("dimensions") and expected.get("actual") and probe["dimensions"] != expected["actual"]:
            result["status"] = DEGRADED
            result["reason"] = "dimension_mismatch"
            return result
        result["status"] = OK
        return result

    def _probe_view(self) -> dict[str, Any] | None:
        if self._last_probe is None or self._last_probe_at is None:
            return None
        return {**self._last_probe, "age_seconds": round(self._clock() - self._last_probe_at, 1)}

    async def _probe(self) -> dict[str, Any] | None:
        """Cached, single-flight active probe."""
        now = self._clock()
        fresh = self._last_probe_at is not None and now - self._last_probe_at < self.probe_interval
        if fresh:
            return self._last_probe
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.get_running_loop().create_task(self._run_probe(), name="readiness-probe")
        try:
            return await asyncio.wait_for(asyncio.shield(self._probe_task), timeout=self.probe_timeout)
        except TimeoutError:
            return {"ok": False, "kind": "unavailable", "error": "probe_timeout"}

    async def _run_probe(self) -> dict[str, Any]:
        from remembra.storage.embeddings import EmbeddingProviderError

        self.probe_count += 1
        try:
            dims = await self.embeddings.probe()
            result: dict[str, Any] = {"ok": True, "dimensions": dims}
        except EmbeddingProviderError as e:
            result = {"ok": False, "kind": e.kind.value, "circuit_open": e.circuit_open}
        except Exception as e:
            result = {"ok": False, "kind": "unavailable", "error_type": type(e).__name__}
        self._last_probe = result
        self._last_probe_at = self._clock()
        if not result["ok"]:
            log.warning("readiness_probe_failed", **{k: v for k, v in result.items() if k != "ok"})
        return result

    def _check_llm(self) -> dict[str, Any]:
        from remembra.core.circuit_breaker import all_breakers

        breaker = all_breakers().get("llm")
        if breaker is None:
            return {"status": OK, "breaker": None}
        state = breaker.state.value
        return {
            "status": DEGRADED if state == "open" else OK,
            "breaker": {
                "state": state,
                "opened_by_kind": breaker.opened_by_kind,
                "last_error_kind": breaker.last_error_kind,
                "retry_after_seconds": breaker.retry_after(),
            },
        }

    def _check_reranker(self) -> dict[str, Any]:
        enabled = bool(getattr(self.settings, "enable_reranking", False))
        if not enabled:
            return {"status": OK, "enabled": False}
        installed = importlib.util.find_spec("sentence_transformers") is not None
        if not installed:
            return {
                "status": DEGRADED,
                "enabled": True,
                "installed": False,
                "reason": "sentence-transformers not installed (reranking silently skipped)",
            }
        result: dict[str, Any] = {
            "status": OK,
            "enabled": True,
            "installed": True,
            "model": getattr(self.settings, "rerank_model", None),
        }
        if self.reranker is not None and hasattr(self.reranker, "status"):
            state = self.reranker.status()
            result.update(
                state=state.get("state"),
                min_logit=state.get("min_logit"),
                last_error=state.get("last_error"),
                last_run=state.get("last_run"),
            )
            if state.get("state") == "unavailable":
                result["status"] = DEGRADED
                result["reason"] = state.get("last_error") or "model failed to load (reranking skipped)"
        return result

    async def _check_pending(self) -> dict[str, Any]:
        if self.pending_queue is None:
            return {"status": OK, "enabled": False}
        try:
            stats = await self.pending_queue.stats()
        except Exception as e:
            return {"status": WARN, "error_type": type(e).__name__}
        status = WARN if stats.get("failed") else OK
        return {"status": status, **stats}

    async def _check_rate_limit(self) -> dict[str, Any]:
        if not getattr(self.settings, "rate_limit_enabled", False):
            return {"status": OK, "enabled": False}
        if self.rate_limiter is None:
            return {"status": DEGRADED, "enabled": True, "reason": "not_initialized"}
        result: dict[str, Any] = {"enabled": True, "backend": self.rate_limiter.backend}
        try:
            # limits storages are synchronous; keep the ping off the event loop.
            await asyncio.wait_for(asyncio.to_thread(self.rate_limiter.check_backend), timeout=3.0)
        except TimeoutError:
            self.rate_limiter._mark_down(TimeoutError())
        state = self.rate_limiter.status()
        result.update(reachable=state["reachable"], fallback=state["fallback"])
        if not state["reachable"]:
            result["status"] = DEGRADED
            result["reason"] = "unreachable (limits enforced per process from memory)"
            result["error_type"] = state["last_error"]
            return result
        result["status"] = OK
        return result
