"""F47 (review): what a session reads after a compaction works without the MCP tools and costs less.

The pointer that replaced the second copy of the brief named only the ``session_brief`` MCP tool, and not
its compact form: an agent that obeyed got the larger default result, and a relay install with hooks and
an API key but no Remembra MCP server was told to call a tool it does not have. Both the relay pointer and
the crew reminder now say ``session_brief(compact=true)`` and name ``remembra-relay brief`` for a session
without the tools. A compaction start also returned before queued handoffs were replayed; with handoffs
waiting in the queue it now runs the whole start, as it did before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from remembra.crew import schemas as S
from remembra.relay import cli, outbox
from remembra.relay.crew import cli as crew_cli
from tests.agent_live_harness import RecordingProxy
from tests.test_relay_adapter_fixtures import _repo, home  # noqa: F401  (home is a fixture)
from tests.test_relay_cli_e2e import relay, server  # noqa: F401  (server is a fixture)

HOOK = ("brief", "--hook", "claude-code", "--agent", "claude-code")


def _start(repo: Path, sid: str, source: str) -> str:
    payload: dict[str, Any] = {"session_id": sid, "cwd": str(repo), "hook_event_name": "SessionStart", "source": source}
    return json.dumps(payload)


def test_the_pointer_names_the_compact_tool_call_and_the_command_for_a_session_without_the_tools(server, home, tmp_path):  # noqa: F811
    repo = _repo(tmp_path)
    with RecordingProxy(server) as proxy:
        first = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "startup"))
        assert first.stdout.startswith("# Remembra brief"), first.stderr
        out = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "compact"))
        assert out.returncode == 0 and out.stderr == "" and len(proxy.briefs()) == 1
        pointer = out.stdout.rstrip("\n")
        assert pointer == cli.COMPACT_POINTER and len(pointer) <= 400
        assert "session_brief(compact=true)" in pointer and "`remembra-relay brief`" in pointer
        assert "close_session" in pointer and "`remembra-relay close`" in pointer

        # The command it names is one this install has: run by hand it prints the brief.
        by_hand = relay(home, proxy.url, "brief", "--agent", "claude-code", "--cwd", str(repo))
        assert by_hand.returncode == 0 and by_hand.stdout.startswith("# Remembra brief"), by_hand.stderr


def test_a_compaction_with_handoffs_waiting_in_the_queue_runs_the_whole_start(server, home, tmp_path):  # noqa: F811
    repo = _repo(tmp_path)
    with RecordingProxy(server) as proxy:
        assert relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "startup")).stdout.startswith("# Remembra brief")
        queued = {"agent_id": "claude-code", "session_id": "earlier", "project_id": "p", "facts": {"branch": "main"}}
        assert outbox.enqueue(home, queued, url=proxy.url, config_source="env", error="timeout")
        assert [e.session_id for e in outbox.pending(home)] == ["earlier"]

        out = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "compact"))
        assert out.returncode == 0 and out.stderr == "", out.stderr
        said, brief = out.stdout.split("\n", 1)
        assert said == "Remembra: sent 1 queued handoff from claude-code." and brief.startswith("# Remembra brief")
        assert [c["session_id"] for c in proxy.closes()] == ["earlier"]  # the queued handoff was sent
        assert outbox.pending(home) == [] and len(proxy.briefs()) == 2

        # With the queue empty again, the next compaction is back to the pointer and asks nothing.
        nxt = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "compact"))
        assert nxt.stdout.rstrip("\n") == cli.COMPACT_POINTER and len(proxy.briefs()) == 2 and len(proxy.closes()) == 1


def test_an_unreadable_queue_does_not_stop_the_pointer(home, monkeypatch):  # noqa: F811
    def broken(_home: Path) -> list[Any]:
        raise OSError("queue directory unreadable")

    monkeypatch.setattr(outbox, "pending", broken)
    assert cli._handoffs_queued(home) is False
    monkeypatch.undo()
    assert cli._handoffs_queued(home) is False  # no queue directory at all


def test_the_crew_reminder_names_the_same_two_ways_inside_its_cap():
    text = crew_cli.compact_reminder({"callsign": "c" * 200, "crew_id": "crw_" + "f" * 200, "observe_only": True})
    assert len(text) <= crew_cli.START_COMPACT_CAP
    assert "session_brief(compact=true)" in text and "`remembra-relay brief`" in text and "crew_status" in text
    assert text.endswith("it cannot claim zones.")  # the last line is whole, not cut at the cap
    assert S.check_agent_text(text, "session_start") == []
    bare = crew_cli.compact_reminder({})
    assert "session_brief(compact=true)" in bare and "`remembra-relay brief`" in bare and "None" not in bare
