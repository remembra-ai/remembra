"""F34 end to end: the real ``remembra-relay`` (subprocess) -> HTTP -> the real API -> SQLite.

A Claude Code session outside a crew: its Stop hook leaves a checkpoint through a real detached
process, the session is then "killed" (its SessionEnd hook never runs), and another agent's brief has
the work. A message sent after the start brief reaches the session through its prompt hook.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from tests.relay_fixtures import Transcript
from tests.test_relay_cli_e2e import _api, _two_clones, home, relay, server  # noqa: F401  (fixtures)

CLAUDE = ("--hook", "claude-code", "--agent", "claude-code")


def _age(home: Path, kind: str, session: str, seconds: float = 60.0) -> None:  # noqa: F811
    """Make one of the session's relay state files ``seconds`` old (the prompt hook asks at most every 30 s)."""
    digest = hashlib.sha256(f"{kind}\x1fclaude-code\x1f{session}".encode()).hexdigest()[:24]
    path = home / ".remembra" / "relay" / "sessions" / f"{kind}-{digest}.json"
    then = time.time() - seconds
    if kind == "turn":
        state = json.loads(path.read_text())
        state["checked_at"] = then
        path.write_text(json.dumps(state))
    os.utime(path, (then, then))


def test_a_killed_session_leaves_a_checkpoint_and_a_late_message_reaches_the_running_one(server, home, tmp_path):  # noqa: F811
    laptop, drive = _two_clones(tmp_path, "AcmeCheckpoint")
    start = json.dumps({"session_id": "sess-K", "cwd": str(laptop), "hook_event_name": "SessionStart", "source": "startup"})
    brief = relay(home, server, "brief", *CLAUDE, stdin=start)
    assert brief.returncode == 0 and "Last session: none recorded for this project." in brief.stdout, brief.stderr

    # --- a message that arrives after the brief -----------------------------------------------
    turn = json.loads((next((home / ".remembra" / "relay" / "sessions").glob("turn-*.json"))).read_text())
    sent = _api(
        server,
        "POST",
        "/inbox/send",
        json={
            "to_agent": "claude-code",
            "from_agent": "codex",
            "subject": "rebase before you push",
            "body": "the migration branch is merged",
            "project_id": turn["project_id"],
        },
    )
    prompt = json.dumps({"session_id": "sess-K", "cwd": str(laptop), "hook_event_name": "UserPromptSubmit", "prompt": "go on"})
    quiet = relay(home, server, "brief", *CLAUDE, "--once", stdin=prompt)
    assert quiet.returncode == 0 and quiet.stdout == ""  # within 30 s of the brief: not asked
    _age(home, "turn", "sess-K")
    _age(home, "brief", "sess-K")
    shown = relay(home, server, "brief", *CLAUDE, "--once", stdin=prompt)
    assert shown.returncode == 0, shown.stderr
    assert f"MESSAGE {sent['inbox_id']} for you from codex" in shown.stdout
    assert "rebase before you push" in shown.stdout and "the migration branch is merged" not in shown.stdout
    assert shown.elapsed < 6
    _age(home, "turn", "sess-K")
    again = relay(home, server, "brief", *CLAUDE, "--once", stdin=prompt)
    assert again.returncode == 0 and again.stdout == ""  # each message once

    # --- the work, then a turn ends ------------------------------------------------------------
    (laptop / "README.md").write_text("# widget\nhalf-done change\n")
    t = Transcript("sess-K", laptop)
    t.bash("pytest -q", 1, "=== 1 failed in 0.1s ===")
    transcript = t.write(tmp_path / "sess-K.jsonl")
    stop = json.dumps({"session_id": "sess-K", "cwd": str(laptop), "transcript_path": str(transcript), "hook_event_name": "Stop"})
    hook = relay(home, server, "close", *CLAUDE, "--checkpoint", stdin=stop)
    assert hook.returncode == 0 and hook.stdout == "" and hook.stderr == ""
    assert hook.elapsed < 5  # the hook only starts a detached process

    # The session is killed here: no SessionEnd. The detached checkpoint arrives on its own.
    deadline = time.time() + 30
    picked = ""
    while time.time() < deadline:
        picked = relay(home, server, "brief", "--agent", "codex", "--cwd", str(drive)).stdout
        if "still open" in picked:
            break
        time.sleep(0.5)
    assert "still open (saved while the session was running)" in picked, picked
    assert "README.md" in picked and "pytest -q" in picked
    log = (home / ".remembra" / "relay" / "relay.log").read_text()
    assert "close --hook claude-code: checkpoint started (Stop, session sess-K)" in log
    assert "close: sent the handoff of claude-code session sess-K" in log

    # the next turn, seconds later: no second process, nothing new on the server
    before = _api(server, "GET", "/trail", params={"project_id": turn["project_id"]})
    relay(home, server, "close", *CLAUDE, "--checkpoint", stdin=stop)
    time.sleep(1.0)
    after = _api(server, "GET", "/trail", params={"project_id": turn["project_id"]})
    assert len(after["items"]) == len(before["items"])
    assert (home / ".remembra" / "relay" / "relay.log").read_text().count("checkpoint started") == 1
