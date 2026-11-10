"""F45: idle heartbeat closes must not fill a project's brief.

A close that records no work (nothing done, nothing failing, only open todos,
no summary) and repeats the open items an earlier such close of the same
agent in the same place already recorded is skipped as "Last session" and in
Recent, and counted; the brief shows the last handoff with real work. The
wording of a heartbeat drifts from run to run, so the repeat is found by
similarity, not by equal text. A first statement of an open item, a different
open item and a close from another place still show. A close sent as
``end_reason="no_change"`` stores nothing over MCP and is never shown.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.relay.handoff import (
    NO_CHANGE_REASONS,
    RELAY_ROW_SOURCE,
    build_sections,
    repeated_idle_closes,
    sections_have_substance,
)
from remembra.security.untrusted import unwrap_untrusted
from tests.agent_api_harness import build_api

START = datetime.now(UTC) - timedelta(days=2)
WORK = {
    "facts_source": "relay-cli:git",
    "branch": "main",
    "head_commit": "b" * 40,
    "unpushed_commits": 0,
    "commits": [{"sha": "b" * 40, "subject": "feat: tier pricing"}],
    "files_changed": ["src/pricing.py"],
    "tests": [{"cmd": "pytest -q", "passed": True, "summary": "12 passed"}],
}
# One long-lived automation thread: the wording drifts between runs, the content does not.
HEARTBEATS = [
    ("Await the full upgrade and the approved offer check", "Monitor the release task without interrupting it."),
    ("Await the full upgrade and the approved offer check", "Monitor the release task without interrupting it."),
    (
        "Refresh the task status; await the full upgrade and the approved offer check",
        "Monitor the release task without interrupting it.",
    ),
    ("Await the full upgrade and the approved offer check.", "Keep monitoring the release task without interrupting it."),
    ("Await the full upgrade and approved offer check", "Monitor the release task without interrupting it."),
    ("Await the full upgrade and the approved offer check", "Monitor the release task, without interrupting it"),
]


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _close(api, n: int, session: str, facts: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = {
        "agent_id": "codex",
        "session_id": session,
        "project_id": "shop",
        "facts": facts,
        "closed_at": (START + timedelta(minutes=n)).isoformat(),
        **over,
    }
    res = api["http"].post("/api/v1/session/close", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _heartbeat(api, n: int, todo: str, nxt: str, **over: Any) -> dict[str, Any]:
    facts = {
        "todos_open": [todo],
        "next_step": nxt,
        "notes": f"run {n} at 0{n % 10}:1{n % 6} UTC heartbeat: task unchanged at {n:04x}c0de",
    }
    return _close(api, n, f"hb-{n}", facts, end_reason="done", **over)


def _brief(api, **params: Any) -> dict[str, Any]:
    res = api["http"].get("/api/v1/session/brief", params={"project_id": "shop", "agent_id": "claude-code", **params})
    assert res.status_code == 200, res.text
    return res.json()


def test_brief_skips_repeated_idle_closes_and_shows_the_last_real_work(api):
    first = _heartbeat(api, 1, *HEARTBEATS[0])  # the first statement of the open item: stays in the trail
    work = _close(api, 2, "work-1", WORK)
    for n, (todo, nxt) in enumerate(HEARTBEATS, start=3):
        _heartbeat(api, n, todo, nxt)

    brief = _brief(api)
    assert brief["handoff"]["id"] == work["handoff_id"]
    assert brief["handoffs_skipped"] == 0 and brief["handoffs_repeated"] == len(HEARTBEATS)
    rendered = brief["rendered"]
    assert rendered.splitlines()[1].startswith("Handoff health: Ready.")  # graded on the real work, not on a heartbeat
    assert "no git state recorded" not in rendered
    last = next(line for line in rendered.splitlines() if line.startswith("Last session:"))
    assert "feat: tier pricing" in last
    assert (
        "Skipped 6 newer closes that recorded no work and repeated open items an earlier close already recorded"
        " (idle or heartbeat runs)." in rendered
    )
    # Recent holds the one first statement, not a column of heartbeats.
    assert [m["id"] for m in brief["recent"] if m["memory_type"] == "handoff"] == [first["handoff_id"]]
    assert rendered.count("no commits or file changes [1 open]") == 1
    # The trail still has every one of them.
    assert api["http"].get("/api/v1/trail", params={"project_id": "shop"}).json()["total"] == len(HEARTBEATS) + 2


def test_a_run_of_only_idle_closes_collapses_to_its_first(api):
    ids = [_heartbeat(api, n, todo, nxt)["handoff_id"] for n, (todo, nxt) in enumerate(HEARTBEATS, start=1)]
    brief = _brief(api)
    assert brief["handoff"]["id"] == ids[0] and brief["handoffs_repeated"] == len(HEARTBEATS) - 1
    assert brief["recent"] == []


def test_a_new_open_item_another_agent_or_another_place_is_not_a_repeat(api):
    todo, nxt = HEARTBEATS[0]
    _heartbeat(api, 1, todo, nxt)
    # Another agent carrying the same item: its own first statement.
    other = _heartbeat(api, 2, todo, nxt, agent_id="claude-desktop")
    assert _brief(api)["handoff"]["id"] == other["handoff_id"]
    # The same agent with a different open item: news.
    new_item = _heartbeat(api, 3, "Write the importer docs", "Draft the docs outline.")
    brief = _brief(api)
    assert brief["handoff"]["id"] == new_item["handoff_id"] and brief["handoffs_repeated"] == 0
    # The same agent and item, closed from a checkout: a different place.
    project = {"git_remote": "https://github.com/acme/shop.git", "root_path": "/work/shop", "host": "mac", "hint_project": "shop"}
    placed = _heartbeat(api, 4, todo, nxt, project=project, project_id=None)
    assert placed["project_id"] == "shop"
    assert _brief(api)["handoff"]["id"] == placed["handoff_id"]
    # A repeat with a summary is a statement of its own.
    said = _heartbeat(api, 5, todo, nxt, summary="Checked the release task: nothing moved since yesterday.")
    assert _brief(api)["handoff"]["id"] == said["handoff_id"]


def test_a_no_change_close_is_never_the_last_session(api):
    work = _close(api, 1, "work-1", WORK)
    _close(
        api,
        2,
        "idle-1",
        {"todos_open": ["Await the upgrade"], "next_step": "Monitor.", "notes": "unchanged"},
        end_reason="no_change",
    )
    brief = _brief(api)
    assert brief["handoff"]["id"] == work["handoff_id"]
    assert brief["handoffs_skipped"] == 1 and brief["handoffs_repeated"] == 0
    assert "Skipped 1 newer session that recorded nothing" in brief["rendered"]
    # With work in it, the reason does not hide it.
    busy = _close(api, 3, "busy-1", {**WORK, "todos_open": ["ship it"]}, end_reason="no_change")
    assert _brief(api)["handoff"]["id"] == busy["handoff_id"]


def test_no_change_reasons_and_what_still_counts_as_substance():
    idle = build_sections({"todos_open": ["wait"], "next_step": "monitor"}, "monitor")
    for reason in sorted(NO_CHANGE_REASONS) + ["No_Change", " idle "]:
        assert sections_have_substance(idle, notes="unchanged", end_reason=reason) is False
    assert sections_have_substance(idle, notes="unchanged", end_reason="done") is True
    assert sections_have_substance(idle, notes="unchanged", end_reason=None) is True
    for facts in ({"uncommitted_files": ["a.py"]}, {"errors": ["boom"]}, {"commits": [{"sha": "a" * 40, "subject": "x"}]}):
        assert sections_have_substance(build_sections(facts), end_reason="no_change") is True


def _row(ident: str, relay: dict[str, Any] | None, agent: str = "codex", content: str = "") -> dict[str, Any]:
    meta: dict[str, Any] = {"source": "relay", "relay": relay} if relay is not None else {}
    return {"id": ident, "agent_id": agent, "content": content, "source": RELAY_ROW_SOURCE, "metadata": meta, "trust_score": 1.0}


def test_repeats_are_found_only_among_relay_handoffs_that_record_no_work():
    idle = {"agent_id": "codex", "done": [], "failing": [], "not_done": ["TODO: wait for the deploy"], "next": "monitor"}
    newest_first = [
        _row("n5", {**idle, "next": "keep monitoring"}),
        _row("n4", None, content="free-form handoff: wait for the deploy"),  # stored through the memory API
        _row("n3", {**idle, "done": ["abc1234 feat"]}),  # recorded work
        _row("n2", {**idle, "grounding": {"status": "consistent"}}),  # carries a summary
        _row("n1", {**idle, "end_reason": "usage_limit"}),  # a stop notice
        _row("n0", idle),
    ]
    assert repeated_idle_closes(newest_first) == {"n5"}
    # Missing or malformed fields never raise and never match.
    odd = [_row("a", {}), _row("b", {"done": None, "not_done": None}), _row("c", {"not_done": ["TODO: x"], "agent_id": None}), {}]
    assert repeated_idle_closes(odd) == {"a"}  # two closes with nothing at all: the older one already said it
    assert repeated_idle_closes([]) == set()
    # Notes-only closes are compared by their notes.
    notes = [
        _row("m2", {"agent_id": "codex"}, content="Notes (agent): 14:10 UTC heartbeat: task unchanged at 1f2e"),
        _row("m1", {"agent_id": "codex"}, content="Notes (agent): 13:05 UTC heartbeat: task unchanged at 9a7b"),
        _row("m0", {"agent_id": "codex"}, content="Notes (agent): decided to ship the CSV export first"),
    ]
    assert repeated_idle_closes(notes) == {"m2"}


# ---------------------------------------------------------------------------
# MCP close_session: a no-change close stores nothing
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_env(api, monkeypatch):
    http = api["http"]

    class ASGIMemory(Memory):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)
            self._client.close()
            self._client = http

    monkeypatch.setattr(server, "Memory", ASGIMemory)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "shop")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "mcp-f45")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(unwrap_untrusted(raw))


def _total(api) -> int:
    return api["http"].get("/api/v1/trail", params={"project_id": "shop"}).json()["total"]


def test_mcp_no_change_close_stores_nothing(mcp_env):
    out = _j(
        server.close_session(
            todos_open=["Await the upgrade"],
            next_step="Monitor the release task.",
            facts={"notes": "14:51 UTC heartbeat: task unchanged"},
            end_reason="no_change",
            project_id="shop",
        )
    )
    assert out["status"] == "ok" and out["changed"] is False and out["stored"] is False and out["handoff_id"] is None
    assert "nothing stored" in out["skipped"]
    assert _total(mcp_env) == 0
    # The same call as an ordinary close is stored ...
    stored = _j(
        server.close_session(todos_open=["Await the upgrade"], next_step="Monitor.", end_reason="done", project_id="shop")
    )
    assert stored["handoff_id"] and _total(mcp_env) == 1
    # ... and so is a "no change" close that declares work or an unresolved error.
    worked = _j(
        server.close_session(facts={"files_changed": ["src/a.py"]}, end_reason="no_change", project_id="shop", session_id="s2")
    )
    assert worked["handoff_id"] and _total(mcp_env) == 2
    failed = _j(server.close_session(errors=["deploy failed"], end_reason="heartbeat", project_id="shop", session_id="s3"))
    assert failed["handoff_id"] and _total(mcp_env) == 3


def test_close_session_tool_tells_agents_how_to_close_an_idle_run():
    tool = next(t for t in server.mcp._tool_manager.list_tools() if t.name == "close_session")
    assert 'end_reason="no_change"' in tool.description and "nothing is stored" in tool.description
