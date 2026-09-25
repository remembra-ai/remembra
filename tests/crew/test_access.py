"""WP-14: human principal, step-up, load_crew / load_crew_entity, RBAC ADMIN exclusion.

Runtime tests against a real SQLite crew.db (spec §3.2 DDL), real FastAPI dependency
chains and real API keys / JWTs from the security harness (auth enabled).
"""

from __future__ import annotations

import itertools
import time
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite
import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException

import remembra.config as config_module
from remembra.api.v1 import admin, auth
from remembra.auth.middleware import AuthenticatedUser
from remembra.auth.rbac import HUMAN_ONLY_PERMISSIONS, ROLE_PERMISSIONS, KeyRole, Permission, Role
from remembra.crew import access
from remembra.crew.access import (
    ENTITY_TABLES,
    STEP_UP_MAX_AGE_S,
    CrewAccess,
    CrewEntity,
    audit_crew_routes,
    crew_access,
    crew_entity,
    effective_permissions,
    human_principal,
    is_human,
    key_permissions,
    load_crew,
    load_crew_entity,
    login_is_fresh,
    require_same_crew,
)
from remembra.crew.schemas import ID_PREFIXES, ROUTES
from tests.crew.crew_ddl import CREW_DB_V1_DDL
from tests.security_harness import JWT_SECRET, make_settings, secure_app

NOW = "2026-09-25T20:00:00Z"
OWNER = "u_owner"
OTHER = "u_other"
CREW_A = "crw_aaaaaaaaaaaaaaaa"
CREW_B = "crw_bbbbbbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# crew.db fixture
# ---------------------------------------------------------------------------


async def open_crew_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(CREW_DB_V1_DDL)
    await conn.commit()
    return conn


async def add_crew(conn: aiosqlite.Connection, crew_id: str, owner: str, project: str) -> None:
    await conn.execute(
        "INSERT INTO crews (id, owner_user_id, project_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (crew_id, owner, project, NOW, NOW),
    )
    await conn.execute(
        "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, 'owner', ?)", (crew_id, owner, NOW)
    )
    await conn.commit()


async def add_member(conn: aiosqlite.Connection, crew_id: str, user_id: str, role: str) -> None:
    await conn.execute(
        "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, ?, ?)", (crew_id, user_id, role, NOW)
    )
    await conn.commit()


async def add_entity(conn: aiosqlite.Connection, kind: str, entity_id: str, crew_id: str) -> None:
    """Insert a minimal valid row for ``kind`` (all NOT NULL columns filled from the table info)."""
    table = ENTITY_TABLES[kind]
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    cols = await cursor.fetchall()
    values: dict[str, Any] = {}
    for _cid, name, ctype, notnull, default, _pk in cols:
        if name == "id":
            values[name] = entity_id
        elif name == "crew_id":
            values[name] = crew_id
        elif notnull and default is None:
            values[name] = next(_SEQ) if "INT" in (ctype or "").upper() else _text_for(table, name, entity_id)
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    await conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values()))
    await conn.commit()


_SEQ = itertools.count(1)
_CHECKED_TEXT = {
    ("crew_claims", "mode"): "exclusive",
    ("crew_claims", "holder_kind"): "session",
    ("crew_claims", "state"): "active",
    ("crew_tasks", "status"): "ready",
    ("crew_reports", "kind"): "partial",
    ("crew_batons", "kind"): "adopt",
    ("crew_baton_offers", "via"): "brief",
    ("crew_zones", "source"): "api",
    ("crew_zone_changes", "state"): "pending",
    ("crew_inbox_items", "audience"): "project",
    ("crew_inbox_items", "state"): "open",
}


def _text_for(table: str, column: str, entity_id: str) -> str:
    return _CHECKED_TEXT.get((table, column), f"{column}-{entity_id}")


def eid(kind: str, suffix: str = "1") -> str:
    return f"{ID_PREFIXES[kind]}_{kind.replace('_', '')}{suffix}"


