"""Harness for the Marshal desk: the production app factory with auth on, over real SQLite.

``create_app()`` builds the real middleware stack and routers (the desk's
included). The production lifespan is replaced by one that wires a real
SQLite ``Database``, real API-key / RBAC / audit / inbox / space managers, a
real ``UsageMeter`` (``cloud_enabled``) and a real ``MemoryService`` (only the
vector store, embedder and extractor are in-process fakes), as
``connector_harness.py`` does. Optionally a real ``crew.db`` is attached, as
Crew mode's startup does, and the connector (OAuth + ``/mcp``) runs.

The model is never the network: :class:`OpenAIScript` is an
``httpx.MockTransport`` that serves scripted chat completions to the real
OpenAI SDK (behind the desk's own ``BreakerTransport``), records every request
body that left the process, and is installed as ``app.state.marshal_llm_transport``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

import remembra.config as config_module
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import Role, RoleManager
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.config import Settings
from remembra.core.circuit_breaker import all_breakers
from remembra.inbox.manager import InboxManager
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.spaces.manager import SpaceManager
from remembra.storage.database import Database
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.security_harness import make_settings

PUBLIC = "https://api.example.com"
PASSWORD = "Str0ng!Passw0rd"
CONV = "5b1f2c7e-9d0a-4c1e-8f7b-2a3d4e5f6a7b"


# ---------------------------------------------------------------------------
# Scripted OpenAI
# ---------------------------------------------------------------------------


def usage_block(prompt: int, completion: int, cached: int = 0) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def tool_call(name: str, args: dict[str, Any] | str | None = None, call_id: str | None = None) -> dict[str, Any]:
    arguments = args if isinstance(args, str) else json.dumps(args or {})
    return {
        "id": call_id or f"call_{secrets.token_hex(4)}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _completion(message: dict[str, Any], finish: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": f"chatcmpl-{secrets.token_hex(4)}",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


@dataclass
class Step:
    """One scripted reply: a status and JSON body, or an async callable that builds the response."""

    status: int = 200
    body: dict[str, Any] | None = None
    handler: Callable[[dict[str, Any]], Awaitable[httpx.Response]] | None = None


def reply_tools(*calls: dict[str, Any], usage: dict[str, Any] | None = None) -> Step:
    message = {"role": "assistant", "content": None, "tool_calls": list(calls)}
    return Step(body=_completion(message, "tool_calls", usage if usage is not None else usage_block(2000, 20)))


def reply_answer(
    text: str, evidence: Iterable[str], commands: Iterable[str] = (), *, usage: dict[str, Any] | None | bool = True
) -> Step:
    content = json.dumps({"text": text, "evidence": list(evidence), "commands": list(commands)})
    return reply_raw(content, usage=usage)


def reply_raw(content: str, *, usage: dict[str, Any] | None | bool = True) -> Step:
    block = usage_block(2200, 60) if usage is True else (None if usage is False else usage)
    return Step(body=_completion({"role": "assistant", "content": content}, "stop", block))


def reply_error(status: int, message: str = "upstream error") -> Step:
    return Step(status=status, body={"error": {"message": message, "type": "server_error"}})


def reply_wait(event: asyncio.Event, then: Step) -> Step:
    """Hold the call until ``event`` is set (a slow model), then reply with ``then``."""

    async def handler(_body: dict[str, Any]) -> httpx.Response:
        await event.wait()
        return httpx.Response(then.status, json=then.body)

    return Step(handler=handler)


@dataclass
class OpenAIScript:
    """Serves ``steps`` in order to ``POST .../chat/completions``; every request body is kept."""

    steps: list[Step]
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "not scripted"}})
        if not self.steps:
            return httpx.Response(500, json={"error": {"message": "script exhausted"}})
        step = self.steps.pop(0)
        if step.handler is not None:
            return await step.handler(body)
        return httpx.Response(step.status, json=step.body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """``event: <name>`` / ``data: <json>`` blocks, in order (comments such as keepalives are skipped)."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith(":") or not line:
                continue
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        if name is not None:
            events.append((name, json.loads("\n".join(data))))
    return events


