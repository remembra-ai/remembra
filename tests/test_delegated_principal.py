"""M2 gate: delegated principals (the Marshal desk, connector grants) can't write or reach the account.

* Every route of the app, Crew mode's routers included, refuses any method but
  GET, HEAD and OPTIONS to a read-only delegated principal (``marshal:``,
  ``copilot:``): 403 ``delegated_principal_refused``. The app-level dependency
  does it, so a route that forgot its own permission check is covered too.
* The account, billing, key, sharing and admin routers refuse every delegated
  principal (``oauth:`` too) on every method, GET included. Their anonymous
  routes (login, the public plan list) are unaffected: the dependency reads
  only the in-process principal.
* RBAC never hands a delegated principal the editor default of a missing role
  row: VIEWER unless its own scopes hold ``memory:store``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.requests import Request

from remembra.api.v1 import account_review, admin, auth, billing, keys, social_auth, spaces, teams, transfer, users, webhooks
from remembra.auth.middleware import AuthenticatedUser, connector_principal
from remembra.auth.rbac import Role
from remembra.auth.scopes import _get_key_role
from tests.marshal_desk_harness import DeskHarness, desk_app

REFUSED = {
    "detail": {
        "error": "delegated_principal_refused",
        "message": "This needs your own dashboard login. Marshal and connected apps can't do it.",
    }
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
GUARDED_MODULES = (auth, social_auth, account_review, billing, keys, webhooks, teams, spaces, transfer, admin, users)
_PARAM = re.compile(r"\{[^}]+\}")


def _principal(prefix: str, user_id: str, scopes: list[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        api_key_id=f"{prefix}0123456789ab",
        rate_limit_tier="standard",
        name=prefix.rstrip(":"),
        role="viewer",
        scopes=scopes if scopes is not None else ["memory:recall"],
    )


def _fill(path: str) -> str:
    return _PARAM.sub("x", path)


async def _call(h: DeskHarness, principal: AuthenticatedUser, method: str, path: str) -> Any:
    with connector_principal(principal):
        kwargs: dict[str, Any] = {} if method in ("GET", "HEAD", "OPTIONS", "DELETE") else {"json": {}}
        return await h.http.request(method, path, **kwargs)


@pytest.fixture()
def crew_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMEMBRA_CREW_MODE", "1")


@pytest.mark.parametrize("prefix", ["marshal:", "copilot:"])
async def test_every_write_route_refuses_a_read_only_delegated_principal(tmp_path, crew_mode, prefix) -> None:
    async with desk_app(tmp_path, connector=True) as h:
        uid = await h.create_user("delegate@example.com")
        principal = _principal(prefix, uid)
        # Every route the app serves (FastAPI resolves included routers lazily; this walks them all).
        routes = [
            (ctx.path, set(ctx.methods or ()))
            for ctx in iter_route_contexts(h.app.routes)
            if isinstance(ctx.original_route, APIRoute) and set(ctx.methods or ()) & WRITE_METHODS
        ]
        paths = {path for path, _ in routes}
        # The whole surface is here: memories, relay, inbox, keys, billing, OAuth, crew and the desk itself.
        for expected in (
            "/api/v1/memories",
            "/api/v1/session/close",
            "/api/v1/projects/resolve",
            "/api/v1/inbox/send",
            "/api/v1/keys",
            "/oauth/token",
            "/api/v1/marshal/ask",
            "/api/v1/marshal/settings",
        ):
            assert expected in paths, expected
        assert any(p.startswith("/api/v1/crews") for p in paths)
        before = await h.total_changes()
        checked = 0
        for path, methods in routes:
            for method in sorted(methods & WRITE_METHODS):
                res = await _call(h, principal, method, _fill(path))
                assert res.status_code == 403, (method, path, res.status_code, res.text[:200])
                assert res.json() == REFUSED, (method, path)
                checked += 1
        assert checked > 100
        assert await h.total_changes() == before  # nothing was written anywhere


def _guarded_routes() -> list[tuple[str, str]]:
    from remembra.connector.oauth import connections_router

    out: list[tuple[str, str]] = []
    for router in [m.router for m in GUARDED_MODULES] + [connections_router]:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods - {"HEAD"}):
                out.append((method, "/api/v1" + _fill(route.path)))
    return out


@pytest.mark.parametrize("prefix", ["marshal:", "oauth:"])
async def test_account_billing_key_and_admin_routers_refuse_every_delegated_principal(tmp_path, prefix) -> None:
    async with desk_app(tmp_path, connector=True) as h:
        uid = await h.create_user("guarded@example.com")
        principal = _principal(prefix, uid, ["memory:recall", "memory:store"])
        routes = _guarded_routes()
        assert ("GET", "/api/v1/keys") in routes and ("GET", "/api/v1/billing/plans") in routes
        assert ("GET", "/api/v1/connector/connections") in routes and ("POST", "/api/v1/auth/login") in routes
        gets = 0
        for method, path in routes:
            res = await _call(h, principal, method, path)
            assert res.status_code == 403, (prefix, method, path, res.status_code, res.text[:200])
            assert res.json() == REFUSED, (method, path)
            gets += method == "GET"
        assert gets > 20


async def test_anonymous_public_routes_of_guarded_routers_are_unchanged(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        await h.create_user("anon@example.com")
        login = await h.http.post("/api/v1/auth/login", json={"email": "anon@example.com", "password": "wrong-Passw0rd!"})
        assert login.status_code == 401, login.text
        ok = await h.http.post("/api/v1/auth/login", json={"email": "anon@example.com", "password": "Str0ng!Passw0rd"})
        assert ok.status_code == 200 and ok.json().get("access_token")
        plans = await h.http.get("/api/v1/billing/plans")
        assert plans.status_code == 200 and plans.json()
        # A dashboard login still lists its keys and a real API key still works on the guarded routers.
        headers = {"Authorization": f"Bearer {ok.json()['access_token']}"}
        assert (await h.http.get("/api/v1/keys", headers=headers)).status_code == 200


class _SpyRoles:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_role(self, api_key_id: str) -> Any:
        self.calls.append(api_key_id)
        raise AssertionError("RoleManager.get_role must not be called for a delegated principal")


def _request_with(roles: Any) -> Request:
    app = type("App", (), {})()
    app.state = type("State", (), {"role_manager": roles})()
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


@pytest.mark.parametrize(
    ("prefix", "scopes", "role"),
    [
        ("marshal:", ["memory:recall"], Role.VIEWER),
        ("copilot:", ["memory:recall"], Role.VIEWER),
        ("oauth:", ["memory:recall"], Role.VIEWER),
        ("oauth:", [], Role.VIEWER),
        ("oauth:", ["memory:recall", "memory:store"], Role.EDITOR),
    ],
)
async def test_key_role_of_a_delegated_principal_never_falls_back_to_editor(prefix, scopes, role) -> None:
    spy = _SpyRoles()
    principal = _principal(prefix, "u1", scopes)
    principal.project_ids = ["widget"]
    key_role = await _get_key_role(_request_with(spy), principal)
    assert key_role.role == role
    assert key_role.scopes == scopes and key_role.project_ids == ["widget"]
    assert spy.calls == []
    assert key_role.has_project_access("widget") and not key_role.has_project_access("gadget")


async def test_an_api_key_still_gets_its_role_row() -> None:
    class Roles:
        async def get_role(self, api_key_id: str) -> Any:
            from remembra.auth.rbac import KeyRole

            return KeyRole(api_key_id=api_key_id, role=Role.ADMIN)

    key_user = AuthenticatedUser(user_id="u1", api_key_id="key_abc", rate_limit_tier="standard")
    assert (await _get_key_role(_request_with(Roles()), key_user)).role == Role.ADMIN