def key_user(user_id: str = OWNER, role: str = "admin", **kw: Any) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=user_id, api_key_id="key_1", rate_limit_tier="standard", role=role, **kw)


def jwt_user(user_id: str = OWNER) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=user_id, api_key_id="jwt_auth", rate_limit_tier="standard")


def now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture()
def settings_prod():
    previous = config_module._settings
    config_module._settings = make_settings(debug=False)
    yield config_module._settings
    config_module._settings = previous


def _body(exc: HTTPException) -> dict[str, Any]:
    assert isinstance(exc.detail, dict)
    return exc.detail


# ---------------------------------------------------------------------------
# RBAC (§3.1, D27)
# ---------------------------------------------------------------------------


def test_admin_role_excludes_human_only_crew_permissions():
    assert {Permission.CREW_OVERRIDE, Permission.CREW_ADMIN} == set(HUMAN_ONLY_PERMISSIONS)
    admin_perms = ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.CREW_OVERRIDE not in admin_perms
    assert Permission.CREW_ADMIN not in admin_perms
    # Everything else is still granted to admin.
    assert admin_perms == set(Permission) - set(HUMAN_ONLY_PERMISSIONS)
    for role, perms in ROLE_PERMISSIONS.items():
        assert not (perms & HUMAN_ONLY_PERMISSIONS), role
    assert {Permission.CREW_READ, Permission.CREW_WRITE, Permission.CREW_CLAIM} <= ROLE_PERMISSIONS[Role.EDITOR]
    assert ROLE_PERMISSIONS[Role.VIEWER] & {p for p in Permission if p.value.startswith("crew:")} == {Permission.CREW_READ}


def test_scopes_cannot_add_a_human_only_permission_to_a_key():
    kr = KeyRole(api_key_id="k", role=Role.ADMIN, scopes=["crew:override", "crew:admin", "crew:read"])
    assert kr.permissions == {Permission.CREW_READ}
    assert not kr.has_permission(Permission.CREW_OVERRIDE)


def test_key_permissions_follow_role_and_scope_whitelist():
    assert key_permissions(key_user(role="admin")) == {"crew:read", "crew:write", "crew:claim"}
    assert key_permissions(key_user(role="viewer")) == {"crew:read"}
    assert key_permissions(key_user(role="editor", scopes=["memory:recall"])) == frozenset()
    assert key_permissions(key_user(role="bogus")) == {"crew:read"}  # unknown role → least privilege
    assert key_permissions(jwt_user()) == {"crew:read", "crew:write", "crew:claim"}


# ---------------------------------------------------------------------------
# is_human (D27) and step-up
# ---------------------------------------------------------------------------


def test_is_human_only_for_jwt_and_dev_outside_production():
    previous = config_module._settings
    try:
        config_module._settings = make_settings(debug=False)
        assert is_human(jwt_user())
        assert not is_human(key_user(role="admin"))
        assert not is_human(AuthenticatedUser(user_id="u", api_key_id="dev_key", rate_limit_tier="standard"))
        assert not is_human(AuthenticatedUser(user_id="u", api_key_id="oauth:grant1", rate_limit_tier="standard"))
        config_module._settings = make_settings(debug=True)
        assert is_human(AuthenticatedUser(user_id="u", api_key_id="dev_key", rate_limit_tier="standard"))
        assert not is_human(key_user(role="admin"))
    finally:
        config_module._settings = previous


def test_login_freshness_window(settings_prod):
    now = now_ms()
    user = jwt_user()
    assert login_is_fresh(user, now - 60_000, now_ms=now)
    assert login_is_fresh(user, now - STEP_UP_MAX_AGE_S * 1000, now_ms=now)
    assert not login_is_fresh(user, now - STEP_UP_MAX_AGE_S * 1000 - 1, now_ms=now)
    assert not login_is_fresh(user, None, now_ms=now)
    assert not login_is_fresh(user, now + 10 * 60_000, now_ms=now)  # forged future issue time
    assert not login_is_fresh(key_user(), now, now_ms=now)


