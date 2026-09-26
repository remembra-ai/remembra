"""Crew startup hooks owned by WP-1: open ``crew.db`` and run the cross-database outbox worker.

Registered through :mod:`remembra.crew.startup` (listed in ``HOOK_MODULES``; §14 interface):

* ``crew.db`` (order 0): opens ``crew.db`` next to the main database
  (:func:`remembra.crew.db.open_crew_db` on ``settings.database_url``, or
  ``$REMEMBRA_CREW_DB_PATH``), applies ``CREW_MIGRATIONS``, sets
  ``app.state.crew_db`` and attaches it to the account eraser
  (``app.state.account_eraser``, with :data:`remembra.crew.erasure.CREW_ERASURE_RULES`);
  detaches and closes it on shutdown. An app that already set
  ``app.state.crew_db`` (tests, an embedding host) keeps its own database and
  this hook neither replaces nor closes it.
* ``crew.outbox`` (order 35): starts :class:`remembra.crew.outbox.CrewOutboxWorker`
  over ``crew.db`` with the production handlers (memory promotions through
  ``app.state.memory_service``, relay handoffs through ``RelayService`` on
  ``app.state.db``) and exposes it as ``app.state.crew_outbox`` so producers can
  ``wake()`` it after enqueueing; stops it on shutdown. It needs the main
  lifespan's ``db`` and ``memory_service`` and fails startup loudly without them,
  because queued effects would otherwise stay ``pending`` forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

from remembra.crew import startup
from remembra.crew.db import CrewDatabase, open_crew_db
from remembra.crew.outbox import CrewOutboxWorker, default_handlers
from remembra.crew.store import CrewStore

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

log = structlog.get_logger(__name__)

CREW_DB_ORDER: Final = 0
OUTBOX_ORDER: Final = 35
_OWNED: Final = "crew_db_owned"
_WORKER: Final = "crew_outbox_worker"


def _attach_to_eraser(app: FastAPI, db: Any) -> None:
    """Account erasure (R-23) covers ``crew.db`` with its own rules (:mod:`remembra.crew.erasure`)."""
    eraser = getattr(app.state, "account_eraser", None)
    if eraser is None:
        return
    from remembra.crew.erasure import crew_extra_database

    eraser.attach(crew_extra_database(db))
    log.info("crew_db_erasure_attached")


async def _start_crew_db(app: FastAPI, rt: startup.CrewRuntime) -> None:
    if getattr(app.state, "crew_db", None) is not None:
        rt.extras[_OWNED] = False
        log.info("crew_db_provided_by_app")
        _attach_to_eraser(app, app.state.crew_db)
        return
    from remembra.config import get_settings

    db = await open_crew_db(get_settings().database_url)
    app.state.crew_db = db
    rt.extras[_OWNED] = True
    _attach_to_eraser(app, db)


async def _stop_crew_db(app: FastAPI, rt: startup.CrewRuntime) -> None:
    eraser = getattr(app.state, "account_eraser", None)
    if eraser is not None:
        eraser.detach("crew")
    if not rt.extras.pop(_OWNED, False):
        return
    db: CrewDatabase | None = getattr(app.state, "crew_db", None)
    app.state.crew_db = None
    if db is not None:
        await db.close()


def _relay_filters(app: FastAPI) -> tuple[Any, Any]:
    """The relay close-out filters the /relay routes use (sanitizer screen, per-value PII scrub).

    Both helpers only read ``request.app.state``, so a stand-in carrying the app is enough.
    """
    from remembra.api.v1.agent_session import screen_text
    from remembra.api.v1.relay import pii_scrubber

    request = cast("Request", SimpleNamespace(app=app))
    return (lambda text: screen_text(request, text, apply_pii=False)), pii_scrubber(request)


async def _start_outbox(app: FastAPI, rt: startup.CrewRuntime) -> None:
    from remembra.services.relay import RelayService

    main_db = getattr(app.state, "db", None)
    memory_service = getattr(app.state, "memory_service", None)
    if main_db is None or memory_service is None:
        raise RuntimeError(
            "crew mode: the outbox worker needs app.state.db and app.state.memory_service (set by the main lifespan)"
        )
    screen, scrub = _relay_filters(app)
    crew = startup.crew_db(app)
    if not isinstance(crew, CrewDatabase):
        raise RuntimeError("crew mode: app.state.crew_db must be a remembra.crew.db.CrewDatabase for the outbox worker")
    from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS, crew_limits_for_owner

    async def limits_for(owner_user_id: str) -> Any:
        meter = getattr(app.state, "usage_meter", None)
        return await crew_limits_for_owner(meter, owner_user_id) if meter is not None else SELF_HOSTED_CREW_LIMITS

    store = CrewStore(crew)
    handlers = default_handlers(
        main_db=main_db,
        memory_service=memory_service,
        relay_service=RelayService(db=main_db, memory_service=memory_service),
        screen=screen,
        scrub=scrub,
        crew_store=store,
        limits_for=limits_for,
        app=app,
    )
    worker = CrewOutboxWorker(store, handlers)
    worker.start()
    rt.extras[_WORKER] = worker
    app.state.crew_outbox = worker
    log.info("crew_outbox_worker_started", kinds=sorted(handlers))


async def _stop_outbox(app: FastAPI, rt: startup.CrewRuntime) -> None:
    worker: CrewOutboxWorker | None = rt.extras.pop(_WORKER, None)
    app.state.crew_outbox = None
    if worker is not None:
        await worker.stop()


def register_hooks() -> None:
    """Add (or re-add, by name) the ``crew.db`` and ``crew.outbox`` hooks. Called by ``startup.start``."""
    startup.add_hook("crew.db", order=CREW_DB_ORDER, start=_start_crew_db, stop=_stop_crew_db)
    startup.add_hook("crew.outbox", order=OUTBOX_ORDER, start=_start_outbox, stop=_stop_outbox)


register_hooks()
