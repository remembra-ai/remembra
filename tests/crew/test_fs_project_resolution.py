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
    # a failure comes back as a CallToolResult (isError set, same text); a success as (content, structured)
    blocks = out[0] if isinstance(out, tuple) else getattr(out, "content", out)
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
    assert out["status"] == "ok" and out["project_id"] == single and out["project_source"] == "relay-project", out
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
    # the named call moved nothing: without a project the next call follows what the session chose (the
    # brief), and says which project that is
    again = call("crew_say", body="second note", kind="note")
    assert again.rstrip().endswith(f"(project {theirs})"), again


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
        # `project` itself is read as project_id since F22 (tests/crew/test_fs_undeclared_arguments.py), so the
        # refusal is shown with a name crew_status still does not have
        call("crew_status", workspace="application-source")
    assert "does not take workspace" in str(status.value) and "project_id" in str(status.value), str(status.value)
    assert "root_path" in str(status.value)  # the valid arguments are listed
    named = project_name("unknownarg-named")
    assert call("crew_status", project=named).startswith(f"CREW {named} ")  # read, not dropped: never the configured one
    with pytest.raises(ToolError) as close:
        # close_session itself takes project and open_work since F17 (tests/crew/test_fs_close_declared_work.py),
        # so the refusal is shown with two names it still does not have
        call("close_session", repo="application-source", open_items=["x"])
    assert "does not take open_items" in str(close.value) and "does not take repo" in str(close.value)
    assert "todos_open" in str(close.value)
    assert "open_work" in str(close.value) and "project_id" in str(close.value)  # the valid arguments are listed
    with pytest.raises(ToolError):
        # message is another name for body since F39: one value is a message, two different ones are refused
        call("crew_say", body="hi", message="something else")
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
        json={
            "git_remote": "../upstream",
            "hint_project": "shared",
            "hint_scope": "folders",
            "git_repo": True,
            "strict_project": True,  # what the MCP client sends; without it see test_fs_resolve_old_client_compat.py
        },
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


# ---------------------------------------------------------------------------
# Review round: a folder is not a way back to the configured project, a call's project stays
# with that call, and close_session says how it found its project
# ---------------------------------------------------------------------------


def test_stdio_crew_call_from_a_plain_or_missing_folder_does_not_write_to_the_configured_project(
    live, stdio, monkeypatch, tmp_path
):
    """root_path on a folder that is not a repository resolved through the hint to REMEMBRA_PROJECT."""
    configured = project_name("cfg")
    use(live, "claude-desktop", "sess-plain", configured, monkeypatch)
    plain = tmp_path / "back-to-hello-import"
    plain.mkdir()
    for folder in (plain, tmp_path / "does-not-exist"):
        out = call("crew_say", body="deploy note from a folder", root_path=str(folder))
        assert out.startswith("CREW: ") and "not a git repository" in out, out
        assert "project_id" in out and configured in out and "(project" not in out, out
    checkpoint = call("crew_checkpoint", files_changed=["a.md"], root_path=str(plain))
    assert checkpoint.startswith("CREW: ") and "not a git repository" in checkpoint, checkpoint
    assert messages(live, configured) == [] and sessions(live, configured) == []
    closed = as_json(call("close_session", summary="Portal fixes", facts={"branch": "main"}, root_path=str(plain)))
    assert closed["status"] == "error" and "not a git repository" in closed["error"], closed
    assert "project_id" in closed["error"] and configured in closed["error"], closed
    assert handoff_of(live, configured) is None
    # nothing was chosen by the refused calls either
    assert call("crew_say", body="still nowhere").startswith("CREW: No project yet")


def test_stdio_plain_folder_works_once_the_agent_chose_that_project(live, stdio, monkeypatch, tmp_path):
    """session_brief showed the agent which project the folder gets: the same folder then works on a call."""
    configured = project_name("cfg")
    use(live, "claude-desktop", "sess-plain-chosen", configured, monkeypatch)
    plain = tmp_path / "notes-folder"
    plain.mkdir()
    assert as_json(call("session_brief", root_path=str(plain), compact=True))["project_id"] == configured
    out = call("crew_say", body="note from the folder", root_path=str(plain))
    assert out.startswith("SENT msg_") and out.rstrip().endswith(f"(project {configured})"), out
    closed = as_json(call("close_session", facts={"branch": "main"}, root_path=str(plain)))
    assert closed["status"] == "ok" and closed["project_id"] == configured, closed
    assert closed["project_source"] in ("hint", "known"), closed


