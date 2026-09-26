"""Project-restricted and agent-scoped keys on /conflicts and /webhooks (Phase 0 security).

Both leaked across projects before this fix:

* ``/conflicts`` quotes memory content (``new_fact``, ``existing_content``) and
  ignored the key's project allow-list, so a key restricted to one project read
  (and could resolve or dismiss) every project's conflicts. Conflict detection
  is on by default.
* ``/webhooks`` has no project dimension: a registered webhook receives stored
  facts and recall queries from every project of the account, so a restricted
  key could read beyond its scope by registering one.

Real routes, real API keys / RBAC / JWT (``security_harness``).
"""

from __future__ import annotations

import pytest

from remembra.api.v1 import conflicts as conflicts_api
from remembra.api.v1 import webhooks as webhooks_api
from remembra.auth.rbac import Role
from remembra.extraction.conflicts import ConflictManager, MemoryConflict
from remembra.webhooks import manager as manager_mod
from remembra.webhooks.manager import WebhookManager
from tests.security_harness import Harness, secure_app

PUBLIC_IP = "93.184.216.34"
EMAIL = "conflicts-owner@example.com"


@pytest.fixture
def dns(monkeypatch):
    async def fake_resolve(hostname, port):
        return [PUBLIC_IP]

    monkeypatch.setattr(manager_mod, "_resolve", fake_resolve)


@pytest.fixture()
async def h(tmp_path):
    async with secure_app(tmp_path, [conflicts_api.router, webhooks_api.router]) as harness:
        conflicts = ConflictManager(db=harness.db)
        await conflicts.init_schema()
        webhooks = WebhookManager(harness.db)
        await webhooks.init_schema()
        harness.app.state.conflict_manager = conflicts
        harness.app.state.webhook_manager = webhooks
        yield harness


async def _key(h: Harness, user_id: str, *, projects=None, agent=None, role="editor", scopes=None) -> dict[str, str]:
    created = await h.keys.create_key(user_id=user_id, name="k", agent_id=agent)
    await h.roles.assign_role(created.id, Role(role), scopes=scopes, project_ids=projects)
    return {"X-API-Key": created.key}


async def _seed_conflicts(h: Harness) -> tuple[str, dict[str, str]]:
    owner = await h.create_user(EMAIL)
    manager: ConflictManager = h.app.state.conflict_manager
    for project in ("alpha", "beta"):
        await manager.record(
            MemoryConflict(
                id=f"c-{project}",
                user_id=owner,
                project_id=project,
                new_fact=f"{project.upper()}-FACT new",
                existing_memory_id=f"m-{project}",
                existing_content=f"{project.upper()}-FACT old",
            )
        )
    other = await h.create_user("other-tenant@example.com")
    await manager.record(MemoryConflict(id="c-other", user_id=other, project_id="alpha", new_fact="OTHER-TENANT"))
    return owner, await _key(h, owner, projects=["alpha"])


async def test_restricted_key_lists_and_counts_only_its_projects_conflicts(h):
    owner, alpha = await _seed_conflicts(h)
    resp = await h.client.get("/api/v1/conflicts", headers=alpha)
    assert resp.status_code == 200, resp.text
    assert [c["id"] for c in resp.json()["conflicts"]] == ["c-alpha"]
    assert "BETA-FACT" not in resp.text and "OTHER-TENANT" not in resp.text

    # Asking for another project is refused, as on every other project-scoped route.
    resp = await h.client.get("/api/v1/conflicts", headers=alpha, params={"project_id": "beta"})
    assert resp.status_code == 403

    stats = (await h.client.get("/api/v1/conflicts/stats", headers=alpha)).json()
    assert stats["total"] == 1 and stats["open"] == 1

    # Unrestricted key and dashboard login keep the full view.
    full = await _key(h, owner)
    assert (await h.client.get("/api/v1/conflicts/stats", headers=full)).json()["total"] == 2
    jwt = h.jwt(owner, EMAIL)
    ids = sorted(c["id"] for c in (await h.client.get("/api/v1/conflicts", headers=jwt)).json()["conflicts"])
    assert ids == ["c-alpha", "c-beta"]


