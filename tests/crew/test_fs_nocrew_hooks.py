"""F34 on a machine with crew hooks: a session that is not a crew session gets what the plain relay hooks do.

``remembra-crew connect`` replaces the ``remembra-relay`` hook entries, and the crew gate exits at once
for a session without a crew session file. So outside a crew checkout such a machine showed no message
that arrived mid-session, wrote no handoff before a context compaction and kept no checkpoint. The
``remembra-crew relay`` hook (UserPromptSubmit, PreCompact, Stop) does nothing for a crew session and
otherwise runs the relay's own command for that event.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from remembra.relay import cli as relay_cli
from remembra.relay import outbox
from remembra.relay.adapters import claude_code, codex
from remembra.relay.adapters.crew_hooks import CrewCommands, render
from remembra.relay.crew import cli
from remembra.relay.crew.gate import Layout, session_key
from tests.agent_api_harness import build_api

ROOT = Path(__file__).resolve().parents[2]
HOST_MARKERS = (
    "GROK_WORKSPACE_ROOT",
    "GEMINI_SESSION_ID",
    "QWEN_CODE_SESSION_ID",
    "DEVIN_PROJECT_DIR",
    "CONTINUE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
)
CMDS = CrewCommands(python="/usr/bin/python3", gate="/home/u/.remembra/crew/bin/crew-gate.py", crew="/opt/bin/remembra-crew")
RELAY_HOOK = "/opt/bin/remembra-crew relay --hook {0} --agent {0} # remembra-crew"
_SERVER_LOG_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[")


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
def env(api, monkeypatch, tmp_path) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "clawbot"  # not a git repository, not a crew checkout
    folder.mkdir()
    monkeypatch.setenv("HOME", str(home))
    keep = ("REMEMBRA_AUTH_ENABLED", "REMEMBRA_RATE_LIMIT_ENABLED")  # the API harness's own switches
    for name in list(os.environ):
        if name in HOST_MARKERS or (name.startswith("REMEMBRA_") and name not in keep):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_inprocess")
    monkeypatch.setenv("REMEMBRA_URL", "http://testserver")
    monkeypatch.setenv("PYTHONPATH", f"{ROOT / 'src'}{os.pathsep}{ROOT}")
    http = api["http"]
    calls: list[tuple[str, str]] = []

    def client(self: relay_cli.Context, config=None, agent=None, budget=None) -> Any:
        http.headers.update({"X-API-Key": (config or self.config).api_key or ""})
        if agent or self.agent:
            http.headers["X-Remembra-Agent-Id"] = agent or self.agent
        return _NoClose(http)

    real_request = http.request

    def request(method: str, url: Any, **kw: Any) -> Any:
        calls.append((method, str(url).split("?")[0]))
        return real_request(method, url, **kw)

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(relay_cli.Context, "client", client)
    spawned: list[dict[str, Any]] = []
    monkeypatch.setattr(relay_cli, "spawn_detached_close", lambda args, payload: spawned.append(payload) or True)
    return {"http": http, "home": home, "folder": folder, "calls": calls, "spawned": spawned, "layout": Layout(home)}


def _hook(env: dict[str, Any], monkeypatch, capsys, adapter: str, payload: dict[str, Any]) -> tuple[int, str]:
    """Run ``remembra-crew relay --hook <adapter>`` as the agent does: the payload on stdin."""
    monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: json.dumps(payload))
    code = cli.main(["relay", "--hook", adapter, "--agent", adapter], layout=env["layout"])
    out = capsys.readouterr().out
    return code, "".join(line for line in out.splitlines(keepends=True) if not _SERVER_LOG_RE.match(line))


def _relay(monkeypatch, capsys, argv: list[str], payload: dict[str, Any]) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert relay_cli.main(argv) == 0
    return capsys.readouterr().out


def _briefed(env: dict[str, Any], monkeypatch, capsys, adapter: str, session: str) -> str:
    """The session's start brief (the crew start hook passes it to the relay for a non-crew session); its project."""
    start = {"session_id": session, "cwd": str(env["folder"]), "hook_event_name": "SessionStart", "source": "startup"}
    _relay(monkeypatch, capsys, ["brief", "--hook", adapter, "--agent", adapter], start)
    then = time.time() - 60
    os.utime(relay_cli._brief_marker_path(env["home"], adapter, session), (then, then))
    relay_cli.save_turn_state(env["home"], adapter, session, checked_at=then)
    return str(relay_cli.load_turn_state(env["home"], adapter, session)["project_id"])