def test_effective_permissions_matrix(settings_prod):
    human, key = jwt_user(), key_user(role="admin")
    allp = {"crew:read", "crew:write", "crew:claim", "crew:override", "crew:admin"}
    assert effective_permissions(human, "owner") == allp
    assert effective_permissions(human, "admin") == allp
    assert effective_permissions(human, "member") == {"crew:read", "crew:write", "crew:claim"}
    assert effective_permissions(human, "viewer") == {"crew:read"}
    assert effective_permissions(key, "owner") == {"crew:read", "crew:write", "crew:claim"}
    assert effective_permissions(key_user(role="viewer"), "owner") == {"crew:read"}


# ---------------------------------------------------------------------------
# load_crew (404 semantics, 403, step-up)
# ---------------------------------------------------------------------------


async def test_load_crew_owner_and_members(settings_prod):
    conn = await open_crew_db()
    try:
        await add_crew(conn, CREW_A, OWNER, "yaadbooks")
        await add_member(conn, CREW_A, "u_admin", "admin")
        await add_member(conn, CREW_A, "u_member", "member")
        await add_member(conn, CREW_A, "u_viewer", "viewer")

        acc = await load_crew(conn, CREW_A, key_user(), "crew:claim")
        assert isinstance(acc, CrewAccess)
        assert (acc.crew_id, acc.role, acc.human, acc.crew.project_id) == (CREW_A, "owner", False, "yaadbooks")

        acc = await load_crew(conn, CREW_A, jwt_user("u_admin"), "crew:override", step_up=True, auth_time_ms=now_ms())
        assert acc.role == "admin" and acc.human and acc.can("crew:admin")

        with pytest.raises(HTTPException) as e:
            await load_crew(conn, CREW_A, jwt_user("u_member"), "crew:override")
        assert e.value.status_code == 403 and _body(e.value)["error"] == "crew_role_required"

        await load_crew(conn, CREW_A, key_user("u_viewer", role="admin"), "crew:read")
        with pytest.raises(HTTPException) as e:
            await load_crew(conn, CREW_A, key_user("u_viewer", role="admin"), "crew:write")
        assert e.value.status_code == 403 and _body(e.value)["error"] == "forbidden"
    finally:
        await conn.close()


async def test_load_crew_is_404_for_every_invisible_case(settings_prod):
    conn = await open_crew_db()
    try:
        await add_crew(conn, CREW_A, OWNER, "yaadbooks")
        await add_crew(conn, CREW_B, OTHER, "otherproj")
        cases = [
            ("unknown id", "crw_doesnotexist0000", key_user()),
            ("malformed id", "../../etc", key_user()),
            ("not a string", None, key_user()),
            ("wrong prefix", "tsk_aaaaaaaaaaaaaaaa", key_user()),
            ("another tenant's crew", CREW_B, key_user()),
            ("another tenant's crew via JWT", CREW_B, jwt_user()),
            ("restricted key, other project", CREW_A, key_user(project_ids=["otherproj"])),
        ]
        bodies = []
        for label, crew_id, user in cases:
            with pytest.raises(HTTPException) as e:
                await load_crew(conn, crew_id, user, "crew:read")
            assert e.value.status_code == 404, label
            bodies.append(_body(e.value))
        assert all(b == {"error": "not_found", "message": "Not found."} for b in bodies)
        # Restricted to the crew's project: visible.
        acc = await load_crew(conn, CREW_A, key_user(project_ids=["yaadbooks"]), "crew:read")
        assert acc.crew_id == CREW_A
        # Not visible beats not permitted: a non-member admin key asking for override still gets 404.
        with pytest.raises(HTTPException) as e:
            await load_crew(conn, CREW_B, key_user(), "crew:override")
        assert e.value.status_code == 404
    finally:
        await conn.close()


