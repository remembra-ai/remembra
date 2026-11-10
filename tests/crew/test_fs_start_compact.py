"""F47: a SessionStart after a compaction does not rejoin the crew or resend the whole brief.

Claude Code runs the SessionStart hook for ``startup``, ``resume``, ``clear`` and ``compact`` alike.
``remembra-crew start`` treated them all the same: a full crewd ``join`` (repo facts, project resolve,
``POST /crews/join``, zone plan, snapshot sync, and a rebuilt local session record) followed by
``GET /session/brief``, serially against the server inside an 8 s deadline, on every compaction. Measured
in one session: 29 compactions, 2 to 8 s each, the brief resent each time, and when the server was slow a
false "Crew rules are not active" notice for a session that was a live crew session.

After a compaction of a session crewd already holds, the hook now only tells crewd the session is alive
(one local RPC) and prints a short reminder that points at ``crew_status`` and ``session_brief``. Anything
else (no session file, an ended session, crewd not holding the session) takes the full start as before.

The first test runs the real CLI against a real crewd socket and the real crew routers.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import schemas as S
from remembra.relay.crew import cli
from remembra.relay.crew.gate import Layout, read_json, session_key, vendor_gate
from tests.crew.wp9_support import crew_server, make_repo, new_crewd, peer

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
START = ["start", "--hook", "claude-code", "--agent", "claude-code"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HOST_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)


def _context(out: str) -> str:
    """The additionalContext of one SessionStart hook run (and nothing else on stdout)."""
    # the in-process test server logs to stdout too: only the hook's own JSON line counts
    lines = [line for line in out.splitlines() if line.startswith("{")]
    assert len(lines) == 1, out
    assert S.validate_hook_stdout("SessionStart", lines[0]) == [], out
    return str(json.loads(lines[0])["hookSpecificOutput"]["additionalContext"])


def _payload(sid: str, cwd: Path, source: str) -> dict[str, Any]:
    return {"session_id": sid, "cwd": str(cwd), "hook_event_name": "SessionStart", "source": source}


async def test_compact_start_of_a_live_crew_session_neither_rejoins_nor_fetches_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    env_file = tmp_path / "claude-env"
    async with crew_server(tmp_path) as srv:
        layout = Layout(tmp_path / "home")
        vendor_gate(layout)
        d = new_crewd(layout, srv)
        await d.start_server()
        first = await d.join(
            peer(me),
            {"adapter": "claude-code", "client_session_id": "sess-1", "cwd": str(repo), "agent_pid": me, "source": "startup"},
        )
        assert first["ok"], first
        key = first["key"]
        assert key == session_key("claude-code", "sess-1")
        # work since the join, which a rejoin would wipe from the local session record
        assert (await d.activity(peer(me), {"key": key, "phase": "start", "tool": "Edit"}))["ok"]
        assert (await d.activity(peer(me), {"key": key, "phase": "end", "tool": "Edit", "paths": ["README.md"]}))["ok"]
        before = dict(d.sessions[key])
        assert before["calls_since_checkpoint"] == 1 and before["files_since_checkpoint"] == 1
        windows_before = [list(w) for w in d.tool_windows[key]]

        ops: list[str] = []
        real_handle = d.handle

        async def handle(op: str, args: Any, who: Any) -> dict[str, Any]:
            ops.append(op)
            return await real_handle(op, args, who)

        d.handle = handle  # type: ignore[method-assign]
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: json.dumps(_payload("sess-1", repo, "compact")))
        capsys.readouterr()
        code = await asyncio.to_thread(cli.main, START, layout=layout)
        assert code == 0
        text = _context(capsys.readouterr().out)

        # one local RPC, no join and no brief: nothing goes to the server
        assert ops == ["activity"], ops
        assert len(text) <= cli.START_COMPACT_CAP, len(text)
        assert f"you are still {first['callsign']} in crew {first['crew_id']}" in text
        assert "crew_status" in text and "session_brief" in text
        assert "not active" not in text and "unavailable" not in text
        assert S.DATA_OPEN not in text  # the reminder carries no recorded text
        # the session record is the one the join made, with the work since kept
        after = d.sessions[key]
        assert after["joined_at"] == before["joined_at"]
        assert after["calls_since_checkpoint"] == 1 and after["files_since_checkpoint"] == 1
        assert [list(w) for w in d.tool_windows[key]] == windows_before  # the keep-alive opens no tool window
        assert after["last_activity_at"] >= before["last_activity_at"]
        assert after["last_action"]["tool"] == "SessionStart" and after["last_action"]["verb"] == "compact"
        assert read_json(layout.session_file(key))["ended"] is False
        # later Bash calls still get the project and member (as the full start writes them)
        exported = env_file.read_text()
        assert f"export REMEMBRA_PROJECT_ID={json.dumps(str(first['project_id']))}" in exported
        assert f"export REMEMBRA_MEMBER={json.dumps(str(first['member_key']))}" in exported

        # the same session started for any other reason still takes the full start
        ops.clear()
        monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: json.dumps(_payload("sess-1", repo, "resume")))
        assert await asyncio.to_thread(cli.main, [*START, "--agent-pid", str(me)], layout=layout) == 0
        assert [op for op in ops if op != "ping"] == ["join", "brief"], ops  # ping: is crewd running
        capsys.readouterr()


class Calls:
    """Records what the hook asked crewd for; ``answers`` maps an op to its reply (default: no reply)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any] | None = None, *, crewd_up: bool = True) -> None:
        self.ops: list[tuple[str, dict[str, Any]]] = []
        self.briefs: list[dict[str, Any]] = []
        answers = answers or {}

        def rpc(_layout: Layout, op: str, args: Any = None, **_: Any) -> Any:
            self.ops.append((op, dict(args or {})))
            return answers.get(op)

        def rpc_ex(_layout: Layout, op: str, args: Any = None, **_: Any) -> Any:
            self.ops.append((op, dict(args or {})))
            return answers.get(op), True

        monkeypatch.setattr(cli, "rpc", rpc)
        monkeypatch.setattr(cli, "rpc_ex", rpc_ex)
        monkeypatch.setattr(cli, "ensure_crewd", lambda *a, **k: crewd_up)
        monkeypatch.setattr(
            cli, "relay_brief_passthrough", lambda adapter, agent, payload, *, timeout=0: self.briefs.append(dict(payload))
        )

    @property
    def names(self) -> list[str]:
        return [op for op, _ in self.ops]