def _send(env: dict[str, Any], project: str, to: str, sender: str, subject: str) -> str:
    res = env["http"].post(
        "/api/v1/inbox/send",
        json={"to_agent": to, "from_agent": sender, "subject": subject, "body": "the body", "project_id": project},
    )
    assert res.status_code == 201, res.text
    return str(res.json()["inbox_id"])


def _join_crew(env: dict[str, Any], adapter: str, session: str) -> None:
    env["layout"].sessions.mkdir(parents=True, exist_ok=True)
    env["layout"].session_file(session_key(adapter, session)).write_text("{}")


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------


def test_a_non_crew_prompt_shows_a_message_that_arrived_mid_session(env, monkeypatch, capsys):
    project = _briefed(env, monkeypatch, capsys, "claude-code", "S1")
    msg = _send(env, project, "claude-code", "codex", "check what Hustle says")
    prompt = {"session_id": "S1", "cwd": str(env["folder"]), "hook_event_name": "UserPromptSubmit", "prompt": "go on"}
    code, out = _hook(env, monkeypatch, capsys, "claude-code", prompt)
    assert code == 0
    body = json.loads(out)["hookSpecificOutput"]  # the crew hooks' output mode for Claude Code
    assert body["hookEventName"] == "UserPromptSubmit"
    assert f"MESSAGE {msg} for you from codex" in body["additionalContext"]
    assert "check what Hustle says" in body["additionalContext"].split("<remembra-data", 1)[1]
    # once per session
    relay_cli.save_turn_state(env["home"], "claude-code", "S1", checked_at=time.time() - 60)
    assert _hook(env, monkeypatch, capsys, "claude-code", prompt) == (0, "")


def test_a_codex_prompt_gets_its_brief_once_back_and_then_messages(env, monkeypatch, capsys):
    """Crew connect removed Codex's ``brief --once`` prompt hook: an auto-restored thread got no brief at all."""
    prompt = {"session_id": "C1", "cwd": str(env["folder"]), "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    code, out = _hook(env, monkeypatch, capsys, "codex", prompt)
    assert code == 0 and "Remembra brief" in out and "MESSAGE inbox_" not in out
    project = str(relay_cli.load_turn_state(env["home"], "codex", "C1")["project_id"])
    msg = _send(env, project, "codex", "claude-code", "for codex")
    then = time.time() - 60
    os.utime(relay_cli._brief_marker_path(env["home"], "codex", "C1"), (then, then))
    relay_cli.save_turn_state(env["home"], "codex", "C1", checked_at=then)
    code, out = _hook(env, monkeypatch, capsys, "codex", prompt)
    assert f"MESSAGE {msg} for you from claude-code" in out and "Remembra brief" not in out


def test_a_crew_session_is_left_to_the_gate_on_every_event(env, monkeypatch, capsys):
    project = _briefed(env, monkeypatch, capsys, "claude-code", "S1")
    _send(env, project, "claude-code", "codex", "the gate shows this one")
    _join_crew(env, "claude-code", "S1")
    env["calls"].clear()
    for event in ("UserPromptSubmit", "Stop", "PreCompact"):
        payload = {"session_id": "S1", "cwd": str(env["folder"]), "hook_event_name": event, "trigger": "auto"}
        assert _hook(env, monkeypatch, capsys, "claude-code", payload) == (0, "")
    assert env["calls"] == [] and env["spawned"] == [] and outbox.pending(env["home"]) == []


def test_other_events_an_empty_payload_and_a_bad_command_line_do_nothing(env, monkeypatch, capsys):
    for payload in (
        {"session_id": "S1", "cwd": str(env["folder"]), "hook_event_name": "PostToolUse"},
        {"session_id": "S1", "cwd": str(env["folder"])},
        {"hook_event_name": "UserPromptSubmit"},
        {},
    ):
        assert _hook(env, monkeypatch, capsys, "claude-code", payload) == (0, "")
    monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: "not json")
    assert cli.main(["relay", "--hook", "claude-code"], layout=env["layout"]) == 0
    assert cli.main(["relay", "--no-such-option"], layout=env["layout"]) == 0  # a hook never fails the agent
    assert env["calls"] == [] and capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# PreCompact and Stop
