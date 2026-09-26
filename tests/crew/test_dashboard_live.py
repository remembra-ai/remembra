"""WP-12 live check: the dashboard crew data layer against a real Remembra server.

Starts the real FastAPI app (crew routers and startup hooks, crew.db, event bus,
``/ws``) under uvicorn on a free local port with JWT auth enabled, seeds one
crew for a real dashboard user, and runs
``dashboard/src/lib/crew/__tests__/live.test.ts`` under vitest against it. That
test drives the same code the browser runs: the crew API client, the shared
WebSocket (first-message auth, subscribe, replay after a dropped connection),
the per-crew store and reducer, the crew list with ``crew.summary`` counts, the
polling fallback and the command-palette actions (freeze, pause, request a
checkpoint, post, bypass code), and checks the reduced state against a fresh
server snapshot. Skipped only when the dashboard toolchain is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import remembra.config as config_module
import remembra.main as main_module
from remembra.api.router import api_router
from remembra.api.v1 import websocket
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import RoleManager
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.core.limiter import limiter
from remembra.core.tasks import TaskRegistry
from remembra.crew import startup
from remembra.security.audit import AuditLogger
from remembra.storage.database import Database
from tests.crew import wp8_seed as seed
from tests.security_harness import _StubMemoryService, make_settings

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
VITEST = DASHBOARD / "node_modules" / ".bin" / "vitest"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not VITEST.exists(),
    reason="dashboard toolchain (node + dashboard/node_modules) not installed",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveServer:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.port = _free_port()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app = self._build_app()
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, name="crew-live-server", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _build_app(self) -> FastAPI:
        tmp_path = self.tmp_path
        live = self

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            live.loop = asyncio.get_running_loop()
            app.state.tasks = TaskRegistry()
            db = Database(str(tmp_path / "remembra.db"))
            await db.connect()
            await db.init_schema()
            roles = RoleManager(db)
            await roles.init_schema()
            await UsageMeter(db).init_schema()
            app.state.db = db
            app.state.api_key_manager = APIKeyManager(db)
            app.state.role_manager = roles
            app.state.audit_logger = AuditLogger(db)
            app.state.memory_service = _StubMemoryService(db)
            app.state.sanitizer = None
            app.state.pii_detector = None
            app.state.users = UserManager(db, config_module.get_settings().jwt_secret)
            yield
            await app.state.tasks.shutdown(timeout=2.0)
            await db.close()

        app = FastAPI(lifespan=lifespan)
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
        app.include_router(api_router)
        main_module.install_crew(app)
        app.include_router(websocket.router)  # /ws at the root, as in main.create_app
        return app

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("live server did not start")
            time.sleep(0.05)

    def call(self, coro: Any) -> Any:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=20)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    monkeypatch.setattr(startup, "_HOOKS", dict(startup._HOOKS))
    monkeypatch.setattr(config_module, "_settings", make_settings(auth_enabled=True))
    monkeypatch.setenv("REMEMBRA_CREW_DB_PATH", str(tmp_path / "crew" / "crew.db"))
    monkeypatch.delenv(startup.TAILER_ENV, raising=False)
    server = LiveServer(tmp_path)
    server.start()
    try:
        yield server
    finally:
        server.stop()


async def _seed(app: FastAPI) -> dict[str, str]:
    users: UserManager = app.state.users
    user, error = await users.create_user(email="mani@example.com", password="Str0ng!Passw0rd")
    assert user is not None, error
    token = users.create_jwt_token(user.id, "mani@example.com")
    db = app.state.crew_db
    crew_id = await seed.crew(db, user.id, "yaadbooks")
    await seed.zone(db, crew_id, "zn_pos", "pos", globs=["src/app/pos/**"], title="POS section")
    await seed.zone(db, crew_id, "zn_reports", "reports", globs=["src/app/reports/**"], title="Reports")
    await seed.task(
        db, crew_id, "tsk_1", 1, "POS split tender", status="in_progress", zone_ids=["zn_pos"], owner_session_id="cs_a"
    )
    await seed.session(db, crew_id, "cs_a", user_id=user.id, callsign="cc-1", current_task_id="tsk_1")
    await seed.session(db, crew_id, "cs_b", user_id=user.id, callsign="codex-1", agent_id="codex", adapter_enforcement="advisory")
    await seed.claim(db, crew_id, "clm_pos", holder="cs_a", zone_id="zn_pos", task_id="tsk_1")
    return {"token": token, "crew_id": crew_id}


def test_dashboard_data_layer_against_a_live_server(live_server: LiveServer, tmp_path: Path) -> None:
    seeded = live_server.call(_seed(live_server.app))
    out = tmp_path / "live-report.json"
    env = {
        **os.environ,
        "CI": "1",
        "CREW_LIVE_URL": live_server.url,
        "CREW_LIVE_JWT": seeded["token"],
        "CREW_LIVE_CREW": seeded["crew_id"],
        "CREW_LIVE_OUT": str(out),
    }
    proc = subprocess.run(
        [str(VITEST), "run", "src/lib/crew/__tests__/live.test.ts"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-3000:]
    assert "1 passed" in proc.stdout, proc.stdout[-3000:]  # the live test ran (it is skipped without CREW_LIVE_URL)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["sockets"] == 2  # one shared socket, replaced once after the forced drop
    assert report["messages"] == 1
    assert "human.override" in report["moments"]  # the freeze is a human action: a moment

    # The server's own log agrees with what the dashboard reduced.
    async def last_seq() -> int:
        row = await live_server.app.state.crew_db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (seeded["crew_id"],))
        return int(row["last_seq"])

    assert live_server.call(last_seq()) == report["last_seq"]