async def test_admin_api_key_gets_403_on_human_only_permissions(settings_prod):
    conn = await open_crew_db()
    try:
        await add_crew(conn, CREW_A, OWNER, "yaadbooks")
        for perm in ("crew:override", "crew:admin"):
            with pytest.raises(HTTPException) as e:
                await load_crew(conn, CREW_A, key_user(role="admin"), perm)
            assert e.value.status_code == 403
            assert _body(e.value)["error"] == "human_only"
        # The same crew owner through a dashboard login succeeds.
        acc = await load_crew(conn, CREW_A, jwt_user(), "crew:override")
        assert acc.human and acc.can("crew:override")
    finally:
        await conn.close()


async def test_step_up_is_enforced(settings_prod):
    conn = await open_crew_db()
    try:
        await add_crew(conn, CREW_A, OWNER, "yaadbooks")
        now = now_ms()
        await load_crew(conn, CREW_A, jwt_user(), "crew:admin", step_up=True, auth_time_ms=now - 5_000, now_ms=now)
        for stale in (None, now - (STEP_UP_MAX_AGE_S + 1) * 1000):
            with pytest.raises(HTTPException) as e:
                await load_crew(conn, CREW_A, jwt_user(), "crew:admin", step_up=True, auth_time_ms=stale, now_ms=now)
            assert e.value.status_code == 401
            assert _body(e.value)["error"] == "step_up_required"
            assert e.value.headers and "insufficient_user_authentication" in e.value.headers["WWW-Authenticate"]
        # Without step-up the stale login is fine for a human-only, non step-up action.
        await load_crew(conn, CREW_A, jwt_user(), "crew:override", auth_time_ms=None, now_ms=now)
    finally:
        await conn.close()


async def test_unknown_permission_is_a_programming_error():
    conn = await open_crew_db()
    try:
        with pytest.raises(ValueError):
            await load_crew(conn, CREW_A, key_user(), "crew:everything")
        with pytest.raises(ValueError):
            crew_access("crew:read", step_up=True)  # step-up only on human-only permissions
        with pytest.raises(ValueError):
            crew_entity("widget", "crew:read")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# load_crew_entity and same-crew validation
# ---------------------------------------------------------------------------


async def test_entity_tables_exist_in_the_spec_ddl():
    conn = await open_crew_db()
    try:
        for kind, table in ENTITY_TABLES.items():
            cursor = await conn.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cursor.fetchall()}
            assert {"id", "crew_id"} <= cols, (kind, table)
            assert kind in ID_PREFIXES, kind
        # Every entity kind the REST contract names is loadable.
        assert {r.entity for r in ROUTES if r.entity} <= set(ENTITY_TABLES)
    finally:
        await conn.close()


async def test_load_crew_entity_for_every_kind(settings_prod):
    conn = await open_crew_db()
    try:
        await add_crew(conn, CREW_A, OWNER, "yaadbooks")
        await add_crew(conn, CREW_B, OTHER, "otherproj")
        for kind in ENTITY_TABLES:
            mine, theirs = eid(kind, "a"), eid(kind, "b")
            await add_entity(conn, kind, mine, CREW_A)
            await add_entity(conn, kind, theirs, CREW_B)

            ent = await load_crew_entity(conn, kind, mine, key_user(), "crew:read")
            assert isinstance(ent, CrewEntity)
            assert (ent.kind, ent.id, ent.row["crew_id"], ent.access.crew_id) == (kind, mine, CREW_A, CREW_A)

            # Another tenant's entity, an unknown id and a malformed id are indistinguishable.
            wrong_prefix = "zz_" + mine.split("_", 1)[1]
            for bad in (theirs, eid(kind, "missing"), wrong_prefix, mine + "/../x"):
                with pytest.raises(HTTPException) as e:
                    await load_crew_entity(conn, kind, bad, key_user(), "crew:read")
                assert e.value.status_code == 404, (kind, bad)
                assert _body(e.value) == {"error": "not_found", "message": "Not found."}
            # A kind mismatch (a task id used on a claim route) is also 404.
            if kind != "task":
                with pytest.raises(HTTPException) as e:
                    await load_crew_entity(conn, kind, eid("task", "a"), key_user(), "crew:read")
                assert e.value.status_code == 404
            # Human-only on an entity: admin key 403, human owner OK.
            with pytest.raises(HTTPException) as e:
                await load_crew_entity(conn, kind, mine, key_user(role="admin"), "crew:override")
            assert e.value.status_code == 403
            ent = await load_crew_entity(conn, kind, mine, jwt_user(), "crew:override")
            assert ent.access.human
    finally:
        await conn.close()


