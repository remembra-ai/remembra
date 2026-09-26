"""Live round trip with the real Gemini CLI (opt-in: REMEMBRA_RELAY_LIVE=1).

Claude Code closes a session (the verified adapter's hook path), then the real
``gemini`` runs in the same repository with the hooks ``remembra-relay
connect`` wrote: SessionStart prints the brief, which reaches the model as
``<hook_context>``; SessionEnd posts Gemini's handoff. The model is a local
stand-in for the Gemini API (tests/agent_live_harness.py, reached through
``GOOGLE_GEMINI_BASE_URL`` with Gemini's "gateway" auth, no key), the Remembra
server is the local test server behind a recording proxy, and HOME is a temp
dir: no credentials are used and the user's ~/.gemini is not touched.

Also covered live, in a pseudo-terminal: after ``/clear`` the new session's
start output is dropped by Gemini, and the BeforeAgent hook (``brief
--once``) gives it the brief with its first prompt; ``/quit`` fires SessionEnd
two or three times, and exactly one close per session is posted. An untrusted
folder runs no hook at all.

``REMEMBRA_RECORD_FIXTURES=1`` rewrites tests/fixtures/relay/gemini/ from the
run (paths replaced); the replay tests in tests/test_relay_adapter_fixtures.py
read those files.

Run: REMEMBRA_RELAY_LIVE=1 REMEMBRA_GEMINI_BIN=/path/to/gemini pytest tests/test_relay_gemini_live.py --no-cov -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.agent_live_harness import (
    ModelStandIn,
    Pty,
    RecordingProxy,
    agent_path,
    by_event,
    captured,
    find_agent,
    record_fixture,
    replacements,
    request_text,
    tee_hook,
)
from tests.relay_fixtures import GIT_ENV, Transcript, commit, git, make_remote_and_clones
from tests.test_relay_cli_e2e import SRC, _api, relay, server  # noqa: F401  (server is a fixture)

LIVE = os.environ.get("REMEMBRA_RELAY_LIVE") == "1"
GEMINI = find_agent("REMEMBRA_GEMINI_BIN", "gemini") if LIVE else None
RECORD = os.environ.get("REMEMBRA_RECORD_FIXTURES") == "1"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay" / "gemini"
API_KEY = "rem_test_key_for_e2e"

pytestmark = pytest.mark.skipif(
    GEMINI is None, reason="set REMEMBRA_RELAY_LIVE=1 with a runnable gemini (REMEMBRA_GEMINI_BIN or PATH)"
)

PICK_UP = "Pick up where the last agent stopped."
BRIEF = "# Remembra brief"


class Rig:
    """Temp HOME + repo + Gemini settings + hook-payload capture for one Gemini setup."""

    def __init__(
        self, tmp: Path, server_url: str, model: ModelStandIn, proxy: RecordingProxy, gemini_home: Path | None = None
    ) -> None:
        assert GEMINI is not None
        self.gemini, self.version = GEMINI
        self.tmp, self.server, self.model, self.proxy = tmp, server_url, model, proxy
        self.home = tmp / "home"
        self.home.mkdir()
        # GEMINI_CLI_HOME: a replacement HOME that holds .gemini (settings and transcripts).
        self.settings = (gemini_home or self.home) / ".gemini" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        # Gemini's "gateway" auth: the model API at GOOGLE_GEMINI_BASE_URL, no key, no login check.
        self.settings.write_text(json.dumps({"security": {"auth": {"selectedType": "gateway", "useExternal": True}}}))
        self.payloads = tmp / "payloads"
        self.payloads.mkdir()
        (tmp / "tmp").mkdir()
        _, clones = make_remote_and_clones(tmp)
        self.repo = clones["laptop"]
        self.slug = "widget-" + "".join(c for c in tmp.name.lower() if c.isalnum())[-12:]
        git(self.repo, "remote", "set-url", "origin", f"https://github.com/acme/{self.slug}.git")
        self.hook = tee_hook(tmp / "remembra-relay", self.payloads)
        self.env = {
            "PATH": agent_path(self.gemini),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "TMPDIR": str(tmp / "tmp"),
            "TERM": "xterm-256color",
            "GEMINI_FORCE_FILE_STORAGE": "true",  # no keychain
            "GOOGLE_GEMINI_BASE_URL": model.url,
            "PYTHONPATH": SRC,
            "REMEMBRA_URL": proxy.url,
            "REMEMBRA_API_KEY": API_KEY,
            **GIT_ENV,
        }
        self.moved = {"GEMINI_CLI_HOME": str(gemini_home)} if gemini_home else {}
        self.env.update(self.moved)

    def connect(self) -> Any:
        """Plain ``connect --apply``: a verified adapter needs no --include-unverified."""
        args = ("connect", "--agent", "gemini", "--apply", "--relay-command", str(self.hook))
        out = relay(self.home, self.server, *args, env=self.moved)
        assert out.returncode == 0, out.stdout + out.stderr
        return out

    def run(self, *args: str, cwd: Path | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.gemini, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(cwd or self.repo),
            timeout=timeout,
        )

    def trail(self, agent: str, timeout: float = 30) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while True:
            out = relay(self.home, self.server, "trail", "--cwd", str(self.repo), "--format", "json")
            items = [i for i in json.loads(out.stdout or "{}").get("items") or [] if i.get("agent_id") == agent]
            if items or time.monotonic() > deadline:
                return items
            time.sleep(0.5)

    def relay_log(self) -> str:
        path = self.home / ".remembra" / "relay" / "relay.log"
        return path.read_text() if path.exists() else ""

    def record(self, name: str, payload: dict[str, Any]) -> None:
        if RECORD:
            record_fixture(FIXTURES, name, payload, replacements(self.tmp, self.home, self.repo))


def _claude_closes(rig: Rig) -> None:
    """Claude Code's session: a commit with a failing test, closed by the verified hook path."""
    start = json.dumps({"session_id": "claude-1", "cwd": str(rig.repo), "hook_event_name": "SessionStart"})
    assert relay(rig.home, rig.server, "brief", "--hook", "claude-code", stdin=start).returncode == 0
    commit(rig.repo, "test_totals.py", "def test_totals():\n    assert round(0.125, 2) == 0.13\n", "test: totals rounding")
    t = Transcript("claude-1", rig.repo)
    t.bash("python -m pytest -q", 1, "F\n1 failed in 0.02s")
    transcript = t.write(rig.tmp / "claude-1.jsonl")
    end = {"session_id": "claude-1", "transcript_path": str(transcript), "cwd": str(rig.repo), "hook_event_name": "SessionEnd"}
    closed = relay(rig.home, rig.server, "close", "--hook", "claude-code", stdin=json.dumps({**end, "reason": "logout"}))
    assert closed.returncode == 0 and closed.stderr == "", closed.stderr
    assert rig.trail("claude-code"), "claude-code handoff missing"