async def test_restricted_key_cannot_read_resolve_or_dismiss_other_projects_conflict(h):
    owner, alpha = await _seed_conflicts(h)
    manager: ConflictManager = h.app.state.conflict_manager
    for method, path in (
        ("GET", "/api/v1/conflicts/c-beta"),
        ("POST", "/api/v1/conflicts/c-beta/resolve"),
        ("POST", "/api/v1/conflicts/c-beta/dismiss"),
        ("GET", "/api/v1/conflicts/c-other"),
    ):
        resp = await h.client.request(method, path, headers=alpha, json={} if method == "POST" else None)
        assert resp.status_code == 404, (path, resp.text)
        assert "BETA-FACT" not in resp.text
    assert (await manager.get_conflict("c-beta", owner))["status"] == "open"

    resp = await h.client.post("/api/v1/conflicts/c-alpha/resolve", headers=alpha, json={"resolved_memory_id": "m-alpha"})
    assert resp.status_code == 200 and resp.json()["status"] == "resolved"
    resp = await h.client.get("/api/v1/conflicts/c-alpha", headers=alpha)
    assert resp.status_code == 200 and resp.json()["existing_content"] == "ALPHA-FACT old"


async def test_conflict_routes_require_recall_and_store_permissions(h):
    owner, _ = await _seed_conflicts(h)
    store_only = await _key(h, owner, scopes=["memory:store"])
    assert (await h.client.get("/api/v1/conflicts", headers=store_only)).status_code == 403
    assert (await h.client.get("/api/v1/conflicts/c-alpha", headers=store_only)).status_code == 403
    viewer = await _key(h, owner, role="viewer")
    assert (await h.client.get("/api/v1/conflicts", headers=viewer)).status_code == 200
    assert (await h.client.post("/api/v1/conflicts/c-alpha/dismiss", headers=viewer)).status_code == 403
    assert (await h.app.state.conflict_manager.get_conflict("c-alpha", owner))["status"] == "open"


async def test_empty_allow_list_fails_closed_in_the_manager(h):
    owner, _ = await _seed_conflicts(h)
    manager: ConflictManager = h.app.state.conflict_manager
    assert await manager.list_conflicts(owner, project_ids=[]) == []
    assert await manager.get_conflict("c-alpha", owner, project_ids=[]) is None
    assert (await manager.get_stats(owner, project_ids=[]))["total"] == 0
    assert await manager.dismiss("c-alpha", owner, project_ids=[]) is None


async def test_webhooks_need_an_unrestricted_credential(h, dns):
    owner = await h.create_user(EMAIL)
    body = {"url": "https://hooks.example.com/in", "events": ["*"]}
    for scoped in (
        await _key(h, owner, projects=["alpha"]),
        await _key(h, owner, agent="claude-code"),
        await _key(h, owner, projects=["alpha"], agent="codex"),
    ):
        resp = await h.client.post("/api/v1/webhooks", headers=scoped, json=body)
        assert resp.status_code == 403, resp.text
        assert (await h.client.get("/api/v1/webhooks", headers=scoped)).status_code == 403

    full = await _key(h, owner)
    resp = await h.client.post("/api/v1/webhooks", headers=full, json=body)
    assert resp.status_code == 201, resp.text
    hook_id = resp.json()["id"]
    restricted = await _key(h, owner, projects=["alpha"])
    for method, path in (
        ("GET", f"/api/v1/webhooks/{hook_id}"),
        ("GET", f"/api/v1/webhooks/{hook_id}/deliveries"),
        ("PATCH", f"/api/v1/webhooks/{hook_id}"),
        ("DELETE", f"/api/v1/webhooks/{hook_id}"),
    ):
        resp = await h.client.request(method, path, headers=restricted, json={"active": False} if method == "PATCH" else None)
        assert resp.status_code == 403, (path, resp.text)
    assert (await h.client.get(f"/api/v1/webhooks/{hook_id}", headers=full)).json()["active"] in (True, 1)
    # The dashboard login manages webhooks as before.
    resp = await h.client.get("/api/v1/webhooks", headers=h.jwt(owner, EMAIL))
    assert resp.status_code == 200 and resp.json()["total"] == 1
