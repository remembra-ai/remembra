"""Crew hooks keep the relay's hook order (0.16.1): skip -> route -> dedupe -> empty close -> send.

``remembra-crew connect`` replaces the ``remembra-relay brief`` / ``close`` entries in an agent's config
with ``remembra-crew start`` / ``end`` / ``stall``. These tests prove the replacement keeps what 0.16.1
added to the relay hooks:

* **skip**: a Codex automation or sub-agent thread joins no crew, gets no brief, and its end neither
  ends the parent's crew session (a sub-agent's hook carries its PARENT's session id) nor sends a handoff;
* **route**: a hook another agent runs (Cursor running Claude Code's hook file) joins no crew as
  Claude Code; its end goes to ``remembra-relay close``, which files it under that agent;
* a session that is not a crew session gets the plain relay close, run for real here: **dedupe** drops
  a repeat of the same end, an **empty close** is not sent, and a close with work is **sent** (queued
  without a key);
* crewd skips an empty close of a crew session too; its ``leave`` still runs.

Temp HOME, temp repos and subprocesses of the real CLIs only; nothing reads the real agent configs,
and no request leaves the machine (no API key, so the relay queues).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from remembra.relay import outbox
from remembra.relay.crew import cli
from remembra.relay.crew import crewd as crewd_mod
from remembra.relay.crew.gate import Layout, session_key
from tests.crew.wp9_support import make_repo

ROOT = Path(__file__).resolve().parents[2]
# Env markers hosts.detect_host reads: a test run inside one of these agents must not route every hook.
HOST_MARKERS = (
    "GROK_WORKSPACE_ROOT",
    "GEMINI_SESSION_ID",
    "QWEN_CODE_SESSION_ID",
    "DEVIN_PROJECT_DIR",
    "CONTINUE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "REMEMBRA_RELAY_INCLUDE_AUTOMATIONS",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for name in HOST_MARKERS:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("REMEMBRA_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONPATH", f"{ROOT / 'src'}{os.pathsep}{ROOT}")
    return h


def _rollout(home: Path, thread_source: str, *, parent: str | None = None) -> str:
    """A Codex rollout whose first line says what kind of thread it is (as Codex writes it)."""
    path = home / ".codex" / "sessions" / "2026" / "09" / "26" / f"rollout-{thread_source}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"id": "thread-1", "thread_source": thread_source, "source": "vscode"}
    if parent:
        meta["parent_thread_id"] = parent
    path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
    return str(path)


def _run(monkeypatch: pytest.MonkeyPatch, layout: Layout, argv: list[str], payload: dict[str, Any]) -> None:
    monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: json.dumps(payload))
    assert cli.main(argv, layout=layout) == 0


class Calls:
    """Records what a hook asked for instead of doing it (crewd, the relay passthroughs)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ops: list[tuple[str, dict[str, Any]]] = []
        self.briefs: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []

        def rpc_any(_layout: Layout, op: str, args: Any = None, **_: Any) -> Any:
            self.ops.append((op, dict(args or {})))
            return None

        def rpc_ex(_layout: Layout, op: str, args: Any = None, **_: Any) -> Any:
            self.ops.append((op, dict(args or {})))
            return None, False

        def rpc_send(_layout: Layout, op: str, args: Any = None, **_: Any) -> bool:
            self.ops.append((op, dict(args or {})))
            return True

        monkeypatch.setattr(cli, "rpc", rpc_any)
        monkeypatch.setattr(cli, "rpc_ex", rpc_ex)
        monkeypatch.setattr(cli, "rpc_send", rpc_send)
        monkeypatch.setattr(cli, "ensure_crewd", lambda *a, **k: False)
        monkeypatch.setattr(
            cli, "relay_brief_passthrough", lambda adapter, agent, payload, *, timeout=0: self.briefs.append(dict(payload))
        )
        monkeypatch.setattr(
            cli, "relay_close_passthrough", lambda adapter, agent, payload, **k: self.closes.append(dict(payload))
        )


# ---------------------------------------------------------------------------
# 1. skip and route decide before any crew work
# ---------------------------------------------------------------------------