# ---------------------------------------------------------------------------


def test_a_non_crew_precompact_writes_the_handoff_again(env, monkeypatch, capsys, tmp_path):
    """The real ``remembra-relay close`` in a subprocess, as before crew connect replaced the relay hooks."""
    monkeypatch.delenv("REMEMBRA_API_KEY")  # the subprocess cannot reach the in-process app: the close is queued
    work = tmp_path / "work"
    work.mkdir()
    payload = {
        "session_id": "S-compact",
        "cwd": str(work),
        "hook_event_name": "PreCompact",
        "trigger": "auto",
        "transcript_path": str(tmp_path / "missing.jsonl"),
    }
    assert _hook(env, monkeypatch, capsys, "claude-code", payload) == (0, "")
    log = outbox.log_path(env["home"]).read_text()
    assert "close --hook claude-code: started (PreCompact, session S-compact)" in log
    (queued,) = outbox.pending(env["home"])
    assert queued.payload["end_reason"] == "pre-compact:auto" and queued.payload["session_id"] == "S-compact"


def test_a_non_crew_stop_starts_a_spaced_checkpoint(env, monkeypatch, capsys, tmp_path):
    stop = {
        "session_id": "S-stop",
        "cwd": str(env["folder"]),
        "hook_event_name": "Stop",
        "transcript_path": str(tmp_path / "t.jsonl"),
    }
    for _ in range(3):
        assert _hook(env, monkeypatch, capsys, "claude-code", stop) == (0, "")
    assert [p["session_id"] for p in env["spawned"]] == ["S-stop"]  # one detached checkpoint, not one per turn
    assert "close --hook claude-code: checkpoint started (Stop, session S-stop)" in outbox.log_path(env["home"]).read_text()
    # Codex has no checkpoint hook of its own (its end hook runs after every turn in the app): nothing to pass on
    assert _hook(env, monkeypatch, capsys, "codex", {**stop, "session_id": "C-stop"}) == (0, "")
    assert len(env["spawned"]) == 1


# ---------------------------------------------------------------------------
# what `remembra-crew connect` writes
# ---------------------------------------------------------------------------


def _commands(text: str, event: str) -> list[str]:
    return [h["command"] for group in json.loads(text)["hooks"].get(event, []) for h in group["hooks"]]


def test_connect_installs_the_relay_hook_next_to_the_gate_entries():
    after, summary = render(None, claude_code.CREW, CMDS, legacy_markers=claude_code.CREW_LEGACY_MARKERS)
    want = RELAY_HOOK.format("claude-code")
    for event in ("UserPromptSubmit", "Stop", "PreCompact"):
        commands = _commands(after, event)
        assert want in commands and any(" -I " in c for c in commands), event  # the gate entry stays
    for event in ("SessionStart", "SessionEnd", "StopFailure", "PreToolUse", "PostToolUse"):
        assert want not in _commands(after, event)
    assert render(after, claude_code.CREW, CMDS, legacy_markers=claude_code.CREW_LEGACY_MARKERS) == (after, [])
    removed, _ = render(after, claude_code.CREW, CMDS, remove=True)
    assert "remembra-crew" not in removed
    # an install written by 0.17.5 gains the three entries and keeps the rest
    old_hooks = tuple(h for h in claude_code.CREW.hooks if h.verb != "relay")
    old_spec = type(claude_code.CREW)(**{**claude_code.CREW.__dict__, "hooks": old_hooks})
    before, _ = render(None, old_spec, CMDS)
    upgraded, summary = render(before, claude_code.CREW, CMDS)
    assert json.loads(upgraded) == json.loads(after)
    assert sum(1 for line in summary if "add `" in line and " relay --hook" in line) == 3
    # Codex: the prompt hook only
    codex_after, _ = render(None, codex.CREW, CMDS)
    assert RELAY_HOOK.format("codex") in _commands(codex_after, "UserPromptSubmit")
    assert RELAY_HOOK.format("codex") not in _commands(codex_after, "Stop")
