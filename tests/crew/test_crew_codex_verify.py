"""``remembra-crew verify --agent codex`` with the real gate and real Codex 0.157.1 payloads.

The fixtures under ``fixtures/captures/codex-0.157.1/real/verify_deny`` were captured by the gate's verify
mode from a live ``codex exec`` round trip (gpt-6-sol, code mode). They pin what Codex actually sends: its
nested calls reach PreToolUse as ``Bash`` (``command``), ``apply_patch`` (patch text in ``command``) and
``mcp__*`` tools. ``<SCRATCH>`` and ``<HOME>`` stand for the scratch repo and the home directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.crew import verify
from remembra.relay.crew.gate import Layout, apply_patch_paths, vendor_gate
from tests.crew.wp10_support import make_crew_env

FIXTURES = Path(__file__).parent / "fixtures" / "captures" / "codex-0.157.1" / "real" / "verify_deny"


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    return make_crew_env(tmp_path, monkeypatch)


def _fixture(name: str, armed: verify.Armed, home: Path) -> dict[str, Any]:
    text = (FIXTURES / name).read_text().replace("<SCRATCH>", armed.scratch_root).replace("<HOME>", str(home))
    return json.loads(text)


def _gate(layout: Layout, sub: str, payload: dict[str, Any], cwd: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "PYTHON"))}
    env["HOME"] = str(layout.home)
    return subprocess.run(
        [sys.executable, "-I", str(layout.gate_script), sub, "--hook", "codex"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )


def _decision(stdout: str) -> str:
    if not stdout.strip():
        return "allow"
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


def test_apply_patch_paths_reads_every_file_the_patch_writes():
    patch = (
        "*** Begin Patch\n*** Add File: a/new.ts\n+x\n*** Update File: /abs/b.ts\n*** Move to: c/moved.ts\n@@\n-1\n+2\n"
        "*** Delete File: d/gone.ts\n*** Update File: a/new.ts\n*** End Patch"
    )
    assert apply_patch_paths({"command": patch}) == ["a/new.ts", "/abs/b.ts", "c/moved.ts", "d/gone.ts"]
    assert apply_patch_paths(patch) == ["a/new.ts", "/abs/b.ts", "c/moved.ts", "d/gone.ts"]
    assert apply_patch_paths({"input": "*** Update File: x.py\n"}) == ["x.py"]
    assert apply_patch_paths({"command": "echo hi"}) == []
    assert apply_patch_paths(None) == []


def test_real_codex_payloads_are_captured_and_its_writes_denied(crew_env):
    home = crew_env["home"]
    layout = Layout(home)
    vendor_gate(layout)
    armed = verify.prepare(home, "codex", ttl_s=300)

    expected = {"002-PreToolUse.json": "allow", "004-PreToolUse.json": "deny", "007-PreToolUse.json": "deny"}
    for name, want in expected.items():
        payload = _fixture(name, armed, home)["payload"]
        res = _gate(layout, "pretool", payload, armed.scratch_root)
        assert res.returncode == 0, res.stderr
        assert _decision(res.stdout) == want, (name, res.stdout, res.stderr)
        if want == "deny":
            reason = json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            assert 'zone "scratch"' in reason
    # an `ls` like the one the verify prompt asks for: a read-only shell command, allowed
    ls_payload = {**_fixture("002-PreToolUse.json", armed, home)["payload"], "tool_input": {"command": "ls"}}
    assert _decision(_gate(layout, "pretool", ls_payload, armed.scratch_root).stdout) == "allow"

    captures = verify.load_captures(home, "codex", armed.nonce)
    assert [c["payload"]["tool_name"] for c in captures] == ["Bash", "apply_patch", "Bash", "Bash"]
    assert [c["decision"] for c in captures] == ["allow", "deny", "deny", "allow"]
    checks = {c.name: c for c in verify.evaluate(home, armed)}
    assert all(c.ok for c in checks.values()), [(c.name, c.detail) for c in checks.values() if not c.ok]


def test_verify_capture_ignores_hooks_outside_the_scratch_repo(crew_env, tmp_path):
    home = crew_env["home"]
    layout = Layout(home)
    vendor_gate(layout)
    armed = verify.prepare(home, "codex", ttl_s=300)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = {**_fixture("007-PreToolUse.json", armed, home)["payload"], "cwd": str(elsewhere)}
    res = _gate(layout, "pretool", payload, str(elsewhere))
    assert res.returncode == 0 and res.stdout == ""  # not a crew session, not the scratch repo: the fast exit
    assert verify.load_captures(home, "codex", armed.nonce) == []


def test_verify_capture_stops_when_the_arming_expires(crew_env):
    home = crew_env["home"]
    layout = Layout(home)
    vendor_gate(layout)
    armed = verify.prepare(home, "codex", ttl_s=300)
    path = verify.captures_dir(home, "codex") / "armed.json"
    data = json.loads(path.read_text())
    data["expires_at"] = 0
    path.write_text(json.dumps(data))
    res = _gate(layout, "pretool", _fixture("007-PreToolUse.json", armed, home)["payload"], armed.scratch_root)
    assert res.stdout == ""
    assert verify.load_captures(home, "codex", armed.nonce) == []
