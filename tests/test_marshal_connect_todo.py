"""``remembra-relay connect`` ends with "You still need to" only when something is left.

The real CLI in a subprocess with an isolated HOME. Codex trust is read from
``~/.codex/config.toml`` against the hooks as written (or as ``--apply`` would
write them after a dry run), with the hash Codex computes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from remembra.relay import cli as relay_cli
from tests.marshal_fixtures import KEY, FakeHome

SRC = str(Path(__file__).resolve().parents[1] / "src")
RELAY = "/opt/bin/remembra-relay"
# The doctor's CODEX_TRUST_MISSING fix and the dashboard's, word for word (remembra.marshal.words).
TRUST = (
    "Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust SessionStart, UserPromptSubmit and SessionEnd."
    " Codex skips untrusted hooks without a message."
)
TAIL = "Then check everything with: remembra-relay doctor"


def connect(fh: FakeHome, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(fh.home), "PYTHONPATH": SRC}
    base.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "remembra.relay.cli", "connect", "--relay-command", RELAY, *args],
        capture_output=True,
        text=True,
        env=base,
        cwd=str(fh.root),
        timeout=60,
    )


def todo(stdout: str) -> list[str]:
    if "You still need to:" not in stdout:
        return []
    block = stdout.split("You still need to:", 1)[1].strip().splitlines()
    assert block[-1] == TAIL, block
    return [line.strip().split(". ", 1)[1] for line in block[:-1]]


def test_codex_apply_lists_trust_until_codex_has_it(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    (fh.home / ".codex").mkdir()
    first = connect(fh, "--agent", "codex", "--apply")
    assert first.returncode == 0, first.stderr
    assert todo(first.stdout) == [TRUST]
    assert first.stdout.rstrip().endswith(TAIL)

    fh.trust_codex()  # what /hooks writes
    again = connect(fh, "--agent", "codex", "--apply")
    assert "already connected, no change" in again.stdout
    assert "You still need to" not in again.stdout

    # A new install path rewrites the hooks: their hash changes, so Codex asks again.
    moved = connect(fh, "--agent", "codex", "--apply", "--relay-command", "/new/bin/remembra-relay")
    assert todo(moved.stdout) == [TRUST]


def test_one_trusted_hook_is_not_enough(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    (fh.home / ".codex").mkdir()
    connect(fh, "--agent", "codex", "--apply")
    fh.trust_codex(trusted=["SessionStart", "SessionEnd"])
    assert todo(connect(fh, "--agent", "codex", "--apply").stdout) == [
        "Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust UserPromptSubmit."
        " Codex skips untrusted hooks without a message."
    ]


def test_a_dry_run_lists_apply_then_trust(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    (fh.home / ".codex").mkdir()
    dry = connect(fh, "--agent", "codex")
    assert todo(dry.stdout) == [
        "Write the hooks (this was a dry run): `remembra-relay connect --apply --agent codex`.",
        "After --apply: " + TRUST,
    ]
    assert not (fh.home / ".codex" / "hooks.json").exists()  # a dry run writes nothing


def test_nothing_left_prints_nothing(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    (fh.home / ".claude").mkdir()
    out = connect(fh, "--agent", "claude-code", "--apply")
    assert out.returncode == 0 and "written" in out.stdout
    assert "You still need to" not in out.stdout


def test_missing_key_and_unverified_agents(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    no_key = {"REMEMBRA_API_KEY": "", "REMEMBRA_URL": ""}
    (fh.home / ".claude").mkdir()
    (fh.home / ".cursor").mkdir()
    out = connect(fh, "--agent", "claude-code", "--agent", "cursor", "--apply", env=no_key)
    assert todo(out.stdout) == [
        "Save your key: run `remembra-install --all` in your own terminal; it asks for the key at a hidden prompt.",
        "Unverified adapters were skipped; to write them anyway:"
        " `remembra-relay connect --apply --agent cursor --include-unverified`.",
    ]
    self_hosted = connect(fh, "--agent", "claude-code", "--apply", env={"REMEMBRA_URL": "https://memory.example.org"})
    assert todo(self_hosted.stdout) == [
        "Save your key: run `remembra-install --all --url https://memory.example.org` in your own terminal;"
        " it asks for the key at a hidden prompt."
    ]
    assert KEY not in out.stdout + self_hosted.stdout


def test_dry_run_with_unverified_agents(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    (fh.home / ".claude").mkdir()
    (fh.home / ".cursor").mkdir()
    out = connect(fh, "--agent", "claude-code", "--agent", "cursor")
    assert todo(out.stdout) == [
        "Write the hooks (this was a dry run): `remembra-relay connect --apply --agent claude-code`.",
        "Unverified adapters, never run against the real tool, only if you want them:"
        " `remembra-relay connect --apply --agent cursor --include-unverified`.",
    ]


def test_an_unreadable_config_is_named(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    settings = fh.home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ nope")
    out = connect(fh, "--agent", "claude-code", "--apply")
    assert todo(out.stdout) == [f"Repair {settings} (it could not be read), then run connect again."]


def test_brief_notices_point_at_the_doctor(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.queue("claude-code", "s-1", error="ConnectError: refused")
    ns = relay_cli.build_parser().parse_args(["brief", "--agent", "codex", "--cwd", str(fh.root)])
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(fh.home)
    try:
        ctx = relay_cli.Context(ns, payload={})
        replay = relay_cli.Replay()
        replay.key_rejected.append("env")
        notices = relay_cli.queue_notices(ctx, replay)
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old
    pointer = "Ask your agent to run remembra_doctor, or run `remembra-relay doctor`."
    assert notices[0].startswith("Remembra: your API key was rejected") and notices[0].endswith(pointer)
    assert "could not be sent yet" in notices[1] and notices[1].endswith(pointer)


def test_an_unknown_agent_is_a_usage_error_without_a_list(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    out = connect(fh, "--agent", "notepad", "--apply")
    assert out.returncode == 2 and "You still need to" not in out.stdout


def test_hooks_an_apply_left_unwritten_are_named(tmp_path: Path) -> None:
    from remembra.marshal import todo

    items = todo.connect_todo(
        tmp_path,
        [todo.Outcome("claude-code", todo.NOT_WRITTEN, path=str(tmp_path / ".claude" / "settings.json"))],
        applied=True,
        missing_key=False,
        server_url="https://api.remembra.dev",
    )
    assert items == ["Claude Code: not written by this run; see its lines above."]
