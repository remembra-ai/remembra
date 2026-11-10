"""F5 (fault survey 2026-11-10): work must not land in the configured project by accident.

What bit real sessions, each covered here through ``FastMCP.call_tool`` against a live API:

* a stdio MCP server whose remembered project was gone (restart, reconnect) sent crew_claim /
  crew_say / crew_checkpoint and a locator-less close_session to ``REMEMBRA_PROJECT`` and said ok;
* the remembered project is one value per MCP process, shared by every conversation an app-level
  server serves, and the writing tools took no project of their own and did not say where they wrote;
* ``crew_status(project_id=...)`` moved the crew tools but not close_session / store_memory;
* a guessed argument name (``project`` for ``project_id``) was dropped without a word;
* a scheme-less remote (``github.com/owner/repo``, the form the server prints) resolved to the
  configured project instead of the repository's.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.crew import schemas as S
from remembra.mcp.crew import CrewBridge
from remembra.relay import identity
from remembra.relay.identity import normalize_git_remote
from remembra.security.untrusted import unwrap_untrusted
from tests.crew.mcp_support import LiveServer, live_server

OWNER_EMAIL = "fs-project@example.com"
WRITING_CREW_TOOLS = ("crew_claim", "crew_guard", "crew_task", "crew_say", "crew_checkpoint", "crew_report")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-project")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture()
def mcp_env(monkeypatch):
    """A fresh MCP process state: no remembered project, no seat, nothing configured in the environment."""
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "default")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.delenv("REMEMBRA_RELAY_PROJECT", raising=False)
    yield
    server._client = None


@pytest.fixture()
def stdio(mcp_env, monkeypatch):
    """What ``main()`` sets for a stdio server: the process an agent host starts on the user's machine."""
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)


_counter = iter(range(1, 10_000))


def project_name(prefix: str) -> str:
    return f"fs{prefix}{next(_counter)}"


def use(live: LiveServer, agent_id: str, session: str, project: str, monkeypatch: Any) -> Memory:
    """The stdio client an MCP server builds from its environment: ``project`` is REMEMBRA_PROJECT."""
    client = Memory(
        base_url=live.url,
        api_key=live.state["key"],
        agent_id=agent_id,
        session_id=session,
        project=project,
        provenance_source="mcp",
    )
    server._client = client
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", project)
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", agent_id)
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", session)
    return client


