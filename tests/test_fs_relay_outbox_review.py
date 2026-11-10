"""Review fixes for F33 / F38 (relay outbox): what the first fix got wrong, each through the real code path.

* A 403 written by a firewall in front of the API (an HTML page, Cloudflare) is not a refusal by Remembra:
  the handoff is not held on the refusal backoff, it goes with the next brief and with the drain.
* ``remembra-relay drain`` that cannot take the drain lock, while no drain is running, starts nothing.
* A queued entry that only :func:`replay_config` holds (another server, no key) starts no drain.
* A drain lock whose pid is alive but that no drain keeps fresh (pid reuse after a restart) does not stop
  a new drain.
* ``outbox drop`` with a session that matches nothing does not erase an unrelated close failure.

The real CLI in subprocesses against a local stub server, an isolated HOME per test.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from remembra.marshal import signals
from remembra.relay import outbox
from tests.test_relay_stop_outbox import KEY, SRC, _free_port, _payload, relay

BLOCK_PAGE = (
    b"<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>"
    b"<body>Sorry, you have been blocked. Cloudflare Ray ID: <strong>8c1f2e3d4a5b6c7d</strong></body></html>"
)


class _Server(http.server.BaseHTTPRequestHandler):
    """``mode``: "ok", "firewall" (an HTML 403), "edge" (a 403 with Cloudflare headers and a short plain body),
    or an HTTP status answered with Remembra's JSON."""

    posts: list[str] = []
    mode = "ok"

    def _send(self, status: int, data: bytes, ctype: str = "application/json", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).posts.append(f"{body.get('agent_id')}:{body.get('session_id')}")
        mode = type(self).mode
        if mode == "firewall":
            self._send(403, BLOCK_PAGE, "text/html")
        elif mode == "edge":
            self._send(403, b"error code: 1020", "text/plain", {"CF-RAY": "8c1f2e3d4a5b6c7d-EWR"})
        elif mode == "ok":
            self._send(200, json.dumps({"ok": True, "handoff_id": "h1", "project_id": "p", "headline": "done"}).encode())
        else:
            self._send(int(mode), json.dumps({"detail": "No access to project 'p'."}).encode())

    def do_GET(self) -> None:
        self._send(200, json.dumps({"rendered": "Remembra brief"}).encode())

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def server(mode: str = "ok", port: int = 0) -> Iterator[tuple[str, type[_Server]]]:
    handler = type("Handler", (_Server,), {"posts": [], "mode": mode})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
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
    path = outbox.log_path(home)
    return path.read_text() if path.exists() else ""


