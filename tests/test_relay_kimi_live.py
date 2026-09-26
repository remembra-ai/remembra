"""Live round trip with the real Kimi Code CLI (opt-in: REMEMBRA_RELAY_LIVE=1).

Claude Code closes a session (the verified adapter's hook path), then the real
``kimi`` TUI runs in the same repository, in a pseudo-terminal, with the hooks
``remembra-relay connect`` wrote. The first prompt's UserPromptSubmit hook
(``brief --once``) prints the brief, which reaches the model; the second
prompt fetches nothing; leaving the TUI (Ctrl-D twice) fires SessionEnd, whose
close posts Kimi's handoff. ``kimi -c -p`` then resumes the session without
fetching the brief again and without a close. ``kimi migrate`` copies the old
adapter's hooks from ``~/.kimi/config.toml`` without their markers, and
``connect`` / ``disconnect`` remove those copies.

The model is a local OpenAI-compatible stand-in (tests/agent_live_harness.py),
the Remembra server is the local test server behind a recording proxy, and
HOME is a temp dir: no credentials are used and the user's ~/.kimi-code is not
touched.

``REMEMBRA_RECORD_FIXTURES=1`` rewrites tests/fixtures/relay/kimi/ from the
run (paths replaced); the replay tests in tests/test_relay_adapter_fixtures.py
read those files.

Run: REMEMBRA_RELAY_LIVE=1 REMEMBRA_KIMI_BIN=/path/to/kimi pytest tests/test_relay_kimi_live.py --no-cov -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
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
KIMI = find_agent("REMEMBRA_KIMI_BIN", "kimi") if LIVE else None
RECORD = os.environ.get("REMEMBRA_RECORD_FIXTURES") == "1"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay" / "kimi"
API_KEY = "rem_test_key_for_e2e"
BRIEF = "# Remembra brief"

pytestmark = pytest.mark.skipif(KIMI is None, reason="set REMEMBRA_RELAY_LIVE=1 with a runnable kimi (REMEMBRA_KIMI_BIN or PATH)")

# What the old kimi adapter wrote into the legacy kimi-cli config (~/.kimi/config.toml).
LEGACY_BLOCK = """# >>> remembra-relay (managed block) >>>
[[hooks]]
event = "SessionStart"
command = "{relay} brief --hook kimi --agent kimi"

