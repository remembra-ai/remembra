"""Viewer keys are read-only on every route; roles mean the same thing everywhere.

Truth audit 2026-09-26 (P-347, P-277, P-279): a viewer key could create and
revoke keys, rename keys and create teams; the dashboard offered an ``admin``
role the server refuses; and the role table that most routes checked
(``auth/middleware.py``) had drifted from ``auth/rbac.py`` and from
``GET /admin/permissions``. Explicit scopes also *replaced* a key's role
instead of narrowing it, and a role change did not reach a key already in the
validation cache.

The first test walks every write route the API serves (enumerated from the
production router, so a new route is covered the day it is added) and sends it
a viewer key. All run against the real routers and SQLite with auth enabled.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.routing import iter_route_contexts

from remembra.api.router import api_router
from remembra.api.v1 import admin, keys, memories, teams
from remembra.auth.middleware import AuthenticatedUser, has_permission
from remembra.auth.rbac import Permission, Role
from remembra.cloud.metering import UsageMeter
from remembra.teams.manager import TeamManager
from tests.security_harness import secure_app

ROOT = Path(__file__).resolve().parents[1]
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MEMORY_ID = "11111111-1111-4111-8111-111111111111"

# Routes that take no credential: a key is not what lets them through.
PUBLIC = {
    ("POST", "/api/v1/auth/signup"): "creates an account",
    ("POST", "/api/v1/auth/login"): "signs in with a password",
    ("POST", "/api/v1/auth/forgot-password"): "emails a reset link",
    ("POST", "/api/v1/auth/reset-password"): "takes the emailed reset token",
    ("POST", "/api/v1/auth/oauth/exchange"): "takes a one-time sign-in code",
    ("POST", "/api/v1/billing/webhook/paddle"): "takes a Paddle-signed webhook",
    ("POST", "/api/v1/cloud/verify-email/confirm"): "takes the emailed verification token",
    ("POST", "/api/v1/csp-report"): "takes browser CSP reports",
}

# POST routes that change nothing: a viewer may call them.
READS = {
    ("POST", "/api/v1/memories/recall"): "recall",
    ("POST", "/api/v1/memories/batch/recall"): "recall",
    ("POST", "/api/v1/spaces/recall"): "recall across spaces",
    ("POST", "/api/v1/debug/recall"): "recall with a score breakdown",
    ("POST", "/api/v1/cloud/promo/validate"): "checks a code and redeems nothing",
    ("POST", "/api/v1/meetings/brief"): "builds a brief from the request body and stores nothing",
    ("POST", "/api/v1/meetings/summarize"): "summarizes the request body and stores nothing",
}

# Routes that write only for some inputs. The request given here is the one that writes.
WRITING_REQUESTS: dict[tuple[str, str], dict] = {
    ("POST", "/api/v1/temporal/cleanup"): {"params": {"dry_run": "false"}},
    ("POST", "/api/v1/projects/resolve"): {
        "json": {"git_remote": "https://github.com/acme/app", "hint_project": "app", "bind": True}
    },
    ("POST", "/api/v1/projects/split"): {"json": {"project": "shared", "apply": True}},
    ("POST", "/api/v1/projects/split/undo"): {"json": {"apply": True}},
    ("POST", "/api/v1/billing/checkout"): {"json": {"plan": "pro"}},
}


def _write_routes(app) -> list[tuple[str, str]]:
    routes = set()
    for route in iter_route_contexts(app.routes):
        for method in (route.methods or set()) & WRITE_METHODS:
            routes.add((method, route.path))
    return sorted(routes, key=lambda r: (r[1], r[0]))


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


async def _insert_memory(db, user_id: str, memory_id: str = MEMORY_ID) -> None:
    now = datetime.now(UTC).isoformat()
    await db.conn.execute(
        "INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at) VALUES (?, ?, 'default', 'x', ?, ?)",
        (memory_id, user_id, now, now),
    )
    await db.conn.commit()


# ---------------------------------------------------------------------------
# Every write route
# ---------------------------------------------------------------------------


async def test_viewer_key_is_refused_on_every_write_route(tmp_path):
    async with secure_app(tmp_path, [api_router], prefix="") as h:
        # Cloud routes answer 503 without a meter, before any credential check.
        h.app.state.usage_meter = UsageMeter(h.db)
        uid = await h.create_user("viewer-owner@example.com", verified=True)
        viewer, _ = await h.api_key(uid, "viewer")
        hdr = {"X-API-Key": viewer}

        routes = _write_routes(h.app)
        assert len(routes) > 90, "the production router was not mounted"
        allowed: dict[tuple[str, str], int] = {}
        for method, path in routes:
            if (method, path) in PUBLIC or (method, path) in READS:
                continue
            request = WRITING_REQUESTS.get((method, path), {})
            kwargs: dict = {"headers": hdr, "params": request.get("params")}
            if method != "DELETE":
                kwargs["json"] = request.get("json", {})
            resp = await h.client.request(method, _concrete(path), **kwargs)
            if resp.status_code not in (401, 403):
                allowed[(method, path)] = resp.status_code
        assert not allowed, f"a viewer key was not refused by: {allowed}"
        # The refusals were about the role: the key itself still works for reads.
        assert (await h.client.get("/api/v1/keys", headers=hdr)).status_code == 200

        # Control: an editor key holds every non-admin permission, so no route refuses it for one.
        editor, _ = await h.api_key(uid, "editor")
        refused = []
        for method, path in routes:
            if (method, path) in PUBLIC or path.startswith("/api/v1/admin/"):
                continue
            request = WRITING_REQUESTS.get((method, path), {})
            kwargs = {"headers": {"X-API-Key": editor}, "params": request.get("params")}
            if method != "DELETE":
                kwargs["json"] = request.get("json", {})
            try:
                resp = await h.client.request(method, _concrete(path), **kwargs)
            except Exception:  # noqa: BLE001 - a route past its permission check may need services the harness lacks
                continue
            if resp.status_code == 403 and "Permission denied" in resp.text:
                refused.append((method, path, resp.text))
        assert not refused, refused


def test_route_lists_name_only_real_routes():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(api_router)
    routes = set(_write_routes(app))
    for listed in (PUBLIC, READS, WRITING_REQUESTS):
        assert set(listed) <= routes, set(listed) - routes
    assert not set(PUBLIC) & set(READS)


# ---------------------------------------------------------------------------
# Keys, teams, feedback: viewer refused, editor still works
# ---------------------------------------------------------------------------


async def test_viewer_cannot_create_rename_or_revoke_keys_but_editor_can(tmp_path):
    async with secure_app(tmp_path, [keys.router]) as h:
        uid = await h.create_user("keys@example.com")
        viewer, viewer_id = await h.api_key(uid, "viewer")
        _, other_viewer_id = await h.api_key(uid, "viewer")
        editor, _ = await h.api_key(uid, "editor")
        v, e = {"X-API-Key": viewer}, {"X-API-Key": editor}

        assert (await h.client.get("/api/v1/keys", headers=v)).status_code == 200  # key:list
        assert (await h.client.post("/api/v1/keys", json={"role": "viewer"}, headers=v)).status_code == 403
        assert (await h.client.patch(f"/api/v1/keys/{viewer_id}", json={"name": "x"}, headers=v)).status_code == 403
        assert (await h.client.delete(f"/api/v1/keys/{other_viewer_id}", headers=v)).status_code == 403
        assert (await h.client.delete(f"/api/v1/keys/{viewer_id}", headers=v)).status_code == 403
        assert (await h.keys.get_key_info(other_viewer_id)).active
        assert len(await h.keys.list_keys(uid)) == 3

        r = await h.client.post("/api/v1/keys", json={"role": "viewer", "name": "ci"}, headers=e)
        assert r.status_code == 201, r.text
        minted = r.json()["id"]
        assert (await h.client.patch(f"/api/v1/keys/{minted}", json={"name": "ci-2"}, headers=e)).status_code == 200
        assert (await h.client.delete(f"/api/v1/keys/{minted}", headers=e)).status_code == 200


async def test_viewer_cannot_create_a_team_but_editor_can(tmp_path):
    async with secure_app(tmp_path, [teams.router]) as h:
        manager = TeamManager(h.db)
        await manager.init_schema()
        h.app.state.team_manager = manager
        uid = await h.create_user("teams@example.com")
        viewer, _ = await h.api_key(uid, "viewer")
        editor, _ = await h.api_key(uid, "editor")

        r = await h.client.post("/api/v1/teams", json={"name": "Acme"}, headers={"X-API-Key": viewer})
        assert r.status_code == 403, r.text
        assert await manager.list_user_teams(uid) == []
        r = await h.client.post("/api/v1/teams", json={"name": "Acme"}, headers={"X-API-Key": editor})
        assert r.status_code == 201, r.text


async def test_viewer_cannot_send_recall_feedback_but_editor_can(tmp_path):
    async with secure_app(tmp_path, [memories.router]) as h:
        uid = await h.create_user("feedback@example.com")
        await _insert_memory(h.db, uid)
        viewer, _ = await h.api_key(uid, "viewer")
        editor, _ = await h.api_key(uid, "editor")
        path = f"/api/v1/memories/{MEMORY_ID}/feedback"

        r = await h.client.post(path, json={"signal": "helpful"}, headers={"X-API-Key": viewer})
        assert r.status_code == 403, r.text
        assert await h.db.get_feedback_scores([MEMORY_ID], uid) == {}
        r = await h.client.post(path, json={"signal": "helpful"}, headers={"X-API-Key": editor})
        assert r.status_code == 200, r.text
        assert await h.db.get_feedback_scores([MEMORY_ID], uid) != {}


# ---------------------------------------------------------------------------
# One role model
# ---------------------------------------------------------------------------


def _user(role: str, scopes: list[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(user_id="u", api_key_id="k", rate_limit_tier="standard", role=role, scopes=scopes)


def test_scopes_narrow_a_role_and_never_widen_it():
    assert not has_permission(_user("viewer", ["memory:store"]), "memory:store")
    assert not has_permission(_user("viewer", ["admin:audit"]), "admin:audit")
    assert has_permission(_user("editor", ["memory:recall"]), "memory:recall")
    assert not has_permission(_user("editor", ["memory:recall"]), "memory:store")
    assert not has_permission(_user("not-a-role"), "memory:recall")


async def test_scoped_viewer_key_cannot_write(tmp_path):
    async with secure_app(tmp_path, [memories.router]) as h:
        uid = await h.create_user("scoped@example.com")
        await _insert_memory(h.db, uid)
        scoped, _ = await h.api_key(uid, "viewer", scopes=["memory:store"])
        r = await h.client.post(f"/api/v1/memories/{MEMORY_ID}/pin", headers={"X-API-Key": scoped})
        assert r.status_code == 403, r.text
        assert not (await h.db.get_memory(MEMORY_ID)).get("pinned")


async def test_admin_permissions_endpoint_reports_what_every_route_enforces(tmp_path):
    """GET /admin/permissions must be the table has_permission (and so every route) uses."""
    async with secure_app(tmp_path, [admin.router]) as h:
        uid = await h.create_user("admin@example.com")
        admin_key, _ = await h.api_key(uid, "admin")
        r = await h.client.get("/api/v1/admin/permissions", headers={"X-API-Key": admin_key})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["permissions"]) == {p.value for p in Permission}
        assert set(body["roles"]) == {r.value for r in Role}
        for role, granted in body["roles"].items():
            for perm in Permission:
                assert has_permission(_user(role), perm.value) is (perm.value in granted), (role, perm.value)


async def test_role_change_applies_to_the_next_request(tmp_path):
    async with secure_app(tmp_path, [memories.router, keys.router]) as h:
        uid = await h.create_user("demote@example.com")
        await _insert_memory(h.db, uid)
        key, key_id = await h.api_key(uid, "editor")
        pin = f"/api/v1/memories/{MEMORY_ID}/pin"
        assert (await h.client.post(pin, headers={"X-API-Key": key})).status_code == 200  # validation now cached

        r = await h.client.patch(f"/api/v1/keys/{key_id}", json={"role": "viewer"}, headers=h.jwt(uid))
        assert r.status_code == 200, r.text
        r = await h.client.post(f"/api/v1/memories/{MEMORY_ID}/unpin", headers={"X-API-Key": key})
        assert r.status_code == 403, r.text


async def test_project_restriction_applies_to_the_next_request(tmp_path):
    async with secure_app(tmp_path, [memories.router, keys.router]) as h:
        uid = await h.create_user("narrow@example.com")
        await _insert_memory(h.db, uid)  # in project "default"
        key, key_id = await h.api_key(uid, "editor")
        pin = f"/api/v1/memories/{MEMORY_ID}/pin"
        assert (await h.client.post(pin, headers={"X-API-Key": key})).status_code == 200  # validation now cached

        r = await h.client.patch(f"/api/v1/keys/{key_id}", json={"project_ids": ["elsewhere"]}, headers=h.jwt(uid))
        assert r.status_code == 200, r.text
        r = await h.client.post(f"/api/v1/memories/{MEMORY_ID}/unpin", headers={"X-API-Key": key})
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _dashboard_roles() -> list[str]:
    source = (ROOT / "dashboard/src/lib/keyRoles.ts").read_text()
    match = re.search(r"CREATABLE_KEY_ROLES\s*=\s*\[([^\]]*)\]", source)
    assert match, "CREATABLE_KEY_ROLES not found in dashboard/src/lib/keyRoles.ts"
    return re.findall(r"'([a-z]+)'", match.group(1))


async def test_dashboard_offers_exactly_the_roles_the_server_accepts(tmp_path):
    offered = _dashboard_roles()
    async with secure_app(tmp_path, [keys.router]) as h:
        uid = await h.create_user("dash@example.com")
        accepted = set()
        for role in Role:
            r = await h.client.post("/api/v1/keys", json={"permission": role.value, "name": role.value}, headers=h.jwt(uid))
            assert r.status_code in (201, 403), r.text
            if r.status_code == 201:
                accepted.add(role.value)
    assert set(offered) == accepted == {"editor", "viewer"}
