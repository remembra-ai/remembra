"""F6: one MCP server process can serve several conversations, so a crew write must not follow another's project.

On stdio the "MCP session" is the OS process, and the Claude desktop app runs one ``remembra-mcp`` for all its
conversations. Before the fix the six crew write tools could not name a project and followed a process-wide
"latest choice wins" pointer, which any conversation's ``session_brief`` or ``crew_status`` moved: a release
note for one project was posted into another project's crew and answered ``SENT``.

Every test goes through ``FastMCP.call_tool`` into the real MCP server module and a live API
(``tests/crew/mcp_support.py``); "two conversations" are two series of calls on the same stdio client.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.crew import schemas as S
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CallerInfo, CrewBridge, CrewUsageError, LocalCrew
from tests.crew.mcp_support import live_server
from tests.crew.test_mcp_crew import (
    OWNER_EMAIL,
    Clock,
    add_zone,
    agent,
    as_json,
    call,
    callsign_in,
    crew_id_of,
    project_name,
    session_row,
    use,
)

WRITE_TOOLS = ("crew_claim", "crew_guard", "crew_task", "crew_say", "crew_checkpoint", "crew_report")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-mcp-scope")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=c))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield c
    server._client = None


def sessions_in(live, project: str) -> list[dict[str, Any]]:
    crews = live.crew_rows("SELECT id FROM crews WHERE project_id = ?", (project,))
    if not crews:
        return []
    return live.crew_rows("SELECT * FROM crew_sessions WHERE crew_id = ?", (crews[0]["id"],))


def messages_in(live, project: str) -> list[dict[str, Any]]:
    crews = live.crew_rows("SELECT id FROM crews WHERE project_id = ?", (project,))
    if not crews:
        return []
    return live.crew_rows("SELECT id, kind FROM crew_messages WHERE crew_id = ?", (crews[0]["id"],))


# ---------------------------------------------------------------------------
# The reported fault: another conversation's session_brief reroutes an unnamed crew write
# ---------------------------------------------------------------------------


def test_unnamed_write_never_follows_another_conversations_brief(live, clock):
    a, b = project_name("bth"), project_name("rem")
    use(agent(live, "claude-desktop", "desktop-shared", "configured-default"))
    # conversation 1 works in project A
    assert call("crew_status", project_id=a).startswith(f"CREW {a} ")
    # conversation 2 (same process) briefs on project B seconds later
    assert json.loads(call("session_brief", project_id=b, compact=True))["project_id"] == b
    # conversation 1 posts its release note without naming a project
    out = call("crew_say", body="Batch 8 is live", kind="note")
    assert not out.startswith("SENT"), out
    assert out.startswith("CREW: "), out
    assert "project_id" in out and a in out and b in out, out
    assert sessions_in(live, b) == [], "the write joined the other conversation's crew"
    assert messages_in(live, b) == [] and messages_in(live, a) == []
    # naming the crew sends it where it belongs, and the reply says where it went
    sent = call("crew_say", body="Batch 8 is live", kind="note", project_id=a)
    assert sent.startswith("SENT msg_"), sent
    assert f"CREW {a}" in sent, sent
    assert [m["kind"] for m in messages_in(live, a)] == ["note"]
    assert messages_in(live, b) == [] and sessions_in(live, b) == []
    # a named write does not move the pointer: an unnamed crew_status still shows conversation 2's project
    assert call("crew_status").startswith(f"CREW {b} ")


def test_a_status_for_another_project_does_not_reroute_a_named_claim_release(live, clock):
    """The second hit in the incident: crew_claim(project_id=A, action="release", zone="api") ran against crew B."""
    a, b = project_name("app"), project_name("other")
    use(agent(live, "claude-desktop", "desktop-shared-2", "configured-default"))
    call("crew_status", project_id=a)
    add_zone(live, a, "api", "src/api/**")
    assert call("crew_claim", zone="api", project_id=a).startswith("GRANTED zone api")
    assert call("crew_status", project_id=b).startswith(f"CREW {b} ")  # the audit conversation looks at B
    refused = call("crew_claim", action="release", zone="api")
    assert refused.startswith("CREW: ") and "project_id" in refused, refused
    released = call("crew_claim", action="release", zone="api", project_id=a)
    assert released.startswith("RELEASED zone api"), released
    assert f"CREW {a}" in released
    crew = crew_id_of(live, a)
    assert live.crew_rows("SELECT id FROM crew_claims WHERE crew_id = ? AND state = 'active'", (crew,)) == []


def test_every_write_tool_takes_a_project_and_names_the_crew_it_used(live, clock):
    a, b = project_name("six"), project_name("elsewhere")
    use(agent(live, "codex", "six-tools", "configured-default"))
    me = callsign_in(call("crew_status", project_id=a))
    add_zone(live, a, "pos", "src/app/pos/**")
    call("crew_status", project_id=b)  # the pointer is on B from here on
    where = f"(in CREW {a} as {me})"
    created = call("crew_task", action="create", title="Export", zones=["pos"], project_id=a)
    assert created.startswith("CREATED T-1"), created
    assert where in created
    assert where in call("crew_task", action="start", task="T-1", project_id=a)
    assert call("crew_guard", paths=["src/app/pos/cart.ts"], project_id=a).split("\n") == ["ALLOW", where]
    assert where in call("crew_claim", paths=["src/app/pos/cart.ts"], project_id=a)
    ckp = call("crew_checkpoint", files_changed=["src/app/pos/cart.ts"], summary="cart", project_id=a)
    assert ckp.startswith("CHECKPOINT ckp_") and where in ckp, ckp
    report = call("crew_report", task="T-1", sections={"done": ["export"]}, project_id=a)
    assert report.startswith("REPORT rpt_") and where in report, report
    said = call("crew_say", body="done", project_id=a)
    assert said.split("\n")[-1] == where, said
    # nothing reached B's crew except the status seat, and the pointer still names B
    assert messages_in(live, b) == []
    crew_b = crew_id_of(live, b)
    assert live.crew_rows("SELECT id FROM crew_tasks WHERE crew_id = ?", (crew_b,)) == []
    assert live.crew_rows("SELECT id FROM crew_checkpoints WHERE crew_id = ?", (crew_b,)) == []
    assert call("crew_status").startswith(f"CREW {b} ")


def test_the_contract_lists_project_id_on_the_six_write_tools():
    for name in WRITE_TOOLS:
        spec = next(t for t in S.MCP_TOOLS if t.name == name)
        param = next((p for p in spec.params if p.name == "project_id"), None)
        assert param is not None and param.type == "str" and not param.required, name
        schema = server.mcp._tool_manager._tools[name].parameters["properties"]
        assert "project_id" in schema, name  # an MCP client can send it: it is no longer dropped silently
    required = {
        "crew_claim": {},
        "crew_guard": {"paths": []},
        "crew_task": {},
        "crew_say": {"body": "x"},
        "crew_checkpoint": {"files_changed": []},
        "crew_report": {"task": "T-1", "sections": {}},
    }
    for name, args in required.items():
        assert S.validate_mcp_call(name, {**args, "project_id": "proj"}) == [], name
        assert S.validate_mcp_call(name, {**args, "project_id": 7}) == ["project_id: expected str"], name


def test_a_usage_error_names_the_crew_it_was_checked_against(live, clock):
    """ "Unknown zone 'api'" came from the wrong crew and gave no hint which one."""
    a = project_name("zones")
    use(agent(live, "codex", "usage-error", a))
    me = callsign_in(call("crew_status", project_id=a))
    out = call("crew_claim", action="release", zone="nope")
    assert out.startswith("CREW: ") and "nope" in out, out
    assert out.endswith(f"(in CREW {a} as {me})"), out


# ---------------------------------------------------------------------------
# Unchanged for a server that only ever saw one project
# ---------------------------------------------------------------------------


def test_one_project_needs_no_project_id_and_the_reply_still_names_the_crew(live, clock, monkeypatch):
    a = project_name("solo")
    use(agent(live, "codex", "solo-conversation", a))
    assert json.loads(call("session_brief", project_id=a, compact=True))["project_id"] == a
    me = callsign_in(call("crew_status"))
    said = call("crew_say", body="hello")
    assert said.startswith("SENT msg_") and said.endswith(f"(in CREW {a} as {me})"), said
    assert call("crew_task", action="list").startswith("NO OPEN TASKS")
    # the first crew tool may still be a write: it joins the configured project (implicit join, WP-11)
    other = project_name("implicit")
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock))  # a fresh server process
    monkeypatch.setattr(server, "_session_projects", {})
    use(agent(live, "codex", "implicit-write", other))
    assert call("crew_say", body="first call").startswith("SENT msg_")
    assert len(sessions_in(live, other)) == 1


def test_a_server_inside_one_agents_process_tree_keeps_latest_choice_wins(monkeypatch):
    """A stdio server crewd recognises as part of one agent's process tree serves one conversation: no guard."""
    who = {"ok": True, "key": "k", "crew_id": "crw_local", "session_id": "cs_local", "project_id": "project-a"}
    monkeypatch.setattr(crew_mod, "local_crew", lambda timeout=1.0: LocalCrew(layout=None, who=dict(who)))
    seen: list[str] = []

    class Probe(CrewBridge):
        def _join_body(self, caller, project, client_session_id, checkout):  # type: ignore[override]
            seen.append(project)
            raise RuntimeError("mcp seat join")

    def caller(chosen: str | None = None) -> CallerInfo:
        return CallerInfo(
            client=None,  # type: ignore[arg-type]
            fingerprint="fp",
            client_session_id="s",
            agent_id="claude-code",
            chosen_project=chosen,
            local=True,
        )

    bridge = Probe()
    first = bridge.write_seat(caller(), None, "crew_say")
    assert first.local is not None and first.project_id == "project-a"
    bridge.note_project(caller(), "project-b")  # the same conversation briefs on B
    with pytest.raises(RuntimeError, match="mcp seat join"):
        bridge.write_seat(caller("project-b"), None, "crew_say")  # follows it, as before
    assert seen == ["project-b"]
    # the same sequence without a hook session (the desktop's shared process) refuses to guess
    monkeypatch.setattr(crew_mod, "local_crew", lambda timeout=1.0: None)
    shared = Probe()
    shared.note_project(caller(), "project-a")
    shared.note_project(caller(), "project-b")
    with pytest.raises(CrewUsageError, match="project_id"):
        shared.write_seat(caller("project-b"), None, "crew_say")
    assert seen == ["project-b"]


