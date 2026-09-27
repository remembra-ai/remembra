"""An agent-scoped key writes only as its own agent, on every write path.

Before this fix a key scoped to ``codex`` could put ``claude-code`` on a
status value (``POST /session/status``, ``metadata.agent_id``) or on imported
memories (``POST /transfer/import``, each item's ``metadata.agent_id``), and
the timeline showed that agent as the writer. The inbox dropped a spoofed
sender without saying so, and removing a link ignored the agent header.

Now every relay write path treats the claim like ``POST /session/close``
already did: a different agent id in the request or in the
``X-Remembra-Agent-Id`` header is refused with 403 and nothing is stored, and
a write that names no agent is stamped with the key's agent. Unscoped keys
keep the id they send.

Production routes over a real SQLite ``Database`` and a real
``MemoryService`` (``agent_api_harness``; only the vector store and the
embedder are fakes).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from tests.agent_api_harness import build_api, row

HEADER = "X-Remembra-Agent-Id"
DENIED = "This API key is scoped to agent 'codex'; the {where} claims 'claude-code'."


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _as(api, **kwargs: Any) -> AuthenticatedUser:
    user = AuthenticatedUser(user_id="default_user", api_key_id="k1", rate_limit_tier="standard", **kwargs)
    api["app"].dependency_overrides[get_current_user] = lambda: user
    return user


def _call(api, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None, **kw: Any):
    return api["http"].request(method, f"/api/v1{path}", json=body, headers=headers or {}, **kw)


def _meta(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value or "{}")


def _memory_count(api) -> int:
    async def _q() -> int:
        cursor = await api["app"].state.db.conn.execute("SELECT COUNT(*) FROM memories")
        return int((await cursor.fetchone())[0])

    return int(api["http"].portal.call(_q))


def _inbox_row(api, inbox_id: str) -> dict[str, Any]:
    async def _q() -> dict[str, Any] | None:
        return await api["app"].state.inbox_manager.get_one("default_user", inbox_id)

    found = api["http"].portal.call(_q)
    assert found is not None
    return dict(found)


def _inbox_count(api) -> int:
    async def _q() -> int:
        cursor = await api["app"].state.db.conn.execute("SELECT COUNT(*) FROM agent_inbox")
        return int((await cursor.fetchone())[0])

    return int(api["http"].portal.call(_q))


def _links(api, project: str) -> list[dict[str, Any]]:
    res = _call(api, "GET", "/projects/links", params={"project_id": project})
    assert res.status_code == 200, res.text
    return list(res.json()["items"])


def _import_json(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format": "json", "data": json.dumps(items), "project_id": "widget"}


# ---------------------------------------------------------------------------
# POST /session/status
# ---------------------------------------------------------------------------


def test_scoped_key_cannot_put_another_agent_on_a_status_value(api):
    _as(api, agent_id="codex")
    body = {"key": "deploy:web", "value": "green", "project_id": "widget", "metadata": {"agent_id": "claude-code"}}
    res = _call(api, "POST", "/session/status", body)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where="request")
    assert _memory_count(api) == 0


def test_scoped_key_status_refuses_another_agent_in_the_header(api):
    _as(api, agent_id="codex")
    body = {"key": "deploy:web", "value": "green", "project_id": "widget"}
    res = _call(api, "POST", "/session/status", body, headers={HEADER: "claude-code"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where=f"{HEADER} header")
    assert _memory_count(api) == 0


def test_scoped_key_status_is_stamped_with_the_keys_agent(api):
    _as(api, agent_id="codex")
    out = _call(api, "POST", "/session/status", {"key": "deploy:web", "value": "green", "project_id": "widget"}).json()
    assert _meta(row(api, out["memory_id"])["metadata"])["agent_id"] == "codex"
    # Naming its own agent, in the body or the header, is fine.
    body = {"key": "deploy:api", "value": "red", "project_id": "widget", "metadata": {"agent_id": "codex", "by": "ci"}}
    res = _call(api, "POST", "/session/status", body, headers={HEADER: "codex"})
    assert res.status_code == 200, res.text
    meta = _meta(row(api, res.json()["memory_id"])["metadata"])
    assert meta["agent_id"] == "codex" and meta["by"] == "ci"
    timeline = _call(api, "GET", "/timeline", params={"project_id": "widget"}).json()
    assert {item["agent_id"] for item in timeline["memories"]} == {"codex"}


def test_unscoped_key_status_keeps_the_agent_it_names(api):
    _as(api)
    body = {"key": "deploy:web", "value": "green", "project_id": "widget", "metadata": {"agent_id": "claude-code"}}
    out = _call(api, "POST", "/session/status", body).json()
    assert _meta(row(api, out["memory_id"])["metadata"])["agent_id"] == "claude-code"
    out = _call(api, "POST", "/session/status", {"key": "deploy:api", "value": "red", "project_id": "widget"}).json()
    assert "agent_id" not in _meta(row(api, out["memory_id"])["metadata"])


# ---------------------------------------------------------------------------
# POST /transfer/import and /transfer/import/file
# ---------------------------------------------------------------------------


def test_scoped_key_cannot_import_memories_as_another_agent(api):
    _as(api, agent_id="codex")
    items = [
        {"content": "Import probe one about the widget build", "metadata": {"agent_id": "codex"}},
        {"content": "Import probe two about the widget deploy", "metadata": {"agent_id": "claude-code"}},
    ]
    res = _call(api, "POST", "/transfer/import", _import_json(items))
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where="imported memory at index 1")
    assert _memory_count(api) == 0  # refused before anything is stored


def test_scoped_key_file_import_refuses_another_agent(api):
    _as(api, agent_id="codex")
    data = json.dumps([{"content": "File import probe about the widget", "metadata": {"agent_id": "claude-code"}}])
    res = api["http"].post(
        "/api/v1/transfer/import/file",
        params={"format": "json", "project_id": "widget"},
        files={"file": ("export.json", data.encode(), "application/json")},
    )
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where="imported memory at index 0")
    assert _memory_count(api) == 0


def test_scoped_key_import_refuses_another_agent_in_the_header(api):
    _as(api, agent_id="codex")
    items = [{"content": "Header import probe about the widget"}]
    res = _call(api, "POST", "/transfer/import", _import_json(items), headers={HEADER: "claude-code"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where=f"{HEADER} header")
    assert _memory_count(api) == 0


def test_scoped_key_imports_are_stamped_with_the_keys_agent(api):
    _as(api, agent_id="codex")
    items = [
        {"content": "Stamp probe one about the widget build", "metadata": {"topic": "build"}},
        {"content": "Stamp probe two about the widget deploy", "metadata": {"agent_id": "codex"}},
        "Stamp probe three about the widget docs",
    ]
    out = _call(api, "POST", "/transfer/import", _import_json(items)).json()
    assert out["imported"] == 3, out
    metas = [_meta(row(api, d["memory_id"])["metadata"]) for d in out["details"]]
    assert [m["agent_id"] for m in metas] == ["codex", "codex", "codex"]
    assert metas[0]["topic"] == "build"


def test_unscoped_key_import_keeps_each_items_agent(api):
    _as(api)
    items = [
        {"content": "Unscoped import probe about the widget", "metadata": {"agent_id": "claude-code"}},
        "Unscoped import probe without an agent",
    ]
    out = _call(api, "POST", "/transfer/import", _import_json(items)).json()
    assert out["imported"] == 2, out
    metas = [_meta(row(api, d["memory_id"])["metadata"]) for d in out["details"]]
    assert metas[0]["agent_id"] == "claude-code" and "agent_id" not in metas[1]


# ---------------------------------------------------------------------------
# POST /inbox/send
# ---------------------------------------------------------------------------


def test_scoped_key_cannot_send_inbox_messages_as_another_agent(api):
    _as(api, agent_id="codex")
    body = {"to_agent": "gemini", "subject": "review", "body": "please review", "from_agent": "claude-code"}
    res = _call(api, "POST", "/inbox/send", body)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where="request")
    res = _call(api, "POST", "/inbox/send", {**body, "from_agent": None}, headers={HEADER: "claude-code"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where=f"{HEADER} header")
    assert _inbox_count(api) == 0


def test_scoped_key_inbox_messages_are_sent_as_the_keys_agent(api):
    _as(api, agent_id="codex")
    body = {"to_agent": "gemini", "subject": "review", "body": "please review"}
    sent = _call(api, "POST", "/inbox/send", body)
    assert sent.status_code == 201, sent.text
    assert _inbox_row(api, sent.json()["inbox_id"])["from_agent"] == "codex"
    sent = _call(api, "POST", "/inbox/send", {**body, "from_agent": "codex"}, headers={HEADER: "codex"})
    assert sent.status_code == 201, sent.text
    assert _inbox_row(api, sent.json()["inbox_id"])["from_agent"] == "codex"


def test_unscoped_key_inbox_sender_is_what_the_request_names(api):
    _as(api)
    sent = _call(api, "POST", "/inbox/send", {"to_agent": "gemini", "subject": "s", "body": "b", "from_agent": "claude-code"})
    assert sent.status_code == 201, sent.text
    assert _inbox_row(api, sent.json()["inbox_id"])["from_agent"] == "claude-code"


# ---------------------------------------------------------------------------
# /projects/links and /session/close
# ---------------------------------------------------------------------------


def test_scoped_key_links_are_recorded_as_the_keys_agent(api):
    _as(api, agent_id="codex")
    link = {"from_project": "widget", "to_project": "widget-web"}
    res = _call(api, "POST", "/projects/links", link, headers={HEADER: "claude-code"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where=f"{HEADER} header")
    assert _links(api, "widget") == []
    assert _call(api, "POST", "/projects/links", link).status_code == 200
    assert [item["created_by_agent"] for item in _links(api, "widget")] == ["codex"]


def test_scoped_key_cannot_remove_a_link_as_another_agent(api):
    _as(api, agent_id="codex")
    assert _call(api, "POST", "/projects/links", {"from_project": "widget", "to_project": "widget-web"}).status_code == 200
    params = {"from_project": "widget", "to_project": "widget-web"}
    res = _call(api, "DELETE", "/projects/links", headers={HEADER: "claude-code"}, params=params)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == DENIED.format(where=f"{HEADER} header")
    assert len(_links(api, "widget")) == 1  # still there
    res = _call(api, "DELETE", "/projects/links", headers={HEADER: "codex"}, params=params)
    assert res.status_code == 200 and res.json() == {"removed": True}


def test_scoped_key_cannot_close_as_another_agent(api):
    _as(api, agent_id="codex")
    close = {"session_id": "s1", "project_id": "widget", "facts": {}}
    res = _call(api, "POST", "/session/close", {**close, "agent_id": "claude-code"})
    assert res.status_code == 403 and res.json()["detail"] == DENIED.format(where="request")
    res = _call(api, "POST", "/session/close", close, headers={HEADER: "claude-code"})
    assert res.status_code == 403 and res.json()["detail"] == DENIED.format(where=f"{HEADER} header")
    assert _memory_count(api) == 0
    out = _call(api, "POST", "/session/close", close).json()
    assert out["agent_id"] == "codex" and out["agent_verified"] is True
