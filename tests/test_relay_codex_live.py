"""Live round trip with the real Codex CLI (opt-in: REMEMBRA_RELAY_LIVE=1).

Claude Code closes a session (the verified adapter's hook path), then the real
``codex exec`` runs in the same repository with the hooks ``remembra-relay
connect`` wrote: SessionStart prints the brief, which reaches the model;
Codex runs commands; SessionEnd posts Codex's handoff from its rollout. The
model is a local stand-in (tests/codex_live_harness.py), the Remembra server
is the local test server, and HOME / CODEX_HOME are temp dirs: no credentials
are used and the user's ~/.codex is not touched.

Also covered live: Codex skips untrusted hooks without a word (so ``connect``
prints the trust step), the UserPromptSubmit hook delivers the brief exactly
once when SessionStart does not fire (openai/codex#24228) and on ``exec
resume``, and a turn that stops on the usage limit reaches the relay as
``ended: usage_limit``.

``REMEMBRA_RECORD_FIXTURES=1`` rewrites tests/fixtures/relay/codex/ from the
run (paths replaced, Codex's built-in instructions trimmed); the replay tests
in tests/test_relay_adapter_fixtures.py read those files.

Run: REMEMBRA_RELAY_LIVE=1 pytest tests/test_relay_codex_live.py --no-cov -q
(REMEMBRA_CODEX_BIN=/path/to/codex to pick a binary).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.facts import parse_codex_rollout
from tests.codex_live_harness import (
    FIXTURE_HOME,
    FIXTURE_REPO,
    MockResponses,
    find_codex,
    newest_rollout,
    run_codex,
    scrub_rollout,
    scrub_text,
    trust_hooks,
    write_codex_config,
)
from tests.relay_fixtures import GIT_ENV, Transcript, commit, git, make_remote_and_clones
from tests.test_relay_cli_e2e import SRC, _api, relay, server  # noqa: F401  (server is a fixture)

LIVE = os.environ.get("REMEMBRA_RELAY_LIVE") == "1"
CODEX = find_codex() if LIVE else None
RECORD = os.environ.get("REMEMBRA_RECORD_FIXTURES") == "1"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay" / "codex"
API_KEY = "rem_test_key_for_e2e"

pytestmark = pytest.mark.skipif(
    CODEX is None, reason="set REMEMBRA_RELAY_LIVE=1 with a runnable codex (REMEMBRA_CODEX_BIN, PATH or ChatGPT.app)"
)

PICK_UP = "Pick up where the last agent stopped."
FOLLOW_UP = "Anything else before you stop?"
LIMIT = "Keep going on the totals fix."


class Rig:
    """Temp HOME + CODEX_HOME + repo + hook-payload capture for one Codex setup."""

    def __init__(self, tmp: Path, server_url: str, mock: MockResponses) -> None:
        assert CODEX is not None
        self.codex, self.version = CODEX
        self.tmp = tmp
        self.server = server_url
        self.home = tmp / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.config = write_codex_config(self.codex_home, mock.base_url)
        self.payloads = tmp / "payloads"
        self.payloads.mkdir()
        _, clones = make_remote_and_clones(tmp)
        self.repo = clones["laptop"]
        # One project per test: the module's server is shared.
        self.slug = "widget-" + "".join(c for c in tmp.name.lower() if c.isalnum())[-12:]
        git(self.repo, "remote", "set-url", "origin", f"https://github.com/acme/{self.slug}.git")
        # The hook command: tee the payload Codex sends to a file, then run the relay.
        self.hook = tmp / "remembra-relay"
        self.hook.write_text(
            "#!/bin/sh\n"
            f'f=$(mktemp "{self.payloads}/payload.XXXXXX")\n'
            f'tee "$f" | exec "{sys.executable}" -m remembra.relay.cli "$@"\n'
        )
        self.hook.chmod(self.hook.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "PYTHONPATH": SRC,
            "REMEMBRA_URL": server_url,
            "REMEMBRA_API_KEY": API_KEY,
            **GIT_ENV,
        }

    def connect(self) -> Any:
        out = relay(self.home, self.server, "connect", "--agent", "codex", "--apply", "--relay-command", str(self.hook))
        assert out.returncode == 0, out.stderr
        return out

    def exec(self, *args: str) -> Any:
        out = run_codex(self.codex, self.env, self.repo, *args)
        assert out.returncode == 0, out.stdout + out.stderr
        return out

    def captured(self) -> dict[str, list[dict[str, Any]]]:
        by_event: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(self.payloads.iterdir(), key=lambda p: p.stat().st_mtime_ns):
            text = path.read_text()
            if text.strip():
                payload = json.loads(text)
                by_event.setdefault(payload["hook_event_name"], []).append(payload)
        return by_event

    def trail(self, agent: str, timeout: float = 30) -> list[dict[str, Any]]:
        """This project's trail entries from ``agent``, waiting for the detached close to land."""
        deadline = time.monotonic() + timeout
        while True:
            out = relay(self.home, self.server, "trail", "--cwd", str(self.repo), "--format", "json")
            items = [i for i in json.loads(out.stdout or "{}").get("items") or [] if i.get("agent_id") == agent]
            if items or time.monotonic() > deadline:
                return items
            time.sleep(0.5)

    def replacements(self) -> dict[str, str]:
        real = {str(self.repo): FIXTURE_REPO, str(self.home): FIXTURE_HOME, str(self.tmp): "/home/dev/tmp"}
        real[sys.executable] = "python"
        real.update({os.path.realpath(k): v for k, v in list(real.items())})
        return real


