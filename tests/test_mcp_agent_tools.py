"""MCP tools end-to-end against the real API (AGT-1/3/4/5/7/8/10/11, ING-24/25).

The MCP server's real ``_get_client`` builds the SDK client from its env
config; ``server.Memory`` is swapped for a subclass whose HTTP client is the
ASGI ``TestClient`` of the production routes (see ``agent_api_harness``).
Tools whose server side needs an LLM/vector index (recall, update) are
exercised against a fake SDK client whose responses are built from the real
API response models.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

import remembra.mcp.server as server
from remembra import __version__
from remembra.client.memory import Memory
from remembra.client.types import EntityItem, MemoryItem, RecallResult
from remembra.models.memory import EntityRef, UpdateResponse
from remembra.security.untrusted import unwrap_untrusted
from tests.agent_api_harness import build_api, row, seed


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


@pytest.fixture()
def mcp_env(api, monkeypatch):
    """MCP server configured like a stdio agent: agent id + project + aliases."""
    http = api["http"]

    class ASGIMemory(Memory):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)
            self._client.close()
            self._client = http

    monkeypatch.setattr(server, "Memory", ASGIMemory)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "claude-code")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "alpha")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {"alpha-old": "alpha"})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "sess-mcp")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    # Tools that return stored content frame it as untrusted data (R-14); the JSON is inside.
    return json.loads(unwrap_untrusted(raw))


# ---------------------------------------------------------------------------
# AGT-1 health_check identity + warnings
# ---------------------------------------------------------------------------


def test_health_check_reports_agent_and_project(mcp_env):
    out = _j(server.health_check())
    assert out["status"] == "ok"
    assert out["agent_id"] == "claude-code"
    assert out["project"] == "alpha"
    assert out["session_id"] == "sess-mcp"
    assert out["client_version"] == __version__
    assert out["warnings"] == []


def test_health_check_warns_when_agent_id_missing_and_version_skew(mcp_env, monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    mcp_env["app"].state.reported_version = "9.9.9"
    out = _j(server.health_check())
    assert out["agent_id"] is None
    assert any("REMEMBRA_AGENT_ID is not set" in w for w in out["warnings"])
    assert any("9.9.9" in w for w in out["warnings"])


def test_health_check_error_still_reports_identity(monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "_current_http_request", lambda: None)
    server._request_api_key.set(None)
    out = _j(server.health_check())
    assert out["status"] == "error" and out["code"] == 401
    assert "project" in out and "warnings" in out


def test_stdio_startup_warns_without_agent_id(monkeypatch, capsys):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "")
    monkeypatch.setattr(server.mcp, "run", lambda transport: None)
    server.main()
    assert "REMEMBRA_AGENT_ID is not set" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AGT-4 provenance + AGT-5 types via store_memory; ING-24/25
# ---------------------------------------------------------------------------


def test_store_memory_stamps_mcp_provenance(mcp_env):
    out = _j(server.store_memory("Decided to ship the brief API first", metadata={"topic": "plan"}))
    assert out["status"] == "stored" and out["stored"] is True
    meta = json.loads(row(mcp_env, out["id"])["metadata"])
    assert meta["agent_id"] == "claude-code"
    assert meta["session_id"] == "sess-mcp"
    assert meta["source"] == "mcp"
    assert meta["client_version"] == __version__
    assert meta["topic"] == "plan"


def test_store_memory_checkpoint_and_metadata_type_mapping(mcp_env):
    cp = _j(server.store_memory("step 2 done", memory_type="checkpoint"))
    assert cp["memory_type"] == "checkpoint" and cp["expires_at"]
    mapped = _j(server.store_memory("[SESSION END] snapshot", metadata={"type": "handoff"}))
    assert row(mcp_env, mapped["id"])["memory_type"] == "handoff"


def test_store_memory_rejects_status_and_unknown_types(mcp_env):
    assert "store_status" in _j(server.store_memory("x", memory_type="status"))["error"]
    assert _j(server.store_memory("x", memory_type="bogus"))["status"] == "error"


def test_store_memory_reports_all_duplicate_as_duplicate(mcp_env):
    service = mcp_env["app"].state.memory_service

    # The duplicate target must be a real, live row: candidates are validated
    # against SQLite (ING-4/6/7), so a vector hit with no row is ignored.
    first = _j(server.store_memory("Alice works at Acme"))
    existing_id = first["id"]

    async def match(**kwargs: Any) -> list[Any]:
        return [(existing_id, 0.97, {"content": "Alice works at Acme"})]

    service.qdrant.search = match
    out = _j(server.store_memory("Alice works at Acme"))
    assert out["status"] == "duplicate"
    assert out["stored"] is False
    assert out["duplicate_of"] == existing_id
    assert "id" not in out


def test_store_memory_destructive_hint_is_honest():
    tool = server.mcp._tool_manager.get_tool("store_memory")
    assert tool is not None and tool.annotations is not None
    assert tool.annotations.destructiveHint is True


# ---------------------------------------------------------------------------
# AGT-3 / AGT-5 session tools
# ---------------------------------------------------------------------------


def test_session_brief_and_status_tools(mcp_env):
    handoff = _j(server.store_memory("[SESSION END] did X, next Y", memory_type="handoff"))
    first = _j(server.store_status("deploy:api", "pushed, not deployed"))
    second = _j(server.store_status("deploy:api", "live"))
    assert second["superseded"] == [first["memory_id"]]
    assert [i["value"] for i in _j(server.list_status())["items"]] == ["live"]

    codex = mcp_env["make_client"](project="alpha", agent_id="codex")
    codex.send_to_inbox(to_agent="claude-code", subject="please review", body="details " * 60)

    brief = _j(server.session_brief())
    assert brief["status"] == "ok"
    assert brief["agent_id"] == "claude-code"
    assert brief["project_id"] == "alpha"
    assert brief["handoff"]["id"] == handoff["id"]
    assert brief["inbox"]["unread_count"] == 1
    assert brief["inbox"]["items"][0]["body_preview"].endswith("...")
    assert [s["value"] for s in brief["status_items"]] == ["live"]
    # Pre-relay keys stay in the default response; the compact text rides along.
    assert {"handoff", "inbox", "status_items", "recent", "known_agents", "warnings"} <= set(brief)
    assert "Last session: claude-code (self-declared)" in brief["brief"] and "[SESSION END] did X, next Y" in brief["brief"]
    assert brief["handoff_id"] == handoff["id"] and brief["inbox_unread"] == 1


def test_session_brief_project_alias_resolves(mcp_env):
    server.store_memory("fact in alpha")
    brief = _j(server.session_brief(project_id="ALPHA-OLD"))
    assert brief["project_id"] == "alpha"
    assert len(brief["recent"]) == 1


# ---------------------------------------------------------------------------
# AGT-7 timeline + AGT-8 list offset
# ---------------------------------------------------------------------------


def test_timeline_tool_filters_dates_server_side(mcp_env):
    for day in (1, 10, 20):
        seed(mcp_env, f"d{day}", f"day {day}", datetime(2026, 3, day, 9, 0))
    out = _j(server.timeline(start_date="2026-03-05", end_date="2026-03-15"))
    assert [m["id"] for m in out["memories"]] == ["d10"]
    assert out["total"] == 1
    newest = _j(server.timeline(order="desc", limit=1))
    assert newest["memories"][0]["id"] == "d20" and newest["total"] == 3
    assert _j(server.timeline(order="sideways"))["status"] == "error"


def test_list_memories_offset_and_next_offset(mcp_env):
    for i in range(3):
        seed(mcp_env, f"m{i}", f"memory {i}", datetime(2026, 4, 1, 10, i))
    page1 = _j(server.list_memories(limit=2, project_id="alpha"))
    page2 = _j(server.list_memories(limit=2, project_id="alpha", offset=page1["next_offset"]))
    assert [m["id"] for m in page1["memories"]] == ["m2", "m1"]
    assert [m["id"] for m in page2["memories"]] == ["m0"]
    assert page2["next_offset"] is None


# ---------------------------------------------------------------------------
# AGT-11 guarded forget
# ---------------------------------------------------------------------------


def test_forget_all_is_project_scoped_dry_run_then_confirmed(mcp_env):
    seed(mcp_env, "a1", "alpha one", datetime(2026, 1, 1))
    seed(mcp_env, "a2", "alpha two", datetime(2026, 1, 2))
    seed(mcp_env, "b1", "beta one", datetime(2026, 1, 3), project_id="beta")

    no_project = _j(server.forget_memories(all_memories=True))
    assert no_project["status"] == "error" and "project_id" in no_project["error"]

    preview = _j(server.forget_memories(all_memories=True, project_id="alpha"))
    assert preview["status"] == "dry_run"
    assert preview["would_delete"] == 2
    assert preview["confirm_phrase"] == "DELETE ALL MEMORIES IN alpha"

    wrong = _j(server.forget_memories(all_memories=True, project_id="alpha", dry_run=False, confirm="yes"))
    assert wrong["status"] == "dry_run" and "nothing was deleted" in wrong["error"]
    assert _j(server.timeline())["total"] == 2

    done = _j(
        server.forget_memories(all_memories=True, project_id="alpha", dry_run=False, confirm="DELETE ALL MEMORIES IN alpha")
    )
    assert done["status"] == "deleted"
    assert _j(server.timeline())["total"] == 0
    assert _j(server.timeline(project_id="beta"))["total"] == 1


def test_forget_requires_exactly_one_target_and_entity_is_honest(mcp_env):
    assert _j(server.forget_memories())["status"] == "error"
    assert _j(server.forget_memories(memory_id="x", all_memories=True))["status"] == "error"
    assert _j(server.forget_memories(entity="Alice"))["status"] == "not_supported"


def test_forget_single_memory_by_id(mcp_env):
    seed(mcp_env, "gone", "to delete", datetime(2026, 1, 1))
    out = _j(server.forget_memories(memory_id="gone"))
    assert out["status"] == "deleted" and out["deleted_memories"] == 1


# ---------------------------------------------------------------------------
# AGT-10 inbox + spaces
# ---------------------------------------------------------------------------


def test_send_to_inbox_warns_on_unknown_agent_and_sets_expiry(mcp_env):
    out = _j(server.send_to_inbox(to_agent="claude_code", subject="s", body="b", expires_in="2d"))
    assert out["ok"] is True
    assert out["from_agent"] == "claude-code"
    assert out["expires_at"]
    assert any("claude_code" in w for w in out["warnings"])
    assert _j(server.send_to_inbox(to_agent="codex", subject="s", body="b", expires_in="soon"))["ok"] is False


def test_send_to_inbox_tags_the_clients_project(mcp_env):
    """Project-restricted readers (the Claude/ChatGPT connector) only see inbox
    messages tagged with their projects, so the desktop tags its sends."""
    assert _j(server.send_to_inbox(to_agent="claude-code", subject="tagged", body="b", metadata={"k": "v"}))["ok"] is True
    assert _j(server.send_to_inbox(to_agent="claude-code", subject="explicit", body="b", metadata={"project_id": "beta"}))["ok"]
    items = {i["subject"]: i for i in _j(server.get_inbox())["items"]}
    assert items["tagged"]["metadata"] == {"k": "v", "project_id": "alpha"}
    assert items["explicit"]["metadata"] == {"project_id": "beta"}


def test_get_inbox_summary_mode(mcp_env):
    codex = mcp_env["make_client"](project="alpha", agent_id="codex")
    codex.send_to_inbox(to_agent="claude-code", subject="long", body="z" * 1000, metadata={"k": "v"})
    summary = _j(server.get_inbox(summary=True))
    item = summary["items"][0]
    assert len(item["body_preview"]) == 203 and "body" not in item and "metadata" not in item
    full = _j(server.get_inbox())
    assert len(full["items"][0]["body"]) == 1000 and full["items"][0]["metadata"] == {"k": "v"}


def test_spaces_list_create_and_share(mcp_env):
    created = _j(server.create_space("fleet", description="all agents"))
    assert created["status"] == "created"
    listed = _j(server.list_spaces())
    assert [s["id"] for s in listed["spaces"]] == [created["id"]]
    stored = _j(server.store_memory("shared decision"))
    shared = _j(server.share_memory(memory_id=stored["id"], space_id=created["id"]))
    assert shared["status"] == "shared"


def test_recent_resource_is_by_time(mcp_env):
    seed(mcp_env, "old", "old", datetime(2025, 1, 1))
    seed(mcp_env, "new", "new", datetime(2026, 1, 1))
    out = _j(server.recent_memories())
    assert [m["id"] for m in out["memories"]] == ["new", "old"]


# ---------------------------------------------------------------------------
# Recall (AGT-8 / RET-9 passthrough) and update (ING-24) against fake clients
# ---------------------------------------------------------------------------


def _fake_recall_client() -> MagicMock:
    client = MagicMock()
    client.recall.return_value = RecallResult(
        context="ctx",
        memories=[
            MemoryItem(
                id="m1",
                content="Mani deployed Remembra to Coolify",
                relevance=0.8123,
                created_at=datetime(2026, 1, 1),
                metadata={"agent_id": "codex", "source_id": "s1"},
                memory_type="fact",
                source_id="s1",
                staleness_warning=True,
                age_days=45,
            )
        ],
        entities=[
            EntityItem(id="1", canonical_name="Remembra", type="product", confidence=1.0),
            EntityItem(id="2", canonical_name="Coolify", type="product", confidence=1.0),
            EntityItem(id="3", canonical_name="A", type="concept", confidence=1.0),
            EntityItem(id="4", canonical_name="bot", type="concept", confidence=1.0),
            EntityItem(id="5", canonical_name="TradeMind", type="product", confidence=1.0),
        ],
    )
    return client


def test_recall_passes_server_knobs_and_trims_entities(monkeypatch):
    client = _fake_recall_client()
    monkeypatch.setattr(server, "_get_client", lambda: client)
    out = _j(
        server.recall_memories(
            query="deploy", retrieval_mode="debug", scope="work", as_of="2026-01-02", max_tokens=400, project_id="p"
        )
    )
    kwargs = client.recall.call_args.kwargs
    assert kwargs["retrieval_mode"] == "debug" and kwargs["scope"] == "work"
    assert kwargs["as_of"] == "2026-01-02" and kwargs["max_tokens"] == 400
    assert kwargs["slim"] is False and kwargs["project_id"] == "p"
    mem = out["memories"][0]
    assert mem["agent_id"] == "codex" and mem["source_id"] == "s1" and mem["memory_type"] == "fact"
    assert mem["staleness_warning"] is True and mem["age_days"] == 45
    assert [e["name"] for e in out["entities"]] == ["Remembra", "Coolify"]
    assert out["entities_total"] == 5


def test_recall_slim_is_sent_to_server(monkeypatch):
    client = _fake_recall_client()
    monkeypatch.setattr(server, "_get_client", lambda: client)
    out = _j(server.recall_memories(query="deploy", slim=True))
    assert client.recall.call_args.kwargs["slim"] is True
    assert set(out) == {"status", "context", "count"}


def test_update_memory_reads_real_response_keys(monkeypatch):
    client = MagicMock()
    client.update.return_value = UpdateResponse(
        id="m1", updated_entities=[EntityRef(id="e1", canonical_name="Acme", type="organization", confidence=0.9)]
    ).model_dump(mode="json")
    monkeypatch.setattr(server, "_get_client", lambda: client)
    out = _j(server.update_memory("m1", "Alice now works at Acme"))
    assert out["id"] == "m1"
    assert out["updated_entities"] == [{"name": "Acme", "type": "organization", "confidence": 0.9}]


def test_relationships_at_hits_the_real_search_route(mcp_env):
    """Regression: the tool called /entities/relationships, which the router
    resolves as GET /entities/{entity_id} -> 404. It must use /relationship-search."""
    from remembra.models.memory import Entity, Relationship

    alice = Entity(canonical_name="Alice", type="person")
    acme = Entity(canonical_name="Acme", type="organization")

    async def _seed() -> None:
        db = mcp_env["app"].state.db
        await db.save_entity(alice, user_id="default_user", project_id="alpha")
        await db.save_entity(acme, user_id="default_user", project_id="alpha")
        await db.save_relationship(Relationship(from_entity_id=alice.id, to_entity_id=acme.id, type="WORKS_AT"))

    mcp_env["http"].portal.call(_seed)
    out = _j(server.relationships_at("Alice"))
    assert out["status"] == "ok", out
    assert out["count"] == 1
    assert out["relationships"][0]["from"] == "Alice" and out["relationships"][0]["to"] == "Acme"
