"""WP-10 live proof: the hook block ``connect`` writes is accepted and run by the real Claude Code binary.

``remembra-crew connect --scope project`` installs the §8.2 block into a temp
repository; the real ``claude -p`` then runs against the S0 mock Messages API
(``tests/crew/fixtures/captures/s0_harness.py``: dummy key, temp
``CLAUDE_CONFIG_DIR``, ``--setting-sources project``, no MCP servers), so no
credential leaves the machine and ``~/.claude`` is never read or written. The
gate and the crew CLI are the stdlib stub (``wp10_stub_gate.py``), which records
every call and denies writes under ``held/``.

Skipped when no ``claude`` binary is installed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.crew import install
from tests.crew.wp10_support import STUB_GATE, make_repo, stub_log, make_crew_env


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    return make_crew_env(tmp_path, monkeypatch)


HARNESS_PATH = Path(__file__).parent / "fixtures" / "captures" / "s0_harness.py"


def _harness() -> Any:
    spec = importlib.util.spec_from_file_location("wp10_s0_harness", HARNESS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["wp10_s0_harness"] = module
    spec.loader.exec_module(module)
    return module


H = _harness()
CLAUDE = H.find_claude()
pytestmark = pytest.mark.skipif(not CLAUDE, reason="claude CLI not installed")


@pytest.fixture(scope="module")
def mock():
    server = H.MockAnthropic()
    yield server
    server.close()


def _connect_project(env: dict[str, Any], repo: Path) -> None:
    master, slave = os.openpty()
    stdin = os.fdopen(slave, "r")
    try:
        code = install.main(
            [
                "--apply",
                "--yes",
                "--scope",
                "project",
                "--repo",
                str(repo),
                "--no-service",
                "--gate-source",
                str(STUB_GATE),
                "--python",
                sys.executable,
                "--crew-command",
                f"{sys.executable} {STUB_GATE}",
            ],
            home=env["home"],
            stdin=stdin,
            out=io.StringIO(),
        )
    finally:
        stdin.close()
        os.close(master)
    assert code == 0


def _claude(env: dict[str, Any], repo: Path, mock: Any, plan: Any, prompt: str) -> subprocess.CompletedProcess[str]:
    mock.reset(plan)
    config = env["tmp"] / "claude-config"
    config.mkdir(exist_ok=True)
    child = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    child.update(
        {
            "CLAUDE_CONFIG_DIR": str(config),
            "ANTHROPIC_API_KEY": H.DUMMY_KEY,
            "ANTHROPIC_BASE_URL": mock.base_url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "WP10_STUB_LOG": str(env["log"]),
        }
    )
    args = [
        CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--setting-sources", "project", "--strict-mcp-config", "--permission-mode", "acceptEdits",
        "--model", "claude-sonnet-4-5", "--max-turns", "4",
    ]  # fmt: skip
    return subprocess.run(args, cwd=repo, env=child, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)


def test_installed_hooks_deny_a_held_write_in_real_claude_code(crew_env, mock, tmp_path):
    repo = make_repo(tmp_path / "repo")
    _connect_project(crew_env, repo)
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*"

    target = repo / "held" / "cart.ts"
    plan = H.Plan(tool={"name": "Write", "input": {"file_path": str(target), "content": "x\n"}})
    proc = _claude(crew_env, repo, mock, plan, "write the cart file")
    assert not target.exists(), "the crew PreToolUse deny must stop the write"

    log = stub_log(crew_env)
    verbs = [e["verb"] for e in log]
    for verb in ("start", "turn", "pretool", "stop", "rewake", "end"):
        assert verb in verbs, (verb, verbs, proc.stderr[-2000:])
    assert "posttool" not in verbs  # PostToolUse does not fire for a denied call (S0)
    start = next(e for e in log if e["verb"] == "start")
    assert start["argv"] == ["start", "--hook", "claude-code", "--agent", "claude-code"]
    assert json.loads(start["stdin"])["hook_event_name"] == "SessionStart"
    pretool = json.loads(next(e for e in log if e["verb"] == "pretool")["stdin"])
    assert pretool["tool_name"] == "Write" and pretool["tool_input"]["file_path"] == str(target)
    sent = json.dumps([r["body"] for r in mock.requests])
    assert "BLOCKED by Remembra Crew (stub)" in sent  # the deny reason reached the model


def test_installed_hooks_run_for_bash_and_the_async_post_tool(crew_env, mock, tmp_path):
    repo = make_repo(tmp_path / "repo")
    _connect_project(crew_env, repo)
    # final_delay_s lets the async PostToolUse finish before -p exits (S0: in-flight async hooks are dropped).
    plan = H.Plan(tool={"name": "Bash", "input": {"command": "ls", "description": "list"}}, final_delay_s=2.5)
    _claude(crew_env, repo, mock, plan, "list the files")
    log = stub_log(crew_env)
    pre = [json.loads(e["stdin"]) for e in log if e["verb"] == "pretool"]
    post = [json.loads(e["stdin"]) for e in log if e["verb"] == "posttool"]
    assert pre and pre[0]["tool_name"] == "Bash" and pre[0]["tool_input"]["command"] == "ls"
    assert post and post[0]["hook_event_name"] == "PostToolUse" and post[0]["tool_name"] == "Bash"