def _session(layout: Layout, sid: str, **fields: Any) -> str:
    key = session_key("claude-code", sid)
    record = {"key": key, "crew_id": "crw_0123456789abcdef", "callsign": "cc-3", "ended": False, **fields}
    layout.session_file(key).parent.mkdir(parents=True, exist_ok=True)
    layout.session_file(key).write_text(json.dumps(record))
    return key


def _run(monkeypatch: pytest.MonkeyPatch, layout: Layout, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: json.dumps(payload))
    assert cli.main(START, layout=layout) == 0


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def test_compact_start_uses_a_keep_alive_that_opens_no_tool_window(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = Layout(home)
    repo = make_repo(tmp_path / "repo")
    key = _session(layout, "s-1", observe_only=True)
    calls = Calls(monkeypatch, {"activity": {"ok": True}})
    _run(monkeypatch, layout, _payload("s-1", repo, "compact"))
    assert calls.ops == [("activity", {"key": key, "phase": "blocked", "tool": "SessionStart", "verb": "compact"})]
    text = _context(capsys.readouterr().out)
    assert len(text) <= cli.START_COMPACT_CAP
    assert "you are still cc-3 in crew crw_0123456789abcdef" in text and "observe-only" in text


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        ({"ok": False, "error": "not_your_session"}, "crewd does not hold the session (restarted, or another process tree)"),
        (None, "crewd did not answer"),
    ],
)
def test_compact_start_takes_the_full_start_when_crewd_does_not_confirm_the_session(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: dict[str, Any] | None,
    why: str,
) -> None:
    layout = Layout(home)
    repo = make_repo(tmp_path / "repo")  # a crew checkout
    _session(layout, "s-1")
    join = {"ok": True, "key": "k", "callsign": "cc-4", "crew_id": "crw_0123456789abcdef"}
    calls = Calls(monkeypatch, {"activity": answer, "join": join, "brief": {"rendered": "# Remembra brief\nfresh"}})
    _run(monkeypatch, layout, _payload("s-1", repo, "compact"))
    assert calls.names == ["activity", "join", "brief"], why
    assert calls.ops[1][1]["source"] == "compact"  # crewd still learns why the session started
    assert _context(capsys.readouterr().out) == "# Remembra brief\nfresh"


@pytest.mark.parametrize("record", ["missing", "ended", "no-crew", "other-key", "not-json"])
def test_compact_start_without_a_live_crew_session_record_takes_the_full_start(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], record: str
) -> None:
    layout = Layout(home)
    repo = make_repo(tmp_path / "repo")
    if record == "ended":
        _session(layout, "s-1", ended=True)
    elif record == "no-crew":
        _session(layout, "s-1", crew_id=None)
    elif record == "other-key":
        _session(layout, "s-1", key="someone-else")
    elif record == "not-json":
        key = _session(layout, "s-1")
        layout.session_file(key).write_text("{not json")
    calls = Calls(monkeypatch, {"activity": {"ok": True}}, crewd_up=False)
    _run(monkeypatch, layout, _payload("s-1", repo, "compact"))
    assert "activity" not in calls.names
    assert "crewd did not start" in _context(capsys.readouterr().out)  # the full start ran (crewd is down here)


def test_compact_start_outside_a_crew_goes_to_the_relay_brief(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = Layout(home)
    repo = make_repo(tmp_path / "repo", zones=None)  # not a crew checkout, and no crew session on record
    calls = Calls(monkeypatch, {"crew_exists": {"ok": True, "exists": False}})
    payload = _payload("s-1", repo, "compact")
    _run(monkeypatch, layout, payload)
    assert calls.names == ["crew_exists"] and calls.briefs == [payload]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("source", ["startup", "resume", "clear", None])
def test_only_a_compaction_takes_the_short_start(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], source: str | None
) -> None:
    layout = Layout(home)
    repo = make_repo(tmp_path / "repo")
    _session(layout, "s-1")
    calls = Calls(monkeypatch, {"activity": {"ok": True}}, crewd_up=False)
    payload = _payload("s-1", repo, "startup")
    if source is None:
        del payload["source"]
    else:
        payload["source"] = source
    _run(monkeypatch, layout, payload)
    assert "activity" not in calls.names
    assert "crewd did not start" in _context(capsys.readouterr().out)


def test_the_reminder_fits_its_cap_with_the_longest_ids() -> None:
    text = cli.compact_reminder({"callsign": "c" * 200, "crew_id": "crw_" + "f" * 200, "observe_only": True})
    assert len(text) <= cli.START_COMPACT_CAP
    assert S.check_agent_text(text, "session_start") == []
    assert "crew_status" in text and "session_brief" in text and "observe-only" in text
    bare = cli.compact_reminder({})  # a record without a callsign or crew id still gives a usable line
    assert "crew_status" in bare and "None" not in bare and S.check_agent_text(bare, "session_start") == []