def test_plain_folder_keeps_the_hint_for_one_namespace_and_off_stdio(live, mcp_env, monkeypatch, tmp_path):
    plain = tmp_path / "scratch"
    plain.mkdir()
    # a remote transport's caller selected the project per request: unchanged
    selected = project_name("selected")
    use(live, "chatgpt", "sess-plain-remote", selected, monkeypatch)
    out = call("crew_say", body="remote note", root_path=str(plain))
    assert out.startswith("SENT msg_") and "remote note" in messages(live, selected), out
    # REMEMBRA_RELAY_PROJECT: one namespace by choice, on a stdio server too
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)
    monkeypatch.setattr(server, "_crew", CrewBridge())
    single = project_name("single")
    use(live, "codex", "sess-plain-single", project_name("cfg"), monkeypatch)
    monkeypatch.setenv("REMEMBRA_RELAY_PROJECT", single)
    out = call("crew_say", body="single note", root_path=str(plain))
    assert out.startswith("SENT msg_") and out.rstrip().endswith(f"(project {single})"), out


def test_a_call_naming_its_project_does_not_move_the_session(live, stdio, monkeypatch):
    """One process, two conversations: B's named call must not re-point A's store, inbox, crew calls or close."""
    configured, a, b = project_name("cfg"), project_name("conva"), project_name("convb")
    use(live, "claude-desktop", "sess-callonly", configured, monkeypatch)
    assert as_json(call("session_brief", project_id=a, compact=True))["project_id"] == a
    named = call("crew_say", body="B's note", kind="note", project_id=b)
    assert named.startswith("SENT msg_") and named.rstrip().endswith(f"(project {b})"), named
    # a named call that fails moves nothing either
    failed = call("crew_claim", zone="nonexistent-zone", project_id=b)
    assert "Unknown zone" in failed and failed.rstrip().endswith(f"(project {b})"), failed
    stored = as_json(call("store_memory", content="Conversation A decided to keep server actions."))
    assert stored["project_id"] == a, stored
    sent = as_json(call("send_to_inbox", to_agent="codex", subject="s", body="A says hi"))
    assert sent["project_id"] == a, sent
    mine = call("crew_say", body="A's note", kind="note")
    assert mine.rstrip().endswith(f"(project {a})"), mine
    assert messages(live, a) == ["A's note"] and messages(live, b) == ["B's note"]
    closed = as_json(call("close_session", summary="A done", facts={"branch": "main"}))
    assert (closed["status"], closed["project_id"], closed["project_source"]) == ("ok", a, "remembered"), closed
    assert handoff_of(live, a) is not None and handoff_of(live, b) is None


def test_a_named_call_with_nothing_chosen_leaves_nothing_chosen_and_a_named_close_ends_its_seat(live, stdio, monkeypatch):
    configured, b = project_name("cfg"), project_name("named")
    use(live, "claude-desktop", "sess-named-close", configured, monkeypatch)
    assert call("crew_say", body="one note", project_id=b).rstrip().endswith(f"(project {b})")
    assert call("crew_say", body="no project").startswith("CREW: No project yet")
    assert as_json(call("close_session", facts={"branch": "main"}))["status"] == "error"
    closed = as_json(call("close_session", facts={"branch": "main"}, project_id=b))
    assert (closed["status"], closed["project_id"], closed["project_source"]) == ("ok", b, "named"), closed
    # the close ended the crew session the named call opened in that project
    assert closed["crew"]["session_left"] is True, closed
    assert [r["state"] for r in sessions(live, b)] == ["ended"], sessions(live, b)
    assert handoff_of(live, configured) is None


def test_a_crew_status_that_fails_does_not_move_the_session(live, stdio, monkeypatch):
    configured, a, b = project_name("cfg"), project_name("conva"), project_name("convb")
    use(live, "", "sess-status-fails", configured, monkeypatch)
    assert as_json(call("session_brief", project_id=a, compact=True))["project_id"] == a
    out = call("crew_status", project_id=b)
    assert out.startswith("CREW: Crew mode needs this agent's id"), out
    stored = as_json(call("store_memory", content="The importer keeps its own retry queue."))
    assert stored["project_id"] == a, stored


