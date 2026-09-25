"""Real-runtime harness for security regression tests.

Builds a FastAPI app with the production routers mounted on a real SQLite
database (temp file), real API-key / RBAC / audit managers, and authentication
ENABLED — so tests exercise the same dependency chain as production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import remembra.config as config_module
from remembra.auth.keys import APIKeyManager
from remembra.auth.rbac import Role, RoleManager
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.config import Settings
from remembra.core.limiter import limiter
from remembra.security.audit import AuditLogger
from remembra.storage.database import Database

JWT_SECRET = "test-jwt-secret-for-security-harness-0123456789"
MASTER_KEY = "test-master-key-for-security-harness-0123456789"


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    db: Database
    keys: APIKeyManager
    roles: RoleManager
    users: UserManager
    settings: Settings
    created: dict[str, Any] = field(default_factory=dict)

    async def create_user(
        self,
        email: str,
        password: str = "Str0ng!Passw0rd",
        *,
        verified: bool = False,
        active: bool = True,
    ) -> str:
        user, error = await self.users.create_user(email=email, password=password)
        assert user is not None, error
        if verified:
            await self.db.update_user_email_verified(user.id, True)
        if not active:
            await self.db.deactivate_user(user.id)
        return user.id

    def jwt(self, user_id: str, email: str = "user@example.com") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.users.create_jwt_token(user_id, email)}"}

    async def api_key(
        self,
        user_id: str,
        role: str = "editor",
        *,
        project_ids: list[str] | None = None,
        scopes: list[str] | None = None,
    ) -> tuple[str, str]:
        """Create a real key; returns (raw_key, key_id)."""
        created = await self.keys.create_key(user_id=user_id, name=f"{role}-key")
        await self.roles.assign_role(created.id, Role(role), scopes=scopes, project_ids=project_ids)
        return created.key, created.id


class _StubMemoryService:
    """Minimal memory service: real DB, no vector store / LLM (tests override as needed)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.qdrant = None

    async def list_memories(self, **_: Any) -> list[dict[str, Any]]:
        return []

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        return await self.db.get_memory(memory_id)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "auth_enabled": True,
        "jwt_secret": JWT_SECRET,
        "auth_master_key": MASTER_KEY,
        "rate_limit_enabled": False,
        "debug": False,
        "owner_emails": ["owner@example.com"],
        "cloud_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


@asynccontextmanager
async def secure_app(
    tmp_path: Any,
    routers: Iterable[APIRouter],
    *,
    settings: Settings | None = None,
    state: dict[str, Any] | None = None,
    prefix: str = "/api/v1",
    client_host: str = "203.0.113.10",
) -> AsyncIterator[Harness]:
    settings = settings or make_settings()
    previous = config_module._settings
    config_module._settings = settings

    db = Database(str(tmp_path / "security.db"))
    await db.connect()
    await db.init_schema()
    roles = RoleManager(db)
    await roles.init_schema()
    # Tenant tables exist in every deployment (superadmin stats read them).
    await UsageMeter(db).init_schema()

    app = FastAPI()
    app.state.db = db
    app.state.api_key_manager = APIKeyManager(db)
    app.state.role_manager = roles
    app.state.audit_logger = AuditLogger(db)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.state.memory_service = _StubMemoryService(db)
    for key, value in (state or {}).items():
        setattr(app.state, key, value)
    for router in routers:
        app.include_router(router, prefix=prefix)

    transport = httpx.ASGITransport(app=app, client=(client_host, 51234))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Harness(
                app=app,
                client=client,
                db=db,
                keys=app.state.api_key_manager,
                roles=roles,
                users=UserManager(db, settings.jwt_secret),
                settings=settings,
            )
    finally:
        await db.close()
        config_module._settings = previous
