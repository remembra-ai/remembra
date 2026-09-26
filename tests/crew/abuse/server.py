"""The red-team target: the real Remembra app with Crew mode on, served by uvicorn on 127.0.0.1.

This is the staging stand-in for the §13.6 suite. It is the production router tree
(``api_router``), mounted with ``main.install_crew`` so every crew router and every crew
startup hook runs (``crew.db``, event bus, outbox worker, reaper, retention, notification
dispatcher), plus ``/ws``. Authentication is on (API keys and dashboard JWTs through the
real auth chain). With ``rate_limits=True`` the real limiters are on: the crew
per-session/per-user/per-host buckets and the app's slowapi per-user limiter.

Real-time alerts go to a signed-webhook target that a human adds through
``POST /notifications/targets``; the network edge of the real ``WebhookSender`` is an
in-process receiver that answers the signed challenge and records every delivery
(``Alerts``). Nothing listens beyond 127.0.0.1 and every credential here is a test
value for a throwaway server.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, TypeVar

import httpx
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
from remembra.auth.rbac import Role, RoleManager
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.core.limiter import limiter
from remembra.core.tasks import TaskRegistry
from remembra.crew import limits as crew_limits
from remembra.crew import startup
from remembra.crew.notify import WebhookSender, verify_signature
from remembra.inbox.manager import InboxManager
from remembra.config import Settings
from remembra.security.audit import AuditLogger
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from remembra.webhooks.manager import ResolvedTarget
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.security_harness import make_settings

T = TypeVar("T")

API = "/api/v1"
OWNER_EMAIL = "owner@example.com"  # in make_settings().owner_emails
OWNER_PASSWORD = "Str0ng!Passw0rd"  # test value for the throwaway server
WEBHOOK_URL = "https://alerts.redteam.test/hook"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Alerts:
    """The webhook receiver: answers the signed challenge, records every signed delivery."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[tuple[float, httpx.Request]] = []
        self.secret: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append((time.time(), request))
        body = json.loads(request.content)
        if body.get("type") == "crew.notification.challenge":
            return httpx.Response(200, json={"challenge": body["challenge"]})
        return httpx.Response(200, json={"ok": True})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def deliveries(self) -> list[tuple[float, dict[str, Any]]]:
        """Signed notification deliveries (the signature is checked against the target's secret)."""
        out: list[tuple[float, dict[str, Any]]] = []
        with self._lock:
            items = list(self.requests)
        for at, req in items:
            body = json.loads(req.content)
            if body.get("type") == "crew.notification.challenge":
                continue
            if self.secret is not None:
                assert verify_signature(self.secret, req.content, req.headers["X-Remembra-Signature"]), body
            out.append((at, body))
        return out

    def items(self) -> list[tuple[float, dict[str, Any]]]:
        """Every notification item, with the time its delivery arrived."""
        return [(at, item) for at, body in self.deliveries() for item in body.get("items") or []]


async def _public_resolver(url: str) -> ResolvedTarget:
    """DNS stand-in for the webhook host only (the notify SSRF policy is WP-7's; it is not under test here)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return ResolvedTarget(url=url, scheme=parsed.scheme, hostname=parsed.hostname or "", port=443, ips=("93.184.216.34",))


class Mailbox:
    """The server's email backend in the red-team runs: keeps every message (no mail leaves the machine)."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Any:
        from remembra.cloud.email import EmailResult

        self.sent.append(message)
        return EmailResult(success=True, message_id=f"redteam-{len(self.sent)}")

    def last_code(self, to: str) -> str:
        import re

        mail = next(m for m in reversed(self.sent) if m.to == to.lower())
        found = re.search(r"<strong>([0-9A-Z]+)</strong>", mail.html)
        assert found, mail.html
        return found.group(1)


