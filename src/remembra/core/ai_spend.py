"""Per-request AI spend tracking (smart-credit settlement).

A :class:`SpendJob` is opened by the API layer when a write is granted
enrichment (a credit reservation was taken). It is published through a
context variable, so every LLM call made while serving that write — in the
request itself or in background tasks spawned from it (asyncio copies the
context into new tasks) — reports its real cost with
:func:`record_llm_usage`.

The job counts its *holders*: the request itself plus every background task
spawned through the enrichment queue or ``extraction.background.spawn``. When the last holder finishes, the job settles
exactly once: the settle callback receives the actual dollars spent, so the
reservation can be reconciled and the unused part refunded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SettleCallback = Callable[[float, bool], Awaitable[Any]]

_current: ContextVar[SpendJob | None] = ContextVar("remembra_spend_job", default=None)

# Process-wide totals (observability; not used for billing).
_totals: dict[str, float] = {"usd": 0.0, "calls": 0.0, "untracked_usd": 0.0}


class SpendJob:
    """AI spend attributed to one metered write."""

    def __init__(
        self,
        *,
        user_id: str,
        settle: SettleCallback | None = None,
        concurrency: int | None = None,
        label: str = "write",
    ) -> None:
        self.user_id = user_id
        self.concurrency = concurrency
        self.label = label
        self.usd = 0.0
        self.llm_calls = 0
        self.enriched = True
        self.settled_result: Any = None
        self._settle_cb = settle
        self._holders = 1  # the opener (the request); released by activate()
        self._closed = False
        self._done = asyncio.Event()

    # -- spend ----------------------------------------------------------
    def add(self, usd: float) -> None:
        self.usd += max(0.0, usd)
        self.llm_calls += 1

    # -- lifetime -------------------------------------------------------
    @property
    def settled(self) -> bool:
        return self._done.is_set()

    def acquire(self) -> None:
        """Register one more piece of work (a background task) that must finish first."""
        if self._closed:
            # Work started after settlement: its spend can no longer be billed
            # through this reservation. Should not happen; keep it visible.
            log.warning("spend_job_acquire_after_settle", user_id=self.user_id, label=self.label)
            return
        self._holders += 1

    def release(self) -> None:
        if self._closed:
            return
        self._holders -= 1
        if self._holders <= 0:
            self._closed = True
            self._schedule_settle()

    def _schedule_settle(self) -> None:
        coro = self._settle()
        try:
            from remembra.core.tasks import get_task_registry

            get_task_registry().spawn(coro, name="credit_settle")
        except RuntimeError:
            # Registry shut down: settle inline on the running loop.
            asyncio.get_running_loop().create_task(coro)

    async def _settle(self) -> None:
        try:
            if self._settle_cb is not None:
                self.settled_result = await self._settle_cb(self.usd, self.enriched)
        except Exception as e:  # never lose the error; the reservation expires if this failed
            log.error("spend_job_settle_failed", user_id=self.user_id, error=str(e), error_type=type(e).__name__)
        finally:
            self._done.set()

    async def wait_settled(self, timeout: float = 10.0) -> Any:
        """Wait until the job settled (tests, graceful shutdown). Returns the settle result."""
        await asyncio.wait_for(self._done.wait(), timeout=timeout)
        return self.settled_result


def current_job() -> SpendJob | None:
    return _current.get()


@contextmanager
def activate(job: SpendJob | None) -> Iterator[SpendJob | None]:
    """Publish ``job`` for the duration of the block; release the opener's hold on exit.

    If the block raises, the job is marked not-enriched: only the dollars
    actually spent are charged, not the chunk minimum.
    """
    if job is None:
        yield None
        return
    token = _current.set(job)
    try:
        yield job
    except BaseException:
        job.enriched = False
        raise
    finally:
        _current.reset(token)
        job.release()


def record_llm_usage(response: Any, model: str) -> float:
    """Attribute the cost of one chat completion to the current job. Returns the dollars."""
    from remembra.cloud.model_prices import usd_for

    usd = usd_for(getattr(response, "usage", None), model)
    _totals["usd"] += usd
    _totals["calls"] += 1
    job = _current.get()
    if job is not None:
        job.add(usd)
    elif usd > 0:
        _totals["untracked_usd"] += usd
    return usd


def totals() -> dict[str, float]:
    """Process-wide AI spend observed since start (for /metrics-style views)."""
    return dict(_totals)
