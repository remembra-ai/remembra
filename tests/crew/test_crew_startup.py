"""WP-2 startup registry: lifespan wrapping, hook order, bus → WebSocket wiring, tailer and retention loops."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import remembra.config as config_module
from remembra.api.v1 import websocket
from remembra.core.tasks import TaskRegistry
from remembra.crew import db_hook, startup
from remembra.crew.events import Actor, CrewEventLog
from remembra.crew.store import CrewStore
from remembra.storage.database import Database
from tests.crew.crewdb import CREW_A, open_crew_db, seed_crew, state_changed
from tests.crew.test_ws_crew import recv, recv_type, sub
from tests.security_harness import make_settings


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    monkeypatch.setattr(startup, "_HOOKS", dict(startup._HOOKS))
    monkeypatch.setattr(config_module, "_settings", make_settings(auth_enabled=False))
    monkeypatch.delenv(startup.TAILER_ENV, raising=False)


class StubMemoryService:
    """What the outbox's memory-promotion handler calls: ``store`` (and ``settings`` for the TTL default)."""

    def __init__(self) -> None:
        self.stored: list = []
        self.settings = SimpleNamespace(checkpoint_default_ttl="7d")

    async def store(self, request, **kwargs):
        self.stored.append(request)
        return SimpleNamespace(id=f"mem_{len(self.stored)}", status="stored")