def _until(done: Callable[[], bool], seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if done():
            return True
        time.sleep(0.05)
    return done()


def _close(home: Path, url: str, session: str, env: dict | None = None):
    args = ("close", "--agent", "claude-code", "--session-id", session, "--summary", "x", "--cwd", str(home))
    return relay(home, url, *args, env=env)


# --- a firewall's 403 is not Remembra's refusal ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["firewall", "edge"])
def test_a_handoff_blocked_by_a_firewall_goes_with_the_next_brief(home, mode):
    with server(mode) as (url, handler):
        close = _close(home, url, "S1")
        assert close.returncode == 0 and "HTTP 403" in close.stderr
        assert "refused the handoff" not in close.stderr and "held" not in close.stderr
        assert "will be sent by the next brief or close" in close.stderr  # (the drain is off in this env)
        [entry] = outbox.pending(home)
        assert entry.data["last_status"] == 403 and outbox.refusal_hold(entry) is None
        assert outbox.blocked_by_firewall(entry)
        listed = relay(home, url, "outbox", "list")
        assert "held:" not in listed.stdout and "goes with the next brief or close" in listed.stdout
        handler.mode = "ok"  # the block is over
        brief = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert brief.returncode == 0, brief.stderr
        assert handler.posts == ["claude-code:S1", "claude-code:S1"]  # delivered by the very next brief
        assert outbox.pending(home) == []
        assert "refused by the server" not in brief.stdout and "sent 1 queued handoff" in brief.stdout


def test_a_firewall_block_on_replay_is_a_failed_resend_not_a_refusal(home):
    with server("firewall") as (url, handler):
        assert outbox.enqueue(home, _payload("claude-code", "old"), url=url, config_source="env", error="ReadTimeout")
        assert outbox.enqueue(home, _payload("claude-code", "newer"), url=url, config_source="env", error="ReadTimeout")
        brief = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert brief.returncode == 0, brief.stderr
        assert handler.posts == ["claude-code:old"]  # the proxy blocks: the run stops, as for an unreachable server
        assert "refused by the server" not in brief.stdout and "could not be sent yet" in brief.stdout
        log = _log(home)
        assert "outbox: resend failed for claude-code session old (kept)" in log and "outbox: refused" not in log
        entries = {e.session_id: e for e in outbox.pending(home)}
        assert entries["old"].data["refusals"] == 0 and outbox.refusal_hold(entries["old"]) is None
        handler.mode = "ok"
        again = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert again.returncode == 0 and outbox.pending(home) == []
        assert handler.posts[1:] == ["claude-code:old", "claude-code:newer"]


def test_a_firewall_blocked_close_starts_a_drain_that_delivers_it(home):
    with server("firewall") as (url, handler):
        close = _close(home, url, "S1", env={"REMEMBRA_RELAY_DRAIN": "1,2"})
        assert "in the background" in close.stderr, close.stderr
        handler.mode = "ok"
        assert _until(lambda: "drain: finished" in _log(home), 25), _log(home)  # (an entry being sent is not listed)
        assert handler.posts == ["claude-code:S1", "claude-code:S1"] and outbox.pending(home) == []


def test_remembras_own_403_is_still_held(home):
    with server("403") as (url, handler):
        close = _close(home, url, "S1")
        assert "refused the handoff (HTTP 403)" in close.stderr
        [entry] = outbox.pending(home)
        assert entry.data["refusals"] == 1 and not outbox.blocked_by_firewall(entry)
        assert (outbox.refusal_hold(entry) or ("", ""))[0] == "backoff"
        handler.mode = "ok"
        relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert handler.posts == ["claude-code:S1"]  # not re-sent by the next brief


def test_an_entry_a_0_17_5_client_queued_on_a_block_page_is_not_held(home):
    """That client stored only the error text: an HTML page after 'HTTP 403:' is the firewall's."""
    error = "HTTP 403: " + BLOCK_PAGE.decode()[:280]
    assert outbox.enqueue(home, _payload("claude-code", "S1"), url="http://127.0.0.1:9", config_source="env", error=error)
    [entry] = outbox.pending(home)
    data = {k: v for k, v in entry.data.items() if k not in ("refusals", "last_try_ts", "refused_by")}
    entry.path.write_text(json.dumps({**data, "last_status": 403, "refused_key_fp": outbox.key_mark(KEY)}))
    [entry] = outbox.pending(home)
    assert outbox.blocked_by_firewall(entry) and outbox.refusal_hold(entry) is None


@pytest.mark.parametrize(
    ("text", "headers"),
    [
        (BLOCK_PAGE.decode(), {}),
        ("error code: 1020", {"cf-ray": "8c1f2e3d4a5b6c7d-EWR"}),
        ("error code: 1020", {"Server": "cloudflare"}),
        ("<html><body>403 Forbidden</body></html>", {}),
        ('{"detail": "No access to project \'p\'."}', {}),
        ("This API key is scoped to agent 'a'; the request claims 'b'.", {}),
        ("", {}),
    ],
)
def test_the_outbox_and_the_doctor_agree_on_what_a_firewall_block_is(text, headers):
    assert outbox.firewall_block(text, headers) == signals.is_firewall_block(403, text, headers)[0]


# --- the drain ------------------------------------------------------------------------------------------------


def test_a_drain_that_cannot_take_the_lock_starts_nothing(home, monkeypatch):
    """The lock path can be neither created nor removed (a full or read-only disk) and no drain is running."""
    url = f"http://127.0.0.1:{_free_port()}"  # nothing listens
    assert outbox.enqueue(home, _payload("a", "s"), url=url, config_source="env", error="ConnectError")
    outbox.drain_lock_path(home).mkdir()
    assert outbox.acquire_drain_lock(home) is False and outbox.drain_running(home) is False
    try:
        drain = relay(home, url, "drain", "--now", env={"REMEMBRA_RELAY_DRAIN": "1,2"})
        assert drain.returncode == 0
        time.sleep(3)
        log = _log(home)
        assert log.count("drain:") == 1 and "drain: could not take the drain lock" in log, log
        assert "another drain is running" not in log and not outbox.drain_kick_path(home).exists()
        found = subprocess.run(["pgrep", "-f", "remembra.relay.cli drain"], capture_output=True, text=True)
        for pid in found.stdout.split():  # none of them may be a drain of THIS home
            ps = subprocess.run(["ps", "eww", "-p", pid], capture_output=True, text=True).stdout
            assert f"HOME={home}" not in ps, ps
        assert [e.session_id for e in outbox.pending(home)] == ["s"]  # still queued for the next brief or close
    finally:
        for entry in outbox.pending(home):  # whatever happened, leave nothing a drain would go on for
            entry.path.unlink()


def test_a_drain_that_finds_another_one_running_tells_it_and_starts_nothing(home):
    url = f"http://127.0.0.1:{_free_port()}"
    assert outbox.enqueue(home, _payload("a", "s"), url=url, config_source="env", error="ConnectError")
    outbox.drain_lock_path(home).write_text(str(os.getpid()))  # a live, fresh lock
    drain = relay(home, url, "drain", "--now", env={"REMEMBRA_RELAY_DRAIN": "1,2"})
    assert drain.returncode == 0
    assert "drain: another drain is running; it was told to go again" in _log(home)
    assert outbox.read_drain_kick(home) is not None
    assert outbox.drain_lock_path(home).read_text() == str(os.getpid())


def test_an_entry_held_for_another_server_or_without_a_key_starts_no_drain(home, monkeypatch):
    assert outbox.enqueue(home, _payload("a", "elsewhere"), url="http://127.0.0.1:9", config_source="env", error="ConnectError")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no process may be started"))
    env = {"HOME": str(home), "REMEMBRA_URL": "http://127.0.0.1:10", "REMEMBRA_API_KEY": KEY, "REMEMBRA_RELAY_DRAIN": "1,2"}
    assert outbox.kick_drain(home, now=True, environ=env) is False  # the key points at another server
    assert outbox.kick_drain(home, now=True, environ={"HOME": str(home), "REMEMBRA_RELAY_DRAIN": "1,2"}) is False  # no key
    assert _log(home).count("drain:") == 0


def test_an_entry_for_this_server_still_starts_a_drain(home, monkeypatch):
    assert outbox.enqueue(home, _payload("a", "here"), url="http://127.0.0.1:9", config_source="env", error="ConnectError")
    started: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: started.append((argv, k["env"])))
    env = {"HOME": str(home), "REMEMBRA_URL": "http://127.0.0.1:9/", "REMEMBRA_API_KEY": KEY, "REMEMBRA_RELAY_DRAIN": "1,2"}
    assert outbox.kick_drain(home, now=True, environ=env) is True
    [(argv, child_env)] = started
    assert argv[-2:] == ["drain", "--now"] and child_env["REMEMBRA_URL"] == env["REMEMBRA_URL"]