def call(name: str, **args: Any) -> str:
    out = asyncio.run(server.mcp.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return str(blocks[0].text)


def as_json(text: str) -> dict[str, Any]:
    """A tool result's JSON: plain, or inside the untrusted-data block results with stored content use."""
    try:
        return json.loads(text)
    except ValueError:
        body, _, _ = text.rpartition("\n" + S.DATA_CLOSE)
        return json.loads(unwrap_untrusted(body + "\n" + S.DATA_CLOSE))


def crews(live: LiveServer, project: str) -> list[dict[str, Any]]:
    return live.crew_rows("SELECT id FROM crews WHERE project_id = ?", (project,))


def sessions(live: LiveServer, project: str) -> list[dict[str, Any]]:
    return live.crew_rows("SELECT s.* FROM crew_sessions s JOIN crews c ON c.id = s.crew_id WHERE c.project_id = ?", (project,))


def messages(live: LiveServer, project: str) -> list[str]:
    rows = live.crew_rows(
        "SELECT m.body FROM crew_messages m JOIN crews c ON c.id = m.crew_id WHERE c.project_id = ? ORDER BY m.seq",
        (project,),
    )
    return [str(r["body"]) for r in rows]


def handoff_of(live: LiveServer, project: str) -> dict[str, Any] | None:
    brief = live.api(
        "GET", "/session/brief", headers={"X-API-Key": live.state["key"]}, params={"project_id": project, "recent_n": 0}
    )
    return brief.get("handoff")


# ---------------------------------------------------------------------------
# No silent default on a stdio server
# ---------------------------------------------------------------------------


def test_stdio_crew_tool_with_no_project_chosen_refuses_instead_of_joining_the_configured_project(live, stdio, monkeypatch):
    configured = project_name("dump")
    use(live, "claude-desktop", "sess-nodefault", configured, monkeypatch)
    for tool, args in (
        ("crew_claim", {"zone": "api"}),
        ("crew_say", {"body": "Batch 8 is live"}),
        ("crew_checkpoint", {"files_changed": ["app/page.tsx"]}),
        ("crew_task", {"action": "list"}),
    ):
        out = call(tool, **args)
        assert out.startswith("CREW: No project yet"), (tool, out)
        # the agent is told what to pass, and that the configured project is not used by itself
        assert "root_path" in out and "project_id" in out and configured in out, out
    assert crews(live, configured) == [] and sessions(live, configured) == []


def test_stdio_single_namespace_opt_in_keeps_the_configured_default(live, stdio, monkeypatch):
    configured = project_name("single")
    use(live, "codex", "sess-single", configured, monkeypatch)
    monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", configured)
    out = call("crew_task", action="list")
    assert out.startswith("NO OPEN TASKS"), out
    assert out.rstrip().endswith(f"(project {configured})"), out
    assert [(r["agent_id"], r["client_kind"]) for r in sessions(live, configured)] == [("codex", "mcp")]


def test_stdio_single_namespace_opt_in_files_a_locator_less_close_there(live, stdio, monkeypatch):
    configured, single = project_name("configured"), project_name("single")
    use(live, "codex", "sess-single-close", configured, monkeypatch)
    monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", single)
    out = as_json(call("close_session", facts={"branch": "main"}))
    assert out["status"] == "ok" and out["project_id"] == single and out["project_source"] == "named", out
    assert handoff_of(live, configured) is None


def test_outside_a_stdio_server_the_callers_project_stays_the_default(live, mcp_env, monkeypatch):
    """A remote transport's client carries the project its caller selected per request: unchanged."""
    selected = project_name("selected")
    use(live, "chatgpt", "sess-remote-default", selected, monkeypatch)
    out = call("crew_task", action="list")
    assert out.startswith("NO OPEN TASKS") and "(project" not in out, out
    assert len(sessions(live, selected)) == 1
    closed = as_json(call("close_session", facts={"branch": "main"}))
    assert closed["status"] == "ok" and closed["project_id"] == selected, closed


def test_stdio_close_session_with_no_project_chosen_is_an_error_and_files_nothing(live, stdio, monkeypatch):
    configured = project_name("closedump")
    use(live, "claude-desktop", "sess-close-none", configured, monkeypatch)
    out = as_json(call("close_session", summary="Portal fixes", facts={"branch": "main"}))
    assert out["status"] == "error", out
    assert "root_path" in out["error"] and "project_id" in out["error"] and configured in out["error"], out
    assert handoff_of(live, configured) is None
    # naming the project is all it takes
    ok = as_json(call("close_session", summary="Portal fixes", facts={"branch": "main"}, project_id=configured))
    assert ok["status"] == "ok" and ok["project_id"] == configured, ok
    assert handoff_of(live, configured) is not None


def test_stdio_close_session_after_a_brief_still_defaults_to_the_briefed_project(live, stdio, monkeypatch):
    configured = project_name("briefed")
    use(live, "claude-code", "sess-briefed", configured, monkeypatch)
    brief = as_json(call("session_brief", compact=True))
    assert brief["project_id"] == configured
    out = as_json(call("close_session", facts={"branch": "main"}))
    assert out["status"] == "ok" and out["project_id"] == configured, out


def test_main_marks_a_stdio_server(monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(server, "_log_to_stderr", lambda: None)
    monkeypatch.setattr(server, "_LOCAL_CREW", False)
    monkeypatch.setattr(server, "_STDIO_SERVER", False, raising=False)
    ran: list[str] = []
    monkeypatch.setattr(server.mcp, "run", lambda transport: ran.append(transport))
    server.main()
    assert ran == ["stdio"] and server._STDIO_SERVER is True


# ---------------------------------------------------------------------------
# The project travels with the call, and every writing result says where it wrote
# ---------------------------------------------------------------------------


def test_every_writing_crew_tool_takes_a_project_and_a_root_path():
    tools = server.mcp._tool_manager._tools
    for name in WRITING_CREW_TOOLS:
        properties = tools[name].parameters["properties"]
        assert {"project_id", "root_path"} <= set(properties), (name, sorted(properties))
    for name in ("store_memory", "send_to_inbox"):
        assert "project_id" in tools[name].parameters["properties"], name


def test_a_call_naming_its_project_writes_there_whatever_another_conversation_briefed(live, stdio, monkeypatch):
    """One app-level MCP process serves two conversations: A briefs on its repository, B then posts."""
    theirs, mine = project_name("theirs"), project_name("mine")
    use(live, "claude-desktop", "sess-shared", project_name("configured"), monkeypatch)
    assert as_json(call("session_brief", project_id=theirs, compact=True))["project_id"] == theirs
    out = call("crew_say", body="Batch 8 is live as 5ef303b", kind="note", project_id=mine)
    assert out.startswith("SENT msg_"), out
    assert out.rstrip().endswith(f"(project {mine})"), out
    assert any("Batch 8 is live" in body for body in messages(live, mine))
    assert crews(live, theirs) == []
    # without a project the call follows the latest choice of this MCP session, and says which that is
    again = call("crew_say", body="second note", kind="note")
    assert again.rstrip().endswith(f"(project {mine})"), again


def test_a_call_naming_its_folder_resolves_the_project_like_session_brief(live, stdio, monkeypatch, tmp_path):
    folder = tmp_path / project_name("folder")
    folder.mkdir()
    use(live, "codex", "sess-rootpath", "default", monkeypatch)
    out = call("crew_checkpoint", files_changed=["notes.md"], summary="wrote notes", root_path=str(folder))
    resolved = as_json(call("session_brief", root_path=str(folder), compact=True))["project_id"]
    assert resolved == folder.name
    assert out.rstrip().endswith(f"(project {resolved})"), out
    assert len(sessions(live, resolved)) == 1


def test_a_bad_project_argument_is_refused_before_any_call(live, stdio, monkeypatch):
    project = project_name("badarg")
    use(live, "codex", "sess-badproject", project, monkeypatch)
    monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", project)
    with pytest.raises(ToolError, match="project_id"):
        call("crew_say", body="hi", project_id=["a"])
    assert messages(live, project) == []
    # an empty value names nothing: the call runs where the session already is
    assert call("crew_say", body="hi", project_id="", root_path="").rstrip().endswith(f"(project {project})")
    assert messages(live, project) == ["hi"]


def test_crew_status_with_a_project_also_moves_close_session_and_store_memory(live, stdio, monkeypatch):
    configured, chosen = project_name("configured"), project_name("chosen")
    use(live, "claude-desktop", "sess-status-close", configured, monkeypatch)
    assert call("crew_status", project_id=chosen).startswith(f"CREW {chosen} ")
    stored = as_json(call("store_memory", content="The portal uses server actions.", memory_type="handoff"))
    assert stored["project_id"] == chosen, stored
    closed = as_json(call("close_session", summary="done", facts={"branch": "main"}))
    assert closed["status"] == "ok" and closed["project_id"] == chosen, closed
    assert handoff_of(live, configured) is None


def test_close_session_uses_the_crew_seat_project_when_nothing_else_names_one(live, stdio, monkeypatch):
    """The seat is all that is left (the brief's memory was cleared): the close goes where the crew session is."""
    configured, chosen = project_name("configured"), project_name("seat")
    use(live, "claude-desktop", "sess-seat-close", configured, monkeypatch)
    assert call("crew_status", project_id=chosen).startswith(f"CREW {chosen} ")
    server._session_projects.clear()
    closed = as_json(call("close_session", facts={"branch": "main"}))
    assert closed["status"] == "ok" and closed["project_id"] == chosen, closed
    assert handoff_of(live, configured) is None


def test_store_memory_and_send_to_inbox_take_a_project_and_report_it(live, stdio, monkeypatch):
    configured, other = project_name("configured"), project_name("other")
    use(live, "claude-code", "sess-store", configured, monkeypatch)
    stored = as_json(call("store_memory", content="Deploys go through the release tag.", project_id=other))
    assert stored["status"] == "stored" and stored["project_id"] == other, stored
    default = as_json(call("store_memory", content="A general note about the editor."))
    assert default["project_id"] == configured, default  # general memory keeps the configured namespace
    sent = as_json(call("send_to_inbox", to_agent="codex", subject="review", body="please review", project_id=other))
    assert sent["ok"] is True and sent["project_id"] == other, sent
    rows = live.api("GET", "/inbox", headers={"X-API-Key": live.state["key"]}, params={"agent_id": "codex", "status": "all"})
    items = rows if isinstance(rows, list) else rows.get("items") or rows.get("messages") or []
    row = next(r for r in items if r.get("inbox_id") == sent["inbox_id"])
    assert (row.get("project_id") or (row.get("metadata") or {}).get("project_id")) == other, row


def test_send_to_inbox_follows_the_project_the_session_chose(live, stdio, monkeypatch):
    configured, chosen = project_name("configured"), project_name("chosen")
    use(live, "claude-code", "sess-inbox-chosen", configured, monkeypatch)
    assert as_json(call("session_brief", project_id=chosen, compact=True))["project_id"] == chosen
    sent = as_json(call("send_to_inbox", to_agent="codex", subject="s", body="b"))
    assert sent["project_id"] == chosen, sent
    explicit = as_json(call("send_to_inbox", to_agent="codex", subject="s", body="b", metadata={"project_id": configured}))
    assert explicit["project_id"] == configured, explicit


# ---------------------------------------------------------------------------
# Unknown arguments
# ---------------------------------------------------------------------------


def test_an_unknown_argument_is_refused_and_names_the_real_ones(live, stdio, monkeypatch):
    configured = project_name("unknownarg")
    use(live, "claude-desktop", "sess-unknown", configured, monkeypatch)
    monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", configured)  # the old silent default: the call must still not run
    with pytest.raises(ToolError) as status:
        call("crew_status", project="application-source")
    assert "'project'" in str(status.value) and "project_id" in str(status.value), str(status.value)
    assert "root_path" in str(status.value)  # the valid arguments are listed
    with pytest.raises(ToolError) as close:
        call("close_session", project="application-source", open_work=["x"])
    assert "'open_work'" in str(close.value) and "'project'" in str(close.value) and "todos_open" in str(close.value)
    with pytest.raises(ToolError):
        call("crew_say", body="hi", message="hi")
    assert sessions(live, configured) == [] and handoff_of(live, configured) is None


def test_every_tool_publishes_a_closed_schema_and_still_takes_its_own_arguments(live, stdio, monkeypatch):
    listed = asyncio.run(server.mcp.list_tools())
    assert listed and all(t.inputSchema.get("additionalProperties") is False for t in listed), [
        t.name for t in listed if t.inputSchema.get("additionalProperties") is not False
    ]
    project = project_name("closed")
    use(live, "codex", "sess-closed", "default", monkeypatch)
    assert call("crew_status", project_id=project, verbose=True).startswith(f"CREW {project} ")
    assert as_json(call("resolve_project", git_remote=f"https://github.com/acme/{project}.git"))["project_id"] == project


# ---------------------------------------------------------------------------
# Scheme-less remotes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "github.com/Acme/Widget",
        "github.com/acme/widget.git",
        "git:github.com/acme/widget",
        "  GitHub.com/acme/widget/ ",
        "www.github.com/acme/widget",
    ],
)
def test_scheme_less_and_printed_remotes_normalize_like_a_url(raw):
    assert normalize_git_remote(raw) == "github.com/acme/widget"
    assert normalize_git_remote(raw) == normalize_git_remote("https://github.com/acme/widget.git")
    assert identity.canonical_git_remote(raw) == "https://github.com/acme/widget"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("https://github.com/Owner/Repo.git", "github.com/owner/repo"),
        ("git@github.com:owner/repo", "github.com/owner/repo"),
        ("git:owner/repo", "git/owner/repo"),  # scp-style with an ssh Host alias named "git"
        ("../upstream", None),
        ("./vendor.d/a/b", None),
        ("vendor/a/b", None),  # a relative path: its first segment is not a host name
        ("backup.git/a/b", None),
        ("/srv/git/repo.git", "local:/srv/git/repo"),
        ("", None),
        (None, None),
    ],
)
def test_other_remote_forms_are_read_as_before(raw, normalized):
    assert normalize_git_remote(raw) == normalized
    # only a scheme-less host/owner/repo (or its printed git: form) is rewritten for the wire
    assert identity.canonical_git_remote(raw) == raw