# ---------------------------------------------------------------------------
# The same pointer served store_memory and close_session
# ---------------------------------------------------------------------------


def test_store_memory_does_not_follow_another_conversations_brief(live, clock):
    a, b = project_name("mem"), project_name("memother")
    use(agent(live, "claude-desktop", "desktop-mem", "configured-default"))
    assert json.loads(call("session_brief", project_id=a, compact=True))["project_id"] == a
    assert json.loads(call("session_brief", project_id=b, compact=True))["project_id"] == b
    refused = as_json(call("store_memory", content="The export job runs nightly at two."))
    assert refused["status"] == "error", refused
    assert "project_id" in refused["error"] and a in refused["error"] and b in refused["error"], refused
    stored = as_json(call("store_memory", content="The export job runs nightly at two.", project_id=a))
    assert stored["status"] == "stored", stored
    headers = {"X-API-Key": live.state["key"]}
    in_a = live.api("GET", "/memories", headers=headers, params={"project_id": a})
    in_b = live.api("GET", "/memories", headers=headers, params={"project_id": b})
    assert stored["id"] in str(in_a) and stored["id"] not in str(in_b)


def test_close_session_ends_its_own_projects_seat_not_the_pointed_one(live, clock):
    a, b = project_name("closea"), project_name("closeb")
    use(agent(live, "claude-desktop", "desktop-close", "configured-default"))
    me_a = callsign_in(call("crew_status", project_id=a))
    me_b = callsign_in(call("crew_status", project_id=b))  # another conversation: the pointer is on B now
    unnamed = as_json(call("close_session", summary="done", end_reason="done"))
    assert unnamed["status"] == "error" and "project_id" in unnamed["error"], unnamed
    assert session_row(live, a, me_a)["state"] != "ended" and session_row(live, b, me_b)["state"] != "ended"
    closed = as_json(call("close_session", summary="done", end_reason="done", project_id=a))
    assert closed["status"] == "ok" and closed["project_id"] == a, closed
    assert closed["crew"]["session_left"] is True
    assert session_row(live, a, me_a)["state"] == "ended"
    assert session_row(live, b, me_b)["state"] != "ended", "the close ended another conversation's crew session"
    # B's seat is still usable by the conversation that holds it
    assert call("crew_say", body="still here", project_id=b).startswith("SENT msg_")