async def test_require_same_crew_rejects_foreign_and_unknown_ids_alike():
    conn = await open_crew_db()
    try:
        await add_crew(conn, CREW_A, OWNER, "yaadbooks")
        await add_crew(conn, CREW_B, OTHER, "otherproj")
        await add_entity(conn, "task", "tsk_mine", CREW_A)
        await add_entity(conn, "task", "tsk_theirs", CREW_B)
        await add_entity(conn, "message", "msg_mine", CREW_A)
        await add_entity(conn, "session", "cs_mine", CREW_A)
        await require_same_crew(conn, CREW_A, [("task", "tsk_mine"), ("message", "msg_mine"), ("session", "cs_mine")])
        await require_same_crew(conn, CREW_A, [])
        bodies = []
        for ref in (("task", "tsk_theirs"), ("task", "tsk_missing"), ("task", "msg_mine"), ("task", None)):
            with pytest.raises(HTTPException) as e:
                await require_same_crew(conn, CREW_A, [("task", "tsk_mine"), ref])
            assert e.value.status_code == 422
            bodies.append(_body(e.value))
        assert all(b["error"] == "cross_crew_reference" for b in bodies)
        assert len({b["message"] for b in bodies}) == 1
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# HTTP: real auth chain (API keys, JWT from /auth/login), every (H) route of the contract
# ---------------------------------------------------------------------------


def _contract_router() -> APIRouter:
    """Register every crew/entity route and every (H) user route of the REST contract with this module's dependencies."""
    router = APIRouter()
    for r in ROUTES:
        if r.access == "crew":
            dep = crew_access(r.perm, step_up=r.step_up)
        elif r.access == "entity":
            assert r.entity is not None
            dep = crew_entity(r.entity, r.perm, step_up=r.step_up)
        elif r.access == "user" and r.human:
            dep = human_principal(step_up=r.step_up)
        else:
            continue

        def _make(dep: Any) -> Any:
            async def endpoint(ctx: Any = Depends(dep)) -> dict[str, Any]:
                if isinstance(ctx, CrewEntity):
                    return {"crew_id": ctx.access.crew_id, "id": ctx.id, "role": ctx.access.role}
                if isinstance(ctx, CrewAccess):
                    return {"crew_id": ctx.crew_id, "role": ctx.role}
                return {"user_id": ctx.user_id}

            return endpoint

        router.add_api_route(r.path, _make(dep), methods=[r.method])
    return router


def _path_for(route: Any, ids: dict[str, str]) -> str:
    path = route.path
    for name, value in ids.items():
        path = path.replace("{" + name + "}", value)
    return "/api/v1" + path


def _stale_jwt(user_id: str, email: str, minutes_ago: int) -> dict[str, str]:
    issued = now_ms() - minutes_ago * 60_000
    payload = {
        "sub": user_id,
        "email": email,
        "iat": issued // 1000,
        "iat_ms": issued,
        "exp": int(time.time()) + 3600,
        "type": "access",
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, JWT_SECRET, algorithm='HS256')}"}


@asynccontextmanager
async def crew_app(tmp_path: Any, routers: list[APIRouter]):
    conn = await open_crew_db()
    try:
        async with secure_app(tmp_path, [auth.router, admin.router, *routers], state={"crew_db": conn}) as h:
            yield h, conn
    finally:
        await conn.close()


