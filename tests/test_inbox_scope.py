"""Agent inbox scoping: project-restricted and agent-scoped credentials (Phase 0 security).

Real routes, real API keys / RBAC / JWT validation and a real SQLite inbox
(``security_harness``). Before this fix a key restricted to one project, or
bound to one agent, could read and ack every agent's inbox in every project
of the owner.

Rules under test:

* a project-restricted key sees and acks only rows tagged with one of its
  projects; untagged (legacy) rows are invisible to it;
* an agent-scoped key (or an agent-bound connector grant) reads and acks only
  rows addressed to its own agent; another agent's inbox is a 404;
* unrestricted keys and dashboard logins keep the full owner view, including
  untagged legacy rows;
* a restricted key can only tag new rows with one of its projects.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from remembra.api.v1 import inbox as inbox_api
from remembra.auth.rbac import Role
from remembra.inbox.manager import InboxManager, project_filter
from tests.security_harness import Harness, secure_app

OWNER_EMAIL = "owner-inbox@example.com"


@pytest.fixture()
async def h(tmp_path):
    async with secure_app(tmp_path, [inbox_api.router]) as harness:
        manager = InboxManager(harness.db)
        await manager.init_schema()
        harness.app.state.inbox_manager = manager
        yield harness


async def _key(
    h: Harness, user_id: str, *, projects: list[str] | None = None, agent: str | None = None, role: str = "editor"
) -> str:
    created = await h.keys.create_key(user_id=user_id, name="k", agent_id=agent)
    await h.roles.assign_role(created.id, Role(role), project_ids=projects)
    return created.key


def _hdr(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


async def _send(h: Harness, key: str, to: str, subject: str, **extra: Any) -> dict[str, Any]:
    body = {"to_agent": to, "subject": subject, "body": f"body of {subject}", "from_agent": "codex", **extra}
    resp = await h.client.post("/api/v1/inbox/send", headers=_hdr(key), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _legacy_row(h: Harness, owner: str, inbox_id: str, to: str, subject: str, metadata: str) -> None:
    """A row written before this fix (raw metadata text, no server-set tag)."""
    await h.db.conn.execute(
        "INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata, status, created_at)"
        " VALUES (?, ?, 'codex', ?, ?, 'legacy body', ?, 'unread', '2026-09-01T00:00:00+00:00')",
        (inbox_id, owner, to, subject, metadata),
    )
    await h.db.conn.commit()


async def _seed(h: Harness) -> tuple[str, str]:
    """Owner with rows in alpha, beta and untagged legacy rows; returns (owner_id, owner_key)."""
    owner = await h.create_user(OWNER_EMAIL)
    owner_key = await _key(h, owner)
    await _send(h, owner_key, "claude-code", "alpha for claude", project_id="alpha")
    await _send(h, owner_key, "claude-code", "beta for claude", metadata={"project_id": "beta"})
    await _send(h, owner_key, "codex", "alpha for codex", project_id="alpha")
    await _send(h, owner_key, "claude-code", "untagged for claude")
    await _legacy_row(h, owner, "inbox_legacy_nometa", "claude-code", "legacy untagged", "{}")
    await _legacy_row(h, owner, "inbox_legacy_badjson", "claude-code", "legacy malformed", "not json at all")
    await _legacy_row(h, owner, "inbox_legacy_numeric", "claude-code", "legacy numeric project", '{"project_id": 7}')
    return owner, owner_key


async def _subjects(h: Harness, key: str, path: str = "/api/v1/inbox", **params: Any) -> list[str]:
    resp = await h.client.get(path, headers=_hdr(key), params={"status": "all", **params})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"] if isinstance(body, dict) else body
    return sorted(i["subject"] for i in items)


# ---------------------------------------------------------------------------
# Project restriction
# ---------------------------------------------------------------------------


async def test_project_restricted_key_reads_only_its_projects(h):
    owner, _ = await _seed(h)
    alpha = await _key(h, owner, projects=["alpha"])

    assert await _subjects(h, alpha, agent_id="claude-code") == ["alpha for claude"]
    assert await _subjects(h, alpha, "/api/v1/inbox/messages") == ["alpha for claude", "alpha for codex"]
    # Narrowing into a project outside the allow-list yields nothing, never beta rows.
    assert await _subjects(h, alpha, agent_id="claude-code", project_id="beta") == []
    assert await _subjects(h, alpha, "/api/v1/inbox/messages", scope="unscoped") == []

    resp = await h.client.get("/api/v1/inbox/summary", headers=_hdr(alpha))
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["unread_total"] == 2
    assert {a["agent_id"]: a["unread"] for a in summary["agents"] if a["unread"]} == {"claude-code": 1, "codex": 1}
    dump = json.dumps(summary)
    assert "beta" not in dump


async def test_multi_project_key_sees_each_allowed_project(h):
    owner, _ = await _seed(h)
    both = await _key(h, owner, projects=["alpha", "beta"])
    assert await _subjects(h, both, agent_id="claude-code") == ["alpha for claude", "beta for claude"]
    assert await _subjects(h, both, agent_id="claude-code", project_id="beta") == ["beta for claude"]


async def test_untagged_legacy_rows_hidden_from_restricted_keys_but_visible_to_owner(h):
    owner, owner_key = await _seed(h)
    alpha = await _key(h, owner, projects=["alpha"])
    legacy = {"legacy untagged", "legacy malformed", "legacy numeric project", "untagged for claude"}

    assert not legacy & set(await _subjects(h, alpha, agent_id="claude-code"))
    assert not legacy & set(await _subjects(h, alpha, "/api/v1/inbox/messages"))

    # Unrestricted key: today's full view, malformed metadata included (no 500).
    everything = await _subjects(h, owner_key, agent_id="claude-code")
    assert legacy <= set(everything) and {"alpha for claude", "beta for claude"} <= set(everything)
    assert set(await _subjects(h, owner_key, "/api/v1/inbox/messages", scope="unscoped")) == legacy
    # Dashboard login (JWT): same full view.
    jwt = h.jwt(owner, OWNER_EMAIL)
    resp = await h.client.get("/api/v1/inbox/messages", headers=jwt, params={"status": "all"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 7
    resp = await h.client.get("/api/v1/inbox/summary", headers=jwt)
    assert resp.json()["unread_total"] == 7


async def test_project_restricted_key_cannot_ack_other_projects_rows(h):
    owner, owner_key = await _seed(h)
    alpha = await _key(h, owner, projects=["alpha"])
    beta_row = next(
        r
        for r in (await h.client.get("/api/v1/inbox", headers=_hdr(owner_key), params={"agent_id": "claude-code"})).json()
        if r["subject"] == "beta for claude"
    )
    for target in (beta_row["inbox_id"], "inbox_legacy_nometa", "inbox_does_not_exist"):
        resp = await h.client.post(f"/api/v1/inbox/{target}/ack", headers=_hdr(alpha), json={"result": "done"})
        assert resp.status_code == 404, resp.text
    row = await h.app.state.inbox_manager.get_one(owner, beta_row["inbox_id"])
    assert row["status"] == "unread" and row["ack_at"] is None
    legacy = await h.app.state.inbox_manager.get_one(owner, "inbox_legacy_nometa")
    assert legacy["status"] == "unread"

    alpha_row = (await h.client.get("/api/v1/inbox", headers=_hdr(alpha), params={"agent_id": "claude-code"})).json()[0]
    resp = await h.client.post(f"/api/v1/inbox/{alpha_row['inbox_id']}/ack", headers=_hdr(alpha), json={"result": "done"})
    assert resp.status_code == 200 and resp.json()["status"] == "done"


async def test_restricted_key_send_is_tagged_inside_its_projects(h):
    owner = await h.create_user(OWNER_EMAIL)
    alpha = await _key(h, owner, projects=["alpha"])
    both = await _key(h, owner, projects=["alpha", "beta"])

    # Single-project key: an untagged send lands in its project, whatever metadata said before.
    sent = await _send(h, alpha, "claude-code", "defaulted")
    assert sent["project_id"] == "alpha"
    row = await h.app.state.inbox_manager.get_one(owner, sent["inbox_id"])
    assert row["metadata"]["project_id"] == "alpha"

    # A forged project (body or metadata) outside the allow-list is refused.
    for extra in ({"project_id": "beta"}, {"metadata": {"project_id": "beta"}}):
        resp = await h.client.post(
            "/api/v1/inbox/send",
            headers=_hdr(alpha),
            json={"to_agent": "codex", "subject": "forged", "body": "x", **extra},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "project_forbidden"

    # Several projects: the project must be named (body or the metadata tag older clients send).
    resp = await h.client.post("/api/v1/inbox/send", headers=_hdr(both), json={"to_agent": "codex", "subject": "s", "body": "b"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "project_required"
    tagged = await _send(h, both, "codex", "via metadata", metadata={"project_id": "beta", "via": "mcp"})
    assert tagged["project_id"] == "beta"
    row = await h.app.state.inbox_manager.get_one(owner, tagged["inbox_id"])
    assert row["metadata"] == {"project_id": "beta", "via": "mcp"}


async def test_unrestricted_send_keeps_metadata_and_optional_project(h):
    owner = await h.create_user(OWNER_EMAIL)
    key = await _key(h, owner)
    plain = await _send(h, key, "codex", "plain", metadata={"kind": "directive"})
    assert plain["project_id"] is None
    assert (await h.app.state.inbox_manager.get_one(owner, plain["inbox_id"]))["metadata"] == {"kind": "directive"}
    # A non-string tag is not a project: dropped, the row stays untagged.
    odd = await _send(h, key, "codex", "odd", metadata={"project_id": 5, "x": 1})
    assert odd["project_id"] is None
    assert (await h.app.state.inbox_manager.get_one(owner, odd["inbox_id"]))["metadata"] == {"x": 1}
    # Body project wins over the metadata tag, whitespace is trimmed.
    both = await _send(h, key, "codex", "both", project_id="  gamma ", metadata={"project_id": "delta"})
    assert both["project_id"] == "gamma"
    assert (await h.app.state.inbox_manager.get_one(owner, both["inbox_id"]))["metadata"] == {"project_id": "gamma"}


# ---------------------------------------------------------------------------
# Agent scope
# ---------------------------------------------------------------------------


async def test_agent_scoped_key_reads_only_its_own_inbox(h):
    owner, _ = await _seed(h)
    claude = await _key(h, owner, agent="claude-code")

    own = await _subjects(h, claude, agent_id="claude-code")
    assert "alpha for codex" not in own and "beta for claude" in own

    # Another agent's inbox: 404 with no content, same as a missing inbox.
    resp = await h.client.get("/api/v1/inbox", headers=_hdr(claude), params={"agent_id": "codex"})
    assert resp.status_code == 404
    assert "alpha for codex" not in resp.text

    # Dashboard list: only rows addressed to its agent, even with no filter.
    listed = await _subjects(h, claude, "/api/v1/inbox/messages")
    assert "alpha for codex" not in listed and set(listed) == set(own)
    resp = await h.client.get("/api/v1/inbox/messages", headers=_hdr(claude), params={"agent_id": "codex"})
    assert resp.status_code == 404
    assert await _subjects(h, claude, "/api/v1/inbox/messages", agent_id="claude-code") == own

    resp = await h.client.get("/api/v1/inbox/summary", headers=_hdr(claude))
    summary = resp.json()
    assert {a["agent_id"]: a["unread"] for a in summary["agents"]} == {"claude-code": 6, "codex": 0}
    assert summary["unread_total"] == 6


async def test_agent_scoped_key_cannot_ack_another_agents_row(h):
    owner, owner_key = await _seed(h)
    claude = await _key(h, owner, agent="claude-code")
    codex_row = (await h.client.get("/api/v1/inbox", headers=_hdr(owner_key), params={"agent_id": "codex"})).json()[0]

    resp = await h.client.post(f"/api/v1/inbox/{codex_row['inbox_id']}/ack", headers=_hdr(claude), json={"result": "done"})
    assert resp.status_code == 404
    assert (await h.app.state.inbox_manager.get_one(owner, codex_row["inbox_id"]))["status"] == "unread"

    mine = (await h.client.get("/api/v1/inbox", headers=_hdr(claude), params={"agent_id": "claude-code"})).json()[0]
    resp = await h.client.post(f"/api/v1/inbox/{mine['inbox_id']}/ack", headers=_hdr(claude), json={"note": "seen"})
    assert resp.status_code == 200 and resp.json()["status"] == "read"


async def test_agent_and_project_scoped_key_needs_both(h):
    owner, _ = await _seed(h)
    key = await _key(h, owner, projects=["alpha"], agent="claude-code")
    assert await _subjects(h, key, agent_id="claude-code") == ["alpha for claude"]
    assert await _subjects(h, key, "/api/v1/inbox/messages") == ["alpha for claude"]
    resp = await h.client.get("/api/v1/inbox", headers=_hdr(key), params={"agent_id": "codex"})
    assert resp.status_code == 404
    # Sending still works and is attributed to the key's agent, inside its project.
    sent = await _send(h, key, "codex", "from scoped", from_agent="spoofed")
    row = await h.app.state.inbox_manager.get_one(owner, sent["inbox_id"])
    assert row["from_agent"] == "claude-code" and row["metadata"]["project_id"] == "alpha"


async def test_other_tenant_rows_never_visible(h):
    owner, _ = await _seed(h)
    other = await h.create_user("someone-else@example.com")
    other_key = await _key(h, other)
    assert await _subjects(h, other_key, agent_id="claude-code") == []
    assert (await h.client.get("/api/v1/inbox/summary", headers=_hdr(other_key))).json()["unread_total"] == 0


# ---------------------------------------------------------------------------
# Manager level: the filter itself and the post-v5 (crew) column path
# ---------------------------------------------------------------------------


def test_project_filter_fragments():
    assert project_filter(None) == ("", [])
    assert project_filter([]) == (" AND 0", [])
    sql, params = project_filter(["a", "b"], "a", "project")
    assert sql == " AND project_id IN (?, ?) AND project_id = ? AND project_id IS NOT NULL"
    assert params == ["a", "b", "a"]
    with pytest.raises(ValueError):
        project_filter(None, scope="bogus")


async def test_empty_allow_list_fails_closed(h):
    owner, _ = await _seed(h)
    manager: InboxManager = h.app.state.inbox_manager
    assert await manager.get_for_agent(owner, "claude-code", "all", project_ids=[]) == []
    assert (await manager.list_messages(owner, "all", project_ids=[]))["total"] == 0
    assert (await manager.summary(owner, project_ids=[]))["agents"] == []


async def test_filter_uses_project_column_once_crew_v5_has_run(h):
    """After the crew branch's main-DB v5 adds ``agent_inbox.project_id`` (and backfills it from
    ``metadata.project_id``), the same manager code filters on the column."""
    owner, _ = await _seed(h)
    conn = h.db.conn
    await conn.execute("ALTER TABLE agent_inbox ADD COLUMN project_id TEXT")
    await conn.execute(
        "UPDATE agent_inbox SET project_id = trim(json_extract(metadata, '$.project_id'))"
        " WHERE json_valid(metadata) AND json_type(metadata, '$.project_id') = 'text'"
    )
    await conn.commit()
    manager = InboxManager(h.db)  # a fresh process sees the column
    rows = await manager.get_for_agent(owner, "claude-code", "all", project_ids=["alpha"])
    assert [r["subject"] for r in rows] == ["alpha for claude"]
    sent = await manager.send(owner, "codex", "claude-code", "post v5", "b", project_id="beta")
    cursor = await conn.execute("SELECT project_id, metadata FROM agent_inbox WHERE inbox_id = ?", (sent["inbox_id"],))
    stored = await cursor.fetchone()
    assert stored[0] == "beta" and json.loads(stored[1]) == {"project_id": "beta"}
    rows = await manager.get_for_agent(owner, "claude-code", "all", project_ids=["beta"])
    assert sorted(r["subject"] for r in rows) == ["beta for claude", "post v5"]
    assert await manager.get_one(owner, "inbox_legacy_nometa", project_ids=["alpha"]) is None
    assert await manager.get_one(owner, "inbox_legacy_nometa") is not None


async def test_session_brief_inbox_survives_malformed_legacy_metadata(h):
    """The brief's inbox for a project-restricted caller skips a malformed legacy row
    instead of failing the whole inbox query."""
    from remembra.services.agent_session import AgentSessionService

    owner, _ = await _seed(h)
    service = AgentSessionService(db=h.db)
    restricted = await service._inbox_summary(owner, "claude-code", 10, ["alpha"])
    assert restricted["available"] is True
    assert [i["subject"] for i in restricted["items"]] == ["alpha for claude"]
    full = await service._inbox_summary(owner, "claude-code", 10, None)
    assert full["unread_count"] == 6
