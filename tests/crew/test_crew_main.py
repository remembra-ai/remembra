"""WP-8 main.py wiring: the Crew-mode flag mounts the crew routers and registers the crew startup hooks.

The end-to-end test starts a real app lifespan (main DB, memory service, task registry), lets
the real crew hooks open ``crew.db`` (``REMEMBRA_CREW_DB_PATH``), start the event bus and the
outbox worker, and then drives ``/api/v1/crews`` and ``/api/v1/session/close`` over HTTP.
"""

from __future__ import annotations

import hashlib

import sys
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import remembra.config as config_module
import remembra.main as main_module
from remembra.api.router import api_router
from remembra.api.v1 import websocket
from remembra.config import Settings
from remembra.core.limiter import limiter
from remembra.core.tasks import TaskRegistry
from remembra.crew import startup
from remembra.crew.access import _walk_routes
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.crew import wp8_seed as seed
from tests.security_harness import make_settings


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    monkeypatch.setattr(startup, "_HOOKS", dict(startup._HOOKS))
    monkeypatch.setattr(config_module, "_settings", make_settings(auth_enabled=False))
    monkeypatch.setenv("REMEMBRA_CREW_DB_PATH", str(tmp_path / "crew" / "crew.db"))
    monkeypatch.delenv(startup.TAILER_ENV, raising=False)


def _paths(app: FastAPI) -> set[tuple[str, str]]:
    return {(m, path) for path, r, _ in _walk_routes(app.routes) for m in r.methods}


@pytest.mark.parametrize(
    ("value", "enabled"), [("1", True), ("true", True), ("ON", True), ("", False), ("0", False), ("no", False)]
)
def test_crew_mode_flag(monkeypatch, value, enabled):
    monkeypatch.setenv(main_module.CREW_MODE_ENV, value)
    assert main_module.crew_mode_enabled() is enabled


def test_create_app_mounts_crew_routes_only_in_crew_mode(monkeypatch):
    monkeypatch.delenv(main_module.CREW_MODE_ENV, raising=False)
    off = main_module.create_app()
    assert ("GET", "/api/v1/crews") not in _paths(off)
    assert not getattr(off.state, "crew_registered", False)

    monkeypatch.setenv(main_module.CREW_MODE_ENV, "1")
    on = main_module.create_app()
    paths = _paths(on)
    assert {("GET", "/api/v1/crews"), ("POST", "/api/v1/crews/resolve"), ("GET", "/api/v1/crews/{crew_id}/snapshot")} <= paths
    assert on.state.crew_registered is True
    # the crew routes come before the SPA catch-all and after the static /api routes
    order = [path for path, _r, _ in _walk_routes(on.routes)]
    assert order.index("/api/v1/crews/{crew_id}") > order.index("/api/v1/session/close")


def test_install_crew_skips_unbuilt_modules_but_fails_on_a_broken_one(tmp_path, monkeypatch):
    pkg = tmp_path / "crewpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/crew-good')\nasync def g():\n    return {}\n"
    )
    (pkg / "broken.py").write_text("import remembra_module_that_does_not_exist\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        app = FastAPI()
        mounted = main_module.install_crew(app, ("crewpkg.missing", "crewpkg.good"))
        assert mounted == ["crewpkg.good"] and ("GET", "/api/v1/crew-good") in _paths(app)
        with pytest.raises(ModuleNotFoundError):
            main_module.install_crew(FastAPI(), ("crewpkg.broken",))
    finally:
        for name in [n for n in sys.modules if n.startswith("crewpkg")]:
            del sys.modules[name]


def _app(tmp_path: Any) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # what main.py's lifespan provides before the crew hooks start
        app.state.tasks = TaskRegistry()
        db = Database(str(tmp_path / "remembra.db"))
        await db.connect()
        await db.init_schema()
        settings = Settings(openai_api_key="test", enable_entity_resolution=False)
        service = MemoryService(settings=settings, qdrant=FakeQdrant(), db=db, embeddings=FakeEmbeddings())  # type: ignore[arg-type]
        service.extractor = OneFactExtractor()  # type: ignore[assignment]
        app.state.db = db
        app.state.memory_service = service
        app.state.sanitizer = None
        app.state.pii_detector = None
        yield
        await app.state.tasks.shutdown(timeout=2.0)
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.include_router(api_router)
    main_module.install_crew(app)
    return app


def test_crew_mode_end_to_end_through_the_real_startup_hooks(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as http:
        state = app.state
        assert state.crew_db is not None and state.crew_events is not None and state.crew_outbox is not None
        assert (tmp_path / "crew" / "crew.db").exists()

        assert http.get("/api/v1/crews").json() == {"crews": [], "count": 0}
        assert http.post("/api/v1/crews/resolve", json={"project_id": "yaadbooks"}).status_code == 404

        async def world() -> str:
            crew_id = await seed.crew(state.crew_db, "default_user", "yaadbooks")
            await seed.session(
                state.crew_db,
                crew_id,
                "cs_a",
                user_id="default_user",
                callsign="cc-1",
                client_session_id="sess-a",
                token_hash=hashlib.sha256(b"rcs_tok-a").hexdigest(),
            )
            return crew_id

        assert http.portal is not None
        crew_id = http.portal.call(world)
        crews = http.get("/api/v1/crews").json()["crews"]
        assert [c["crew"]["id"] for c in crews] == [crew_id] and crews[0]["live"] == 1

        closed = http.post(
            "/api/v1/session/close",
            json={"agent_id": "claude-code", "session_id": "sess-a", "project_id": "yaadbooks", "facts": {"branch": "main"}},
            headers={"X-Remembra-Crew-Session": "rcs_tok-a"},  # the session token proves the crew session (§11.2)
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["crew"]["session_left"] is True
        events = http.get(f"/api/v1/crews/{crew_id}/events").json()["events"]
        assert [e["type"] for e in events] == ["handoff.created", "session.left"]
        snap = http.get(f"/api/v1/crews/{crew_id}/snapshot").json()
        assert snap["as_of_seq"] == 2 and snap["crew"]["mode"] == "solo"
        brief = http.get("/api/v1/session/brief", params={"project_id": "yaadbooks", "agent_id": "claude-code"}).json()
        assert brief["crew"]["crew_id"] == crew_id and "CREW yaadbooks (solo · 0 live)" in brief["rendered"]
    assert app.state.crew_runtime is None  # hooks stopped with the lifespan