def test_a_lock_no_drain_keeps_fresh_does_not_stop_a_new_drain(home, monkeypatch):
    """After a restart the pid in drain.lock can belong to another process: the lock is only trusted while fresh."""
    assert outbox.enqueue(home, _payload("a", "s"), url="http://127.0.0.1:9", config_source="env", error="ConnectError")
    lock = outbox.drain_lock_path(home)
    lock.write_text(str(os.getppid()))  # alive, and not a drain
    assert outbox.drain_running(home) is True  # just written: it could be a drain
    stale = time.time() - outbox.DRAIN_LOCK_STALE_SECONDS - 5
    os.utime(lock, (stale, stale))
    assert outbox.drain_running(home) is False
    started: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: started.append(argv))
    env = {"HOME": str(home), "REMEMBRA_URL": "http://127.0.0.1:9", "REMEMBRA_API_KEY": KEY, "REMEMBRA_RELAY_DRAIN": "1,2"}
    assert outbox.kick_drain(home, now=True, environ=env) is True and len(started) == 1
    assert outbox.acquire_drain_lock(home) is True and lock.read_text() == str(os.getpid())
    outbox.release_drain_lock(home)


def test_a_running_drain_keeps_its_lock_fresh(home):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"  # nothing listens yet
    assert outbox.enqueue(home, _payload("claude-code", "s"), url=url, config_source="env", error="ConnectError")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": SRC,
        "REMEMBRA_URL": url,
        "REMEMBRA_API_KEY": KEY,
        "REMEMBRA_RELAY_DRAIN": "4,4",
    }
    assert outbox.kick_drain(home, environ=env) is True
    lock = outbox.drain_lock_path(home)
    assert _until(lock.exists, 15), _log(home)
    old = time.time() - 600
    os.utime(lock, (old, old))
    assert _until(lambda: lock.exists() and time.time() - lock.stat().st_mtime < 60, 5)  # the drain touched it again
    with server("ok", port) as (_url, handler):
        assert _until(lambda: "drain: finished" in _log(home), 25), _log(home)
        assert handler.posts == ["claude-code:s"] and outbox.pending(home) == []
    assert not lock.exists()


