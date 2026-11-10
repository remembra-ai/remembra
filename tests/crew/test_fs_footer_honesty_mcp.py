"""F15 through the real MCP tools and a live API: session_brief and crew_status say the same thing.

``FastMCP.call_tool`` into the real MCP server module, which calls a live uvicorn API over HTTP with real
API keys, the relay routes, the crew routers and a real ``crew.db`` (``tests/crew/mcp_support.py``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import MCP_FOOTER, CrewBridge, LocalCrew
from remembra.relay import handoff as H
from tests.crew.mcp_support import LiveServer, live_server

AUTOMATIC = "Crew mode is automatic"
MANUAL = "nothing is claimed for you automatically"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-footer")) as s:
        s.state["owner"] = s.user("owner@example.com")
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_LOCAL_CREW", False)
    yield
    server._client = None


_n = iter(range(1, 10_000))


def project_name() -> str:
    return f"fsfoot{next(_n)}"


def agent(live: LiveServer, agent_id: str, session: str, project: str) -> Memory:
    return Memory(
        base_url=live.url,
        api_key=live.state["key"],
        agent_id=agent_id,
        session_id=session,
        project=project,
        provenance_source="mcp",
    )


def call(name: str, **args: Any) -> str:
    out = asyncio.run(server.mcp.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return str(blocks[0].text)


def brief_text(**args: Any) -> str:
    return str(json.loads(call("session_brief", compact=True, **args))["brief"])


def join_hook(live: LiveServer, project: str, agent_id: str, session: str, adapter: str) -> dict[str, Any]:
    return live.api(
        "POST",
        "/crews/join",
        headers={"X-API-Key": live.state["key"]},
        json={
            "project_id": project,
            "agent_id": agent_id,
            "session_id": session,
            "adapter": adapter,
            "client_kind": "hook",
            "source": "startup",
        },
        status=201,
    )


def server_brief(live: LiveServer, project: str, agent_id: str, session: str | None) -> dict[str, Any]:
    params = {"project_id": project, "agent_id": agent_id, "recent_n": 0}
    if session:
        params["session_id"] = session
    return live.api("GET", "/session/brief", headers={"X-API-Key": live.state["key"]}, params=params)


def test_codex_without_a_hook_session_hears_the_same_from_both_tools(live):
    """The reported contradiction: brief "automatic by hook", crew_status "mcp, advisory", two seconds apart."""
    project = project_name()
    server._client = agent(live, "codex", f"{project}-mcp", project)
    status = call("crew_status", project_id=project)  # joins the advisory MCP seat (and creates the crew)
    assert "mcp, advisory" in status and status.rstrip().endswith(MCP_FOOTER), status
    # a Claude Code hook session is live in the same crew, as on the owner's machine ("1 live" was cc-1)
    join_hook(live, project, "claude-code", "cc-hook-1", "claude-code")
    brief = brief_text(project_id=project)
    assert f"CREW {project} (" in brief, brief
    assert AUTOMATIC not in brief, brief
    assert brief.rstrip().split("\n")[-1] == H.crew_footer_no_hook("codex")
    assert MANUAL in brief and MANUAL in status
    again = call("crew_status", project_id=project)
    assert AUTOMATIC not in again and again.rstrip().endswith(MCP_FOOTER), again


def test_the_server_tells_a_resolved_reader_by_its_kind(live):
    project = project_name()
    server._client = agent(live, "codex", f"{project}-mcp", project)
    call("crew_status", project_id=project)
    # the MCP seat reading its own brief (crew_status's request): advisory, as crew_status ends
    seat = server_brief(live, project, "codex", f"{project}-mcp")
    assert seat["crew"]["viewer"] == {"client_kind": "mcp", "enforcement": "advisory"}
    assert seat["rendered"].split("\n")[-1] == MCP_FOOTER
    # a hook-joined Codex is (hook, advisory): its hooks act, so the automatic line is true for it
    join_hook(live, project, "codex", "codex-hook-1", "codex")
    hooked = server_brief(live, project, "codex", "codex-hook-1")
    assert hooked["crew"]["viewer"] == {"client_kind": "hook", "enforcement": "advisory"}
    assert hooked["rendered"].split("\n")[-1] == H.CREW_FOOTER
    join_hook(live, project, "claude-code", "cc-hook-1", "claude-code")
    claude = server_brief(live, project, "claude-code", "cc-hook-1")
    assert claude["crew"]["viewer"] == {"client_kind": "hook", "enforcement": "enforced"}
    assert claude["rendered"].split("\n")[-1] == H.CREW_FOOTER
    # a reader the server cannot resolve while a hook session of that agent is live: no claim either way
    for session in (None, "some-other-session"):
        unknown = server_brief(live, project, "codex", session)
        assert unknown["crew"]["viewer"] is None and unknown["crew"]["hook_live"] == 1
        assert unknown["rendered"].split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
        assert AUTOMATIC not in unknown["rendered"]
    assert brief_text(project_id=project).rstrip().split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    # an ended hook session no longer counts
    live.run(_end_hook_sessions(live, project, "codex"))
    gone = server_brief(live, project, "codex", None)
    assert gone["crew"]["hook_live"] == 0 and gone["crew"]["reader_agent"] == "codex"
    assert gone["rendered"].split("\n")[-1] == H.crew_footer_no_hook("codex")


async def _end_hook_sessions(live: LiveServer, project: str, agent_id: str) -> None:
    db = live.app.state.crew_db
    await db.conn.execute(
        "UPDATE crew_sessions SET state = 'ended' WHERE agent_id = ? AND client_kind = 'hook'"
        " AND crew_id IN (SELECT id FROM crews WHERE project_id = ?)",
        (agent_id, project),
    )
    await db.conn.commit()


def _as_0_17_5(client: Memory) -> None:
    """Make the client see what the production 0.17.5 server sends: the constant footer, no viewer fields."""
    real = client.session_brief

    def old(**kw: Any) -> dict[str, Any]:
        brief = real(**kw)
        lines = [H.CREW_FOOTER if H.is_crew_footer(ln) else ln for ln in str(brief["rendered"]).split("\n")]
        brief["rendered"] = "\n".join(lines)
        if isinstance(brief.get("crew"), dict):
            for key in ("viewer", "hook_live", "reader_agent"):
                brief["crew"].pop(key, None)
        return brief

    client.session_brief = old  # type: ignore[method-assign]


def test_a_fixed_client_against_the_old_server_does_not_repeat_its_line(live, monkeypatch):
    project = project_name()
    client = agent(live, "codex", f"{project}-mcp", project)
    server._client = client
    status = call("crew_status", project_id=project)
    _as_0_17_5(client)
    assert client.session_brief(project_id=project)["rendered"].endswith(H.CREW_FOOTER)  # the old server's text
    brief = brief_text(project_id=project)
    assert AUTOMATIC not in brief, brief
    assert brief.rstrip().split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    assert "crew_status" in brief.rstrip().split("\n")[-1] and MANUAL in status
    # the full (non-compact) result carries the same corrected text
    assert AUTOMATIC not in call("session_brief", project_id=project)

    # on the agent's machine with a hook session for THIS project, the automatic line is true and kept
    monkeypatch.setattr(server, "_LOCAL_CREW", True)
    who = {"ok": True, "key": "k", "crew_id": "crew_x", "session_id": "cs_x", "project_id": project}
    monkeypatch.setattr(crew_mod, "local_crew", lambda timeout=1.0: LocalCrew(SimpleNamespace(), who))
    assert brief_text(project_id=project).rstrip().split("\n")[-1] == H.CREW_FOOTER
    # ... but a hook session in ANOTHER project's crew says nothing about this brief
    other = {**who, "project_id": "another-project"}
    monkeypatch.setattr(crew_mod, "local_crew", lambda timeout=1.0: LocalCrew(SimpleNamespace(), other))
    assert brief_text(project_id=project).rstrip().split("\n")[-1] == H.CREW_FOOTER_UNKNOWN
    # ... and a remote transport never asks this machine's crewd
    monkeypatch.setattr(server, "_LOCAL_CREW", False)
    monkeypatch.setattr(crew_mod, "local_crew", lambda timeout=1.0: (_ for _ in ()).throw(AssertionError("asked crewd")))
    assert brief_text(project_id=project).rstrip().split("\n")[-1] == H.CREW_FOOTER_UNKNOWN


def test_a_local_hook_session_overrules_a_fixed_servers_unknown(live, monkeypatch):
    project = project_name()
    server._client = agent(live, "claude-code", f"{project}-mcp", project)
    call("crew_status", project_id=project)
    assert brief_text(project_id=project).rstrip().split("\n")[-1] == H.crew_footer_no_hook("claude-code")
    monkeypatch.setattr(server, "_LOCAL_CREW", True)
    who = {"ok": True, "key": "k", "crew_id": "crew_x", "session_id": "cs_x", "project_id": project}
    monkeypatch.setattr(crew_mod, "local_crew", lambda timeout=1.0: LocalCrew(SimpleNamespace(), who))
    assert brief_text(project_id=project).rstrip().split("\n")[-1] == H.CREW_FOOTER


def test_a_brief_without_a_crew_block_is_untouched(live):
    project = project_name()
    server._client = agent(live, "codex", f"{project}-mcp", project)
    brief = brief_text(project_id=project)  # no crew for this project yet
    assert "CREW " not in brief and AUTOMATIC not in brief and MANUAL not in brief


def test_a_baton_in_crew_status_leads_with_crew_claim_for_the_mcp_seat(live):
    project = project_name()
    a = agent(live, "claude-desktop", f"{project}-a", project)
    server._client = a
    call("crew_status", project_id=project)
    assert "art/lls/cover.psd" in call("crew_claim", paths=["art/lls/cover.psd"])
    released = call("crew_claim", action="release", paths=["art/lls/cover.psd"], baton=True, reason="out of credits soon")
    assert "reserved" in released, released
    server._client = agent(live, "codex", f"{project}-b", project)
    status = call("crew_status", project_id=project)
    assert "YOUR BATON (offered to you): path art/lls/cover.psd" in status, status
    assert '  To continue: crew_claim(action="adopt", paths=["art/lls/cover.psd"])' in status, status
    assert "remembra-crew adopt" not in status, status
    # with saved work, the line says where it can be restored instead of offering a command that refuses here
    live.run(_set_baton_ref(live, "art/lls/cover.psd", "refs/remembra/baton/cs_fs/1"))
    status = call("crew_status", project_id=project)
    line = next(ln for ln in status.split("\n") if ln.startswith("  To continue"))
    assert line == (
        '  To continue: crew_claim(action="adopt", paths=["art/lls/cover.psd"])'
        "   (the saved work is restored only from a session the crew hooks joined: remembra-crew adopt --path art/lls/cover.psd)"
    ), status
    # and the form it is given works, without promising a restore this seat cannot do
    adopted = call("crew_claim", action="adopt", paths=["art/lls/cover.psd"])
    assert adopted.startswith("ADOPTED art/lls/cover.psd: you now hold"), adopted
    assert "saved as refs/remembra/baton/cs_fs/1. It is NOT restored into your checkout" in adopted, adopted
    assert "the remembra-crew CLI restores it" not in adopted


async def _set_baton_ref(live: LiveServer, path: str, ref: str) -> None:
    db = live.app.state.crew_db
    await db.conn.execute("UPDATE crew_claims SET baton_ref = ? WHERE path_glob = ?", (ref, path))
    await db.conn.commit()
