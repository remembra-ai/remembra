"""A live Remembra API (uvicorn on a local port) for the MCP crew tools (WP-11).

The MCP server is a separate process that reaches the API over HTTP with the caller's own
key, so these tests run the real thing: uvicorn serving the production relay routes and the
crew routers, authentication ENABLED with real API keys and roles, a real main SQLite
database, a real ``crew.db`` with the real event log and bus. Only the vector store and the
embedder are in-process fakes. The MCP tools are called through ``FastMCP.call_tool`` (the
dispatch path an MCP client uses), with the stdio client swapped per agent.
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import remembra.config as config_module
from remembra.api.router import api_router
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import Role, RoleManager
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.config import Settings
from remembra.core.limiter import limiter
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog
from remembra.extraction.extractor import ExtractionOutcome
from remembra.inbox.manager import InboxManager
from remembra.main import CREW_ROUTER_MODULES
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from tests.security_harness import make_settings


class _FakeQdrant:
    async def upsert(self, memory: Any) -> None:
        return None

    async def search(self, **kwargs: Any) -> list[Any]:
        return []

    async def delete_by_project(self, user_id: str, project_id: str) -> int:
        return 0

    async def delete(self, memory_id: str, user_id: str | None = None) -> None:
        return None


class _FakeEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _OneFact:
    async def extract(self, content: str) -> list[str]:
        return [content]

    async def extract_detailed(self, content: str, reference_date: Any = None) -> ExtractionOutcome:
        return ExtractionOutcome(facts=[content], method="llm")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class LiveServer:
    url: str
    app: FastAPI
    loop: asyncio.AbstractEventLoop
    crew: bool
    settings: Settings
    state: dict[str, Any] = field(default_factory=dict)

    def run(self, coro: Any, timeout: float = 30) -> Any:
        """Run a coroutine on the server's event loop (its DB connections live there)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def user(self, email: str) -> str:
        users = UserManager(self.app.state.db, self.settings.jwt_secret)

        async def _make() -> str:
            user, error = await users.create_user(email=email, password="Str0ng!Passw0rd")
            assert user is not None, error
            return str(user.id)

        return str(self.run(_make()))

    def key(self, user_id: str, role: str = "admin", *, agent_id: str | None = None) -> str:
        async def _make() -> str:
            created = await self.app.state.api_key_manager.create_key(user_id=user_id, name=f"{role}-key", agent_id=agent_id)
            await self.app.state.role_manager.assign_role(created.id, Role(role))
            return str(created.key)

        return str(self.run(_make()))

    def jwt(self, user_id: str, email: str) -> dict[str, str]:
        users = UserManager(self.app.state.db, self.settings.jwt_secret)
        return {"Authorization": f"Bearer {users.create_jwt_token(user_id, email)}"}

    def api(self, method: str, path: str, *, headers: dict[str, str], status: int | tuple[int, ...] = 200, **kw: Any) -> Any:
        res = httpx.request(method, f"{self.url}/api/v1{path}", headers=headers, timeout=30, **kw)
        wanted = status if isinstance(status, tuple) else (status,)
        assert res.status_code in wanted, f"{method} {path}: {res.status_code} {res.text}"
        return res.json() if res.content else None

    def crew_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        db: CrewDatabase = self.app.state.crew_db

        async def _q() -> list[dict[str, Any]]:
            return [dict(r) for r in await db.fetchall(sql, params)]

        return list(self.run(_q()))


@contextmanager
def live_server(tmp_path: Any, *, crew: bool = True) -> Iterator[LiveServer]:
    settings = make_settings()
    previous = config_module._settings
    config_module._settings = settings
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        db = Database(str(tmp_path / "main.db"))
        await db.connect()
        await db.init_schema()
        roles = RoleManager(db)
        await roles.init_schema()
        await UsageMeter(db).init_schema()
        inbox = InboxManager(db)
        await inbox.init_schema()
        service = MemoryService(
            settings=Settings(openai_api_key="test", enable_entity_resolution=False),
            qdrant=_FakeQdrant(),  # type: ignore[arg-type]
            db=db,
            embeddings=_FakeEmbeddings(),  # type: ignore[arg-type]
        )
        service.extractor = _OneFact()  # type: ignore[assignment]
        app.state.db = db
        app.state.api_key_manager = APIKeyManager(db)
        app.state.role_manager = roles
        app.state.audit_logger = AuditLogger(db)
        app.state.memory_service = service
        app.state.inbox_manager = inbox
        app.state.sanitizer = ContentSanitizer()
        app.state.pii_detector = None
        crew_db = None
        if crew:
            crew_db = CrewDatabase(str(tmp_path / "crew.db"))
            await crew_db.init_schema()
            bus = CrewBus(loader=db_loader(crew_db))
            app.state.crew_db = crew_db
            app.state.crew_bus = bus
            app.state.crew_events = CrewEventLog(crew_db, bus)
        holder["loop"] = asyncio.get_running_loop()
        yield
        if crew_db is not None:
            await crew_db.close()
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.include_router(api_router)
    if crew:
        for name in CREW_ROUTER_MODULES:
            app.include_router(importlib.import_module(name).router, prefix="/api/v1")

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        assert time.time() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield LiveServer(url=f"http://127.0.0.1:{port}", app=app, loop=holder["loop"], crew=crew, settings=settings)
    finally:
        srv.should_exit = True
        thread.join(10)
        config_module._settings = previous