def test_close_session_by_location_ends_the_seat_of_the_project_it_resolves(live, clock):
    a, b = project_name("locclose"), project_name("locother")
    headers = {"X-API-Key": live.state["key"]}
    live.api(
        "POST", "/projects/resolve", headers=headers, json={"git_remote": f"https://github.com/acme/{a}.git", "hint_project": a}
    )
    use(agent(live, "claude-desktop", "desktop-locclose", "configured-default"))
    status = call("crew_status", git_remote=f"git@github.com:acme/{a}.git")
    assert status.startswith(f"CREW {a} "), status
    me_a = callsign_in(status)
    me_b = callsign_in(call("crew_status", project_id=b))  # the pointer moves to B
    closed = as_json(call("close_session", end_reason="done", git_remote=f"https://github.com/acme/{a}.git"))
    assert closed["status"] == "ok" and closed["project_id"] == a, closed
    assert closed["crew"]["session_left"] is True
    assert session_row(live, a, me_a)["state"] == "ended"
    assert session_row(live, b, me_b)["state"] != "ended"


def test_a_close_for_a_project_without_a_seat_sends_no_other_seats_token(live, clock):
    a, c = project_name("held"), project_name("noseat")
    use(agent(live, "claude-desktop", "desktop-noseat", "configured-default"))
    me_a = callsign_in(call("crew_status", project_id=a))
    closed = as_json(call("close_session", end_reason="done", project_id=c))
    assert closed["status"] == "ok" and closed["project_id"] == c, closed
    assert "crew" not in closed
    assert session_row(live, a, me_a)["state"] != "ended"


def test_an_empty_project_id_counts_as_none(live, clock):
    a = project_name("empty")
    use(agent(live, "codex", "empty-project", a))
    me = callsign_in(call("crew_status", project_id=a))
    assert call("crew_say", body="hello", project_id="").endswith(f"(in CREW {a} as {me})")