def test_verdict_skips_codex_automations_and_sub_agents_and_routes_foreign_hooks(home: Path) -> None:
    automation = {"session_id": "s-auto", "cwd": "/w", "transcript_path": _rollout(home, "automation")}
    sub = {"session_id": "parent", "agent_id": "thread-9", "transcript_path": _rollout(home, "subagent", parent="parent")}
    user = {"session_id": "s-user", "cwd": "/w", "transcript_path": _rollout(home, "user")}
    assert cli.relay_hook_verdict("codex", automation, "brief") == ("skip", "automation")
    assert cli.relay_hook_verdict("codex", sub, "close") == ("skip", "subagent")
    assert cli.relay_hook_verdict("codex", user, "brief") is None
    # Cursor runs the user's Claude Code hooks with its own payload (recorded marker: cursor_version).
    cursor = {"session_id": "c-1", "cursor_version": "2026.09.26", "hook_event_name": "sessionStart"}
    assert cli.relay_hook_verdict("claude-code", cursor, "brief") == ("route", "cursor")
    # A real Claude Code session: its transcript is in ~/.claude, whatever else the payload holds.
    own = home / ".claude" / "projects" / "p" / "s.jsonl"
    own.parent.mkdir(parents=True)
    own.write_text("{}\n")
    assert cli.relay_hook_verdict("claude-code", {"session_id": "cc-1", "transcript_path": str(own)}, "brief") is None
    assert cli.relay_hook_verdict("claude-code", {}, "brief") is None
    log = outbox.log_path(home).read_text()
    assert "skipped brief: codex automation session s-auto" in log
    assert "crew brief --hook claude-code: run by cursor, not a crew session of claude-code" in log


def test_start_of_an_automation_or_a_foreign_hook_joins_no_crew_and_prints_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = Layout(home)
    repo = make_repo(tmp_path / "repo")  # a crew checkout (.remembra/zones.yml)
    calls = Calls(monkeypatch)
    auto = {"session_id": "s-auto", "cwd": str(repo), "transcript_path": _rollout(home, "automation"), "source": "startup"}
    _run(monkeypatch, layout, ["start", "--hook", "codex", "--agent", "codex"], auto)
    cursor = {"session_id": "c-1", "cwd": str(repo), "cursor_version": "2026.09.26"}
    _run(monkeypatch, layout, ["start", "--hook", "claude-code", "--agent", "claude-code"], cursor)
    assert calls.ops == [] and calls.briefs == []  # no crew_exists, no join, no brief
    assert capsys.readouterr().out == ""
    # The same checkout for a person's own thread goes on to the crew (here: crewd is down, so it says so).
    user = {"session_id": "s-user", "cwd": str(repo), "transcript_path": _rollout(home, "user"), "source": "startup"}
    _run(monkeypatch, layout, ["start", "--hook", "codex", "--agent", "codex"], user)
    assert "crewd did not start" in capsys.readouterr().out