def _briefs_in(body: dict[str, Any]) -> int:
    return request_text(body).count(BRIEF)


@pytest.fixture()
def rig(server, tmp_path):  # noqa: F811
    with ModelStandIn() as model, RecordingProxy(server) as proxy:
        yield Rig(tmp_path, server, model, proxy)


def test_gemini_headless_round_trip_after_claude(rig):
    model: ModelStandIn = rig.model
    _claude_closes(rig)
    connect = rig.connect()
    assert "Gemini CLI runs hooks, these user-level ones included, only in trusted folders" in connect.stdout
    hooks = json.loads(rig.settings.read_text())["hooks"]
    assert {event: groups[0]["hooks"][0]["timeout"] for event, groups in hooks.items()} == {
        "SessionStart": 15000,
        "BeforeAgent": 15000,
        "SessionEnd": 15000,
    }

    run = rig.run("-p", PICK_UP, "--skip-trust")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Stand-in reply" in run.stdout
    turns = model.turns()
    assert turns, "no turn reached the model"
    first = request_text(turns[0])
    assert _briefs_in(turns[0]) == 1, first  # SessionStart gave it; BeforeAgent (--once) printed nothing
    assert "<hook_context>" in first and "Last session: claude-code" in first and "test: totals rounding" in first
    assert "you are gemini" in first

    events = by_event(rig.payloads)
    start, prompt, end = events["SessionStart"][0], events["BeforeAgent"][0], events["SessionEnd"][0]
    session_id = start["session_id"]
    assert start["source"] == "startup" and end["reason"] == "exit"
    # Headless, Gemini puts the start hook's context in front of the prompt before BeforeAgent sees it.
    assert prompt["prompt"].startswith("<hook_context>") and prompt["prompt"].endswith(PICK_UP)
    assert prompt["session_id"] == end["session_id"] == session_id
    assert Path(end["transcript_path"]).name.endswith(".jsonl") and Path(end["transcript_path"]).is_relative_to(rig.home)
    envs = [item["env"] for item in captured(rig.payloads)]
    assert all(env.get("GEMINI_SESSION_ID") == session_id for env in envs), envs

    closes = rig.proxy.wait_for_closes(1)
    assert len(closes) == 1, closes
    body = closes[0]
    assert body["agent_id"] == "gemini" and body["session_id"] == session_id and body["end_reason"] == "exit"
    assert body["facts"]["facts_source"] == "relay-cli:git"  # Gemini's chat records are not parsed
    assert [b["agent"] for b in rig.proxy.briefs()] == ["gemini"]
    items = rig.trail("gemini")
    assert len(items) == 1, items
    timeline = _api(rig.server, "GET", "/timeline", params={"project_id": items[0]["project_id"], "memory_type": "handoff"})
    content = next(m["content"] for m in timeline["memories"] if m["content"].startswith("[HANDOFF] gemini"))
    assert "ended: exit" in content

    rig.record("session_start.json", start)
    rig.record("before_agent.json", prompt)
    rig.record("session_end.json", end)
    if RECORD:
        (FIXTURES / "RECORDED.json").write_text(
            json.dumps(
                {
                    "gemini_cli": rig.version,
                    "recorded_by": "tests/test_relay_gemini_live.py",
                    "model": "local stand-in for the Gemini API (GOOGLE_GEMINI_BASE_URL, gateway auth)",
                    "hook_env": sorted(envs[0]),
                },
                indent=2,
            )
            + "\n"
        )