async def _seed_contract_entities(conn: aiosqlite.Connection, crew_id: str, suffix: str) -> dict[str, str]:
    ids = {"crew_id": crew_id, "user_id": "u_someone"}
    for kind in ENTITY_TABLES:
        entity_id = eid(kind, suffix)
        await add_entity(conn, kind, entity_id, crew_id)
        ids[access.ENTITY_PATH_PARAMS.get(kind, f"{kind}_id")] = entity_id
    return ids


async def test_every_human_only_route_admin_key_403_owner_login_ok(tmp_path):
    async with crew_app(tmp_path, [_contract_router()]) as (h, conn):
        owner = await h.create_user("owner-crew@example.com", password="Str0ng!Passw0rd")
        await add_crew(conn, CREW_A, owner, "yaadbooks")
        ids = await _seed_contract_entities(conn, CREW_A, "a")

        admin_key, _ = await h.api_key(owner, "admin")
        key_headers = {"X-API-Key": admin_key}
        login = await h.client.post("/api/v1/auth/login", json={"email": "owner-crew@example.com", "password": "Str0ng!Passw0rd"})
        assert login.status_code == 200, login.text
        fresh = {"Authorization": f"Bearer {login.json()['access_token']}"}
        stale = _stale_jwt(owner, "owner-crew@example.com", minutes_ago=20)

        human_routes = [r for r in ROUTES if r.human and r.access in ("crew", "entity", "user")]
        assert len(human_routes) >= 20
        for r in human_routes:
            url = _path_for(r, ids)
            res = await h.client.request(r.method, url, headers=key_headers)
            assert res.status_code == 403, (r.method, r.path, res.status_code, res.text)
            assert res.json()["detail"]["error"] == "human_only"

            res = await h.client.request(r.method, url, headers=fresh)
            assert res.status_code == 200, (r.method, r.path, res.text)

            res = await h.client.request(r.method, url, headers=stale)
            if r.step_up:
                assert res.status_code == 401, (r.method, r.path, res.text)
                assert res.json()["detail"]["error"] == "step_up_required"
                assert "max_age" in res.headers["www-authenticate"]
            else:
                assert res.status_code == 200, (r.method, r.path, res.text)

        # Non-human routes: the admin key works (it is still the owner's credential).
        for r in ROUTES:
            if r.access in ("crew", "entity") and not r.human:
                res = await h.client.request(r.method, _path_for(r, ids), headers=key_headers)
                assert res.status_code == 200, (r.method, r.path, res.text)


async def test_http_404_for_other_tenants_and_restricted_keys(tmp_path):
    async with crew_app(tmp_path, [_contract_router()]) as (h, conn):
        owner = await h.create_user("a@example.com")
        intruder = await h.create_user("b@example.com")
        await add_crew(conn, CREW_A, owner, "yaadbooks")
        ids = await _seed_contract_entities(conn, CREW_A, "a")
        intruder_key, _ = await h.api_key(intruder, "admin")
        restricted_key, _ = await h.api_key(owner, "editor", project_ids=["elsewhere"])
        allowed_key, _ = await h.api_key(owner, "editor", project_ids=["yaadbooks"])
        unknown_ids = {k: (v[:-1] + "z" if k.endswith("_id") and k != "user_id" else v) for k, v in ids.items()}

        for r in ROUTES:
            if r.access not in ("crew", "entity"):
                continue
            url = _path_for(r, ids)
            for headers in ({"X-API-Key": intruder_key}, h.jwt(intruder, "b@example.com"), {"X-API-Key": restricted_key}):
                res = await h.client.request(r.method, url, headers=headers)
                assert res.status_code == 404, (r.method, r.path, res.text)
                assert res.json()["detail"] == {"error": "not_found", "message": "Not found."}
            res = await h.client.request(r.method, _path_for(r, unknown_ids), headers={"X-API-Key": allowed_key})
            assert res.status_code == 404, (r.method, r.path)
            assert res.json()["detail"] == {"error": "not_found", "message": "Not found."}

        res = await h.client.get(f"/api/v1/crews/{CREW_A}", headers={"X-API-Key": allowed_key})
        assert res.status_code == 200 and res.json() == {"crew_id": CREW_A, "role": "owner"}
        # No credentials at all is still a 401 from the auth layer.
        assert (await h.client.get(f"/api/v1/crews/{CREW_A}")).status_code == 401


