"""Synthetic HTTP authorization regressions; no external services or production data."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from remembra.api.v1 import ingest, temporal, transfer
from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.config import get_settings
from remembra.models.memory import ConversationIngestResponse
from remembra.storage.database import Database


@dataclass(frozen=True)
class Endpoint:
    path: str
    permission: str = "memory:recall"
    method: str = "GET"
    body_kind: str | None = None


ENDPOINTS = [
    Endpoint("/ingest/conversation", "memory:store", "POST", "conversation"),
    Endpoint("/ingest/conversation/stream", "memory:store", "POST", "conversation"),
    Endpoint("/ingest/changelog", "memory:store", "POST", "changelog"),
    Endpoint("/transfer/import", "memory:store", "POST", "import"),
    Endpoint("/transfer/import/file?format=plaintext", "memory:store", "POST", "file"),
    Endpoint("/transfer/export"),
    Endpoint("/temporal/decay/report"),
    Endpoint("/temporal/memory/own/decay"),
    Endpoint("/temporal/archive"),
    Endpoint("/temporal/archive/stats"),
    Endpoint("/temporal/archive/own"),
    Endpoint("/temporal/archive/own/restore", "memory:store", "POST"),
    Endpoint("/temporal/cleanup?dry_run=true", "memory:recall", "POST"),
    Endpoint("/temporal/cleanup?dry_run=false", "memory:delete", "POST"),
    Endpoint("/temporal/adaptive/threshold"),
    Endpoint("/temporal/adaptive/mode?mode=balanced", "memory:store", "POST"),
    Endpoint("/temporal/adaptive/reset", "memory:store", "POST"),
]


@pytest.fixture
async def auth_api(in_memory_db, monkeypatch):
    # Real SQLite keeps export and the ID-based decay query honest about ownership.
    conn = in_memory_db.conn
    await conn.execute(
        """CREATE TABLE memories (
            id TEXT, content TEXT, user_id TEXT, project_id TEXT,
            extracted_facts TEXT, metadata TEXT, created_at TEXT, updated_at TEXT,
            expires_at TEXT, source TEXT, trust_score REAL,
            access_count INTEGER, last_accessed TEXT,
            memory_type TEXT, pinned INTEGER
        )"""
    )
    records = {}
    for memory_id, user_id, project_id in [
        ("own", "tenant-a", "alpha"),
        ("other-project", "tenant-a", "beta"),
        ("other-tenant", "tenant-b", "alpha"),
    ]:
        record = {
            "id": memory_id,
            "content": f"SYNTHETIC-{memory_id}",
            "user_id": user_id,
            "project_id": project_id,
            "created_at": "2026-01-01T00:00:00",
            "archived_at": "2026-02-01T00:00:00",
        }
        records[memory_id] = record
        await conn.execute(
            """INSERT INTO memories
            (id, content, user_id, project_id, created_at, access_count)
            VALUES (?, ?, ?, ?, ?, 0)""",
            (memory_id, record["content"], user_id, project_id, record["created_at"]),
        )
    await conn.commit()
    db = Database.__new__(Database)
    db._connection = conn
    db.get_archived_memory = AsyncMock(side_effect=lambda memory_id: records.get(memory_id))
    db.get_archived_memories = AsyncMock(return_value=[])
    db.get_archive_stats = AsyncMock(return_value={})
    db.search_archived_memories = AsyncMock(return_value=[])
    db.restore_memory = AsyncMock(return_value=True)
    db.index_memory_fts = AsyncMock()
    service = SimpleNamespace(db=db, qdrant=None, store=AsyncMock(return_value=SimpleNamespace(id="synthetic-new")))
    processor = SimpleNamespace(ingest=AsyncMock(return_value=ConversationIngestResponse()))
    cleanup = MagicMock()
    cleanup.return_value.run_cleanup = AsyncMock(return_value={})
    monkeypatch.setattr(temporal, "TemporalCleanupJob", cleanup)
    adaptive_factory = MagicMock(wraps=temporal.create_adaptive_manager)
    monkeypatch.setattr(temporal, "create_adaptive_manager", adaptive_factory)
    parser = MagicMock(wraps=transfer._parse_import)
    monkeypatch.setattr(transfer, "_parse_import", parser)
    export_fetch = AsyncMock(wraps=transfer._fetch_all_memories)
    monkeypatch.setattr(transfer, "_fetch_all_memories", export_fetch)
    app = FastAPI()
    app.state.memory_service = service
    app.state.conversation_ingest = processor
    app.state.audit_logger = AsyncMock()
    app.state.sanitizer = MagicMock()
    identity = AuthenticatedUser("tenant-a", "synthetic-key", "standard", project_ids=["alpha"])
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(sanitization_enabled=False)
    for router in (ingest.router, transfer.router, temporal.router):
        app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            user=identity,
            service=service,
            processor=processor,
            cleanup=cleanup,
            adaptive_factory=adaptive_factory,
            parser=parser,
            export_fetch=export_fetch,
            app=app,
        )


async def call_endpoint(api, endpoint, project="alpha", memory_id="own"):
    path = endpoint.path.replace("/own", f"/{memory_id}")
    params = {"project_id": project}
    kwargs = {}
    if endpoint.body_kind == "conversation":
        kwargs["json"] = {
            "messages": [{"role": "user", "content": "Synthetic authorization test."}],
            "user_id": "tenant-b",  # Must never override the authenticated identity.
            "project_id": project,
        }
    elif endpoint.body_kind == "changelog":
        kwargs["json"] = {"content": "## [1.0.0] - 2026-01-01\n### Added\n- Synthetic fact", "project_id": project}
    elif endpoint.body_kind == "import":
        kwargs["json"] = {"format": "plaintext", "data": "Synthetic fact", "project_id": project}
    elif endpoint.body_kind == "file":
        kwargs["files"] = {"file": ("synthetic.txt", b"Synthetic fact", "text/plain")}
    # Preserve required mode/format/dry_run query arguments already in the path.
    return await api.client.request(
        endpoint.method,
        f"/api/v1{path}&project_id={project}" if "?" in path else f"/api/v1{path}",
        params=None if "?" in path else params,
        **kwargs,
    )


def assert_no_effects(api):
    api.processor.ingest.assert_not_awaited()
    api.service.store.assert_not_awaited()
    api.parser.assert_not_called()
    api.export_fetch.assert_not_awaited()
    api.service.db.restore_memory.assert_not_awaited()
    api.service.db.index_memory_fts.assert_not_awaited()
    api.cleanup.assert_not_called()
    api.adaptive_factory.assert_not_called()


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.path)
async def test_explicit_scope_denied_before_effects(auth_api, endpoint):
    # Explicit scopes must restrict even an admin; non-memory scope grants none.
    auth_api.user.role = "admin"
    auth_api.user.scopes = ["entity:read"]
    response = await call_endpoint(auth_api, endpoint)
    assert response.status_code == 403, response.text
    assert_no_effects(auth_api)


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.path)
async def test_scoped_key_cannot_cross_projects(auth_api, endpoint):
    response = await call_endpoint(auth_api, endpoint, project="beta", memory_id="other-project")
    assert response.status_code == 403, response.text
    assert "SYNTHETIC-" not in response.text
    assert_no_effects(auth_api)


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.path)
async def test_role_matrix_preserves_allowed_project_access(auth_api, endpoint, role):
    auth_api.user.role = role
    response = await call_endpoint(auth_api, endpoint)
    if role == "viewer" and endpoint.permission != "memory:recall":
        assert response.status_code == 403, response.text
        assert_no_effects(auth_api)
    else:
        assert response.status_code in (200, 201), response.text
        if endpoint.body_kind == "conversation":
            body = auth_api.processor.ingest.await_args.args[0]
            assert (body.user_id, body.project_id) == ("tenant-a", "alpha")
        if endpoint.body_kind in ("changelog", "import", "file"):
            body = auth_api.service.store.await_args.args[0]
            assert (body.user_id, body.project_id) == ("tenant-a", "alpha")


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.path)
async def test_explicit_required_scope_is_sufficient(auth_api, endpoint):
    auth_api.user.role = "viewer"
    auth_api.user.scopes = [endpoint.permission]
    response = await call_endpoint(auth_api, endpoint)
    assert response.status_code in (200, 201), response.text


@pytest.mark.parametrize("path", ["/temporal/memory/own/decay", "/temporal/archive/own", "/temporal/archive/own/restore"])
async def test_foreign_owner_never_exposes_or_restores(auth_api, path):
    endpoint = Endpoint(path, method="POST" if path.endswith("restore") else "GET")
    response = await call_endpoint(auth_api, endpoint, memory_id="other-tenant")
    assert response.status_code in (403, 404), response.text
    assert "SYNTHETIC-" not in response.text
    assert_no_effects(auth_api)


async def test_export_filters_real_sqlite_tenants_and_projects(auth_api):
    response = await call_endpoint(auth_api, Endpoint("/transfer/export"))
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["memories"]] == ["own"]
    auth_api.user.project_ids = None
    response = await call_endpoint(auth_api, Endpoint("/transfer/export"), project="beta")
    assert [m["id"] for m in response.json()["memories"]] == ["other-project"]


async def test_decay_missing_memory_is_404(auth_api):
    response = await call_endpoint(auth_api, Endpoint("/temporal/memory/own/decay"), memory_id="missing")
    assert response.status_code == 404


async def test_decay_storage_returns_authorization_fields(auth_api):
    memory = await auth_api.service.db.get_memory_with_decay("own")
    assert memory["user_id"] == "tenant-a"
    assert memory["project_id"] == "alpha"


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.path)
async def test_multi_project_key_preserves_explicit_allowed_project(auth_api, endpoint):
    auth_api.user.project_ids = ["alpha", "beta"]
    response = await call_endpoint(auth_api, endpoint, project="beta", memory_id="other-project")
    assert response.status_code in (200, 201), response.text


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "rem_synthetic_invalid"}])
async def test_real_auth_dependency_rejects_missing_or_invalid_key(auth_api, monkeypatch, headers):
    from remembra.auth import middleware

    auth_api.app.dependency_overrides.pop(get_current_user)
    monkeypatch.setattr(middleware, "get_settings", lambda: SimpleNamespace(auth_enabled=True, jwt_secret=None))
    auth_api.app.state.api_key_manager = SimpleNamespace(validate_key=AsyncMock(return_value=None))
    auth_api.client.headers.update(headers)
    for endpoint in ENDPOINTS:
        response = await call_endpoint(auth_api, endpoint)
        assert response.status_code == 401, (endpoint.path, response.text)
    assert_no_effects(auth_api)


@pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
async def test_key_transport_preserves_role_scope_and_project_restrictions(auth_api, monkeypatch, header):
    from remembra.auth import middleware

    auth_api.app.dependency_overrides.pop(get_current_user)
    monkeypatch.setattr(middleware, "get_settings", lambda: SimpleNamespace(auth_enabled=True, jwt_secret=None))
    # Only credential lookup is stubbed; the actual auth and permission dependencies run.
    auth_api.app.state.api_key_manager = SimpleNamespace(
        validate_key=AsyncMock(
            return_value={
                "user_id": "tenant-a",
                "id": "synthetic-key",
                "role": "admin",
                "scopes": ["memory:recall"],
                "project_ids": ["alpha"],
            }
        )
    )
    auth_api.client.headers[header] = "Bearer rem_synthetic" if header == "Authorization" else "rem_synthetic"
    for endpoint in ENDPOINTS:
        response = await call_endpoint(auth_api, endpoint)
        assert response.status_code == (200 if endpoint.permission == "memory:recall" else 403), response.text
    response = await call_endpoint(auth_api, Endpoint("/transfer/export"), project="beta")
    assert response.status_code == 403


@pytest.mark.parametrize("denial", ["role", "project", None])
async def test_archive_search_handler_authorization(auth_api, denial):
    # Isolate the real route: the existing /archive/{memory_id} registration shadows
    # /archive/search in the aggregate router. Fixing that routing defect is separate.
    search_route = next(route for route in temporal.router.routes if route.path == "/temporal/archive/search")
    isolated_router = APIRouter()
    isolated_router.routes.append(search_route)
    app = FastAPI()
    app.state = auth_api.app.state
    app.dependency_overrides = auth_api.app.dependency_overrides
    app.include_router(isolated_router, prefix="/api/v1")
    if denial == "role":
        auth_api.user.scopes = ["entity:read"]
    project = "beta" if denial == "project" else "alpha"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await call_endpoint(
            SimpleNamespace(client=client), Endpoint("/temporal/archive/search?q=SYNTHETIC"), project=project
        )
    assert response.status_code == (403 if denial else 200), response.text
    if denial:
        auth_api.service.db.search_archived_memories.assert_not_awaited()
    else:
        auth_api.service.db.search_archived_memories.assert_awaited_once_with(
            user_id="tenant-a", query="SYNTHETIC", project_id="alpha", limit=20
        )
