"""How Claude Code reports a failed Bash call, and the fake agent that stands in for it (WP-15).

Recorded against the real Claude Code 2.1.168 binary with the S0 harness's mock Messages API (no credits,
temp config dir, ``--setting-sources project``): a Bash command that exits non-zero fires
**PostToolUseFailure** (``error: "Exit code N\\n<output>"``, no ``tool_response``), not PostToolUse.
``fixtures/claude-code-2.1.168/bash_fail`` holds the sanitised payloads (the recording machine's shell
start-up noise was removed from ``error``).

The model-free E2E's :mod:`fake_claude` must behave the same way, or it would feed the crew hooks
payloads Claude Code never sends. The live re-check runs with ``REMEMBRA_CREW_LIVE_CLAUDE=1`` when the
``claude`` binary is installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
CAPTURE = HERE / "fixtures" / "claude-code-2.1.168" / "bash_fail"
S0_HARNESS = HERE.parent / "fixtures" / "captures" / "s0_harness.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def recorded(event: str) -> dict[str, Any]:
    [path] = sorted(CAPTURE.glob(f"*-{event}.json"))
    return dict(json.loads(path.read_text())["payload"])


def test_the_capture_is_a_failure_event_without_a_tool_response():
    events = [json.loads(p.read_text())["event"] for p in sorted(CAPTURE.glob("*.json"))]
    assert events == ["PreToolUse", "PostToolUseFailure", "Stop", "SessionEnd"]  # no PostToolUse at all
    payload = recorded("PostToolUseFailure")
    assert "tool_response" not in payload and payload["error"].startswith("Exit code 1\n")
    assert payload["is_interrupt"] is False and payload["tool_name"] == "Bash"
    text = json.dumps(payload)
    assert "/Users/" not in text and "sk-ant-" not in text


def test_the_fake_agent_reports_a_failed_bash_the_same_way(tmp_path):
    fake = _load(HERE / "fake_claude.py", "wp15_fake_claude")
    out = tmp_path / "hooks.log"
    record = f"cat >> {shlex.quote(str(out))}; echo >> {shlex.quote(str(out))}"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": record}]}],
                    "PostToolUseFailure": [{"matcher": "Bash", "hooks": [{"type": "command", "command": record}]}],
                }
            }
        )
    )
    agent = fake.Agent(str(settings), str(tmp_path), None)
    pre = {**recorded("PreToolUse"), "cwd": str(tmp_path)}
    post = {k: v for k, v in pre.items() if k != "hook_event_name"}
    post["hook_event_name"] = "PostToolUse"
    res = agent.tool(pre, post)
    assert res["ok"] is False and not res["denied"]
    [line] = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
    want = recorded("PostToolUseFailure")
    assert set(line) == set(want), (sorted(line), sorted(want))
    assert line["hook_event_name"] == "PostToolUseFailure" and "tool_response" not in line
    assert line["error"] == "Exit code 1\nTests: 1 failed, 2 passed"


@pytest.mark.skipif(os.environ.get("REMEMBRA_CREW_LIVE_CLAUDE") != "1", reason="live: set REMEMBRA_CREW_LIVE_CLAUDE=1")
def test_live_claude_fires_post_tool_use_failure_for_a_failed_bash(tmp_path):
    h = _load(S0_HARNESS, "wp15_s0_harness")
    if not h.find_claude():
        pytest.skip("claude CLI not installed")
    mock = h.MockAnthropic()
    try:

        def hooks(capdir: Path, repo: Path) -> dict[str, list[dict[str, Any]]]:
            return h._record_all(capdir, "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd")

        command = "sh -c 'echo Tests: 1 failed, 2 passed; exit 1'"
        sc = h.Scenario(
            "bash_fail",
            "S0: run the failing test command with Bash.",
            h.Plan(tool={"name": "Bash", "input": {"command": command, "description": "failing tests"}}),
            hooks,
            extra_args=("--allowedTools", "Bash(sh:*)"),
        )
        res = h.run(sc, tmp_path, mode="mock", mock=mock)
    finally:
        mock.close()
    assert res.events() == ["PreToolUse", "PostToolUseFailure", "Stop", "SessionEnd"], res.events()
    payload = res.capture("PostToolUseFailure")["payload"]
    assert "tool_response" not in payload and payload["error"].startswith("Exit code 1")
