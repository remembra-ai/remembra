"""F34: a session outside a crew gets a message sent after its start brief on its next prompt, and
keeps a rolling "still open" handoff while it works.

Before: a Relay message that arrived mid-session was shown only by the next session's start brief
(the per-prompt path existed for crew sessions only), and a session outside a crew wrote a handoff
only when it ended, so a kill before the end hook left nothing.

The CLI runs in-process with its HTTP client routed into the production routes over SQLite.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.relay import cli, outbox
from remembra.relay.adapters import REGISTRY
from remembra.relay.adapters.base import is_relay_command
from remembra.relay.handoff import PRE_COMPACT_REASON, end_reason_note
from tests.agent_api_harness import build_api
from tests.relay_fixtures import Transcript, git, make_remote_and_clones

CLAUDE = ["--hook", "claude-code", "--agent", "claude-code"]
CODEX = ["--hook", "codex", "--agent", "codex"]
_SERVER_LOG_RE = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d\S* \[")
_SERVER_LOG_KEYS = {"event", "level", "timestamp"}


def _is_server_log(line: str) -> bool:
    """A structlog line of the in-process server, in either renderer.

    structlog's default is the console form; once any earlier test has run ``configure_logging`` with
    stderr not a terminal (every CI run), the same lines are JSON objects with event, level and timestamp.
    """
    if _SERVER_LOG_RE.match(line):
        return True
    if not line.startswith("{"):
        return False
    try:
        record = json.loads(line)
    except ValueError:
        return False
    return isinstance(record, dict) and record.keys() >= _SERVER_LOG_KEYS


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


class _NoClose:
    def __init__(self, http: Any) -> None:
        self.http = http

    def __enter__(self) -> Any:
        return self.http

    def __exit__(self, *exc: Any) -> bool:
        self.http.headers.pop("X-Remembra-Agent-Id", None)
        return False


@pytest.fixture()
def wired(api, monkeypatch, tmp_path) -> dict[str, Any]:
    """CLI with its HTTP client routed into the app; isolated HOME and env; a folder that is not a repository."""
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "clawbot"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_inprocess")
    monkeypatch.setenv("REMEMBRA_URL", "http://testserver")
    for var in list(os.environ):
        if var.startswith("REMEMBRA_") and var not in ("REMEMBRA_API_KEY", "REMEMBRA_URL"):
            if var not in ("REMEMBRA_AUTH_ENABLED", "REMEMBRA_RATE_LIMIT_ENABLED"):
                monkeypatch.delenv(var, raising=False)
    http = api["http"]
    calls: list[tuple[str, str]] = []

    def client(self: cli.Context, config=None, agent=None, budget=None) -> Any:
        http.headers.update({"X-API-Key": (config or self.config).api_key or ""})
        if agent or self.agent:
            http.headers["X-Remembra-Agent-Id"] = agent or self.agent
        return _NoClose(http)

    real_request = http.request

    def request(method: str, url: Any, **kw: Any) -> httpx.Response:
        calls.append((method, str(url).split("?")[0]))
        return real_request(method, url, **kw)

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(cli.Context, "client", client)
    spawned: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "spawn_detached_close", lambda args, payload: spawned.append(payload) or True)
    return {"http": http, "home": home, "folder": folder, "calls": calls, "spawned": spawned, "tmp": tmp_path}


def _run(monkeypatch, capsys, argv: list[str], payload: dict[str, Any] | None = None) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if payload is not None else ""))
    code = cli.main(argv)
    out = capsys.readouterr()
    # in-process, the server's log lines share stdout with the CLI: keep the CLI's own lines
    own = [line for line in out.out.splitlines(keepends=True) if not _is_server_log(line)]
    return code, "".join(own), out.err


def _start(session: str, cwd: Path) -> dict[str, Any]:
    return {"session_id": session, "cwd": str(cwd), "hook_event_name": "SessionStart", "source": "startup"}


def _prompt(session: str, cwd: Path) -> dict[str, Any]:
    return {"session_id": session, "cwd": str(cwd), "hook_event_name": "UserPromptSubmit", "prompt": "next"}


def _send(wired: dict[str, Any], project: str, subject: str, to: str = "claude-code", sender: str = "codex") -> str:
    res = wired["http"].post(
        "/api/v1/inbox/send",
        json={"to_agent": to, "from_agent": sender, "subject": subject, "body": f"body of {subject}", "project_id": project},
    )
    assert res.status_code == 201, res.text
    return str(res.json()["inbox_id"])


def _project(wired: dict[str, Any], key: str, session: str) -> str:
    state = cli.load_turn_state(wired["home"], key, session)
    assert state.get("project_id"), state
    return str(state["project_id"])


def _later(wired: dict[str, Any], key: str, session: str, seconds: float = 60.0) -> None:
    """Move this session's last inbox check ``seconds`` into the past (the check runs at most every 30 s)."""
    then = time.time() - seconds
    path = cli._turn_state_path(wired["home"], key, session)
    if path.exists():
        state = json.loads(path.read_text())
        state["checked_at"] = then
        path.write_text(json.dumps(state))
    marker = cli._brief_marker_path(wired["home"], key, session)  # the brief was printed that long ago too
    os.utime(marker, (then, then))


