"""Crew startup registry (WP-2 owns it; §14 interface).

``main.py`` (WP-8) calls ``remembra.crew.startup.register(app)`` once, when crew
mode is enabled. Registration wraps the app's lifespan: after the main lifespan
has started (main DB, task registry, …) every registered **hook** starts in
``order``; on shutdown they stop in reverse order, before the main lifespan
tears down.

Other work packages add their background pieces **here**, not in ``main.py``:

* append their module path to :data:`HOOK_MODULES` (one line), and
* in that module define ``register_hooks()`` calling :func:`add_hook` (and call it
  at import time too); :func:`start` calls ``register_hooks()`` on every start, so
  the hooks are present even when the module was imported earlier. WP-1's
  :mod:`remembra.crew.db_hook` opens ``crew.db`` at ``order=0`` (sets
  ``app.state.crew_db``) and runs the outbox worker at ``order=35``; WP-4 adds
  the reaper at ``order=40``.

Built-in hooks (this module):

* ``crew.bus`` (order 10): checks the ``crew_events`` schema, creates the
  ``CrewBus`` (``app.state.crew_bus``) and the ``CrewEventLog``
  (``app.state.crew_events``), and connects the bus to the WebSocket
  ``ConnectionManager``.
* ``crew.tailer`` (order 20): the DB tailer, **only** when enabled
  (``REMEMBRA_CREW_DB_TAILER=1``) for multi-process deployments.
* ``crew.retention`` (order 30): the nightly retention and chain-verify job.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from fastapi import FastAPI

from remembra.crew.bus import CrewBus, CrewEventTailer, db_loader
from remembra.crew.events import CrewDatabase, CrewEventLog, check_schema
from remembra.crew.retention import retention_loop, usage_meter_resolver

log = structlog.get_logger(__name__)

StartFn = Callable[[FastAPI, "CrewRuntime"], Awaitable[None]]
StopFn = Callable[[FastAPI, "CrewRuntime"], Awaitable[None]]

# Modules whose import registers hooks (other WPs add one line each).
HOOK_MODULES: tuple[str, ...] = ("remembra.crew.db_hook", "remembra.crew.notify")

TAILER_ENV: Final = "REMEMBRA_CREW_DB_TAILER"


@dataclass(frozen=True)
class Hook:
    name: str
    order: int
    start: StartFn
    stop: StopFn | None = None


_HOOKS: dict[str, Hook] = {}


def add_hook(name: str, *, order: int, start: StartFn, stop: StopFn | None = None) -> None:
    """Register (or replace, by name) a start/stop pair run inside the app lifespan."""
    _HOOKS[name] = Hook(name, order, start, stop)


def hooks() -> list[Hook]:
    return sorted(_HOOKS.values(), key=lambda h: (h.order, h.name))


@dataclass
class CrewRuntime:
    """What the crew hooks share while the app runs (``app.state.crew_runtime``)."""

    tailer_enabled: bool = False
    bus: CrewBus | None = None
    event_log: CrewEventLog | None = None
    tailer: CrewEventTailer | None = None
    started: list[Hook] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


def tailer_enabled_from_env() -> bool:
    return os.environ.get(TAILER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def crew_db(app: FastAPI) -> CrewDatabase:
    db: CrewDatabase | None = getattr(app.state, "crew_db", None)
    if db is None:
        raise RuntimeError("crew mode: app.state.crew_db is not set (the crew.db hook from WP-1 did not run)")
    return db


def _spawn(app: FastAPI, rt: CrewRuntime, coro: Any, name: str) -> None:
    tasks = getattr(app.state, "tasks", None)
    if tasks is None:
        coro.close()
        raise RuntimeError("crew mode: app.state.tasks (TaskRegistry) is required to run background loops")
    rt.extras[f"task:{name}"] = tasks.spawn(coro, name=name, loop_task=True)


async def _cancel(rt: CrewRuntime, name: str) -> None:
    task: asyncio.Task[Any] | None = rt.extras.pop(f"task:{name}", None)
    if task is None or task.done():
        return
    await asyncio.sleep(0)  # let a just-spawned task start, so its coroutine is closed cleanly on cancel
    task.cancel()
    await asyncio.wait({task}, timeout=5.0)


async def _start_bus(app: FastAPI, rt: CrewRuntime) -> None:
    from remembra.api.v1.websocket import connection_manager

    db = crew_db(app)
    await check_schema(db.conn)
    rt.bus = CrewBus(loader=db_loader(db))
    rt.event_log = CrewEventLog(db, rt.bus)
    app.state.crew_bus = rt.bus
    app.state.crew_events = rt.event_log
    rt.extras["ws_detach"] = connection_manager.attach_crew_bus(rt.bus)


async def _stop_bus(app: FastAPI, rt: CrewRuntime) -> None:
    detach = rt.extras.pop("ws_detach", None)
    if detach is not None:
        detach()
    app.state.crew_bus = None
    app.state.crew_events = None


async def _start_tailer(app: FastAPI, rt: CrewRuntime) -> None:
    if not rt.tailer_enabled:
        log.info("crew_tailer_disabled", reason="publisher runs in this process")
        return
    if rt.bus is None:
        raise RuntimeError("crew.tailer requires crew.bus")
    rt.tailer = CrewEventTailer(crew_db(app), rt.bus)
    await rt.tailer.prime()
    _spawn(app, rt, rt.tailer.run(), "crew-event-tailer")
    log.info("crew_tailer_enabled", from_rowid=rt.tailer.last_rowid)


async def _start_retention(app: FastAPI, rt: CrewRuntime) -> None:
    resolver = usage_meter_resolver(getattr(app.state, "usage_meter", None))
    _spawn(app, rt, retention_loop(crew_db(app), resolver), "crew-retention")


async def _stop_tailer(app: FastAPI, rt: CrewRuntime) -> None:
    await _cancel(rt, "crew-event-tailer")
    rt.tailer = None


async def _stop_retention(app: FastAPI, rt: CrewRuntime) -> None:
    await _cancel(rt, "crew-retention")


add_hook("crew.bus", order=10, start=_start_bus, stop=_stop_bus)
add_hook("crew.tailer", order=20, start=_start_tailer, stop=_stop_tailer)
add_hook("crew.retention", order=30, start=_start_retention, stop=_stop_retention)


async def start(app: FastAPI, *, tailer: bool | None = None) -> CrewRuntime:
    """Import hook modules and start every hook in order. On failure, stop what started and re-raise."""
    for module in HOOK_MODULES:
        mod = importlib.import_module(module)
        register_hooks = getattr(mod, "register_hooks", None)
        if callable(register_hooks):
            register_hooks()
    rt = CrewRuntime(tailer_enabled=tailer_enabled_from_env() if tailer is None else tailer)
    app.state.crew_runtime = rt
    try:
        for hook in hooks():
            await hook.start(app, rt)
            rt.started.append(hook)
            log.info("crew_hook_started", hook=hook.name)
    except BaseException:
        await stop(app)
        raise
    return rt


async def stop(app: FastAPI) -> None:
    rt: CrewRuntime | None = getattr(app.state, "crew_runtime", None)
    if rt is None:
        return
    for hook in reversed(rt.started):
        if hook.stop is None:
            continue
        try:
            await hook.stop(app, rt)
        except Exception as e:
            log.error("crew_hook_stop_failed", hook=hook.name, error_type=type(e).__name__, error=str(e))
    rt.started.clear()
    app.state.crew_runtime = None


def register(app: FastAPI, *, tailer: bool | None = None) -> None:
    """Wrap the app lifespan so crew hooks start after it and stop before its teardown. Idempotent."""
    if getattr(app.state, "crew_registered", False):
        return
    app.state.crew_registered = True
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        async with original(a) as state:
            await start(app, tailer=tailer)
            try:
                yield state
            finally:
                await stop(app)

    app.router.lifespan_context = lifespan