async def test_http_crew_role_from_membership(tmp_path):
    async with crew_app(tmp_path, [_contract_router()]) as (h, conn):
        owner = await h.create_user("o@example.com")
        mate = await h.create_user("m@example.com")
        await add_crew(conn, CREW_A, owner, "yaadbooks")
        await add_member(conn, CREW_A, mate, "member")
        mate_jwt = h.jwt(mate, "m@example.com")
        res = await h.client.get(f"/api/v1/crews/{CREW_A}", headers=mate_jwt)
        assert res.status_code == 200 and res.json()["role"] == "member"
        res = await h.client.post(f"/api/v1/crews/{CREW_A}/members", headers=mate_jwt)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "crew_role_required"


async def test_http_503_when_crew_db_is_not_registered(tmp_path):
    router = APIRouter()

    @router.get("/crews/{crew_id}")
    async def get_crew(ctx: CrewAccess = Depends(crew_access("crew:read"))) -> dict[str, str]:
        return {"crew_id": ctx.crew_id}

    async with secure_app(tmp_path, [router]) as h:
        uid = await h.create_user("x@example.com")
        key, _ = await h.api_key(uid, "editor")
        res = await h.client.get(f"/api/v1/crews/{CREW_A}", headers={"X-API-Key": key})
        assert res.status_code == 503 and res.json()["detail"]["error"] == "crew_unavailable"


async def test_admin_permissions_endpoint_reports_the_enforced_mapping(tmp_path):
    async with secure_app(tmp_path, [admin.router]) as h:
        res = await h.client.get("/api/v1/admin/permissions")
        assert res.status_code == 200
        body = res.json()
        assert "crew:override" in body["permissions"]
        assert "crew:override" not in body["roles"]["admin"]
        assert "crew:admin" not in body["roles"]["admin"]
        assert set(body["roles"]["admin"]) == {p.value for p in ROLE_PERMISSIONS[Role.ADMIN]}
        assert "crew:claim" in body["roles"]["editor"]


# ---------------------------------------------------------------------------
# Route-table audit
# ---------------------------------------------------------------------------


def test_route_audit_passes_for_the_full_contract():
    app = FastAPI()
    app.include_router(_contract_router(), prefix="/api/v1")
    assert audit_crew_routes(app.routes, require_all=True) == []


def test_route_audit_reports_every_kind_of_violation():
    app = FastAPI()
    r = APIRouter()

    async def ok() -> None:
        return None

    # Wrong permission on a crew route.
    r.add_api_route("/crews/{crew_id}/snapshot", _endpoint(crew_access("crew:write")), methods=["GET"])
    # No access dependency at all.
    r.add_api_route("/crews/{crew_id}/events", ok, methods=["GET"])
    # Wrong entity kind and wrong step-up.
    r.add_api_route("/claims/{claim_id}/override", _endpoint(crew_entity("task", "crew:override")), methods=["POST"])
    # Two access dependencies.
    r.add_api_route(
        "/tasks/{task_id}",
        _endpoint2(crew_entity("task", "crew:read"), crew_access("crew:read")),
        methods=["GET"],
    )
    # Crew access where the contract says entity.
    r.add_api_route("/zones/{zone_id}", _endpoint(crew_access("crew:write", param="zone_id")), methods=["PATCH"])
    # (H) user route without human_principal.
    r.add_api_route("/notifications/targets", ok, methods=["POST"])
    app.include_router(r, prefix="/api/v1")
    problems = audit_crew_routes(app.routes)
    joined = "\n".join(problems)
    assert "GET /crews/{crew_id}/snapshot: permission 'crew:write'" in joined
    assert "GET /crews/{crew_id}/events: expected exactly one" in joined
    assert "POST /claims/{claim_id}/override: entity kind 'task'" in joined
    assert "POST /claims/{claim_id}/override: step_up=False" in joined
    assert "GET /tasks/{task_id}: expected exactly one crew_access/crew_entity dependency, found 2" in joined
    assert "PATCH /zones/{zone_id}: uses crew access" in joined
    assert "POST /notifications/targets: (H) route without human_principal" in joined
    # Unregistered routes are only reported on request.
    assert not any("not registered" in p for p in problems)
    assert any("not registered" in p for p in audit_crew_routes(app.routes, require_all=True))


