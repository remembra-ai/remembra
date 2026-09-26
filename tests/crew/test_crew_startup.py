"""WP-2 startup registry: lifespan wrapping, hook order, bus → WebSocket wiring, tailer and retention loops."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import remembra.config as config_module
from remembra.api.v1 import websocket
from remembra.core.tasks import TaskRegistry
from remembra.crew import startup
from remembra.crew.events import Actor, CrewEventLog
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


def make_app(tmp_path, *, with_crew_db=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.tasks = TaskRegistry()
        db = None
        if with_crew_db:
            db = await open_crew_db(tmp_path)
            await seed_crew(db, CREW_A, owner="default_user")
            app.state.crew_db = db
        yield
        await app.state.tasks.shutdown(timeout=2.0)
        if db is not None:
            await db.close()

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
        assert [h.name for h in rt.started] == ["crew.bus", "crew.tailer", "crew.retention"]
        assert rt.tailer is None and "crew-retention" in app.state.tasks.names()
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


async def test_missing_crew_db_fails_startup_loudly(tmp_path):
    app = make_app(tmp_path, with_crew_db=False)
    with pytest.raises(RuntimeError, match="crew_db is not set"), TestClient(app):
        pass


async def test_hook_modules_order_and_rollback_on_failure(tmp_path, monkeypatch):
    sys.modules.pop("tests.crew.startup_hook_fixture", None)
    monkeypatch.setattr(startup, "HOOK_MODULES", ("tests.crew.startup_hook_fixture",))
    app = make_app(tmp_path)
    with TestClient(app):
        from tests.crew import startup_hook_fixture as fixture

        rt = app.state.crew_runtime
        assert [h.name for h in rt.started] == ["crew.bus", "crew.tailer", "crew.retention", "test.fixture"]
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