def test_a_sub_agent_end_never_ends_its_parents_crew_session(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = Layout(home)
    calls = Calls(monkeypatch)
    parent_key = session_key("codex", "parent")
    layout.session_file(parent_key).parent.mkdir(parents=True, exist_ok=True)
    layout.session_file(parent_key).write_text(json.dumps({"key": parent_key, "crew_id": "crew_1"}))
    sub = {"session_id": "parent", "agent_id": "thread-9", "transcript_path": _rollout(home, "subagent", parent="parent")}
    _run(monkeypatch, layout, ["end", "--hook", "codex", "--agent", "codex"], sub)
    assert calls.ops == [] and calls.closes == []
    # The parent's own end does reach crewd.
    parent = {"session_id": "parent", "transcript_path": _rollout(home, "user"), "reason": "exit"}
    _run(monkeypatch, layout, ["end", "--hook", "codex", "--agent", "codex"], parent)
    assert [op for op, _ in calls.ops] == ["end"] and calls.ops[0][1]["key"] == parent_key and calls.closes == []
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# 2. Not a crew session: the plain relay close
# ---------------------------------------------------------------------------


def test_end_and_limit_stall_of_a_non_crew_session_run_the_relay_close(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = Layout(home)
    calls = Calls(monkeypatch)
    end = {"session_id": "s-1", "cwd": "/w", "hook_event_name": "SessionEnd", "reason": "exit"}
    _run(monkeypatch, layout, ["end", "--hook", "claude-code", "--agent", "claude-code"], end)
    # A routed hook (Cursor running Claude Code's) goes to the relay too: it files it under Cursor.
    cursor = {"session_id": "c-1", "cursor_version": "2026.09.26", "hook_event_name": "sessionEnd"}
    _run(monkeypatch, layout, ["end", "--hook", "claude-code", "--agent", "claude-code"], cursor)
    # Gemini's orphaned third SessionEnd has no payload: nothing to do.
    _run(monkeypatch, layout, ["end", "--hook", "gemini", "--agent", "gemini"], {})
    assert calls.closes == [end, cursor] and calls.ops == []
    # StopFailure: only the stops the relay's own hook closes on (usage and billing limits).
    stop = {"session_id": "s-1", "cwd": "/w", "hook_event_name": "StopFailure"}
    for error in ("rate_limit", "billing_error", "server_error", ""):
        _run(monkeypatch, layout, ["stall", "--hook", "claude-code", "--agent", "claude-code"], {**stop, "error": error})
    assert [c.get("error") for c in calls.closes[2:]] == ["rate_limit", "billing_error"]
    assert calls.ops == []


def _relay_log(home: Path) -> str:
    path = outbox.log_path(home)
    return path.read_text() if path.exists() else ""


def test_the_relay_close_passthrough_keeps_dedupe_empty_close_and_send(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real ``remembra-relay close`` in a subprocess, as the crew SessionEnd hook runs it."""
    repo = make_repo(tmp_path / "repo", zones=None)
    (repo / "README.md").write_text("changed\n")  # work to hand off: an uncommitted change
    payload = {
        "session_id": "s-work",
        "cwd": str(repo),
        "hook_event_name": "SessionEnd",
        "reason": "exit",
        "transcript_path": str(tmp_path / "missing.jsonl"),
    }
    cli.relay_close_passthrough("claude-code", "claude-code", payload)
    queued = outbox.pending(home)
    assert len(queued) == 1, _relay_log(home)  # sent: no API key here, so queued for the next brief or close
    assert queued[0].agent_id == "claude-code"
    # dedupe: the same end again within the window is dropped, nothing more is queued
    cli.relay_close_passthrough("claude-code", "claude-code", payload)
    assert len(outbox.pending(home)) == 1
    assert "close: dropped a repeat sessionend of claude-code session s-work" in _relay_log(home)
    # empty close: a session that recorded nothing (not a git checkout, no notes) is not sent
    plain = tmp_path / "notes"
    plain.mkdir()
    cli.relay_close_passthrough("claude-code", "claude-code", {**payload, "session_id": "s-idle", "cwd": str(plain)})
    assert len(outbox.pending(home)) == 1
    assert "close: nothing to hand off for claude-code session s-idle" in _relay_log(home)
    assert capsys.readouterr().out == ""  # a hook's stdout stays clean


def test_relay_close_passthrough_passes_cursor_ack_and_survives_a_missing_python(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.relay_close_passthrough("cursor", "cursor", {"conversation_id": "x", "hook_event_name": "sessionEnd"})
    assert capsys.readouterr().out.strip() == "{}"  # Cursor logs empty stdout as a failed hook
    monkeypatch.setattr(cli.sys, "executable", str(home / "no-python"))
    cli.relay_close_passthrough("cursor", "cursor", {"conversation_id": "x"})
    assert capsys.readouterr().out.strip() == "{}"


def test_relay_brief_passthrough_prints_nothing_when_the_relay_has_nothing_to_say(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skipped Codex automation: the relay brief prints nothing, and so does the passthrough (no
    "brief unavailable" notice that would tell an automation to call session_brief)."""
    auto = {"session_id": "s-auto", "cwd": str(tmp_path), "transcript_path": _rollout(home, "automation")}
    cli.relay_brief_passthrough("codex", "codex", auto, timeout=30)
    assert capsys.readouterr().out == ""
    # A brief that cannot run at all still says so.
    cli.relay_brief_passthrough("claude-code", "claude-code", {"session_id": "s"}, timeout=0.0)
    assert "Remembra brief unavailable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3. A crew session: crewd sends no empty handoff
# ---------------------------------------------------------------------------


def test_crewd_empty_close_rule_matches_the_relay_cli() -> None:
    from remembra.relay.cli import nothing_to_hand_off as relay_rule

    bodies = [
        {"facts": {}, "end_reason": "exit"},
        {"facts": {"files_changed": ["a.py"]}, "end_reason": "exit"},
        {"facts": {"commits": [{"sha": "abc1234", "subject": "fix"}]}},
        {"facts": {"notes": "halfway"}},
        {"facts": {}, "end_reason": "usage_limit"},
        {"facts": {}, "summary": "nothing changed, but read the code"},
        {"facts": {"git_state_unknown": True}},
    ]
    for body in bodies:
        assert crewd_mod.nothing_to_hand_off(body) == relay_rule(dict(body)), body
    assert crewd_mod.nothing_to_hand_off(bodies[0]) is True
    assert crewd_mod.nothing_to_hand_off(bodies[1]) is False


async def test_crewd_relay_close_skips_an_idle_crew_session_and_sends_one_with_work(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", zones=None)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
    sess = {
        "toplevel": str(repo),
        "adapter": "claude-code",
        "agent_id": "claude-code",
        "client_session_id": "s-crew",
        "project_id": "demo",
        "started_head": head.strip(),
        "joined_at": 0,
    }
    sent: list[dict[str, Any]] = []

    class Stub:
        def api_for(self, _sess: Any) -> str:
            return "api"

        async def request(self, _api: Any, method: str, path: str, **kw: Any) -> None:
            sent.append({"method": method, "path": path, **kw})

    stub = Stub()
    await crewd_mod.Crewd.relay_close(stub, sess, "exit", None)  # type: ignore[arg-type]
    assert sent == []  # clean checkout, no commits since the session started: nothing to hand off
    (repo / "README.md").write_text("changed\n")
    await crewd_mod.Crewd.relay_close(stub, sess, "exit", None)  # type: ignore[arg-type]
    assert [(s["method"], s["path"]) for s in sent] == [("POST", "/session/close")]
    assert sent[0]["json_body"]["session_id"] == "s-crew" and sent[0]["json_body"]["facts"]["files_changed"]
