"""F33: a queued handoff the server refused (403) is not re-sent by every brief and close.

One close written as an agent the key may not write as was queued, then POSTed again by every later brief
and close (twice per close) for three days, and each session start said "the server refused this key"
although the session's own key worked. A refusal is now held: never re-sent on its own when the key is
scoped to another agent, re-tried on a backoff otherwise; the notice names the queued handoff, not the
reader's key; and ``remembra-relay outbox list | drop | send`` gives the owner a way out.

The real CLI in a subprocess against a local stub server, an isolated HOME per test.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from remembra.relay import outbox
from tests.test_relay_stop_outbox import _payload, relay

AGENT_REFUSAL = "This API key is scoped to agent 'claude-code'; the request claims '{agent}'."
PROJECT_REFUSAL = "No access to project 'p'."


class _Server(http.server.BaseHTTPRequestHandler):
    """A key scoped to claude-code. ``refuse_projects`` also refuses claude-code's own closes (a project refusal);
    ``refuse_briefs`` refuses the brief itself."""

    posts: list[str] = []
    refuse_projects = False
    refuse_briefs = False

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        agent = str(body.get("agent_id"))
        type(self).posts.append(f"{agent}:{body.get('session_id')}")
        if agent != "claude-code":
            self._send(403, {"detail": AGENT_REFUSAL.format(agent=agent)})
        elif type(self).refuse_projects:
            self._send(403, {"detail": PROJECT_REFUSAL})
        else:
            self._send(200, {"ok": True, "handoff_id": "h1", "project_id": "p", "headline": "done"})

    def do_GET(self) -> None:
        if type(self).refuse_briefs:
            self._send(403, {"detail": PROJECT_REFUSAL})
        else:
            self._send(200, {"rendered": "Remembra brief"})

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def scoped_server() -> Iterator[tuple[str, type[_Server]]]:
    handler = type("Handler", (_Server,), {"posts": [], "refuse_projects": False, "refuse_briefs": False})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", handler
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _log(home: Path) -> str:
    return outbox.log_path(home).read_text()


def _age_last_try(home: Path, seconds: float) -> None:
    """Make every queued entry look as if its last try was ``seconds`` ago."""
    for entry in outbox.pending(home):
        data = dict(entry.data)
        data["last_try_ts"] = time.time() - seconds
        entry.path.write_text(json.dumps(data))


def test_a_refused_handoff_of_another_agent_is_sent_once_and_does_not_blame_the_readers_key(home):
    with scoped_server() as (url, handler):
        close = relay(home, url, "close", "--agent", "codex-task", "--session-id", "S", "--summary", "x", "--cwd", str(home))
        assert close.returncode == 0 and "HTTP 403" in close.stderr
        assert "will be sent by the next brief or close" not in close.stderr
        assert "outbox drop" in close.stderr  # the way out is named when it is queued

        briefs = [relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home)) for _ in range(3)]
        own = relay(home, url, "close", "--agent", "claude-code", "--session-id", "A", "--summary", "y", "--cwd", str(home))
        assert own.returncode == 0, own.stderr

        assert handler.posts.count("codex-task:S") == 1, handler.posts  # the close itself, never again
        assert handler.posts.count("claude-code:A") == 1
        for brief in briefs:
            assert brief.returncode == 0 and "Remembra brief" in brief.stdout
            assert "refused this key" not in brief.stdout
            assert "the next brief or close retries" not in brief.stdout
            assert "1 from codex-task" in brief.stdout and "held" in brief.stdout
            assert "not this session's key" in brief.stdout and "outbox drop" in brief.stdout
        assert _log(home).count("outbox: refused") == 0  # nothing was re-sent, so nothing was refused again

        status = relay(home, url, "status", "--no-check")
        assert "codex-task session S" in status.stdout and "1 attempt(s)" in status.stdout
        assert "held: the server refused it" in status.stdout and "never sent on its own" in status.stdout

        listed = relay(home, url, "outbox", "list")
        assert listed.returncode == 0 and "codex-task" in listed.stdout and "HTTP 403" in listed.stdout
        as_json = json.loads(relay(home, url, "outbox", "list", "--format", "json").stdout)
        assert [(e["agent_id"], e["session_id"], e["held"] is not None) for e in as_json] == [("codex-task", "S", True)]

        dropped = relay(home, url, "outbox", "drop", "codex-task", "S")
        assert dropped.returncode == 0 and "dropped" in dropped.stdout
        assert outbox.pending(home) == []
        assert "outbox: dropped codex-task session S: by hand" in _log(home)
        # status and doctor no longer list the agent's queued failure
        assert "last_failure" not in outbox.read_status(home)["agents"]["codex-task"]
        after = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert "held" not in after.stdout and "codex-task" not in after.stdout


def test_a_queued_handoff_first_refused_on_replay_is_named_and_then_left_alone(home):
    """Queued on a timeout, refused when replayed: the notice is about that handoff, and it is not sent twice
    (not by the next brief, and not by the two replays of one close)."""
    with scoped_server() as (url, handler):
        assert outbox.enqueue(home, _payload("codex-task", "S2"), url=url, config_source="env", error="ReadTimeout")
        first = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert handler.posts == ["codex-task:S2"]
        assert "refused this key" not in first.stdout and "1 from codex-task" in first.stdout
        assert _log(home).count("outbox: refused codex-task session S2 (kept)") == 1
        relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        relay(home, url, "close", "--agent", "claude-code", "--session-id", "A", "--summary", "y", "--cwd", str(home))
        assert handler.posts.count("codex-task:S2") == 1
        assert _log(home).count("outbox: refused") == 1


def test_a_project_refusal_is_retried_on_a_backoff_and_delivered_once_access_is_given(home):
    with scoped_server() as (url, handler):
        handler.refuse_projects = True
        close = relay(home, url, "close", "--agent", "claude-code", "--session-id", "P", "--summary", "x", "--cwd", str(home))
        assert "HTTP 403" in close.stderr and "queued" in close.stderr
        for _ in range(2):
            brief = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
            assert "refused this key" not in brief.stdout and "1 from claude-code" in brief.stdout
        assert handler.posts == ["claude-code:P"]  # inside the backoff: not re-sent
        status = relay(home, url, "status", "--no-check")
        assert "held: the server refused it" in status.stdout and "tried again" in status.stdout

        _age_last_try(home, outbox.REFUSAL_BACKOFF_SECONDS[0] + 60)  # an hour later, still refused
        relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert handler.posts == ["claude-code:P", "claude-code:P"]
        [entry] = outbox.pending(home)
        assert entry.data["refusals"] == 2
        _age_last_try(home, outbox.REFUSAL_BACKOFF_SECONDS[0] + 60)  # the second wait is longer than the first
        relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert len(handler.posts) == 2

        handler.refuse_projects = False  # the owner gave the key access to the project
        _age_last_try(home, outbox.REFUSAL_BACKOFF_SECONDS[1] + 60)
        sent = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert "sent 1 queued handoff from claude-code" in sent.stdout
        assert outbox.pending(home) == [] and len(handler.posts) == 3


def test_outbox_send_sends_a_held_handoff_now(home):
    with scoped_server() as (url, handler):
        handler.refuse_projects = True
        relay(home, url, "close", "--agent", "claude-code", "--session-id", "P", "--summary", "x", "--cwd", str(home))
        refused = relay(home, url, "outbox", "send", "claude-code", "P")
        assert refused.returncode == 1 and "HTTP 403" in refused.stderr and len(handler.posts) == 2
        assert [e.session_id for e in outbox.pending(home)] == ["P"]  # kept
        handler.refuse_projects = False
        sent = relay(home, url, "outbox", "send", "claude-code", "P")
        assert sent.returncode == 0 and "sent" in sent.stdout, sent.stderr
        assert outbox.pending(home) == [] and len(handler.posts) == 3
        assert "outbox: delivered claude-code session P" in _log(home)


def test_outbox_drop_and_send_say_when_nothing_matches(home):
    with scoped_server() as (url, _handler):
        for command in ("drop", "send"):
            missing = relay(home, url, "outbox", command, "nobody", "nothing")
            assert missing.returncode == 1 and "nothing queued" in missing.stderr
        empty = relay(home, url, "outbox", "list")
        assert empty.returncode == 0 and "0 handoff(s)" in empty.stdout


def test_outbox_drop_clears_a_stale_queued_failure_left_by_an_entry_removed_by_hand(home):
    """The entry of 2026-10-25 was moved out by hand; status.json kept the agent's 'close (queued)' failure."""
    with scoped_server() as (url, _handler):
        outbox.record(home, agent_id="codex-task", command="close (queued)", ok=False, error="HTTP 403: x", http_status=403)
        cleared = relay(home, url, "outbox", "drop", "codex-task", "S")
        assert cleared.returncode == 0 and "cleared" in cleared.stdout
        assert "last_failure" not in outbox.read_status(home)["agents"]["codex-task"]