def _ready(term: Pty, since: int = 0) -> None:
    term.wait_for(r"Type your message", timeout=90, since=since)


def test_gemini_interactive_clear_and_quit(rig):
    """/clear: the new session's brief comes from BeforeAgent. /quit: SessionEnd fires 2-3 times, one close."""
    model: ModelStandIn = rig.model
    _claude_closes(rig)
    rig.connect()
    term = Pty([rig.gemini, "--skip-trust"], rig.env, rig.repo)
    try:
        _ready(term)
        term.type("First prompt.")
        model.wait_for_turns(1)
        term.wait_for("Stand-in reply", timeout=60)
        first = model.turns()[0]
        assert _briefs_in(first) == 1, request_text(first)

        term.type("/clear")
        term.wait_until(lambda: len(by_event(rig.payloads).get("SessionStart", [])) >= 2, what="SessionStart after /clear")
        time.sleep(2)  # the screen is redrawn after the new session starts; keys typed meanwhile are lost
        term.type("Second prompt.")
        model.wait_for_turns(2)
        term.wait_until(lambda: "Second prompt." in request_text(model.turns()[-1]), what="the second prompt")
        second = model.turns()[-1]
        assert "First prompt." not in request_text(second)  # a new session
        assert _briefs_in(second) == 1, request_text(second)  # BeforeAgent delivered it

        term.type("/quit")
        term.wait_exit(60)
    finally:
        term.close()

    events = by_event(rig.payloads)
    starts = events["SessionStart"]
    assert [s["source"] for s in starts] == ["startup", "clear"]
    old, new = starts[0]["session_id"], starts[1]["session_id"]
    assert old != new
    ends = events["SessionEnd"]
    assert [e["reason"] for e in ends if e.get("session_id") == old] == ["clear"]
    # /quit: SessionEnd for the new session more than once, the last copy often with empty stdin.
    quits = [e for e in ends if e.get("session_id") == new]
    assert [e["reason"] for e in quits][:1] == ["exit"] and len(quits) + len(events.get("", [])) >= 2, events

    closes = rig.proxy.wait_for_closes(2)
    assert sorted((c["session_id"], c["end_reason"]) for c in closes) == sorted([(old, "clear"), (new, "exit")]), closes
    assert [b["agent"] for b in rig.proxy.briefs()] == ["gemini", "gemini"]  # one per session: startup, then BeforeAgent
    assert "close: dropped a repeat sessionend of gemini session" in rig.relay_log()
    rig.record("session_start_clear.json", starts[1])
    rig.record("session_end_clear.json", next(e for e in ends if e.get("session_id") == old))


def test_untrusted_folder_runs_no_hook(rig, tmp_path):
    """Folder trust gates every hook, the user-level ones included: headless gemini stops before any of them."""
    rig.connect()
    fresh = tmp_path / "untrusted"
    fresh.mkdir()
    run = rig.run("-p", "Say hi.", cwd=fresh)
    assert run.returncode == 55, run.stdout + run.stderr
    assert re.search(r"trust", run.stdout + run.stderr, re.I)
    assert captured(rig.payloads) == []
    assert rig.proxy.requests == [] and rig.model.turns() == []


def test_connect_follows_gemini_cli_home(server, tmp_path):  # noqa: F811
    """GEMINI_CLI_HOME replaces the home Gemini keeps .gemini in: connect writes the hooks there, and Gemini runs them."""
    with ModelStandIn() as model, RecordingProxy(server) as proxy:
        rig = Rig(tmp_path, server, model, proxy, gemini_home=tmp_path / "gemini-home")
        _claude_closes(rig)
        connect = rig.connect()
        assert f"-> {rig.settings}" in connect.stdout
        assert "SessionStart" in json.loads(rig.settings.read_text())["hooks"]
        assert not (rig.home / ".gemini").exists()
        run = rig.run("-p", PICK_UP, "--skip-trust")
        assert run.returncode == 0, run.stdout + run.stderr
        assert _briefs_in(model.turns()[0]) == 1 and "Last session: claude-code" in request_text(model.turns()[0])
        end = by_event(rig.payloads)["SessionEnd"][0]
        assert Path(end["transcript_path"]).is_relative_to(tmp_path / "gemini-home" / ".gemini")
        closes = proxy.wait_for_closes(1)
        assert [(c["agent_id"], c["session_id"], c["end_reason"]) for c in closes] == [("gemini", end["session_id"], "exit")]
        assert not (rig.home / ".gemini").exists()
