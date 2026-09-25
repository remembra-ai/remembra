"""Spike S0 contract tests.

Three layers:

1. The recorded fixtures under ``claude-code-2.1.168/`` are real stdin payloads
   captured from the installed Claude Code 2.1.168. These tests pin the field
   shapes WP-3/WP-9/WP-10 build their PayloadMap against, and pin the verdicts
   that ``docs/crew/S0-results.md`` reports, so the doc cannot drift from the
   evidence.
2. The capture hook and the mock Messages API are exercised for real
   (subprocess and HTTP), without needing Claude Code.
3. ``REMEMBRA_S0_LIVE=1`` re-runs the core scenarios against the installed
   ``claude`` binary (mock API, dummy key, temp config dir; costs nothing).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "claude-code-2.1.168"
HOOK = HERE / "s0_hook.py"

_spec = importlib.util.spec_from_file_location("s0_harness", HERE / "s0_harness.py")
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
sys.modules["s0_harness"] = harness
_spec.loader.exec_module(harness)

STOP_FAILURE_ERRORS = {
    "authentication_failed",
    "oauth_org_not_allowed",
    "billing_error",
    "rate_limit",
    "overloaded",
    "invalid_request",
    "model_not_found",
    "server_error",
    "unknown",
    "max_output_tokens",
}
COMMON = {"session_id", "transcript_path", "cwd", "hook_event_name"}


def _captures() -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(FIXTURES.glob("*/*/[0-9][0-9]-*.json")):
        out.append((p, json.loads(p.read_text())))
    return out


def _summary(mode: str, scenario: str) -> dict:
    return json.loads((FIXTURES / mode / scenario / "summary.json").read_text())


# ---------------------------------------------------------------------------
# 1. Recorded fixtures
# ---------------------------------------------------------------------------


def test_fixture_set_is_present():
    caps = _captures()
    assert len(caps) >= 40
    events = {c["payload"]["hook_event_name"] for _, c in caps}
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "StopFailure", "SessionEnd"} <= events


@pytest.mark.parametrize("path,cap", _captures(), ids=lambda x: x.parent.name + "/" + x.name if isinstance(x, Path) else "")
def test_capture_payload_shape(path: Path, cap: dict):
    payload = cap["payload"]
    event = cap["event"]
    assert payload["hook_event_name"] == event
    assert payload.keys() >= COMMON, f"{path}: missing {COMMON - payload.keys()}"
    assert isinstance(payload["session_id"], str) and len(payload["session_id"]) >= 32
    # Sanitised: no absolute home or temp paths leak into committed fixtures.
    blob = json.dumps(cap)
    assert str(Path.home()) not in blob
    assert "sk-ant-" not in blob
    if event in ("PreToolUse", "PostToolUse"):
        assert {"tool_name", "tool_input", "tool_use_id", "permission_mode"} <= payload.keys()
        assert payload["tool_use_id"].startswith("toolu_")
        if payload["tool_name"] == "Write":
            assert {"file_path", "content"} <= payload["tool_input"].keys()
    if event == "PostToolUse":
        assert "tool_response" in payload
    if event == "SessionStart":
        assert payload["source"] in {"startup", "resume", "clear", "compact"}
    if event == "UserPromptSubmit":
        assert isinstance(payload["prompt"], str) and payload["prompt"]
    if event == "Stop":
        assert isinstance(payload["stop_hook_active"], bool)
    if event == "SessionEnd":
        assert isinstance(payload["reason"], str)
    if event == "StopFailure":
        assert payload["error"] in STOP_FAILURE_ERRORS
        assert "last_assistant_message" in payload


def test_verdict_pretooluse_deny_blocks_write_and_reason_reaches_model():
    s = _summary("mock", "deny")
    v = s["verdict"]
    assert v["file_written"] is False
    assert v["deny_reason_in_tool_result"] is True
    assert v["tool_result_is_error"] is True
    assert v["posttooluse_fired"] is False
    assert any("S0-DENY-" in json.dumps(t) and t.get("is_error") for t in s["tool_results_seen_by_model"])
    iv = _summary("interactive", "interactive_deny_rewake")["verdict"]
    assert iv["file_written"] is False and iv["deny_reason_reached_model"] is True


def test_verdict_additional_context_without_decision_is_injected():
    v = _summary("mock", "context")["verdict"]
    assert v["file_written"] is True, "additionalContext without a decision must not block the tool"
    assert v["sessionstart_ctx_in_first_request"] is True
    assert v["prompt_ctx_in_first_request"] is True
    assert v["pretool_ctx_in_later_request"] is True


def test_verdict_claude_env_file():
    assert _summary("mock", "envfile")["verdict"]["env_value_in_bash_output"] is True


def test_verdict_async_posttooluse_under_print_mode():
    # In-flight async hooks are dropped at -p exit and SessionEnd is skipped.
    for name in ("async_post", "async_post_fast"):
        v = _summary("mock", name)["verdict"]
        assert v["marker_after_wait"] is False
        assert v["sessionend_fired"] is False
    settled = _summary("mock", "async_post_settled")["verdict"]
    assert settled["marker_at_exit"] is True and settled["sessionend_fired"] is True
    inter = _summary("interactive", "interactive_async_post")["verdict"]
    assert inter["marker_at_exit"] is True and inter["sessionend_fired"] is True


def test_verdict_async_rewake():
    v = _summary("mock", "async_rewake")["verdict"]
    assert v["rewake_msg_reached_model"] is True
    assert v["request_gaps_s"][0] >= 2.5, "-p waits for an in-flight asyncRewake hook before the next model call"
    idle = _summary("mock", "async_rewake_idle")["verdict"]
    assert idle["rewake_msg_reached_model"] is True and idle["stop_count"] == 2
    iv = _summary("interactive", "interactive_deny_rewake")["verdict"]
    assert iv["rewake_msg_reached_model_while_idle"] is True


@pytest.mark.parametrize(
    "scenario,error",
    [
        ("stopfailure_billing", "billing_error"),
        ("stopfailure_ratelimit", "rate_limit"),
        ("stopfailure_overloaded", "server_error"),
        ("stopfailure_auth", "authentication_failed"),
        ("stopfailure_model", "model_not_found"),
        ("stopfailure_network", "unknown"),
    ],
)
def test_verdict_stopfailure_mapping(scenario: str, error: str):
    v = _summary("mock", scenario)["verdict"]
    assert v["stopfailure_fired"] is True
    assert v["stop_fired"] is False, "StopFailure fires instead of Stop"
    assert v["error"] == error
    assert v["returncode"] == 1


def test_verdict_stopfailure_real_api_and_slow_hook():
    real = _summary("real", "real_stopfailure_model")["verdict"]
    assert real["stopfailure_fired"] is True and real["error"] in STOP_FAILURE_ERRORS
    slow = _summary("mock", "stopfailure_slow_hook")["verdict"]
    assert slow["slow_hook_finished_before_exit"] is False
    assert slow["slow_hook_finished_after_wait"] is True, "a StopFailure hook outlives the -p process"


def test_results_doc_lists_every_scenario():
    doc = HERE.parents[3] / "docs" / "crew" / "S0-results.md"
    text = doc.read_text()
    for d in FIXTURES.glob("*/*"):
        if d.is_dir():
            assert f"`{d.name}`" in text, f"{d.name} is not reported in S0-results.md"


# ---------------------------------------------------------------------------
# 2. Capture hook and mock API, exercised for real
# ---------------------------------------------------------------------------


def _hook(tmp_path: Path, *args: str, stdin: str = '{"hook_event_name": "PreToolUse"}') -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), str(tmp_path), *args], input=stdin, capture_output=True, text=True, timeout=30
    )


def test_hook_record_is_silent_and_ordered(tmp_path: Path):
    for event in ("SessionStart", "PreToolUse", "SessionStart"):
        r = _hook(tmp_path, event, "record", stdin=json.dumps({"hook_event_name": event}))
        assert r.returncode == 0 and r.stdout == ""
    names = sorted(p.name for p in tmp_path.glob("*.json"))
    assert names == ["01-SessionStart.json", "02-PreToolUse.json", "03-SessionStart.json"]
    assert json.loads((tmp_path / "02-PreToolUse.json").read_text())["payload"] == {"hook_event_name": "PreToolUse"}


def test_hook_deny_and_ctx_outputs(tmp_path: Path):
    deny = json.loads(_hook(tmp_path, "PreToolUse", "deny", "no").stdout)
    assert deny == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "no"}
    }
    ctx = json.loads(_hook(tmp_path, "UserPromptSubmit", "ctx", "hello").stdout)
    assert ctx == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "hello"}}
    assert "permissionDecision" not in json.dumps(ctx)


def test_hook_rewake_once_and_envfile(tmp_path: Path):
    r = _hook(tmp_path, "Stop", "rewake_once", "0", "wake up")
    assert r.returncode == 2 and "wake up" in r.stderr
    again = _hook(tmp_path, "Stop", "rewake_once", "0", "wake up")
    assert again.returncode == 0 and again.stderr == ""
    env_file = tmp_path / "env.sh"
    subprocess.run(
        [sys.executable, str(HOOK), str(tmp_path), "SessionStart", "envfile", "K", "v1"],
        input="{}",
        text=True,
        env={**os.environ, "CLAUDE_ENV_FILE": str(env_file)},
        check=True,
        timeout=30,
    )
    assert env_file.read_text() == "export K=v1\n"


def test_hook_survives_garbage_stdin(tmp_path: Path):
    r = _hook(tmp_path, "PreToolUse", "record", stdin="not json")
    assert r.returncode == 0
    cap = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert cap["payload"] == {"_unparsed_stdin": "not json"}


def _post(url: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_mock_api_scripted_tool_then_text_and_errors():
    mock = harness.MockAnthropic()
    try:
        mock.reset(harness.Plan(tool={"name": "Write", "input": {"file_path": "/x", "content": "y"}}))
        url = mock.base_url + "/v1/messages"
        first = {"stream": True, "tools": [{"name": "Write"}], "messages": [{"role": "user", "content": "go"}]}
        status, body = _post(url, first)
        assert status == 200 and '"tool_use"' in body and "input_json_delta" in body and "message_stop" in body
        second = {
            "stream": False,
            "tools": [{"name": "Write"}],
            "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}],
        }
        status, body = _post(url, second)
        assert status == 200 and json.loads(body)["content"] == [{"type": "text", "text": "S0 ack"}]
        assert len(mock.requests) == 2
        mock.reset(harness.Plan(error="billing"))
        status, body = _post(url, first)
        assert status == 400 and "credit balance is too low" in body.lower()
        mock.reset(harness.Plan(error="ratelimit"))
        assert _post(url, first)[0] == 429
    finally:
        mock.close()


def test_sanitize_strips_paths_and_keys(tmp_path: Path):
    reps = harness._replacements(tmp_path)
    out = harness.sanitize({"a": [f"{tmp_path}/repo", f"{Path.home()}/x", "sk-ant-api03-abc_DEF-1"]}, reps)
    assert out == {"a": ["<S0_TMP>/repo", "<HOME>/x", "<API_KEY>"]}


# ---------------------------------------------------------------------------
# 3. Live re-run against the installed claude binary (opt-in)
# ---------------------------------------------------------------------------

LIVE = os.environ.get("REMEMBRA_S0_LIVE") == "1" and harness.find_claude() is not None


@pytest.mark.integration
@pytest.mark.skipif(not LIVE, reason="set REMEMBRA_S0_LIVE=1 with Claude Code installed")
@pytest.mark.parametrize(
    "name", ["deny", "context", "envfile", "async_rewake_idle", "stopfailure_billing", "stopfailure_ratelimit"]
)
def test_live_mock_scenarios(tmp_path: Path, name: str):
    mock = harness.MockAnthropic()
    try:
        res = harness.run(harness.scenarios()[name], tmp_path, mode="mock", mock=mock)
    finally:
        mock.close()
    v = harness.verdict(res)
    assert v["ok"], v
