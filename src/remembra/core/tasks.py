"""Tracked background tasks (REL-16).

``asyncio.create_task``/``ensure_future`` results that nobody holds can be
garbage-collected mid-flight, have unbounded concurrency, swallow exceptions,
and are silently dropped on shutdown. ``TaskRegistry`` fixes all four:

* strong references until completion;
* optional concurrency bound (``limited=True`` acquires a semaphore slot);
* exceptions are logged with the task name;
* ``shutdown()`` cancels long-running loops immediately, gives one-shot work a
  grace period to finish, then cancels whatever is left.

The app creates one registry in its lifespan and publishes it via
:func:`set_task_registry`; library code uses :func:`get_task_registry`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

from remembra.core.metrics import BACKGROUND_TASKS

log = structlog.get_logger(__name__)


class TaskRegistry:
    def __init__(self, max_concurrency: int = 16) -> None:
        self._max_concurrency = max(1, max_concurrency)
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: dict[asyncio.Task[Any], bool] = {}  # task -> is_loop
        self._closed = False

    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    @property
    def running(self) -> int:
        return len(self._tasks)

    def names(self) -> list[str]:
        return sorted(t.get_name() for t in self._tasks)

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        limited: bool = False,
        loop_task: bool = False,
    ) -> asyncio.Task[Any]:
        """Start ``coro`` as a tracked task.

        Args:
            name: task name (shows up in logs and ``names()``).
            limited: wait for a concurrency slot before running (one-shot work
                like enrichment); loops should not be limited.
            loop_task: a never-ending loop — cancelled first on shutdown.
        """
        if self._closed:
            coro.close()
            raise RuntimeError("TaskRegistry is shut down")

        async def _runner() -> Any:
            if limited:
                async with self._sem():
                    return await coro
            return await coro

        task = asyncio.get_running_loop().create_task(_runner(), name=name)
        self._tasks[task] = loop_task
        BACKGROUND_TASKS.set(len(self._tasks))
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(task, None)
        BACKGROUND_TASKS.set(len(self._tasks))
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "background_task_failed",
                task=task.get_name(),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Stop accepting work, cancel loops, drain one-shot tasks, cancel stragglers."""
        self._closed = True
        loops = [t for t, is_loop in self._tasks.items() if is_loop]
        for t in loops:
            t.cancel()
        pending = list(self._tasks)
        if not pending:
            return
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        for t in still_running:
            log.warning("background_task_cancelled_on_shutdown", task=t.get_name())
            t.cancel()
        if still_running:
            await asyncio.wait(still_running, timeout=2.0)
        log.info("background_tasks_shutdown", finished=len(done), cancelled=len(still_running))


_registry: TaskRegistry | None = None


def set_task_registry(registry: TaskRegistry | None) -> None:
    global _registry
    _registry = registry


def get_task_registry() -> TaskRegistry:
    """The app's registry; a private fallback registry outside the app (scripts/tests)."""
    global _registry
    if _registry is None:
        _registry = TaskRegistry()
    return _registry