def test_a_scheme_less_remote_resolves_to_the_repository_not_the_configured_project(live):
    headers = {"X-API-Key": live.state["key"]}
    repo = project_name("widget")
    body = {"hint_project": "shared", "hint_scope": "folders", "git_repo": True}
    plain = live.api("POST", "/projects/resolve", headers=headers, json={"git_remote": f"github.com/Acme/{repo}", **body})
    assert (plain["project_id"], plain["source"]) == (repo, "derived"), plain
    assert plain["fingerprint"] == f"git:github.com/acme/{repo}"
    # the key the server prints, pasted back, and the URL forms all name the same project
    for remote in (plain["fingerprint"], f"https://github.com/acme/{repo}.git", f"git@github.com:acme/{repo}"):
        again = live.api("POST", "/projects/resolve", headers=headers, json={"git_remote": remote, **body})
        assert (again["project_id"], again["source"]) == (repo, "known"), (remote, again)


def test_a_remote_the_server_cannot_read_is_an_error_not_the_configured_project(live):
    headers = {"X-API-Key": live.state["key"]}
    refused = live.api(
        "POST",
        "/projects/resolve",
        headers=headers,
        status=400,
        json={"git_remote": "../upstream", "hint_project": "shared", "hint_scope": "folders", "git_repo": True},
    )
    assert "git_remote" in refused["detail"] and "root_path" in refused["detail"], refused
    # a 0.16.0 client (no hint_scope: one namespace for everything) is answered as before
    legacy = live.api("POST", "/projects/resolve", headers=headers, json={"git_remote": "../upstream", "hint_project": "shared"})
    assert (legacy["project_id"], legacy["source"]) == ("shared", "hint"), legacy
    # and so is a location-less call that names no repository at all
    bare = live.api("POST", "/projects/resolve", headers=headers, json={"hint_project": "shared", "hint_scope": "folders"})
    assert (bare["project_id"], bare["source"]) == ("shared", "hint"), bare


def test_mcp_close_with_a_scheme_less_remote_files_under_the_repository(live, stdio, monkeypatch):
    configured, repo = project_name("configured"), project_name("yaad")
    use(live, "claude-code", "sess-schemeless", configured, monkeypatch)
    out = as_json(call("close_session", facts={"branch": "main"}, git_remote=f"github.com/Mani/{repo}"))
    assert out["status"] == "ok" and out["project_id"] == repo, out
    assert out["project_source"] in ("derived", "known"), out
    assert handoff_of(live, configured) is None


def test_the_mcp_client_sends_a_url_an_older_server_reads(mcp_env):
    """Server 0.17.5 reads ``github.com/owner/repo`` as a relative path; the client sends the https form."""
    locator, _ = server._locator(git_remote="git:github.com/Acme/Widget", root_path=None)
    assert locator is not None and locator["git_remote"] == "https://github.com/acme/widget"
    locator, _ = server._locator(git_remote="git@github.com:Acme/Widget.git", root_path=None)
    assert locator is not None and locator["git_remote"] == "git@github.com:Acme/Widget.git"
