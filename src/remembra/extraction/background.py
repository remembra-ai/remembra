"""Tracked fire-and-forget tasks for the ingest pipeline.

``asyncio.ensure_future`` without a stored reference lets the event loop
garbage-collect a pending task mid-flight. Every background task the store
and recall paths spawn (entity resolution, async enrichment, Jev shadow
evaluation) goes through :func:`spawn`, which delegates to the app-wide
:class:`remembra.core.tasks.TaskRegistry` (REL-16): strong references,
bounded concurrency (``limited=True``), logged failures and graceful
shutdown. A local reference set is kept as well so :func:`drain` can wait for
exactly the tasks spawned here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_TASKS: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
    """Schedule ``coro`` on the app task registry and keep a strong reference until it completes."""
    from remembra.core.tasks import get_task_registry

    try:
        task = get_task_registry().spawn(coro, name=name, limited=True)
    except RuntimeError:
        # The registry is shut down (process stopping) and has closed the
        # coroutine; record the drop instead of failing the caller.
        log.warning("background_task_dropped_on_shutdown", task=name)
        task = asyncio.ensure_future(asyncio.sleep(0))
    _TASKS.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.warning("background_task_failed", task=name, error=str(exc), error_type=type(exc).__name__)

    task.add_done_callback(_done)
    return task


def pending() -> int:
    """Number of background tasks still running."""
    return len(_TASKS)


async def drain(timeout: float = 5.0) -> None:
    """Wait (bounded) for all tracked tasks — used by tests and graceful shutdown."""
    deadline = asyncio.get_running_loop().time() + timeout
    while _TASKS:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.wait(set(_TASKS), timeout=remaining)
