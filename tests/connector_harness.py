"""Harness for the remote MCP connector: the production app factory with auth ON.

``create_app()`` builds the real middleware stack, the real REST routers and
the connector (OAuth server + /mcp). Its production lifespan is replaced by
one that wires a real SQLite ``Database``, real API-key / RBAC / audit /
inbox managers and a real ``MemoryService`` (only the vector store, embedder
and LLM extractor are in-process fakes), then runs ``connector_lifespan`` —
the same function production uses — for the OAuth tables and the MCP
session manager.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

import remembra.config as config_module
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import Role, RoleManager
from remembra.auth.users import UserManager
from remembra.config import Settings
from remembra.connector import connector_lifespan
from remembra.connector.policy import CLAUDE_REDIRECT_URI
from remembra.inbox.manager import InboxManager
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.security.untrusted import unwrap_untrusted
from remembra.services.memory import MemoryService
from remembra.spaces.manager import SpaceManager
from remembra.storage.database import Database
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.security_harness import make_settings

PUBLIC = "https://api.example.com"
RESOURCE = PUBLIC + "/mcp"
PASSWORD = "Str0ng!Passw0rd"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class Connected:
    client_id: str
    access_token: str
    refresh_token: str
    verifier: str
    token_response: dict[str, Any]


@dataclass
class ConnectorHarness:
    app: Any
    http: httpx.AsyncClient
    db: Database
    settings: Settings
    last_tool_text: str = ""

    # -- accounts -------------------------------------------------------
    async def create_user(self, email: str) -> str:
        user, error = await UserManager(self.db, self.settings.jwt_secret).create_user(email=email, password=PASSWORD)
        assert user is not None, error
        return user.id

    def jwt(self, user_id: str, email: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {UserManager(self.db, self.settings.jwt_secret).create_jwt_token(user_id, email)}"}

    async def api_key(self, user_id: str) -> str:
        created = await APIKeyManager(self.db).create_key(user_id=user_id, name="desktop")
        await RoleManager(self.db).assign_role(created.id, Role("editor"))
        return created.key

    async def seed_memory(self, user_id: str, project: str, content: str, memory_type: str | None = None) -> None:
        from remembra.core.time import utcnow

        await self.db.save_memory_metadata(
            memory_id=secrets.token_hex(8),
            user_id=user_id,
            project_id=project,
            content=content,
            extracted_facts=[content],
            metadata={"agent_id": "claude-code"},
            created_at=utcnow(),
            memory_type=memory_type,
        )

    # -- OAuth ----------------------------------------------------------
    async def register(self, redirect_uri: str = CLAUDE_REDIRECT_URI, method: str = "none", **extra: Any) -> httpx.Response:
        body = {
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            **extra,
        }
        return await self.http.post("/oauth/register", json=body)

    async def authorize(self, client_id: str, challenge: str, **overrides: Any) -> httpx.Response:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "state": "st-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "session:brief memory:recall memory:store",
            "resource": RESOURCE,
        }
        params.update(overrides)
        return await self.http.get("/oauth/authorize", params={k: v for k, v in params.items() if v is not None})

    @staticmethod
    def request_id(page: httpx.Response) -> str:
        match = re.search(r"name='request_id' value='([^']+)'", page.text)
        assert match, page.text
        return match.group(1)

    async def login(self, request_id: str, email: str, password: str = PASSWORD, **extra: str) -> httpx.Response:
        return await self.http.post(
            "/oauth/authorize/login",
            data={"request_id": request_id, "email": email, "password": password, "decision": "login", **extra},
        )

    async def consent(
        self, request_id: str, projects: list[str], agent: str = "claude-app", decision: str = "approve", new_project: str = ""
    ) -> httpx.Response:
        data: dict[str, Any] = {
            "request_id": request_id,
            "project": projects,
            "new_project": new_project,
            "agent_id": agent,
            "decision": decision,
        }
        return await self.http.post("/oauth/authorize/consent", data=data)

    @staticmethod
    def redirect_params(resp: httpx.Response) -> dict[str, str]:
        assert resp.status_code == 303, (resp.status_code, resp.text)
        location = resp.headers["location"]
        assert location.startswith(CLAUDE_REDIRECT_URI + "?"), location
        return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}

    async def token(self, data: dict[str, str], auth: tuple[str, str] | None = None) -> httpx.Response:
        return await self.http.post("/oauth/token", data=data, auth=auth)

    async def connect(
        self,
        email: str,
        projects: list[str],
        *,
        scope: str = "session:brief memory:recall memory:store",
        agent: str = "claude-app",
    ) -> Connected:
        """Register -> authorize (PKCE) -> login -> consent -> token."""
        reg = await self.register()
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]
        verifier, challenge = pkce()
        page = await self.authorize(client_id, challenge, scope=scope)
        assert page.status_code == 200, page.text
        rid = self.request_id(page)
        consent_page = await self.login(rid, email)
        assert consent_page.status_code == 200, consent_page.text
        params = self.redirect_params(await self.consent(rid, projects, agent=agent))
        assert params["state"] == "st-123" and params["iss"] == PUBLIC
        tok = await self.token(
            {
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": CLAUDE_REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": RESOURCE,
            }
        )
        assert tok.status_code == 200, tok.text
        body = tok.json()
        return Connected(client_id, body["access_token"], body["refresh_token"], verifier, body)

    # -- MCP over raw JSON-RPC -----------------------------------------------
    async def mcp_post(self, token: str | None, payload: Any) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await self.http.post("/mcp", content=json.dumps(payload), headers=headers)

    async def tool(self, token: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self.mcp_post(
            token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        )
        assert resp.status_code == 200, (resp.status_code, resp.text)
        result = resp.json()["result"]
        text = result["content"][0]["text"]
        self.last_tool_text = text  # the raw result, for tests of the untrusted-data framing
        # Results that carry stored content are framed as untrusted data (R-14); the JSON is inside.
        return dict(json.loads(unwrap_untrusted(text)))


@asynccontextmanager
async def connector_app(tmp_path: Any, **overrides: Any) -> AsyncIterator[ConnectorHarness]:
    options: dict[str, Any] = {
        "connector_enabled": True,
        "public_url": PUBLIC,
        "openai_api_key": "test",
        "enable_entity_resolution": False,
    }
    options.update(overrides)
    settings = make_settings(**options)
    previous = config_module._settings
    config_module._settings = settings
    try:
        from remembra.main import create_app

        app = create_app()
        db = Database(str(tmp_path / "connector.db"))

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
        state.usage_meter = None
        state.webhook_manager = None

        # The MCP session manager's task group must be entered and exited in
        # one task (as uvicorn's lifespan task does); pytest-asyncio runs fixture
        # setup and teardown in different tasks, so host it in its own task.
        started, stop = asyncio.Event(), asyncio.Event()

        async def run_lifespan() -> None:
            async with connector_lifespan(app, settings):
                started.set()
                await stop.wait()

        runner = asyncio.create_task(run_lifespan())
        await asyncio.wait({runner, asyncio.create_task(started.wait())}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done():
            runner.result()  # surface a startup failure
        try:
            transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 51234))
            async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as http:
                yield ConnectorHarness(app=app, http=http, db=db, settings=settings)
        finally:
            stop.set()
            await runner
            await db.close()
    finally:
        config_module._settings = previous