# ---------------------------------------------------------------------------
# A. a message sent after the start brief reaches the session on its next prompt
# ---------------------------------------------------------------------------


def test_a_message_sent_after_the_brief_is_shown_on_the_next_prompt_outside_a_crew(wired, monkeypatch, capsys):
    folder = wired["folder"]
    code, out, err = _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S1", folder))
    assert code == 0 and "Remembra brief" in out, err
    project = _project(wired, "claude-code", "S1")

    # nothing new: the prompt hook says nothing, and within 30 s of the brief it does not even ask the server
    wired["calls"].clear()
    assert _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder)) == (0, "", "")
    assert wired["calls"] == []

    first = _send(wired, project, "the migration is merged, rebase before you push")
    for n in range(3):
        _send(wired, project, f"later note {n}")
    _send(wired, project, "not for this agent", to="gemini")
    _later(wired, "claude-code", "S1")
    code, out, err = _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder))
    assert code == 0, err
    assert f"MESSAGE {first} for you from codex (self-declared): get_inbox to read" in out
    assert out.count("MESSAGE inbox_") == 2 and "(+2 more new messages: get_inbox)" in out  # two a turn, oldest first
    assert "not for this agent" not in out
    # the subject is agent-authored: it is inside the untrusted data block, the template line is not
    head, _, block = out.partition('<remembra-data untrusted="true">')
    assert "the migration is merged" in block and "the migration is merged" not in head and "MESSAGE" in head
    assert "body of" not in out  # the body is read with get_inbox, not pushed into the prompt
    assert ("GET", "/api/v1/inbox") in wired["calls"]

    # each message once: the next prompts show the other two, then nothing
    _later(wired, "claude-code", "S1")
    code, out, _ = _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder))
    assert out.count("MESSAGE inbox_") == 2 and first not in out and "more new messages" not in out
    _later(wired, "claude-code", "S1")
    assert _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder)) == (0, "", "")
    # another session of the same agent has not seen them
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S2", folder))
    assert cli.load_turn_state(wired["home"], "claude-code", "S2")["shown"]


def test_a_message_the_start_brief_listed_is_not_repeated(wired, monkeypatch, capsys):
    folder = wired["folder"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S0", folder))  # resolves the folder's project
    project = _project(wired, "claude-code", "S0")
    early = _send(wired, project, "sent before the session started")
    code, out, _ = _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S1", folder))
    assert f"[{early}]" in out
    assert early in cli.load_turn_state(wired["home"], "claude-code", "S1")["shown"]
    late = _send(wired, project, "sent after the brief")
    _later(wired, "claude-code", "S1")
    code, out, _ = _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder))
    assert late in out and early not in out and out.count("MESSAGE inbox_") == 1