def make_app(tmp_path, *, with_crew_db=True, with_main=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.tasks = TaskRegistry()
        main = None
        if with_main:  # what main.py's lifespan provides before crew hooks start
            main = Database(str(tmp_path / "remembra.db"))
            await main.connect()
            await main.init_schema()
            app.state.db = main
            app.state.memory_service = StubMemoryService()
        db = None
        if with_crew_db:
            db = await open_crew_db(tmp_path)
            await seed_crew(db, CREW_A, owner="default_user")
            app.state.crew_db = db
        yield
        await app.state.tasks.shutdown(timeout=2.0)
        if db is not None:
            await db.close()
        if main is not None:
            await main.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(websocket.router)
    startup.register(app)
    startup.register(app)  # idempotent
    return app


def _emit_kwargs():
    return dict(crew_id=CREW_A, type="session.state_changed", actor=Actor.system(), payload=state_changed(), summary="x")


async def test_lifespan_starts_hooks_wires_bus_to_websocket_and_stops_cleanly(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        rt = app.state.crew_runtime
        assert [h.name for h in rt.started] == [
            "crew.db",
            "crew.bus",
            "crew.tailer",
            "crew.retention",
            "crew.outbox",
            "crew.reaper",
            "crew.claims",
            "crew.notify",
            "crew.report_invariant",
        ]
        assert "task:crew-report-invariant" in rt.extras  # WP-6 nightly invariant job is running
        assert rt.tailer is None and "crew-retention" in app.state.tasks.names()
        assert "crew-reaper" in app.state.tasks.names()  # WP-4 reaper, registered through HOOK_MODULES
        assert "crew-event-tailer" not in app.state.tasks.names()
        retention_task = rt.extras["task:crew-retention"]
        with client.websocket_connect("/ws") as ws:
            recv_type(ws, "connected")
            ws.send_text(sub())
            assert recv(ws)["type"] == "crew.subscribed"
            # a service emitting through app.state.crew_events reaches the subscriber after COMMIT
            result = client.portal.call(lambda: app.state.crew_events.emit(**_emit_kwargs()))
            frame = recv(ws)
            assert frame["type"] == "crew.event" and frame["data"] == result.envelope
    assert app.state.crew_runtime is None and app.state.crew_bus is None
    assert retention_task.cancelled()
    assert not rt.bus._listeners  # the WebSocket listener was detached


async def test_register_opens_crew_db_and_runs_the_outbox_with_no_manual_state(tmp_path, monkeypatch):
    """Production path: nothing sets app.state.crew_db; crew.db opens next to the main database."""
    monkeypatch.setattr(
        config_module, "_settings", make_settings(auth_enabled=False, database_url=f"sqlite:///{tmp_path}/remembra.db")
    )
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    app = make_app(tmp_path, with_crew_db=False)
    with TestClient(app) as client:
        db = app.state.crew_db
        assert db is not None and db.db_path == str(tmp_path / "crew.db")
        assert (tmp_path / "crew.db").exists() and client.portal.call(db.get_schema_version) == 1
        worker = app.state.crew_outbox
        assert worker.running and {"memory_promotion", "relay_handoff", "crew_notify"} <= set(worker.handlers)
        screen, scrub = db_hook._relay_filters(app)  # the relay routes' filters, read from app.state
        assert screen("handoff text")[0] == "handoff text" and scrub is None

        async def promote():
            await seed_crew(db, CREW_A, owner="default_user")
            store = CrewStore(db)
            async with db.transaction():
                oid = await store.enqueue_outbox(
                    CREW_A,
                    "memory_promotion",
                    {"user_id": "default_user", "project_id": "yaadbooks", "memory_type": "checkpoint", "content": "T-14 done"},
                )
            worker.wake()
            for _ in range(200):
                row = await store.get_outbox(oid)
                if row["state"] != "pending":
                    return row
                await asyncio.sleep(0.01)
            return row

        row = client.portal.call(promote)
        assert row["state"] == "done", row
        stored = app.state.memory_service.stored
        assert len(stored) == 1 and stored[0].content == "T-14 done" and stored[0].metadata["crew_outbox_id"] == row["id"]
    assert app.state.crew_db is None and not worker.running  # closed by the hook that opened it
    assert db.is_connected is False


async def test_an_app_provided_crew_db_is_kept_and_not_closed(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app):
        provided = app.state.crew_db
        assert app.state.crew_runtime.extras["crew_db_owned"] is False
    assert provided.is_connected is False  # closed by the app's own lifespan, after the crew hooks stopped


async def test_outbox_without_the_main_database_fails_startup_loudly(tmp_path):
    app = make_app(tmp_path, with_main=False)
    with pytest.raises(RuntimeError, match="needs app.state.db and app.state.memory_service"), TestClient(app):
        pass
    assert app.state.crew_runtime is None


async def test_hook_modules_order_and_rollback_on_failure(tmp_path, monkeypatch):
    sys.modules.pop("tests.crew.startup_hook_fixture", None)
    monkeypatch.setattr(startup, "HOOK_MODULES", ("tests.crew.startup_hook_fixture",))
    # only the built-in hooks, whether or not remembra.crew.db_hook was imported earlier in the run
    monkeypatch.setattr(
        startup,
        "_HOOKS",
        {k: v for k, v in startup._HOOKS.items() if k.startswith(("crew.bus", "crew.tailer", "crew.retention"))},
    )
    app = make_app(tmp_path)
    with TestClient(app):
        from tests.crew import startup_hook_fixture as fixture

        rt = app.state.crew_runtime
        assert [h.name for h in rt.started] == ["crew.bus", "crew.tailer", "crew.retention", "test.fixture"]  # db_hook not listed
        assert rt.extras["fixture_saw_bus"] is True
    assert fixture.CALLS == ["fixture.start", "fixture.stop"]

    async def boom(app, rt):
        raise RuntimeError("hook failed")

    startup.add_hook("test.boom", order=50, start=boom)
    app2 = make_app(tmp_path / "second")
    (tmp_path / "second").mkdir()
    with pytest.raises(RuntimeError, match="hook failed"), TestClient(app2):
        pass
    # everything that started was stopped again
    assert app2.state.crew_runtime is None and app2.state.crew_bus is None
    assert fixture.CALLS[-1] == "fixture.stop"


async def test_tailer_enabled_delivers_events_written_by_another_connection(tmp_path, monkeypatch):
    monkeypatch.setenv(startup.TAILER_ENV, "1")
    app = make_app(tmp_path)
    with TestClient(app) as client:
        rt = app.state.crew_runtime
        assert rt.tailer is not None and "crew-event-tailer" in app.state.tasks.names()
        rt.tailer.interval_s = 0.05
        with client.websocket_connect("/ws") as ws:
            recv_type(ws, "connected")
            ws.send_text(sub())
            recv(ws)

            async def write_elsewhere():
                other = Database(str(tmp_path / "crew.db"))
                await other.connect()
                try:
                    return await CrewEventLog(other).emit(**_emit_kwargs())  # no in-process bus
                finally:
                    await other.close()

            result = client.portal.call(write_elsewhere)
            frame = recv(ws)
            assert frame["data"]["seq"] == result.seq == 1
            # the in-process publisher and the tailer both see the next event; it arrives once
            client.portal.call(lambda: app.state.crew_events.emit(**_emit_kwargs()))
            assert recv(ws)["data"]["seq"] == 2
            ws.send_text("ping")
            assert recv(ws) == "pong"  # no duplicate seq 2 before the pong
