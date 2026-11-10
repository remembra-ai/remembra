"""F36 (review): activity that only the desktop app's request records carry, and work done after
the agent's own handoff.

- The desktop app can leave a ``tool_use`` only inside an ``api-request-blob`` record (a session
  that was relocated or resumed keeps its earlier messages nowhere else). Those calls were not
  counted, so such a session was still dropped as "nothing to hand off".
- One successful ``close_session`` anywhere in the transcript suppressed the relay's note for the
  whole session, including everything the agent did after it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from remembra.relay import facts as factlib
from tests.relay_fixtures import Transcript
from tests.test_fs_activity_handoff import (  # noqa: F401  (env is a fixture)
    SECRET_PROMPT,
    Desktop,
    _hook_close,
    _rollout,
    env,
)

CLOSED = '{"status": "ok", "handoff_id": "84cde101"}'


def _blob_only(t: Desktop, name: str, n: int) -> None:
    """A tool call and its result that exist only in ``api-request-blob`` records."""
    use_id = f"toolu_blob_{n:04d}"
    use = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": use_id, "name": name, "input": {"url": "https://example.test/x"}}],
    }
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use_id, "content": "ok"}]}
    for i, message in enumerate((use, result)):
        t.lines.append({"type": "api-request-blob", "hash": f"b{n}-{i}", "sessionId": t.session_id, "message": message})


def test_calls_that_only_the_request_records_carry_are_counted_once(tmp_path):
    t = Desktop("s-blob", tmp_path)
    for n in range(7):
        _blob_only(t, "mcp__Claude_Browser__navigate", n)
    _blob_only(t, "mcp__Claude_Browser__navigate", 3)  # the same request record again
    t.tool("WebSearch", {"query": "q"}, "results")  # on an assistant line and repeated in a blob
    facts = factlib.parse_claude_transcript(t.write(tmp_path / "t.jsonl"))
    assert facts.tool_calls == {"navigate": 7, "WebSearch": 1}
    assert facts.prompts == 1  # a blob's user record is a tool result, never a prompt


def test_a_sub_agents_calls_are_still_not_counted(tmp_path):
    t = Desktop("s-side", tmp_path)
    t.browse(3)
    for _ in range(2):  # a sub-agent's call and result: on the session's lines, marked as a side chain
        Transcript.tool(t, "WebSearch", {"query": "q"}, "results")
        t.lines[-1]["isSidechain"] = t.lines[-2]["isSidechain"] = True
    t.browse(4)
    assert factlib.parse_claude_transcript(t.write(tmp_path / "t.jsonl")).tool_calls == {"navigate": 7}


def test_a_session_whose_calls_are_all_in_request_records_is_handed_off(env, monkeypatch, capsys):  # noqa: F811
    t = Desktop("s-relocated", env["folder"])
    for n in range(9):
        _blob_only(t, "mcp__Claude_Browser__navigate", n)
    log = _hook_close(env, monkeypatch, capsys, t.write(env["tmp"] / "t.jsonl"), "s-relocated", env["folder"])
    assert "nothing to hand off" not in log
    (body,) = env["http"].closes()
    assert "1 prompt, 9 tool calls" in body["facts"]["notes"] and "navigate 9" in body["facts"]["notes"]
    assert "example.test" not in json.dumps(body) and SECRET_PROMPT not in json.dumps(body)


def test_work_after_the_agents_own_handoff_is_handed_off(env, monkeypatch, capsys):  # noqa: F811
    t = Desktop("s-after", env["folder"])
    t.browse(30)
    t.tool("mcp__remembra__close_session", {"summary": "listings cleaned"}, CLOSED)
    t.prompt("one more directory")
    t.browse(12)
    parsed = factlib.parse_claude_transcript(t.write(env["tmp"] / "t.jsonl"))
    assert parsed.agent_handoff is True and parsed.calls_after_handoff == 12
    log = _hook_close(env, monkeypatch, capsys, env["tmp"] / "t.jsonl", "s-after", env["folder"])
    assert "nothing to hand off" not in log
    (body,) = env["http"].closes()
    notes = body["facts"]["notes"]
    assert "2 prompts, 42 tool calls" in notes and "navigate 42" in notes
    assert "The agent stored its own handoff during the session (close_session); 12 of these tool calls came after it." in notes


def test_a_handoff_that_ends_the_session_still_needs_no_second_one(tmp_path):
    """close_session last, or followed by less than a note's worth of work: the agent's handoff stands alone."""
    last = Desktop("s-last", tmp_path)
    last.browse(30)
    last.tool("mcp__remembra__close_session", {"summary": "done"}, CLOSED)
    parsed = factlib.parse_claude_transcript(last.write(tmp_path / "a.jsonl"))
    assert parsed.agent_handoff is True and parsed.calls_after_handoff == 0
    assert factlib.activity_note(parsed) is None

    few = Desktop("s-few", tmp_path)
    few.browse(30)
    few.tool("mcp__remembra__close_session", {"summary": "done"}, CLOSED)
    few.browse(2)
    few.tool("mcp__remembra__get_inbox", {}, "[]")  # relay bookkeeping is not work
    assert factlib.activity_note(factlib.parse_claude_transcript(few.write(tmp_path / "b.jsonl"))) is None

    twice = Desktop("s-twice", tmp_path)
    twice.browse(5)
    twice.tool("mcp__remembra__close_session", {"summary": "first"}, CLOSED)
    twice.browse(8)
    twice.tool("mcp__remembra__close_session", {"summary": "second"}, CLOSED)
    twice.browse(1)
    parsed = factlib.parse_claude_transcript(twice.write(tmp_path / "c.jsonl"))
    assert parsed.calls_after_handoff == 1 and factlib.activity_note(parsed) is None

    refused = Desktop("s-refused", tmp_path)
    refused.browse(5)
    refused.tool("mcp__remembra__close_session", {"summary": "x"}, "permission denied", is_error=True)
    parsed = factlib.parse_claude_transcript(refused.write(tmp_path / "d.jsonl"))
    note = factlib.activity_note(parsed)
    assert parsed.agent_handoff is False and note and "stored its own handoff" not in note


def test_codex_work_after_its_own_handoff_is_handed_off(tmp_path):
    rollout = _rollout(tmp_path / "rollout-z.jsonl", tmp_path, calls=4)
    lines = [json.loads(line) for line in rollout.read_text().splitlines()]

    def call(name: str, call_id: str, minute: int) -> list[dict[str, Any]]:
        stamp = f"2026-11-07T22:{minute:02d}:30.000Z"
        asked = {"type": "function_call", "name": name, "call_id": call_id, "arguments": "{}"}
        answered = {"type": "function_call_output", "call_id": call_id, "output": "{}"}
        return [{"timestamp": stamp, "type": "response_item", "payload": payload} for payload in (asked, answered)]

    lines += call("mcp__remembra__close_session", "close_1", 40)
    assert _parse(tmp_path, lines).calls_after_handoff == 0
    assert factlib.activity_note(_parse(tmp_path, lines)) is None
    for n in range(5):
        lines += call("mcp__shopify__get-product", f"late_{n}", 41 + n)
    parsed = _parse(tmp_path, lines)
    assert parsed.agent_handoff is True and parsed.calls_after_handoff == 5
    note = factlib.activity_note(parsed)
    assert note and "9 tool calls" in note and "5 of these tool calls came after it" in note


def _parse(tmp_path: Path, lines: list[dict[str, Any]]) -> factlib.TranscriptFacts:
    path = tmp_path / "rollout-edit.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return factlib.parse_codex_rollout(path)