def test_the_prompt_hook_output_follows_the_agents_hook_format(wired, monkeypatch, capsys):
    folder = wired["folder"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S1", folder))
    msg = _send(wired, _project(wired, "claude-code", "S1"), "hook-json please")
    _later(wired, "claude-code", "S1")
    code, out, _ = _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once", "--format", "hook-json"], _prompt("S1", folder))
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit" and f"MESSAGE {msg}" in body["additionalContext"]


def test_codex_keeps_its_brief_once_and_then_gets_messages_on_the_same_hook(wired, monkeypatch, capsys):
    """Codex's installed prompt hook is ``brief --once``: no hook changes, so nothing to trust again."""
    folder = wired["folder"]
    # an auto-restored thread: no SessionStart, the first prompt delivers the brief (and no message twice)
    code, out, err = _run(monkeypatch, capsys, ["brief", *CODEX, "--once"], _prompt("C1", folder))
    assert code == 0 and "Remembra brief" in out and "MESSAGE inbox_" not in out, err
    msg = _send(wired, _project(wired, "codex", "C1"), "for codex", to="codex", sender="claude-code")
    _later(wired, "codex", "C1")
    code, out, _ = _run(monkeypatch, capsys, ["brief", *CODEX, "--once"], _prompt("C1", folder))
    assert f"MESSAGE {msg} for you from claude-code" in out and "Remembra brief" not in out


def test_a_crew_session_is_left_to_the_crew_gate(wired, monkeypatch, capsys):
    from remembra.relay.crew.gate import Layout, session_key

    folder = wired["folder"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S1", folder))
    _send(wired, _project(wired, "claude-code", "S1"), "the gate shows this one")
    _later(wired, "claude-code", "S1")
    layout = Layout(wired["home"])
    layout.sessions.mkdir(parents=True)
    layout.session_file(session_key("claude-code", "S1")).write_text("{}")
    wired["calls"].clear()
    assert _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder)) == (0, "", "")
    assert wired["calls"] == []


def test_a_session_whose_brief_recorded_no_project_resolves_it_once(wired, monkeypatch, capsys):
    """A session briefed by an older client (or whose brief failed) has no project on record."""
    folder = wired["folder"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S0", folder))
    project = _project(wired, "claude-code", "S0")
    cli.mark_brief_delivered(wired["home"], "claude-code", "S-old", "SessionStart")  # what 0.17.5 leaves: a marker only
    _later(wired, "claude-code", "S-old")
    msg = _send(wired, project, "reaches an old session too")
    wired["calls"].clear()
    code, out, _ = _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S-old", folder))
    assert f"MESSAGE {msg}" in out
    assert wired["calls"] == [("POST", "/api/v1/projects/resolve"), ("GET", "/api/v1/inbox")]
    assert _project(wired, "claude-code", "S-old") == project
    assert ("GET", "/api/v1/session/brief") not in wired["calls"]  # no second pickup is recorded


def test_a_server_that_fails_or_refuses_never_breaks_the_prompt(wired, monkeypatch, capsys):
    folder = wired["folder"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("S1", folder))
    _send(wired, _project(wired, "claude-code", "S1"), "unreachable for now")
    for failure in (httpx.Response(500, json={"detail": "boom"}), httpx.Response(401, json={"detail": "no"}), None):
        _later(wired, "claude-code", "S1")

        def request(self, method, path, *, failure=failure, **kw):
            if failure is None:
                raise httpx.ConnectError("down")
            return failure

        monkeypatch.setattr(cli.Context, "request", request)
        assert _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder)) == (0, "", "")
        monkeypatch.undo()
        monkeypatch.setenv("HOME", str(wired["home"]))  # undo() restored the real HOME: set the test's again
    # no API key: nothing to ask with, nothing printed
    monkeypatch.delenv("REMEMBRA_API_KEY", raising=False)
    _later(wired, "claude-code", "S1")
    assert _run(monkeypatch, capsys, ["brief", *CLAUDE, "--once"], _prompt("S1", folder))[:2] == (0, "")


# ---------------------------------------------------------------------------
# the hooks `connect` writes
# ---------------------------------------------------------------------------