class RedTeamServer:
    """uvicorn + the full app in a background thread; ``call`` runs a coroutine on the server loop."""

    def __init__(self, tmp_path: Path, *, rate_limits: bool = False) -> None:
        self.tmp_path = tmp_path
        self.rate_limits = rate_limits
        self.port = free_port()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.alerts = Alerts()
        self.mail = Mailbox()
        self.app = self._build_app()
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, name="crew-redteam-server", daemon=True)
        self.owner_id = ""
        self.admin_key = ""
        self.http = httpx.Client(base_url=self.url, timeout=30.0)

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
            # the real memory service (relay handoffs, promotions) over the real main DB; only the
            # vector store, the embedder and the extractor are in-process fakes (no network, no LLM)
            service = MemoryService(
                settings=Settings(openai_api_key="test", enable_entity_resolution=False),
                qdrant=FakeQdrant(),  # type: ignore[arg-type]
                db=db,
                embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
            )
            service.extractor = OneFactExtractor()  # type: ignore[assignment]
            app.state.memory_service = service
            app.state.sanitizer = None
            app.state.pii_detector = None
            app.state.users = UserManager(db, config_module.get_settings().jwt_secret)
            inbox = InboxManager(db)
            await inbox.init_schema()
            app.state.inbox_manager = inbox
            yield
            await app.state.tasks.shutdown(timeout=2.0)
            await db.close()

        app = FastAPI(lifespan=lifespan)
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
        app.state.crew_webhook_sender = WebhookSender(resolver=_public_resolver, transport=self.alerts.transport())
        app.state.crew_email_backend = self.mail
        app.include_router(api_router)
        main_module.install_crew(app)
        app.include_router(websocket.router)
        return app

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("red-team server did not start")
            time.sleep(0.05)
        self.owner_id, self.admin_key = self.call(self._seed_owner())

    def stop(self) -> None:
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def call(self, coro: Awaitable[T], timeout: float = 30) -> T:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)  # type: ignore[arg-type]

    async def _seed_owner(self) -> tuple[str, str]:
        users: UserManager = self.app.state.users
        user, error = await users.create_user(email=OWNER_EMAIL, password=OWNER_PASSWORD)
        assert user is not None, error
        key = await self.app.state.api_key_manager.create_key(user_id=user.id, name="agent-admin")
        await self.app.state.role_manager.assign_role(key.id, Role.ADMIN)
        return user.id, key.key

    # -- credentials -------------------------------------------------------------------

    def api_key(self, role: str = "admin", *, agent_id: str | None = None, project_ids: list[str] | None = None) -> str:
        async def make() -> str:
            key = await self.app.state.api_key_manager.create_key(
                user_id=self.owner_id, name=f"{role}-{agent_id or 'any'}", agent_id=agent_id
            )
            await self.app.state.role_manager.assign_role(key.id, Role(role), project_ids=project_ids)
            return str(key.key)

        return self.call(make())

    def login(self) -> dict[str, str]:
        """A fresh dashboard login (the human principal, D27): the real /auth/login route."""
        res = self.http.post(f"{API}/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
        assert res.status_code == 200, res.text
        return {"Authorization": f"Bearer {res.json()['access_token']}"}

    def add_webhook_target(self, human: dict[str, str]) -> None:
        """A human adds the signed webhook and turns it on for real-time alerts of every crew they own."""
        res = self.http.post(f"{API}/notifications/targets", json={"kind": "webhook", "target": WEBHOOK_URL}, headers=human)
        assert res.status_code == 201, res.text
        self.alerts.secret = res.json()["signing_secret"]

    def enable_realtime_webhook(self, crew_id: str, human: dict[str, str]) -> None:
        crew = self.http.get(f"{API}/crews/{crew_id}", headers=human)
        assert crew.status_code == 200, crew.text
        version = crew.headers.get("ETag", "").strip('"W/') or str(crew.json().get("settings_version", ""))
        res = self.http.patch(
            f"{API}/crews/{crew_id}",
            json={"settings": {"notify": {"realtime": ["webhook"]}}},
            headers={**human, "If-Match": version},
        )
        assert res.status_code == 200, res.text

    # -- inspection (runs on the server loop, reads the real crew.db) --------------------

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async def q() -> list[dict[str, Any]]:
            db = self.app.state.crew_db
            cur = await db.conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=False)) for r in await cur.fetchall()]

        return self.call(q())

    def main_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async def q() -> list[dict[str, Any]]:
            conn = self.app.state.db.conn
            cur = await conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=False)) for r in await cur.fetchall()]

        return self.call(q())

    def events(self, crew_id: str, *, types: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        rows = self.rows(
            "SELECT seq, type, payload, actor_kind, session_id, ts, moment FROM crew_events WHERE crew_id = ? ORDER BY seq",
            (crew_id,),
        )
        out = []
        for r in rows:
            if types and r["type"] not in types:
                continue
            r["payload"] = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            out.append(r)
        return out

    def state_digest(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every row of every crew table (for 'the refused call changed nothing' checks)."""
        tables = [
            r["name"]
            for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'crew%' ORDER BY name")
        ]
        out: dict[str, list[tuple[Any, ...]]] = {}
        for t in tables:
            if t in ("crew_idempotency", "crew_read_cursors", "crew_outbox"):
                continue  # bookkeeping of the server itself, not crew state
            out[t] = [tuple(r.values()) for r in self.rows(f"SELECT * FROM {t} ORDER BY rowid")]  # noqa: S608
        return out


@contextmanager
def running_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rate_limits: bool = False) -> Iterator[RedTeamServer]:
    """Start a :class:`RedTeamServer` with isolated process-wide state (settings, hooks, WS manager, limiters)."""
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    monkeypatch.setattr(startup, "_HOOKS", dict(startup._HOOKS))
    monkeypatch.setattr(config_module, "_settings", make_settings(auth_enabled=True, rate_limit_enabled=rate_limits))
    monkeypatch.setenv("REMEMBRA_CREW_DB_PATH", str(tmp_path / "crew" / "crew.db"))
    monkeypatch.delenv(startup.TAILER_ENV, raising=False)
    monkeypatch.setattr(limiter, "enabled", rate_limits)
    crew_limits.set_crew_rate_limiter(crew_limits.CrewRateLimiter("memory://") if rate_limits else None)
    limiter.reset()
    server = RedTeamServer(tmp_path, rate_limits=rate_limits)
    server.start()
    try:
        yield server
    finally:
        server.stop()
        crew_limits.set_crew_rate_limiter(None)
        limiter.reset()
