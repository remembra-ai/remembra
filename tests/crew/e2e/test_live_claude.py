"""Step 2 of §13.3 with the real Claude Code binary (WP-15; ``REMEMBRA_CREW_LIVE_CLAUDE=1``).

S0 proved the real ``claude -p`` cheap and reliable against a local mock of the Messages API (dummy key,
temp ``CLAUDE_CONFIG_DIR``, ``--setting-sources project``, no MCP servers, no credits), so one run here
uses it with the **real** crew stack: the hooks ``connect --scope project`` installed, the vendored
gate, crewd and the crew-mode server. A (a fake agent) holds POS; the real Claude Code session in B's
worktree is told to edit POS. Its SessionStart brief names cc-1 on POS, its Write is denied by the real
gate before the file is touched, the model receives the reason, and SessionEnd leaves the crew.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.crew.e2e.harness import World, wait_for

S0_HARNESS = Path(__file__).parent.parent / "fixtures" / "captures" / "s0_harness.py"
pytestmark = pytest.mark.skipif(os.environ.get("REMEMBRA_CREW_LIVE_CLAUDE") != "1", reason="live: REMEMBRA_CREW_LIVE_CLAUDE=1")


def _harness() -> Any:
    spec = importlib.util.spec_from_file_location("wp15_live_s0", S0_HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["wp15_live_s0"] = module
    spec.loader.exec_module(module)
    return module


def test_real_claude_code_is_denied_by_the_real_crew_stack(tmp_path: Path) -> None:
    h = _harness()
    claude = h.find_claude()
    if not claude:
        pytest.skip("claude CLI not installed")
    w = World(tmp_path)
    mock = h.MockAnthropic()
    try:
        wts = w.make_repos()
        w.start_server()
        w.connect()  # global (temp HOME): the fake agent A
        w.connect("--scope", "project", "--repo", str(wts["wt-b"]), global_settings=False)  # B's real Claude Code
        assert (wts["wt-b"] / ".claude" / "settings.json").exists() or (wts["wt-b"] / ".claude" / "settings.local.json").exists()
        a = w.agent("A", wts["wt-a"])
        a.start()
        crew = str(a.local_session()["crew_id"])
        feed = w.feed(crew)
        assert not a.write("src/app/pos/split.ts", "export const split = 21;\n")["denied"]  # A holds POS
        # a new file (Claude Code refuses to overwrite a file it has not read, before any hook runs)
        target = wts["wt-b"] / "src/app/pos/rounding.ts"
        mock.reset(
            h.Plan(
                tool={
                    "name": "Write",
                    "input": {"file_path": str(target), "content": "export const round2 = (n: number) => n;\n"},
                }
            )
        )
        config = tmp_path / "claude-config"
        config.mkdir()
        env = w.agent_env()
        env.update(
            {
                "CLAUDE_CONFIG_DIR": str(config),
                "ANTHROPIC_API_KEY": h.DUMMY_KEY,
                "ANTHROPIC_BASE_URL": mock.base_url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_MAX_RETRIES": "0",
            }
        )
        args = [
            claude, "-p", "Fix the rounding in src/app/pos/split.ts.", "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project", "--strict-mcp-config", "--permission-mode", "acceptEdits",
            "--model", "claude-sonnet-4-5", "--max-turns", "4",
        ]  # fmt: skip
        res = subprocess.run(
            args, cwd=wts["wt-b"], env=env, capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL
        )
        assert res.returncode == 0, res.stderr[-2000:]
        assert not target.exists()  # denied before the file was written
        bodies = [json.dumps(r["body"], ensure_ascii=False) for r in mock.requests if r["path"].endswith("/v1/messages")]
        assert "DO NOT TOUCH: zone pos → cc-1" in bodies[0], bodies[0][:3000]  # the SessionStart brief reached the model
        assert any("BLOCKED by Remembra Crew" in b and "cc-1" in b for b in bodies[1:]), bodies[-1][:3000]
        joined = [e for e in feed.events() if e["type"] == "session.joined" and e["actor"]["callsign"] == "cc-2"]
        assert joined, " ".join(feed.types())
        b_sid = joined[0]["actor"]["id"]
        assert wait_for(
            lambda: any(e["type"] == "guard.blocked" and e["actor"]["id"] == b_sid for e in feed.events()), timeout=20
        ), " ".join(feed.types())
        assert wait_for(
            lambda: any(e["type"] == "session.left" and e["actor"]["id"] == b_sid for e in feed.events()), timeout=30
        ), " ".join(feed.types())
    finally:
        mock.close()
        w.close()
