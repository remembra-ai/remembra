"""Tracked fire-and-forget tasks for the ingest pipeline.

``asyncio.ensure_future`` without a stored reference lets the event loop
garbage-collect a pending task mid-flight. Every background task the store
path spawns (entity resolution, async enrichment, Jev shadow evaluation) goes
through :func:`spawn` so a strong reference is held until it finishes and
failures are logged instead of vanishing.

The reliability stream (REL-16) is building an app-wide task registry in
``remembra.core``; when it lands, :func:`spawn` should delegate to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_TASKS: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
    """Schedule ``coro`` and keep a strong reference until it completes."""
    task = asyncio.ensure_future(coro)
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