[[hooks]]
event = "SessionEnd"
command = "{relay} close --hook kimi --agent kimi"
# <<< remembra-relay (managed block) <<<
"""


class Rig:
    """Temp HOME + repo + Kimi Code config + hook-payload capture for one Kimi setup."""

    def __init__(self, tmp: Path, server_url: str, model: ModelStandIn, proxy: RecordingProxy) -> None:
        assert KIMI is not None
        self.kimi, self.version = KIMI
        self.tmp, self.server, self.model, self.proxy = tmp, server_url, model, proxy
        self.home = tmp / "home"
        self.config = self.home / ".kimi-code" / "config.toml"
        self.config.parent.mkdir(parents=True)
        # An "openai" provider pointing at the stand-in, with a placeholder key.
        self.provider = (
            'default_model = "stand-in"\n\n'
            "[providers.local]\n"
            'type = "openai"\n'
            f'base_url = "{model.url}/v1"\n'
            'api_key = "stand-in-placeholder"\n\n'
            '[models."stand-in"]\n'
            'provider = "local"\n'
            'model = "stand-in"\n'
            "max_context_size = 128000\n"
        )
        self.config.write_text(self.provider)
        self.payloads = tmp / "payloads"
        self.payloads.mkdir()
        (tmp / "tmp").mkdir()
        _, clones = make_remote_and_clones(tmp)
        self.repo = clones["laptop"]
        self.slug = "widget-" + "".join(c for c in tmp.name.lower() if c.isalnum())[-12:]
        git(self.repo, "remote", "set-url", "origin", f"https://github.com/acme/{self.slug}.git")
        self.hook = tee_hook(tmp / "remembra-relay", self.payloads)
        self.env = {
            "PATH": agent_path(self.kimi),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "TMPDIR": str(tmp / "tmp"),
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_TELEMETRY": "1",
            "PYTHONPATH": SRC,
            "REMEMBRA_URL": proxy.url,
            "REMEMBRA_API_KEY": API_KEY,
            **GIT_ENV,
        }

    def connect(self, *extra: str) -> Any:
        """Plain ``connect --apply``: a verified adapter needs no --include-unverified."""
        out = relay(self.home, self.server, "connect", "--agent", "kimi", "--apply", "--relay-command", str(self.hook), *extra)
        assert out.returncode == 0, out.stdout + out.stderr
        return out

    def kimi_run(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.kimi, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(self.repo),
            timeout=timeout,
        )

    def doctor_ok(self) -> bool:
        """``kimi doctor`` loads config.toml and finds it valid (Kimi rejects the whole file otherwise)."""
        out = self.kimi_run("doctor")
        assert out.returncode == 0 and re.search(r"^OK config\.toml\s", out.stdout, re.M), out.stdout + out.stderr
        return True

    def hooks(self) -> list[dict[str, Any]]:
        return list(tomllib.loads(self.config.read_text()).get("hooks") or [])

    def turn_with(self, prompt: str) -> dict[str, Any] | None:
        return next((t for t in self.model.turns() if prompt in request_text(t)), None)

    def ask(self, term: Pty, prompt: str) -> dict[str, Any]:
        """Type ``prompt``; the model turn that carries it, once its reply is on the screen."""
        shown = term.output.count(REPLY_SHOWN)
        term.type(prompt)
        term.wait_until(lambda: self.turn_with(prompt) is not None, what=f"the turn for {prompt!r}")
        term.wait_until(lambda: term.output.count(REPLY_SHOWN) > shown, what=f"the reply to {prompt!r}")
        turn = self.turn_with(prompt)
        assert turn is not None
        return turn

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


def test_kimi_tui_round_trip_after_claude_and_resume(rig):
    _claude_closes(rig)
    connect = rig.connect()
    assert "(verified)" in connect.stdout
    assert [(h["event"], h["timeout"]) for h in rig.hooks()] == [("UserPromptSubmit", 15), ("SessionEnd", 15)]
    assert rig.config.read_text().startswith(rig.provider)  # the user's config is kept as written
    assert rig.doctor_ok()

    term = Pty([rig.kimi], rig.env, rig.repo)
    try:
        term.wait_for("Trust this folder", timeout=90)
        time.sleep(1)
        term.write("\r")  # "Trust this folder" is preselected
        term.wait_for("No session yet", timeout=60)
        time.sleep(1.5)
        first = rig.ask(term, "First prompt.")
        text = request_text(first)
        assert text.count(BRIEF) == 1 and "Last session: claude-code" in text and "you are kimi" in text, text
        time.sleep(1)
        second = rig.ask(term, "Second prompt.")
        assert request_text(second).count(BRIEF) == 1  # the first prompt's copy, kept in the history; no new one
        term.write("\x04")
        time.sleep(0.5)
        term.write("\x04")  # Ctrl-D twice leaves the TUI
        term.wait_exit(60)
    finally:
        term.close()

    events = by_event(rig.payloads)
    prompts, end = events["UserPromptSubmit"], events["SessionEnd"][0]
    session_id = prompts[0]["session_id"]
    assert session_id.startswith("session_") and prompts[1]["session_id"] == session_id
    assert end["session_id"] == session_id and end["reason"] == "exit" and end["client_type"] == "kimi_code_cli"
    assert "SessionStart" not in events  # not installed: Kimi drops its output
    assert [b["agent"] for b in rig.proxy.briefs()] == ["kimi"]  # fetched once, on the first prompt
    closes = rig.proxy.wait_for_closes(1)
    assert [(c["agent_id"], c["session_id"], c["end_reason"]) for c in closes] == [("kimi", session_id, "exit")], closes
    assert closes[0]["facts"]["facts_source"] == "relay-cli:git"
    items = rig.trail("kimi")
    assert len(items) == 1, items
    timeline = _api(rig.server, "GET", "/timeline", params={"project_id": items[0]["project_id"], "memory_type": "handoff"})
    assert any(m["content"].startswith("[HANDOFF] kimi") and "ended: exit" in m["content"] for m in timeline["memories"])

    # kimi -c -p resumes the same session: the prompt hook fetches nothing, and prompt mode never closes.
    resumed = rig.kimi_run("-c", "-p", "Resumed prompt.")
    assert resumed.returncode == 0 and "Stand-in reply" in resumed.stdout, resumed.stdout + resumed.stderr
    again = by_event(rig.payloads)["UserPromptSubmit"][-1]
    assert again["session_id"] == session_id
    assert len(rig.proxy.briefs()) == 1 and len(rig.proxy.wait_for_closes(2, timeout=5)) == 1
    assert BRIEF not in resumed.stdout  # the prompt hook printed nothing

    rig.record("user_prompt_submit.json", prompts[0])
    rig.record("session_end.json", end)
    if RECORD:
        (FIXTURES / "RECORDED.json").write_text(
            json.dumps(
                {
                    "kimi_code": rig.version,
                    "recorded_by": "tests/test_relay_kimi_live.py",
                    "model": "local OpenAI-compatible stand-in ([providers] type openai in config.toml)",
                    "hook_env": sorted({k for item in captured(rig.payloads) for k in item["env"]}),
                },
                indent=2,
            )
            + "\n"
        )


def test_kimi_migrate_copies_are_removed_by_connect_and_disconnect(rig):
    legacy = rig.home / ".kimi" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text(LEGACY_BLOCK.format(relay=rig.hook))
    migrated = rig.kimi_run("migrate", "--run", "--config-only")
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    copies = [h for h in rig.hooks() if "--hook kimi" in h.get("command", "")]
    assert [h["event"] for h in copies] == ["SessionStart", "SessionEnd"], rig.config.read_text()
    assert "remembra-relay (managed block)" not in rig.config.read_text()  # the markers were not copied

    connect = rig.connect()
    assert "remove 2 relay [[hooks]] tables outside the block (copied by `kimi migrate`)" in connect.stdout
    assert [h["event"] for h in rig.hooks()] == ["UserPromptSubmit", "SessionEnd"]
    assert rig.doctor_ok()
    assert "stand-in-placeholder" in rig.config.read_text()  # the provider migrate/connect left alone

    gone = relay(rig.home, rig.server, "disconnect", "--agent", "kimi", "--apply")
    assert gone.returncode == 0, gone.stdout + gone.stderr
    assert rig.hooks() == [] and "[providers.local]" in rig.config.read_text()
    assert rig.doctor_ok()