def test_the_sessions_own_refused_key_is_still_named(home):
    with scoped_server() as (url, handler):
        handler.refuse_briefs = True
        brief = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert "the server refused this key (HTTP 403)" in brief.stdout


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("HTTP 403: " + AGENT_REFUSAL.format(agent="codex-task"), "agent"),
        ("HTTP 403: " + PROJECT_REFUSAL, "backoff"),
    ],
)
def test_a_refused_entry_written_by_the_0_17_5_client_is_held_too(home, error, kind):
    """An entry queued by the installed client has no ``refusals`` or ``last_try_ts``: only ``last_try_at``."""
    path = outbox.enqueue(
        home,
        _payload("codex-task", "old"),
        url="http://127.0.0.1:9",
        config_source="env",
        error=error,
        http_status=403,
        api_key="k",
    )
    assert path is not None
    data = json.loads(path.read_text())
    for name in ("refusals", "last_try_ts"):
        data.pop(name, None)
    data["last_try_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(data))
    [entry] = outbox.pending(home)
    hold = outbox.refusal_hold(entry)
    assert hold is not None and hold[0] == kind
    if kind == "backoff":  # and it goes again once the first wait is over
        assert outbox.refusal_hold(entry, now=time.time() + outbox.REFUSAL_BACKOFF_SECONDS[0] + 5) is None
    else:
        assert outbox.refusal_hold(entry, now=time.time() + 13 * 86400) is not None


@pytest.mark.parametrize("data", [{}, {"last_status": 403}, {"last_status": 403, "last_error": None, "refusals": "x"}])
def test_refusal_hold_survives_missing_fields(tmp_path, data):
    entry = outbox.Entry(path=tmp_path / "e.json", data={"payload": {}, **data})
    assert outbox.refusal_hold(entry) is None  # no refusal, or no time recorded at all: never held for ever
    if data.get("last_status") == 403:
        queued = outbox.Entry(path=entry.path, data={**entry.data, "queued_ts": time.time()})
        assert queued.refusals == 1 and outbox.refusal_hold(queued) is not None  # as the 0.17.5 client queued it
        assert outbox.refusal_hold(queued, now=time.time() + outbox.REFUSAL_BACKOFF_SECONDS[0] + 5) is None


def test_doctor_reports_an_agent_refused_entry_as_held_with_the_way_to_drop_it(home):
    from remembra.marshal import signals

    env = {"HOME": str(home), "REMEMBRA_API_KEY": "k", "REMEMBRA_URL": "http://127.0.0.1:9"}
    for session, error in (("scoped", AGENT_REFUSAL.format(agent="codex-task")), ("project", PROJECT_REFUSAL)):
        assert outbox.enqueue(
            home,
            _payload("codex-task", session),
            url=env["REMEMBRA_URL"],
            config_source="env",
            error=f"HTTP 403: {error}",
            http_status=403,
            api_key="k",
        )
    held = {e.session_id: signals.held_reason(e, env, home) for e in outbox.pending(home)}
    assert held["scoped"] and "another agent than codex-task" in held["scoped"]
    assert held["project"] is None  # a project refusal is retried (on a backoff): doctor keeps it under "queued"