def test_claude_code_gets_a_prompt_hook_and_a_checkpoint_hook(tmp_path):
    adapter = REGISTRY["claude-code"]
    events = adapter.events()
    assert ("prompt", "UserPromptSubmit") in events and ("checkpoint", "Stop") in events
    plan = adapter.plan(tmp_path, "/bin/remembra-relay")
    hooks = json.loads(plan.after)["hooks"]
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert prompt["command"] == "/bin/remembra-relay brief --hook claude-code --agent claude-code --once"
    stop = hooks["Stop"][0]["hooks"][0]
    assert stop["command"] == "/bin/remembra-relay close --hook claude-code --agent claude-code --checkpoint"
    assert stop["timeout"] == 10 and prompt["timeout"] == 15
    assert is_relay_command(stop["command"]) and is_relay_command("/odd/path/rr close --hook x --agent y --checkpoint")
    # idempotent, and disconnect removes both
    plan.path.parent.mkdir(parents=True)
    plan.path.write_text(plan.after)
    assert not adapter.plan(tmp_path, "/bin/remembra-relay").changed
    removal = adapter.plan_removal(tmp_path)
    assert "--checkpoint" not in removal.after and "--once" not in removal.after
    # Codex's hooks are unchanged (a changed hook must be trusted again in Codex)
    codex = json.loads(REGISTRY["codex"].plan(tmp_path, "/bin/remembra-relay").after)["hooks"]
    assert sorted(codex) == ["SessionEnd", "SessionStart", "UserPromptSubmit"]
    assert codex["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("brief --hook codex --agent codex --once")


# ---------------------------------------------------------------------------
# B. a rolling checkpoint while the session works
# ---------------------------------------------------------------------------


def _stop(session: str, cwd: Path, transcript: Path) -> dict[str, Any]:
    return {"session_id": session, "cwd": str(cwd), "transcript_path": str(transcript), "hook_event_name": "Stop"}


def _log(home: Path) -> str:
    path = outbox.log_path(home)
    return path.read_text() if path.exists() else ""


def _brief_for(wired: dict[str, Any], monkeypatch, capsys, agent: str, cwd: Path) -> str:
    return _run(monkeypatch, capsys, ["brief", "--agent", agent, "--cwd", str(cwd)])[1]


def test_a_session_killed_before_its_end_hook_leaves_a_still_open_handoff(wired, monkeypatch, capsys):
    _, clones = make_remote_and_clones(wired["tmp"], ("a", "b"))
    repo, other = clones["a"], clones["b"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("K1", repo))
    (repo / "README.md").write_text("# widget\nhalf-done change\n")
    t = Transcript("K1", repo)
    t.bash("pytest -q", 1, "=== 1 failed in 0.1s ===")
    transcript = t.write(wired["tmp"] / "K1.jsonl")
    checkpoint = ["close", *CLAUDE, "--checkpoint"]

    # the Stop hook returns at once: the work is handed to a detached process
    assert _run(monkeypatch, capsys, checkpoint, _stop("K1", repo, transcript)) == (0, "", "")
    assert [p["session_id"] for p in wired["spawned"]] == ["K1"]
    assert ("POST", "/api/v1/session/close") not in wired["calls"]
    # the detached child
    code, out, err = _run(monkeypatch, capsys, [*checkpoint, "--foreground"], _stop("K1", repo, transcript))
    assert (code, out) == (0, ""), err
    assert ("POST", "/api/v1/session/close") in wired["calls"]
    # ... and the session is killed here: no SessionEnd ever runs. Another agent's brief has the work:
    brief = _brief_for(wired, monkeypatch, capsys, "codex", other)
    assert "still open (saved while the session was running)" in brief
    assert "README.md" in brief and "pytest -q" in brief

    # the next turns inside the interval do not start another process
    wired["spawned"].clear()
    assert _run(monkeypatch, capsys, checkpoint, _stop("K1", repo, transcript)) == (0, "", "")
    assert wired["spawned"] == []
    # after the interval, nothing changed: the detached child sends nothing
    wired["calls"].clear()
    _run(monkeypatch, capsys, [*checkpoint, "--foreground"], _stop("K1", repo, transcript))
    assert ("POST", "/api/v1/session/close") not in wired["calls"]
    assert "is unchanged since its last handoff; not sent" in _log(wired["home"])

    # the session's real end replaces the checkpoint: one handoff, no longer "still open"
    end = {"session_id": "K1", "cwd": str(repo), "transcript_path": str(transcript), "hook_event_name": "SessionEnd"}
    _run(monkeypatch, capsys, ["close", *CLAUDE], {**end, "reason": "exit"})
    brief = _brief_for(wired, monkeypatch, capsys, "codex", other)
    assert "still open" not in brief and "README.md" in brief


def test_a_checkpoint_of_a_session_that_did_nothing_is_not_sent(wired, monkeypatch, capsys):
    _, clones = make_remote_and_clones(wired["tmp"], ("a",))
    repo = clones["a"]
    _run(monkeypatch, capsys, ["brief", *CLAUDE], _start("K2", repo))
    transcript = Transcript("K2", repo).write(wired["tmp"] / "K2.jsonl")
    wired["calls"].clear()
    _run(monkeypatch, capsys, ["close", *CLAUDE, "--checkpoint", "--foreground"], _stop("K2", repo, transcript))
    assert ("POST", "/api/v1/session/close") not in wired["calls"]
    assert "close: nothing to hand off for claude-code session K2" in _log(wired["home"])


def test_a_crew_session_and_a_sub_agent_get_no_relay_checkpoint(wired, monkeypatch, capsys):
    from remembra.relay.crew.gate import Layout, session_key

    _, clones = make_remote_and_clones(wired["tmp"], ("a",))
    repo = clones["a"]
    (repo / "README.md").write_text("changed\n")
    transcript = Transcript("K3", repo).write(wired["tmp"] / "K3.jsonl")
    layout = Layout(wired["home"])
    layout.sessions.mkdir(parents=True)
    layout.session_file(session_key("claude-code", "K3")).write_text("{}")
    assert _run(monkeypatch, capsys, ["close", *CLAUDE, "--checkpoint"], _stop("K3", repo, transcript)) == (0, "", "")
    assert wired["spawned"] == [] and "crew session" in _log(wired["home"])
    # no session id in the payload: nothing to file a checkpoint under
    assert _run(monkeypatch, capsys, ["close", *CLAUDE, "--checkpoint"], {"hook_event_name": "Stop"}) == (0, "", "")
    assert wired["spawned"] == []


def test_the_checkpoint_reason_reads_as_still_open_on_an_older_server():
    """0.17.5 knows ``pre-compact`` and ``pre-compact:<trigger>`` only: the reason must stay under that prefix."""
    from remembra.relay.handoff import CHECKPOINT_REASON

    assert CHECKPOINT_REASON.startswith(PRE_COMPACT_REASON + ":")
    assert end_reason_note(CHECKPOINT_REASON) == "still open (saved while the session was running)"
    assert end_reason_note("pre-compact:auto") == "still open (saved before context compaction)"
    assert end_reason_note("exit") is None


def test_checkpoints_of_one_session_are_spaced(wired, monkeypatch, capsys):
    _, clones = make_remote_and_clones(wired["tmp"], ("a",))
    repo = clones["a"]
    (repo / "README.md").write_text("changed\n")
    transcript = Transcript("K4", repo).write(wired["tmp"] / "K4.jsonl")
    checkpoint = ["close", *CLAUDE, "--checkpoint"]
    for _ in range(3):
        _run(monkeypatch, capsys, checkpoint, _stop("K4", repo, transcript))
    assert len(wired["spawned"]) == 1
    assert _log(wired["home"]).count("checkpoint started (Stop, session K4)") == 1
    then = time.time() - cli.CHECKPOINT_INTERVAL_SECONDS - 5
    os.utime(cli._checkpoint_claim_path(wired["home"], "claude-code", "K4"), (then, then))
    _run(monkeypatch, capsys, checkpoint, _stop("K4", repo, transcript))
    assert len(wired["spawned"]) == 2
    assert git(repo, "status", "--porcelain") == "M README.md"  # a checkpoint only reads


@pytest.mark.parametrize(("agent", "event"), [("gemini", "BeforeAgent"), ("kimi", "UserPromptSubmit")])
def test_the_other_prompt_hook_agents_get_messages_from_the_hook_they_already_have(wired, monkeypatch, capsys, agent, event):
    """Gemini CLI and Kimi Code run ``brief --once`` on every prompt too: the same command now carries messages."""
    folder = wired["folder"]
    argv = ["brief", "--hook", agent, "--agent", agent, "--once"]
    prompt = {"session_id": "P1", "cwd": str(folder), "hook_event_name": event}
    if agent == "kimi":
        prompt["client_type"] = "kimi_code_cli"
    code, out, err = _run(monkeypatch, capsys, argv, prompt)
    assert code == 0 and "Remembra brief" in out, err
    msg = _send(wired, _project(wired, agent, "P1"), "for the prompt hook", to=agent)
    _later(wired, agent, "P1")
    code, out, err = _run(monkeypatch, capsys, argv, prompt)
    assert code == 0, err
    if REGISTRY[agent].spec.output == "hook-json":
        body = json.loads(out)["hookSpecificOutput"]
        assert body["hookEventName"] == event
        out = body["additionalContext"]
    assert f"MESSAGE {msg} for you from codex" in out and "Remembra brief" not in out