def event_names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


@dataclass
class DeskHarness:
    app: Any
    http: httpx.AsyncClient
    db: Database
    settings: Settings
    crew_db: Any = None
    script: OpenAIScript | None = None

    # -- accounts -------------------------------------------------------
    async def create_user(self, email: str) -> str:
        user, error = await UserManager(self.db, self.settings.jwt_secret).create_user(email=email, password=PASSWORD)
        assert user is not None, error
        return user.id

    def jwt(self, user_id: str, email: str) -> dict[str, str]:
        token = UserManager(self.db, self.settings.jwt_secret).create_jwt_token(user_id, email)
        return {"Authorization": f"Bearer {token}"}

    async def api_key(
        self,
        user_id: str,
        *,
        name: str = "desktop",
        agent_id: str | None = None,
        project_ids: list[str] | None = None,
        role: str = "editor",
    ) -> dict[str, str]:
        created = await APIKeyManager(self.db).create_key(user_id=user_id, name=name, agent_id=agent_id)
        await RoleManager(self.db).assign_role(created.id, Role(role), project_ids=project_ids)
        return {"X-API-Key": created.key}

    # -- the model --------------------------------------------------------
    def openai_script(self, *steps: Step) -> OpenAIScript:
        script = OpenAIScript(list(steps))
        self.script = script
        self.app.state.marshal_llm_transport = script.transport()
        return script

    # -- relay data -------------------------------------------------------
    async def seed_handoff(
        self,
        key: dict[str, str],
        *,
        agent: str,
        project: str = "widget",
        session: str | None = None,
        next_step: str = "carry on",
        memory_type: str = "handoff",
        **facts: Any,
    ) -> str:
        """A real close (``POST /session/close``) by ``agent``; returns the handoff id."""
        body = {
            "agent_id": agent,
            "session_id": session or f"s-{secrets.token_hex(4)}",
            "project_id": project,
            "facts": {"branch": "main", "next_step": next_step, **facts},
        }
        res = await self.http.post("/api/v1/session/close", json=body, headers=key)
        assert res.status_code == 200, res.text
        return str(res.json()["handoff_id"])

    async def seed_pickup(self, key: dict[str, str], *, reader: str, project: str = "widget") -> dict[str, Any]:
        """A real brief read by ``reader`` (records a pickup of another agent's handoff)."""
        res = await self.http.get("/api/v1/session/brief", params={"project_id": project, "agent_id": reader}, headers=key)
        assert res.status_code == 200, res.text
        return dict(res.json())

    async def seed_checkpoint(
        self, user_id: str, *, agent: str, project: str = "widget", content: str = "checkpoint", created_at: Any = None
    ) -> str:
        from remembra.core.time import utcnow

        memory_id = secrets.token_hex(8)
        await self.db.save_memory_metadata(
            memory_id=memory_id,
            user_id=user_id,
            project_id=project,
            content=content,
            extracted_facts=[content],
            metadata={"agent_id": agent},
            created_at=created_at or utcnow(),
            memory_type="checkpoint",
        )
        return memory_id

    # -- asks -------------------------------------------------------------
    async def ask(
        self,
        headers: dict[str, str],
        question: str = "why is codex waiting",
        *,
        context: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
        source: str = "prompt",
        conv: str = CONV,
    ) -> httpx.Response:
        body = {"question": question, "conv": conv, "history": history or [], "context": context, "source": source}
        return await self.http.post(
            "/api/v1/marshal/ask", json=body, headers={**headers, "Accept": "text/event-stream"}, timeout=60.0
        )

    # -- the database -----------------------------------------------------
    async def total_changes(self) -> int:
        cursor = await self.db.conn.execute("SELECT total_changes()")
        return int((await cursor.fetchone())[0])

    async def count(self, table: str, where: str = "1 = 1", params: tuple[Any, ...] = ()) -> int:
        cursor = await self.db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
        return int((await cursor.fetchone())[0])

    async def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await self.db.conn.execute(sql, params)
        return [dict(r) for r in await cursor.fetchall()]

    async def crew_changes(self) -> int:
        assert self.crew_db is not None
        cursor = await self.crew_db.conn.execute("SELECT total_changes()")
        return int((await cursor.fetchone())[0])