def _brief_count(texts: list[str]) -> int:
    return sum(1 for t in texts if "# Remembra brief" in t)


def _claude_closes(rig: Rig) -> str:
    """Claude Code's session: a commit with a failing test, closed by the verified hook path."""
    start = json.dumps({"session_id": "claude-1", "cwd": str(rig.repo), "hook_event_name": "SessionStart"})
    assert relay(rig.home, rig.server, "brief", "--hook", "claude-code", stdin=start).returncode == 0
    commit(rig.repo, "test_totals.py", "def test_totals():\n    assert round(0.125, 2) == 0.13\n", "test: totals rounding")
    t = Transcript("claude-1", rig.repo)
    t.bash("python -m pytest -q", 1, "F\n1 failed in 0.02s")
    transcript = t.write(rig.tmp / "claude-1.jsonl")
    end = json.dumps(
        {
            "session_id": "claude-1",
            "transcript_path": str(transcript),
            "cwd": str(rig.repo),
            "hook_event_name": "SessionEnd",
            "reason": "logout",
        }
    )
    closed = relay(rig.home, rig.server, "close", "--hook", "claude-code", stdin=end)
    assert closed.returncode == 0 and closed.stderr == "", closed.stderr
    assert rig.trail("claude-code"), "claude-code handoff missing"
    return transcript.name


def _record(rig: Rig, name: str, content: str) -> None:
    if RECORD:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / name).write_text(scrub_text(content, rig.replacements()))


def test_codex_round_trip_after_claude(server, tmp_path):  # noqa: F811
    pytest_cmd = f"PYTHONDONTWRITEBYTECODE=1 {sys.executable} -m pytest -q -p no:cacheprovider"
    scripts = {
        PICK_UP: [("exec", pytest_cmd), ("exec", "sh scripts/build.sh"), ("say", "test_totals still fails.")],
        FOLLOW_UP: [("say", "No, that is all.")],
        "Say hi.": [("say", "Hi.")],
    }
    with MockResponses(scripts) as mock:
        rig = Rig(tmp_path, server, mock)
        _claude_closes(rig)
        connect = rig.connect()
        assert "run /hooks to trust the three remembra-relay hooks" in connect.stdout

        # 1. Untrusted: Codex skips the hooks without saying so. No brief reaches the model.
        rig.exec("Say hi.")
        assert rig.captured() == {}
        assert _brief_count(MockResponses.developer_texts(mock.requests[-1])) == 0

        # 2. Trust them the way /hooks does, then work in Codex.
        trusted = trust_hooks(rig.codex, rig.env, rig.repo, rig.config)
        assert {h["eventName"] for h in trusted} == {"sessionStart", "userPromptSubmit", "sessionEnd"}
        before = len(mock.requests)
        run = rig.exec(PICK_UP)
        turn = mock.requests[before:]
        first = MockResponses.developer_texts(turn[0])
        brief = [t for t in first if "# Remembra brief" in t]
        assert len(brief) == 1, first  # SessionStart printed it; UserPromptSubmit (--once) printed nothing
        assert "Last session: claude-code" in brief[0] and "test: totals rounding" in brief[0]
        assert "you are codex" in brief[0]

        # 3. SessionEnd -> detached close -> Codex's handoff in the trail, facts from the rollout.
        items = rig.trail("codex")
        assert len(items) == 1, items
        entry = items[0]
        assert entry["memory_type"] == "handoff"
        timeline = _api(server, "GET", "/timeline", params={"project_id": entry["project_id"], "memory_type": "handoff"})
        content = next(m["content"] for m in timeline["memories"] if m["content"].startswith("[HANDOFF] codex"))
        assert content.startswith("[HANDOFF] codex") and "ended: other" in content
        assert "Facts: collected by remembra-relay from git and the session transcript." in content
        assert "`sh scripts/build.sh` exited 127" in content
        assert "FAILING: PYTHONDONTWRITEBYTECODE=1" in content and "(1 failed in" in content
        assert "1 commit(s) not pushed to origin/main" in content  # git facts too

        captured = rig.captured()
        session_id = captured["SessionStart"][0]["session_id"]
        assert captured["SessionStart"][0]["source"] == "startup"
        assert captured["UserPromptSubmit"][0]["prompt"] == PICK_UP
        end = captured["SessionEnd"][0]
        assert end["session_id"] == session_id and end["reason"] == "other"
        rollout = Path(end["transcript_path"])
        assert rollout == newest_rollout(rig.codex_home)
        facts = parse_codex_rollout(rollout, root=str(rig.repo))
        assert [c["exit_code"] for c in facts.commands] == [1, 127]
        assert facts.tests and facts.tests[0]["passed"] is False and "1 failed" in (facts.tests[0]["summary"] or "")
        assert facts.session_id == session_id and facts.usage_limit is None

        # 4. exec resume: the restored context already has the brief; no second copy.
        before = len(mock.requests)
        rig.exec("resume", session_id, FOLLOW_UP)
        resumed = MockResponses.developer_texts(mock.requests[before])
        assert _brief_count(resumed) == 1, resumed
        after = rig.captured()

        _record(rig, "session_start.json", json.dumps(captured["SessionStart"][0], indent=2) + "\n")
        _record(rig, "user_prompt_submit.json", json.dumps(captured["UserPromptSubmit"][0], indent=2) + "\n")
        _record(rig, "session_end.json", json.dumps(end, indent=2) + "\n")
        _record(rig, "rollout_round_trip.jsonl", scrub_rollout(rollout.read_text(), rig.replacements()))
        resume_starts = [p for p in after.get("SessionStart", []) if p.get("source") == "resume"]
        if resume_starts:
            _record(rig, "session_start_resume.json", json.dumps(resume_starts[0], indent=2) + "\n")
        _record(
            rig,
            "RECORDED.json",
            json.dumps(
                {
                    "codex_cli": rig.version,
                    "recorded_by": "tests/test_relay_codex_live.py",
                    "exec_output_tail": run.stdout.strip().splitlines()[-1:],
                    "resume_fired_session_start": bool(resume_starts),
                },
                indent=2,
            )
            + "\n",
        )


