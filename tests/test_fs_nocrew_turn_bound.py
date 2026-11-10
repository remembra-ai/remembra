"""F34 (review): the per-prompt message check asks the server inside the prompt hook, so the time
it may take is bounded, short, and stated in the guide (which used to say "never held up")."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import httpx

from remembra.relay import cli
from tests.test_fs_activity_handoff import env  # noqa: F401  (a fixture)


def test_a_server_that_does_not_answer_holds_a_prompt_up_for_two_seconds_at_most(env, monkeypatch, capsys):  # noqa: F811
    asked: list[str] = []

    def stall(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        time.sleep(8)
        return httpx.Response(200, json={"project_id": "notes", "messages": []})

    monkeypatch.setattr(
        cli.Context,
        "client",
        lambda self, config=None, agent=None, budget=None: httpx.Client(
            base_url="http://relay.test", transport=httpx.MockTransport(stall)
        ),
    )
    payload = {"session_id": "s-stall", "cwd": str(env["folder"]), "hook_event_name": "UserPromptSubmit", "prompt": "next"}
    # the session was briefed earlier (its marker is old), so this prompt asks the server
    marker = cli._brief_marker_path(env["home"], "claude-code", "s-stall")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    then = time.time() - 120
    os.utime(marker, (then, then))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    began = time.monotonic()
    assert cli.main(["brief", "--hook", "claude-code", "--agent", "claude-code", "--once"]) == 0
    took = time.monotonic() - began
    out = capsys.readouterr().out
    assert asked, "the check did not reach the server"
    assert out == ""
    assert took < cli.TURN_BUDGET_SECONDS + 0.6 and cli.TURN_BUDGET_SECONDS <= 2.0
    # and it is not asked again on the next prompt (one check per interval)
    asked.clear()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    began = time.monotonic()
    assert cli.main(["brief", "--hook", "claude-code", "--agent", "claude-code", "--once"]) == 0
    assert asked == [] and time.monotonic() - began < 0.5


def test_the_guide_states_the_bound_of_the_message_check():
    root = Path(__file__).resolve().parents[1]
    for doc in ("docs/guides/relay.md", "src/remembra/marshal/pack/relay.md"):
        text = " ".join((root / doc).read_text().split())
        assert "the prompt is never held up" not in text
        assert f"at most about {cli.TURN_BUDGET_SECONDS:g} seconds" in text