# --- outbox drop ----------------------------------------------------------------------------------------------


def test_drop_with_a_session_that_matches_nothing_keeps_an_unrelated_close_failure(home):
    with server("422") as (url, _handler):
        close = _close(home, url, "S1")  # rejected for good: recorded, not queued
        assert "HTTP 422" in close.stderr and outbox.pending(home) == []
        before = outbox.read_status(home)["agents"]["claude-code"]
        assert before["last_failure"]["command"] == "close" and before["last_failure"]["http_status"] == 422
        drop = relay(home, url, "outbox", "drop", "claude-code", "no-such-session")
        assert drop.returncode == 1 and "nothing queued" in drop.stderr and "cleared" not in drop.stdout
        assert outbox.read_status(home)["agents"]["claude-code"] == before


def test_clearing_a_stale_queued_failure_removes_it_everywhere_and_keeps_the_rest(home):
    with server("ok") as (url, _handler):
        outbox.record(home, agent_id="codex-task", command="brief", ok=False, error="HTTP 500: x", http_status=500)
        outbox.record(home, agent_id="codex-task", command="close (queued)", ok=False, error="HTTP 403: x", http_status=403)
        cleared = relay(home, url, "outbox", "drop", "codex-task", "S")
        assert cleared.returncode == 0 and "cleared" in cleared.stdout
        slot = outbox.read_status(home)["agents"]["codex-task"]
        assert "last_failure" not in slot.get("by_command", {}).get("close (queued)", {})
        assert [f["command"] for f in slot["recent_failures"]] == ["brief"]
        assert slot["last_failure"]["command"] == "brief"  # the newest failure that is left
        status = relay(home, url, "status", "--no-check")
        assert "close (queued)" not in status.stdout
        again = relay(home, url, "outbox", "drop", "codex-task", "S")
        assert again.returncode == 1 and "nothing queued" in again.stderr


def test_dropping_an_entry_clears_the_close_failure_that_queued_it(home):
    url = f"http://127.0.0.1:{_free_port()}"  # nothing listens: the close queues
    close = _close(home, url, "S1")
    assert close.returncode == 0 and [e.session_id for e in outbox.pending(home)] == ["S1"]
    assert outbox.read_status(home)["agents"]["claude-code"]["last_failure"]["command"] == "close"
    drop = relay(home, url, "outbox", "drop", "claude-code", "S1")
    assert drop.returncode == 0 and "dropped the queued handoff" in drop.stdout
    slot = outbox.read_status(home)["agents"]["claude-code"]
    assert "last_failure" not in slot and slot.get("recent_failures") == []
