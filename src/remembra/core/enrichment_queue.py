"""Bounded, per-tenant enrichment queue.

Background enrichment (async fact extraction, entity resolution) used to be
spawned one task per stored fact with only a process-wide cap, so a single
tenant storing a burst could hold every slot and drain the organisation's
OpenAI rate limit and credits for everyone. This queue bounds it three ways:

* **per-tenant concurrency** — each account runs at most N jobs at once
  (its plan's ``enrichment_concurrency``: Free 2, Solo 4, Pro/Team 8);
* **global concurrency** — at most ``enrichment_global_concurrency`` jobs
  run across all tenants;
* **per-tenant backlog** — at most ``enrichment_max_pending_per_tenant`` jobs
  may be queued or running for one tenant. Beyond it, *droppable* work
  (entity linking) is skipped — the memory itself is already stored — while
  non-droppable work (the async extraction of a stored source) still queues.

Jobs run as tracked tasks on the app :class:`~remembra.core.tasks.TaskRegistry`
and hold the current :class:`~remembra.core.ai_spend.SpendJob` open until they
finish, so a credit reservation settles only after its background work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import structlog

from remembra.core import ai_spend

log = structlog.get_logger(__name__)


@dataclass
class _Tenant:
    semaphore: asyncio.Semaphore
    size: int
    pending: int = 0
    running: int = 0


class EnrichmentQueue:
    def __init__(self, *, global_concurrency: int = 16, default_concurrency: int = 4, max_pending_per_tenant: int = 200) -> None:
        self.global_concurrency = max(1, global_concurrency)
        self.default_concurrency = max(1, default_concurrency)
        self.max_pending_per_tenant = max(1, max_pending_per_tenant)
        self._global: asyncio.Semaphore | None = None
        self._global_loop: asyncio.AbstractEventLoop | None = None
        self._tenants: dict[str, _Tenant] = {}
        self.dropped = 0
        self._tasks: set[asyncio.Task[Any]] = set()

    def _global_sem(self) -> asyncio.Semaphore:
        # One semaphore per event loop (a process normally has one; tests have many).
        loop = asyncio.get_running_loop()
        if self._global is None or self._global_loop is not loop:
            self._global = asyncio.Semaphore(self.global_concurrency)
            self._global_loop = loop
        return self._global

    def _tenant(self, tenant_id: str, concurrency: int) -> _Tenant:
        state = self._tenants.get(tenant_id)
        if state is None:
            state = _Tenant(semaphore=asyncio.Semaphore(concurrency), size=concurrency)
            self._tenants[tenant_id] = state
        return state

    def pending(self, tenant_id: str) -> int:
        state = self._tenants.get(tenant_id)
        return state.pending if state else 0

    def running(self, tenant_id: str | None = None) -> int:
        if tenant_id is not None:
            state = self._tenants.get(tenant_id)
            return state.running if state else 0
        return sum(s.running for s in self._tenants.values())

    def submit(
        self,
        tenant_id: str,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        concurrency: int | None = None,
        droppable: bool = True,
    ) -> asyncio.Task[Any] | None:
        """Queue ``coro`` for ``tenant_id``. Returns the task, or None if it was dropped."""
        size = max(1, concurrency or self.default_concurrency)
        state = self._tenant(tenant_id, size)
        if droppable and state.pending >= self.max_pending_per_tenant:
            coro.close()
            self.dropped += 1
            log.warning("enrichment_dropped_backlog_full", tenant=tenant_id, task=name, pending=state.pending)
            return None

        state.pending += 1
        job = ai_spend.current_job()
        if job is not None:
            job.acquire()  # the reservation settles only after this work

        async def _run() -> Any:
            try:
                async with state.semaphore, self._global_sem():
                    state.running += 1
                    try:
                        return await coro
                    finally:
                        state.running -= 1
            finally:
                self._finish(tenant_id, state)
                if job is not None:
                    job.release()

        from remembra.core.tasks import get_task_registry

        try:
            task = get_task_registry().spawn(_run(), name=name)
        except RuntimeError:
            # Registry shut down (process stopping): _run() was closed unstarted.
            coro.close()
            self._finish(tenant_id, state)
            if job is not None:
                job.release()
            log.warning("enrichment_dropped_on_shutdown", tenant=tenant_id, task=name)
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def active_tasks(self) -> set[asyncio.Task[Any]]:
        """Queued or running jobs (used by drain in tests and shutdown)."""
        return set(self._tasks)

    def _finish(self, tenant_id: str, state: _Tenant) -> None:
        state.pending -= 1
        # Forget idle tenants so a plan change resizes their semaphore.
        if state.pending == 0 and self._tenants.get(tenant_id) is state:
            del self._tenants[tenant_id]


_queue: EnrichmentQueue | None = None


def set_enrichment_queue(queue: EnrichmentQueue | None) -> None:
    global _queue
    _queue = queue


def get_enrichment_queue() -> EnrichmentQueue:
    """The process-wide queue, built from settings on first use."""
    global _queue
    if _queue is None:
        from remembra.config import get_settings

        s = get_settings()
        _queue = EnrichmentQueue(
            global_concurrency=s.enrichment_global_concurrency,
            default_concurrency=s.enrichment_default_concurrency,
            max_pending_per_tenant=s.enrichment_max_pending_per_tenant,
        )
    return _queue