def test_prompt_hook_delivers_the_brief_once_when_session_start_does_not_fire(server, tmp_path):  # noqa: F811
    """openai/codex#24228: bare `codex` auto-restoring a thread fires no SessionStart.

    Reproduced by leaving SessionStart untrusted (so it does not run): the
    UserPromptSubmit hook delivers the brief on the first prompt, and not again.
    """
    scripts = {"First prompt.": [("say", "ok")], "Second prompt.": [("say", "ok again")]}
    with MockResponses(scripts) as mock:
        rig = Rig(tmp_path, server, mock)
        _claude_closes(rig)
        rig.connect()
        trust_hooks(rig.codex, rig.env, rig.repo, rig.config, events={"userPromptSubmit", "sessionEnd"})
        before = len(mock.requests)
        rig.exec("First prompt.")
        first = MockResponses.developer_texts(mock.requests[before])
        assert _brief_count(first) == 1 and any("Last session: claude-code" in t for t in first), first
        session_id = rig.captured()["UserPromptSubmit"][0]["session_id"]
        assert "SessionStart" not in rig.captured()

        before = len(mock.requests)
        rig.exec("resume", session_id, "Second prompt.")
        second = MockResponses.developer_texts(mock.requests[before])
        assert _brief_count(second) == 1, second  # the restored copy only
        prompts = rig.captured()["UserPromptSubmit"]
        assert [p["prompt"] for p in prompts] == ["First prompt.", "Second prompt."]
        assert {p["session_id"] for p in prompts} == {session_id}


def test_usage_limit_stop_reaches_the_relay(server, tmp_path):  # noqa: F811
    """Codex has no usage-limit hook (openai/codex#45977); the rollout tail carries it."""
    with MockResponses({LIMIT: [("limit",)]}) as mock:
        rig = Rig(tmp_path, server, mock)
        rig.connect()
        trust_hooks(rig.codex, rig.env, rig.repo, rig.config)
        out = run_codex(rig.codex, rig.env, rig.repo, LIMIT)
        assert "hit your usage limit" in (out.stdout + out.stderr).replace("’", "'")
        items = rig.trail("codex")
        assert len(items) == 1, items
        timeline = _api(server, "GET", "/timeline", params={"project_id": items[0]["project_id"], "memory_type": "handoff"})
        content = timeline["memories"][0]["content"]
        assert "ended: usage_limit" in content
        assert "Stopped on a usage limit: You" in content and "hit your usage limit" in content.replace("’", "'")
        end = rig.captured()["SessionEnd"][0]
        rollout = Path(end["transcript_path"])
        assert parse_codex_rollout(rollout).usage_limit
        _record(rig, "rollout_usage_limit.jsonl", scrub_rollout(rollout.read_text(), rig.replacements()))
