"""F47: ``remembra-relay brief`` after a compaction points at the brief instead of fetching it again.

Claude Code runs its SessionStart hook with source ``compact`` after every compaction. The relay hook
fetched and printed the whole brief each time (one request to the server and up to 6,000 characters per
compaction). A session that already got its brief now gets two lines: where to read it again, and the
closing reminder. A session that never got one (no marker) still gets the whole brief.

The real CLI runs as a subprocess against the local test server, through a proxy that counts requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from remembra.relay import cli
from tests.agent_live_harness import RecordingProxy
from tests.test_relay_adapter_fixtures import _repo, home  # noqa: F401  (home is a fixture)
from tests.test_relay_cli_e2e import relay, server  # noqa: F401  (server is a fixture)

HOOK = ("brief", "--hook", "claude-code", "--agent", "claude-code")


def _start(repo: Path, sid: str, source: str) -> str:
    payload: dict[str, Any] = {"session_id": sid, "cwd": str(repo), "hook_event_name": "SessionStart", "source": source}
    return json.dumps(payload)


def test_a_compacted_session_gets_a_pointer_not_a_second_brief(server, home, tmp_path):  # noqa: F811
    repo = _repo(tmp_path)
    with RecordingProxy(server) as proxy:
        first = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "startup"))
        assert first.returncode == 0 and first.stdout.startswith("# Remembra brief"), first.stderr
        assert len(proxy.briefs()) == 1

        for _ in range(3):  # every later compaction of that session
            out = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", "compact"))
            assert out.returncode == 0 and out.stderr == "", out.stderr
            assert out.stdout.rstrip("\n") == cli.COMPACT_POINTER
        assert len(proxy.briefs()) == 1  # nothing was fetched for them
        assert len(cli.COMPACT_POINTER) <= 400
        assert "session_brief" in cli.COMPACT_POINTER and "close_session" in cli.COMPACT_POINTER
        assert cli.brief_delivered(home, "claude-code", "s-1")  # the marker is the first brief's, untouched

        # The pointer in the other output modes a hook can ask for.
        as_hook = relay(home, proxy.url, *HOOK, "--format", "hook-json", stdin=_start(repo, "s-1", "compact"))
        body = json.loads(as_hook.stdout)["hookSpecificOutput"]
        assert body == {"hookEventName": "SessionStart", "additionalContext": cli.COMPACT_POINTER}
        as_json = json.loads(relay(home, proxy.url, *HOOK, "--format", "json", stdin=_start(repo, "s-1", "compact")).stdout)
        assert as_json == {"rendered": cli.COMPACT_POINTER, "compacted": True}
        assert len(proxy.briefs()) == 1

        # A resume and a new start of the same session still fetch the brief, as before.
        for source in ("resume", "startup"):
            again = relay(home, proxy.url, *HOOK, stdin=_start(repo, "s-1", source))
            assert again.stdout.startswith("# Remembra brief"), (source, again.stdout, again.stderr)
        assert len(proxy.briefs()) == 3


def test_a_compaction_of_a_session_that_never_got_its_brief_prints_the_brief(server, home, tmp_path):  # noqa: F811
    """No marker (the start hook failed or was added mid-session, or the marker expired): nothing to point at."""
    repo = _repo(tmp_path)
    with RecordingProxy(server) as proxy:
        out = relay(home, proxy.url, *HOOK, stdin=_start(repo, "never-briefed", "compact"))
        assert out.returncode == 0 and out.stdout.startswith("# Remembra brief"), out.stderr
        assert len(proxy.briefs()) == 1
        # ...and from then on that session has it, so its next compaction gets the pointer.
        nxt = relay(home, proxy.url, *HOOK, stdin=_start(repo, "never-briefed", "compact"))
        assert nxt.stdout.rstrip("\n") == cli.COMPACT_POINTER
        # A payload without a session id cannot be matched to a marker: the brief is printed.
        anon = json.dumps({"cwd": str(repo), "hook_event_name": "SessionStart", "source": "compact"})
        assert relay(home, proxy.url, *HOOK, stdin=anon).stdout.startswith("# Remembra brief")
        assert len(proxy.briefs()) == 2
