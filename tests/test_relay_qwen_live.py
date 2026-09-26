"""Live round trip with the real Qwen Code (opt-in: REMEMBRA_RELAY_LIVE=1).

Claude Code closes a session (the verified adapter's hook path), then the real
``qwen`` runs in the same repository, in a pseudo-terminal, with the hooks
``remembra-relay connect`` wrote. SessionStart prints the brief, which reaches
the model; ``/compress`` (PreCompact), ``/clear`` (SessionEnd, then a new
session that gets its own brief), a turn that fails with a 402 quota error
(StopFailure ``billing_error``) and ``/quit`` each post a close with their
own end reason. SIGTERM also closes, a resumed session (``--continue``) gets
a fresh brief, and one-shot ``qwen -p`` gets the brief but writes no handoff.

The model is a local OpenAI-compatible stand-in (tests/agent_live_harness.py),
the Remembra server is the local test server behind a recording proxy, and
HOME is a temp dir: no credentials are used and the user's ~/.qwen is not
touched.

``REMEMBRA_RECORD_FIXTURES=1`` rewrites tests/fixtures/relay/qwen/ from the
run (paths replaced); the replay tests in tests/test_relay_adapter_fixtures.py
read those files.

Run: REMEMBRA_RELAY_LIVE=1 REMEMBRA_QWEN_BIN=/path/to/qwen pytest tests/test_relay_qwen_live.py --no-cov -q
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.agent_live_harness import (
    REPLY_SHOWN,
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
QWEN = find_agent("REMEMBRA_QWEN_BIN", "qwen") if LIVE else None
RECORD = os.environ.get("REMEMBRA_RECORD_FIXTURES") == "1"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay" / "qwen"
API_KEY = "rem_test_key_for_e2e"
BRIEF = "# Remembra brief"
QUOTA_ERROR = {
    "error": {
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}

pytestmark = pytest.mark.skipif(QWEN is None, reason="set REMEMBRA_RELAY_LIVE=1 with a runnable qwen (REMEMBRA_QWEN_BIN or PATH)")


class Rig:
    """Temp HOME + repo + Qwen settings + hook-payload capture for one Qwen setup."""

    def __init__(self, tmp: Path, server_url: str, model: ModelStandIn, proxy: RecordingProxy) -> None:
        assert QWEN is not None
        self.qwen, self.version = QWEN
        self.tmp, self.server, self.model, self.proxy = tmp, server_url, model, proxy
        self.home = tmp / "home"
        self.settings = self.home / ".qwen" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        # An "openai" provider: the model at OPENAI_BASE_URL (the stand-in), a placeholder key.
        self.settings.write_text(json.dumps({"security": {"auth": {"selectedType": "openai"}}, "model": {"name": "stand-in"}}))
        self.payloads = tmp / "payloads"
        self.payloads.mkdir()
        (tmp / "tmp").mkdir()
        _, clones = make_remote_and_clones(tmp)
        self.repo = clones["laptop"]
        self.slug = "widget-" + "".join(c for c in tmp.name.lower() if c.isalnum())[-12:]
        git(self.repo, "remote", "set-url", "origin", f"https://github.com/acme/{self.slug}.git")
        self.hook = tee_hook(tmp / "remembra-relay", self.payloads)
        self.env = {
            "PATH": agent_path(self.qwen),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "TMPDIR": str(tmp / "tmp"),
            "TERM": "xterm-256color",
            "OPENAI_API_KEY": "stand-in-placeholder",  # not a key: the stand-in never checks it
            "OPENAI_BASE_URL": model.url + "/v1",
            "OPENAI_MODEL": "stand-in",
            "PYTHONPATH": SRC,
            "REMEMBRA_URL": proxy.url,
            "REMEMBRA_API_KEY": API_KEY,
            **GIT_ENV,
        }

    def connect(self) -> Any:
        """Plain ``connect --apply``: a verified adapter needs no --include-unverified."""
        out = relay(self.home, self.server, "connect", "--agent", "qwen", "--apply", "--relay-command", str(self.hook))
        assert out.returncode == 0, out.stdout + out.stderr
        return out

    def tui(self, *args: str) -> Pty:
        term = Pty([self.qwen, *args], self.env, self.repo)
        term.wait_for(r"Type your message", timeout=90)
        time.sleep(1.5)  # the first frame is drawn before the input is live
        return term

    def ask(self, term: Pty, prompt: str) -> dict[str, Any]:
        """Type ``prompt``; the model turn that carries it, once its reply is on the screen."""
        shown = term.output.count(REPLY_SHOWN)
        term.type(prompt)
        term.wait_until(lambda: self.turn_with(prompt) is not None, what=f"the turn for {prompt!r}")
        term.wait_until(lambda: term.output.count(REPLY_SHOWN) > shown, what=f"the reply to {prompt!r}")
        turn = self.turn_with(prompt)
        assert turn is not None
        return turn

    def turn_with(self, prompt: str) -> dict[str, Any] | None:
        return next((t for t in self.model.turns() if prompt in request_text(t)), None)

    def trail(self, agent: str, timeout: float = 30) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while True:
            out = relay(self.home, self.server, "trail", "--cwd", str(self.repo), "--format", "json")
            items = [i for i in json.loads(out.stdout or "{}").get("items") or [] if i.get("agent_id") == agent]
            if items or time.monotonic() > deadline:
                return items
            time.sleep(0.5)

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


@pytest.fixture()
def rig(server, tmp_path):  # noqa: F811
    with ModelStandIn() as model, RecordingProxy(server) as proxy:
        yield Rig(tmp_path, server, model, proxy)


def test_qwen_interactive_session_compress_clear_quota_stop_and_quit(rig):
    _claude_closes(rig)
    connect = rig.connect()
    assert "(verified)" in connect.stdout
    hooks = json.loads(rig.settings.read_text())["hooks"]
    assert {event: groups[0]["hooks"][0]["timeout"] for event, groups in hooks.items()} == {
        "SessionStart": 15,
        "SessionEnd": 15,
        "StopFailure": 15,
        "PreCompact": 15,
    }
    assert hooks["StopFailure"][0]["matcher"] == "rate_limit|billing_error"

    term = rig.tui()
    try:
        first = rig.ask(term, "First prompt.")
        text = request_text(first)
        assert text.count(BRIEF) == 1 and "Last session: claude-code" in text and "you are qwen" in text, text

        term.type("/compress")
        term.wait_until(lambda: "PreCompact" in by_event(rig.payloads), what="PreCompact")
        time.sleep(2)
        term.type("/clear")
        term.wait_until(lambda: len(by_event(rig.payloads).get("SessionStart", [])) >= 2, what="SessionStart after /clear")
        time.sleep(2)
        second = rig.ask(term, "Second prompt.")
        assert "First prompt." not in request_text(second)  # a new session
        assert request_text(second).count(BRIEF) == 1  # Qwen keeps the start context after /clear

        rig.model.fail_with = (402, QUOTA_ERROR)
        term.type("Third prompt.")
        term.wait_until(lambda: "StopFailure" in by_event(rig.payloads), timeout=90, what="StopFailure")
        rig.model.fail_with = None
        time.sleep(2)
        term.type("/quit")
        term.wait_exit(60)
    finally:
        term.close()

    events = by_event(rig.payloads)
    starts = events["SessionStart"]
    assert [s["source"] for s in starts] == ["startup", "clear"]
    old, new = starts[0]["session_id"], starts[1]["session_id"]
    precompact, stop = events["PreCompact"][0], events["StopFailure"][0]
    assert precompact["session_id"] == old and precompact["trigger"] == "manual"
    assert stop["session_id"] == new and stop["error"] == "billing_error"
    ends = events["SessionEnd"]
    assert [(e["session_id"], e["reason"]) for e in ends] == [(old, "clear"), (new, "prompt_input_exit")]
    envs = {item["payload"]["session_id"]: item["env"] for item in captured(rig.payloads)}
    assert all(env.get("QWEN_CODE_SESSION_ID") == sid and env.get("QWEN_PROJECT_DIR") for sid, env in envs.items()), envs

    closes = rig.proxy.wait_for_closes(4)
    assert [(c["session_id"], c["end_reason"]) for c in closes] == [
        (old, "pre-compact:manual"),
        (old, "clear"),
        (new, "billing_error"),
        (new, "prompt_input_exit"),
    ], closes
    assert all(c["agent_id"] == "qwen" and c["facts"]["facts_source"] == "relay-cli:git" for c in closes)
    assert [b["agent"] for b in rig.proxy.briefs()] == ["qwen", "qwen"]  # startup, then the session /clear started
    items = rig.trail("qwen")
    assert items
    timeline = _api(rig.server, "GET", "/timeline", params={"project_id": items[0]["project_id"], "memory_type": "handoff"})
    handoffs = [m["content"] for m in timeline["memories"] if m["content"].startswith("[HANDOFF] qwen")]
    assert any("ended: prompt_input_exit" in h for h in handoffs), handoffs

    rig.record("session_start.json", starts[0])
    rig.record("session_start_clear.json", starts[1])
    rig.record("pre_compact.json", precompact)
    rig.record("stop_failure.json", stop)
    rig.record("session_end.json", ends[1])
    rig.record("session_end_clear.json", ends[0])
    if RECORD:
        (FIXTURES / "RECORDED.json").write_text(
            json.dumps(
                {
                    "qwen_code": rig.version,
                    "recorded_by": "tests/test_relay_qwen_live.py",
                    "model": "local OpenAI-compatible stand-in (security.auth.selectedType openai, OPENAI_BASE_URL)",
                    "hook_env": sorted(envs[old]),
                },
                indent=2,
            )
            + "\n"
        )


def test_qwen_sigterm_closes_and_continue_gets_a_fresh_brief(rig):
    rig.connect()
    term = rig.tui()
    try:
        rig.ask(term, "First prompt.")
        term.signal(signal.SIGTERM)
        term.wait_exit(30)
    finally:
        term.close()
    closes = rig.proxy.wait_for_closes(1)
    session_id = by_event(rig.payloads)["SessionStart"][0]["session_id"]
    assert [(c["session_id"], c["end_reason"]) for c in closes] == [(session_id, "prompt_input_exit")], closes

    # --continue resumes the session; Qwen does not restore the old start context, so it gets a new brief.
    term = rig.tui("--continue")
    try:
        resumed = rig.ask(term, "Resumed prompt.")
        term.type("/quit")
        term.wait_exit(60)
    finally:
        term.close()
    starts = by_event(rig.payloads)["SessionStart"]
    assert [(s["source"], s["session_id"]) for s in starts] == [("startup", session_id), ("resume", session_id)]
    assert request_text(resumed).count(BRIEF) == 1
    assert len(rig.proxy.wait_for_closes(2)) == 2
    rig.record("session_start_resume.json", starts[1])


def test_qwen_one_shot_gets_the_brief_and_writes_no_handoff(rig):
    rig.connect()
    run = subprocess.run(
        [rig.qwen, "-p", "Say hi."],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=rig.env,
        cwd=str(rig.repo),
        timeout=120,
    )
    assert run.returncode == 0 and "Stand-in reply" in run.stdout, run.stdout + run.stderr
    turn = rig.turn_with("Say hi.")
    assert turn is not None and request_text(turn).count(BRIEF) == 1
    assert list(by_event(rig.payloads)) == ["SessionStart"]  # no SessionEnd: `qwen -p` never ends its session
    assert rig.proxy.wait_for_closes(1, timeout=5) == []