def _endpoint(dep: Any) -> Any:
    async def endpoint(ctx: Any = Depends(dep)) -> None:
        return None

    return endpoint


def _endpoint2(dep1: Any, dep2: Any) -> Any:
    async def endpoint(a: Any = Depends(dep1), b: Any = Depends(dep2)) -> None:
        return None

    return endpoint


def test_route_audit_sees_router_level_dependencies_and_nested_includes():
    inner = APIRouter()

    async def snapshot() -> None:
        return None

    inner.add_api_route("/snapshot", snapshot, methods=["GET"])
    middle = APIRouter()
    middle.include_router(inner, prefix="/crews/{crew_id}", dependencies=[Depends(crew_access("crew:read"))])
    app = FastAPI()
    app.include_router(middle, prefix="/api/v1")
    assert audit_crew_routes(app.routes) == []
    # The same route with the wrong router-level permission is reported.
    inner2 = APIRouter()
    inner2.add_api_route("/snapshot", snapshot, methods=["GET"])
    app2 = FastAPI()
    app2.include_router(inner2, prefix="/api/v1/crews/{crew_id}", dependencies=[Depends(crew_access("crew:write"))])
    assert audit_crew_routes(app2.routes) == ["GET /crews/{crew_id}/snapshot: permission 'crew:write', contract says 'crew:read'"]


def _request(headers: dict[str, str]) -> Any:
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""})


def test_jwt_auth_time_reads_only_the_callers_valid_access_token(settings_prod):
    from remembra.auth.users import UserManager
    from remembra.crew.access import jwt_auth_time_ms

    token = UserManager(None, settings_prod.jwt_secret).create_jwt_token(OWNER, "o@example.com")  # type: ignore[arg-type]
    before = now_ms()
    got = jwt_auth_time_ms(_request({"Authorization": f"Bearer {token}"}), jwt_user())
    assert got is not None and abs(got - before) < 5_000
    assert jwt_auth_time_ms(_request({"Authorization": f"Bearer {token}"}), jwt_user(OTHER)) is None  # another user
    assert jwt_auth_time_ms(_request({"Authorization": f"Bearer {token}"}), key_user()) is None  # not a JWT principal
    assert jwt_auth_time_ms(_request({}), jwt_user()) is None
    assert jwt_auth_time_ms(_request({"Authorization": "Bearer not-a-jwt"}), jwt_user()) is None
    forged = jwt.encode(
        {"sub": OWNER, "iat_ms": now_ms(), "type": "access"},
        "a-different-secret-that-is-long-enough-0123456789",
        algorithm="HS256",
    )
    assert jwt_auth_time_ms(_request({"Authorization": f"Bearer {forged}"}), jwt_user()) is None


def test_dev_principal_passes_step_up_only_outside_production():
    previous = config_module._settings
    dev = AuthenticatedUser(user_id="default_user", api_key_id="dev_key", rate_limit_tier="standard")
    try:
        config_module._settings = make_settings(debug=True)
        assert login_is_fresh(dev, None)
        config_module._settings = make_settings(debug=False)
        assert not login_is_fresh(dev, None)
    finally:
        config_module._settings = previous
