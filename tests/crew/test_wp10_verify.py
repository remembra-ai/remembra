"""WP-10: ``remembra-crew verify`` round trip (spec §8.3 rule 2) and adapters.json."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import gatecore
from remembra.crew import schemas as S
from remembra.relay.crew import verify
from tests.crew.wp10_support import make_crew_env


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    return make_crew_env(tmp_path, monkeypatch)


CAPTURES = Path(__file__).parent / "fixtures" / "captures" / "claude-code-2.1.168" / "mock"


def _capture(home: Path, armed: verify.Armed, n: int, event: str, verb: str, payload: dict[str, Any], decision: str = "") -> None:
    """What the gate writes while armed (the contract documented in verify.py)."""
    path = verify.captures_dir(home, armed.agent) / f"{n:02d}-{event}.json"
    path.write_text(
        json.dumps(
            {
                "event": event,
                "verb": verb,
                "adapter": armed.agent,
                "nonce": armed.nonce,
                "payload": payload,
                "decision": decision,
                "received_at": time.time(),
            }
        )
    )


def _s0_payload(event_file: str, armed: verify.Armed, **tool_input: Any) -> dict[str, Any]:
    """A real Claude Code 2.1.168 payload from spike S0, moved into the scratch repo."""
    raw = json.loads((CAPTURES / event_file).read_text())["payload"]
    raw["cwd"] = armed.scratch_root
    raw["transcript_path"] = str(Path(armed.scratch_root) / "t.jsonl")
    if tool_input:
        raw["tool_input"] = tool_input
    return raw


def test_prepare_builds_a_valid_synthetic_snapshot_that_the_gate_denies(crew_env):
    home = crew_env["home"]
    armed = verify.prepare(home, "claude-code", ttl_s=300)
    root = Path(armed.scratch_root)
    assert not root.is_relative_to(home.resolve())  # outside the protected ~/.remembra (crew-policy)
    assert root.is_relative_to((crew_env["tmp"] / "tmpdir").resolve())
    assert (root / ".remembra" / "zones.yml").is_file()  # the gate's crew-repo fast check passes
    assert armed.scratch_path.read_text().startswith("remembra-crew verify ")
    capdir = verify.captures_dir(home, "claude-code")
    assert (capdir / "armed.json").stat().st_mode & 0o777 == 0o600
    assert (verify.crew_home(home)).stat().st_mode & 0o777 == 0o700

    snapshot = json.loads((capdir / "snapshot.json").read_text())
    assert S.validate(snapshot, S.LOCAL_SNAPSHOT) == []
    assert S.verify_snapshot_hmac(bytes.fromhex(armed.hmac_key), snapshot)

    write = _s0_payload("deny/03-PreToolUse.json", armed, file_path=str(armed.scratch_path), content="edited\n")
    verdict = gatecore.evaluate_hook_payload(
        write, snapshot=snapshot, caller=armed.probe_session, home=str(home), now=snapshot["server_time"]
    )
    assert verdict.decision == "deny" and verdict.rule == 9
    assert S.validate_hook_stdout("PreToolUse", verdict.hook_stdout()) == []

    ls = dict(write, tool_name="Bash", tool_input={"command": "ls"})
    assert gatecore.evaluate_hook_payload(ls, snapshot=snapshot, caller=armed.probe_session, home=str(home)).decision == "allow"


def test_round_trip_passes_and_switches_the_adapter_to_enforce(crew_env):
    home = crew_env["home"]
    armed = verify.prepare(home, "codex", ttl_s=300)
    assert verify.adapter_enforcement(home, "codex") == "advisory"
    _capture(
        home,
        armed,
        1,
        "SessionStart",
        "start",
        {"session_id": "sess-1", "cwd": armed.scratch_root, "hook_event_name": "SessionStart"},
    )
    # Codex writes through its shell tool; the pre-tool hook sees the command and the gate denies it.
    _capture(
        home,
        armed,
        2,
        "PreToolUse",
        "pretool",
        {
            "session_id": "sess-1",
            "cwd": armed.scratch_root,
            "tool_name": "Bash",
            "tool_input": {"command": "printf edited > held/scratch.txt"},
        },
        "deny",
    )
    _capture(
        home,
        armed,
        3,
        "PreToolUse",
        "pretool",
        {"session_id": "sess-1", "cwd": armed.scratch_root, "tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    out = io.StringIO()
    assert verify.main(["--agent", "codex", "--check"], home=home, out=out) == 0, out.getvalue()
    assert "Verified" in out.getvalue()
    entry = verify.read_adapters(home)["adapters"]["codex"]
    assert entry["verified_local"] is True and entry["enforcement"] == "enforce"
    assert verify.adapter_enforcement(home, "codex") == "enforced"
    assert verify.load_armed(home, "codex") is None  # disarmed


def test_claude_round_trip_with_real_s0_payload_shapes(crew_env):
    home = crew_env["home"]
    armed = verify.prepare(home, "claude-code")
    _capture(home, armed, 1, "SessionStart", "start", _s0_payload("deny/01-SessionStart.json", armed))
    _capture(
        home,
        armed,
        2,
        "PreToolUse",
        "pretool",
        _s0_payload("deny/03-PreToolUse.json", armed, file_path=str(armed.scratch_path), content="edited"),
        "deny",
    )
    _capture(home, armed, 3, "PreToolUse", "pretool", _s0_payload("deny/03-PreToolUse.json", armed, command="ls"))
    checks = verify.evaluate(home, armed)
    assert all(c.ok for c in checks), checks


@pytest.mark.parametrize(
    "mutation, failing",
    [
        ("edit_file", "file_unchanged"),
        ("no_ls", "pre_shell"),
        ("wrong_nonce", "captures"),
        ("no_session", "payload_map"),
        ("outside_cwd", "payload_map"),
    ],
)
def test_round_trip_fails_closed(crew_env, mutation, failing):
    home = crew_env["home"]
    armed = verify.prepare(home, "gemini")
    base = {"session_id": "g-1", "cwd": armed.scratch_root}
    if mutation == "no_session":
        base = {"cwd": armed.scratch_root}
    if mutation == "outside_cwd":
        base = {"session_id": "g-1", "cwd": str(crew_env["tmp"])}
    write = {**base, "tool_name": "write_file", "tool_input": {"file_path": "held/scratch.txt", "content": "edited"}}
    _capture(home, armed, 1, "BeforeTool", "pretool", write, "deny")
    if mutation != "no_ls":
        _capture(
            home,
            armed,
            2,
            "BeforeTool",
            "pretool",
            {**base, "tool_name": "run_shell_command", "tool_input": {"command": "ls -la"}},
        )
    if mutation == "edit_file":
        armed.scratch_path.write_text("edited\n")
    if mutation == "wrong_nonce":
        for p in verify.captures_dir(home, "gemini").glob("0*.json"):
            data = json.loads(p.read_text())
            data["nonce"] = "someone-else"
            p.write_text(json.dumps(data))
    checks = {c.name: c.ok for c in verify.evaluate(home, armed)}
    assert checks[failing] is False
    out = io.StringIO()
    assert verify.main(["--agent", "gemini", "--check"], home=home, out=out) == 1
    assert "stays in observe" in out.getvalue()
    assert verify.adapter_enforcement(home, "gemini") == "advisory"
    old_root = armed.scratch_root
    assert verify.prepare(home, "gemini").scratch_root != old_root
    assert not Path(old_root).exists()  # re-arming cleans the previous scratch repo
    with pytest.raises(ValueError):
        verify.record_verified(home, armed, verify.evaluate(home, armed))


def test_cursor_cannot_pass_with_a_file_tool_edit(crew_env):
    """Cursor has no pre-write file hook: an edit is only seen after the fact, so it stays advisory."""
    home = crew_env["home"]
    armed = verify.prepare(home, "cursor")
    base = {"conversation_id": "c-1", "workspace_roots": [armed.scratch_root]}
    _capture(home, armed, 1, "beforeShellExecution", "pretool", {**base, "command": "ls"})
    armed.scratch_path.write_text("edited\n")
    _capture(home, armed, 2, "afterFileEdit", "posttool", {**base, "file_path": str(armed.scratch_path)})
    checks = {c.name: c.ok for c in verify.evaluate(home, armed)}
    assert checks == {"captures": True, "payload_map": True, "pre_write": False, "pre_shell": True, "file_unchanged": False}


def test_waits_for_payloads_then_times_out(crew_env):
    home = crew_env["home"]
    out = io.StringIO()
    t0 = time.monotonic()
    assert verify.main(["--agent", "qwen", "--timeout", "1", "--poll", "0.2"], home=home, out=out) == 1
    assert time.monotonic() - t0 < 5
    text = out.getvalue()
    assert verify.VERIFY_PROMPT in text and "Not verified" in text
    assert "not recorded as installed" in text


def test_check_without_arming_and_disarm(crew_env):
    home = crew_env["home"]
    out = io.StringIO()
    assert verify.main(["--agent", "kimi", "--check"], home=home, out=out) == 1
    verify.prepare(home, "kimi")
    assert verify.main(["--agent", "kimi", "--disarm"], home=home, out=out) == 0
    assert verify.load_armed(home, "kimi") is None
    assert verify.main(["--agent", "nope"], home=home, out=out) == 2


def test_render_adapters_merges_and_is_stable():
    first = verify.render_adapters(None, {"codex": {"installed": True, "enforcement": "observe"}})
    again = verify.render_adapters(first, {"codex": {"installed": True, "enforcement": "observe"}})
    assert again == first
    merged = json.loads(verify.render_adapters(first, {"codex": {"verified_local": True}, "kimi": {"installed": True}}))
    assert merged["adapters"]["codex"] == {"installed": True, "enforcement": "observe", "verified_local": True}
    removed = json.loads(verify.render_adapters(first, {"codex": None}))
    assert removed["adapters"] == {}
    assert verify.render_adapters("not json", {"a": {"x": 1}}).startswith("{")