def test_close_session_says_how_it_found_its_project(live, mcp_env, monkeypatch):
    # off stdio the caller's configured project is still the default, and the result says so
    selected = project_name("selected")
    use(live, "chatgpt", "sess-source-remote", selected, monkeypatch)
    closed = as_json(call("close_session", facts={"branch": "main"}))
    assert (closed["project_id"], closed["project_source"]) == (selected, "configured"), closed
    # the crew seat, when the brief's memory is gone
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)
    monkeypatch.setattr(server, "_crew", CrewBridge())
    seat = project_name("seat")
    use(live, "claude-desktop", "sess-source-seat", project_name("cfg"), monkeypatch)
    assert call("crew_status", project_id=seat).startswith(f"CREW {seat} ")
    remembered = as_json(call("close_session", facts={"branch": "main"}))
    assert (remembered["project_id"], remembered["project_source"]) == (seat, "remembered"), remembered
    monkeypatch.setattr(server, "_crew", CrewBridge())
    use(live, "claude-desktop", "sess-source-seat2", project_name("cfg"), monkeypatch)
    assert call("crew_status", project_id=seat).startswith(f"CREW {seat} ")
    server._session_projects.clear()
    by_seat = as_json(call("close_session", facts={"branch": "main"}))
    assert (by_seat["project_id"], by_seat["project_source"]) == (seat, "crew-seat"), by_seat


def test_a_refused_call_does_not_echo_its_argument_values(live, stdio, monkeypatch):
    configured = project_name("cfg")
    use(live, "claude-desktop", "sess-noecho", configured, monkeypatch)
    with pytest.raises(ToolError) as unknown:
        call("store_memory", content="private note: door code 4417 for the client site", bogus=1)
    text = str(unknown.value)
    assert "does not take bogus" in text and "content" in text, text
    assert "4417" not in text and "private note" not in text and "input_value" not in text, text
    with pytest.raises(ToolError) as wrong_type:
        call("crew_say", body="the vault code is 4417", project_id=["a"])
    assert "project_id" in str(wrong_type.value) and "4417" not in str(wrong_type.value), str(wrong_type.value)


def test_store_memory_reads_a_project_alias_like_every_other_tool(live, stdio, monkeypatch):
    target = project_name("aliased")
    use(live, "claude-code", "sess-alias", project_name("cfg"), monkeypatch)
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {"oldname": target})
    stored = as_json(call("store_memory", content="The alias points at the renamed project.", project_id="oldname"))
    assert stored["status"] == "stored" and stored["project_id"] == target, stored


@pytest.mark.asyncio
async def test_stdio_close_with_nothing_chosen_uses_the_agents_crew_hook_session(live, stdio, monkeypatch, tmp_path):
    """Claude Code's usual flow: the hooks brief the agent, it never calls session_brief or a crew tool
    over MCP, and closes with only a summary. A real crewd answers whoami for this process."""
    import os

    from tests.crew.test_wp9_gate import _start_crewd
    from tests.crew.wp9_support import crew_server, make_repo, peer

    configured = project_name("cfg")
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        monkeypatch.setenv("HOME", str(layout.home))
        joined = await d.join(
            peer(me), {"adapter": "claude-code", "client_session_id": "sess-hook", "cwd": str(repo), "agent_pid": me}
        )
        hook_project = (await asyncio.to_thread(server.local_crew)).who["project_id"]
        assert hook_project and hook_project != configured, joined
        use(live, "claude-code", "sess-hook-close", configured, monkeypatch)
        # the hook session is consulted only by a server running on the agent's machine
        off = as_json(await asyncio.to_thread(call, "close_session", summary="done", facts={"branch": "main"}))
        assert off["status"] == "error", off
        monkeypatch.setattr(server, "_LOCAL_CREW", True)
        closed = as_json(await asyncio.to_thread(call, "close_session", summary="done", facts={"branch": "main"}))
    assert (closed["status"], closed["project_id"], closed["project_source"]) == ("ok", hook_project, "crew-seat"), closed
    assert handoff_of(live, hook_project) is not None and handoff_of(live, configured) is None