def reset_breakers() -> None:
    for breaker in all_breakers().values():
        breaker.reset()


@asynccontextmanager
async def desk_app(
    tmp_path: Any,
    *,
    crew: bool = False,
    connector: bool = False,
    client_host: str = "203.0.113.10",
    **overrides: Any,
) -> AsyncIterator[DeskHarness]:
    options: dict[str, Any] = {
        "public_url": PUBLIC,
        "openai_api_key": "sk-test-not-a-real-key",
        "enable_entity_resolution": False,
        "cloud_enabled": True,
        "marshal_enabled": True,
        "connector_enabled": connector,
    }
    options.update(overrides)
    settings = make_settings(**options)
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    previous = config_module._settings
    config_module._settings = settings
    reset_breakers()
    try:
        from remembra.main import create_app

        app = create_app()
        db = Database(str(tmp_path / "desk.db"))

        @asynccontextmanager
        async def lifespan(app_: Any) -> AsyncIterator[None]:
            yield

        app.router.lifespan_context = lifespan
        await db.connect()
        await db.init_schema()
        roles = RoleManager(db)
        await roles.init_schema()
        inbox = InboxManager(db)
        await inbox.init_schema()
        spaces = SpaceManager(db)
        await spaces.init_schema()
        meter = UsageMeter(db)
        await meter.init_schema()
        service = MemoryService(settings=settings, qdrant=FakeQdrant(), db=db, embeddings=FakeEmbeddings())  # type: ignore[arg-type]
        service.extractor = OneFactExtractor()  # type: ignore[assignment]
        state = app.state
        state.db = db
        state.memory_service = service
        state.api_key_manager = APIKeyManager(db)
        state.role_manager = roles
        state.audit_logger = AuditLogger(db)
        state.sanitizer = ContentSanitizer()
        state.pii_detector = None
        state.inbox_manager = inbox
        state.space_manager = spaces
        state.usage_meter = meter
        state.webhook_manager = None
        state.conflict_manager = None

        crew_db = None
        if crew:
            from remembra.crew.bus import CrewBus, db_loader
            from remembra.crew.db import CrewDatabase
            from remembra.crew.events import CrewEventLog

            crew_db = CrewDatabase(str(tmp_path / "crew.db"))
            await crew_db.init_schema()
            state.crew_db = crew_db
            state.crew_events = CrewEventLog(crew_db, CrewBus(loader=db_loader(crew_db)))

        runner: asyncio.Task[None] | None = None
        stop = asyncio.Event()
        if connector:
            from remembra.connector import connector_lifespan

            started = asyncio.Event()

            async def run_lifespan() -> None:
                async with connector_lifespan(app, settings):
                    started.set()
                    await stop.wait()

            runner = asyncio.create_task(run_lifespan())
            await asyncio.wait({runner, asyncio.create_task(started.wait())}, return_when=asyncio.FIRST_COMPLETED)
            if runner.done():
                runner.result()
        try:
            transport = httpx.ASGITransport(app=app, client=(client_host, 51234))
            async with httpx.AsyncClient(transport=transport, base_url=PUBLIC, timeout=60.0) as http:
                yield DeskHarness(app=app, http=http, db=db, settings=settings, crew_db=crew_db)
        finally:
            from remembra.core import ai_spend

            await ai_spend.drain_settles(timeout=10.0)
            if runner is not None:
                stop.set()
                await runner
            if crew_db is not None:
                await crew_db.close()
            await db.close()
    finally:
        config_module._settings = previous
        reset_breakers()
