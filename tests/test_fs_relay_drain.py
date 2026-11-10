"""F38: a handoff queued on a timeout or DNS failure is sent without waiting for another relay hook.

The relay outbox was sent only by a later ``remembra-relay brief`` or ``close``. On a machine whose hooks
are ``remembra-crew start`` / ``end`` those run only for sessions that are not crew sessions, so a queued
handoff waited for hours while agents were working (2 h on 2026-11-05), and after a logout or restart until
the first non-crew session. Now a close that queues on a transient failure starts a detached
``remembra-relay drain`` that retries on a short schedule, and a crew session's start and end start one
when something is queued, so the first session after a login sends what the shutdown left behind.

The real CLIs in subprocesses against a local stub server, an isolated HOME per test.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from remembra.relay import outbox
from remembra.relay.crew import cli as crew_cli
from remembra.relay.crew.gate import Layout, session_key
from tests.crew.wp9_support import make_repo
from tests.test_relay_stop_outbox import KEY, SRC, _free_port, _payload, relay

FAST = {"REMEMBRA_RELAY_DRAIN": "1,2"}  # the drain's waits, in seconds, instead of the defaults
HOST_MARKERS = (
    "GROK_WORKSPACE_ROOT",
    "GEMINI_SESSION_ID",
    "QWEN_CODE_SESSION_ID",
    "DEVIN_PROJECT_DIR",
    "CONTINUE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "REMEMBRA_RELAY_INCLUDE_AUTOMATIONS",
)


class _Recorder(http.server.BaseHTTPRequestHandler):
    posts: list[str] = []
    status = 200

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).posts.append(f"{body.get('agent_id')}:{body.get('session_id')}")
        ok = type(self).status < 400
        data = json.dumps({"ok": True, "handoff_id": "h1"} if ok else {"detail": "Invalid or revoked API key"}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def recorder(port: int, status: int = 200) -> Iterator[type[_Recorder]]:
    handler = type("Handler", (_Recorder,), {"posts": [], "status": status})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield handler
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _until(done: Callable[[], bool], seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if done():
            return True
        time.sleep(0.1)
    return done()


def _log(home: Path) -> str:
    try:
        return outbox.log_path(home).read_text()
    except OSError:
        return ""


def _drain_gone(home: Path) -> bool:
    return not outbox.drain_lock_path(home).exists()


def test_a_queued_close_is_sent_without_another_hook(home):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    close = relay(home, url, "close", "--agent", "a", "--session-id", "s", "--summary", "x", "--cwd", str(home), env=FAST)
    assert close.returncode == 0 and "queued" in close.stderr and "in the background" in close.stderr
    assert [e.session_id for e in outbox.pending(home)] == ["s"]
    with recorder(port) as server:  # the network is back; no brief and no close runs
        assert _until(lambda: server.posts == ["a:s"], 15), _log(home)
        assert _until(lambda: outbox.pending(home) == [] and _drain_gone(home), 10), _log(home)
    log = _log(home)
    assert "outbox: delivered a session s" in log
    assert "drain: started" in log and "drain: finished, sent 1" in log


def test_the_close_does_not_wait_for_its_drain(home):
    """The hook returns while the drain is still waiting for its first try (its output is not the hook's)."""
    url = f"http://127.0.0.1:{_free_port()}"
    slow = {"REMEMBRA_RELAY_DRAIN": "4"}
    close = relay(home, url, "close", "--agent", "a", "--session-id", "s", "--summary", "x", "--cwd", str(home), env=slow)
    assert close.returncode == 0
    assert _until(lambda: "drain: started" in _log(home), 5)
    assert outbox.drain_lock_path(home).exists()  # still running after the close has returned
    assert _until(lambda: _drain_gone(home), 15)  # one failed try, then it gives up: the next hook sends it
    assert [e.session_id for e in outbox.pending(home)] == ["s"]
    assert "drain: finished, sent 0, 1 still queued" in _log(home)


def test_two_queued_closes_share_one_drain(home):
    url = f"http://127.0.0.1:{_free_port()}"
    for session in ("s1", "s2"):
        relay(home, url, "close", "--agent", "a", "--session-id", session, "--summary", "x", "--cwd", str(home), env=FAST)
    second = relay(home, url, "drain", env=FAST)  # a third, started by hand while the first is running
    assert second.returncode == 0
    assert _until(lambda: _drain_gone(home), 20)
    log = _log(home)
    assert log.count("drain: started") == 1, log
    assert "drain: another drain is running" in log


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_or_refused_close_starts_no_drain(home, status):
    port = _free_port()
    with recorder(port, status=status) as server:
        url = f"http://127.0.0.1:{port}"
        close = relay(home, url, "close", "--agent", "a", "--session-id", "s", "--summary", "x", "--cwd", str(home), env=FAST)
        assert f"HTTP {status}" in close.stderr and "in the background" not in close.stderr
        time.sleep(3)
        assert server.posts == ["a:s"]  # waiting cannot fix a key: nothing retries it in the background
    assert "drain:" not in _log(home) and _drain_gone(home)


def test_a_drain_stops_when_the_key_is_rejected(home):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    assert outbox.enqueue(home, _payload("a", "s"), url=url, config_source="env", error="ReadTimeout")
    with recorder(port, status=401) as server:
        drain = relay(home, url, "drain", "--now", env={"REMEMBRA_RELAY_DRAIN": "1,60"})
        assert drain.returncode == 0 and drain.elapsed < 20  # it did not sit out the 60 s wait
        assert server.posts == ["a:s"]
    assert [e.session_id for e in outbox.pending(home)] == ["s"]


def test_drain_can_be_turned_off(home):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    off = {"REMEMBRA_RELAY_DRAIN": "off"}
    close = relay(home, url, "close", "--agent", "a", "--session-id", "s", "--summary", "x", "--cwd", str(home), env=off)
    assert "will be sent by the next brief or close" in close.stderr and "in the background" not in close.stderr
    with recorder(port) as server:
        time.sleep(2.5)
        assert server.posts == [] and "drain:" not in _log(home)
        assert relay(home, url, "drain", "--now", env=off).returncode == 0 and server.posts == []
        brief = relay(home, url, "brief", "--agent", "b", "--cwd", str(home), env=off)  # the old way still works
        assert "sent 1 queued handoff from a" in brief.stdout


def test_a_drain_with_nothing_to_send_exits_at_once(home):
    url = f"http://127.0.0.1:{_free_port()}"
    drain = relay(home, url, "drain", "--now", env={"REMEMBRA_RELAY_DRAIN": "30,60"})
    assert drain.returncode == 0 and drain.elapsed < 10 and _drain_gone(home)
    assert outbox.kick_drain(home, environ={"HOME": str(home)}) is False  # nothing queued: nothing started


# ---------------------------------------------------------------------------
# Crew hooks: a crew session's start and end send what is queued (they never ran the relay's brief or close).
# ---------------------------------------------------------------------------


@pytest.fixture()
def crew_env(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, type[_Recorder]]]:
    """This process is the hook: HOME, a key and the server in its environment; one handoff already queued."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    for name in list(os.environ):
        if name.startswith("REMEMBRA_") or name in HOST_MARKERS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_URL", url)
    monkeypatch.setenv("REMEMBRA_API_KEY", KEY)
    monkeypatch.setenv("REMEMBRA_RELAY_DRAIN", "1,2")
    monkeypatch.setenv("PYTHONPATH", SRC)
    assert outbox.enqueue(home, _payload("codex", "left-at-shutdown"), url=url, config_source="env", error="ConnectError: dns")
    with recorder(port) as server:
        yield url, server
        _until(lambda: _drain_gone(home), 15)


def _hook(monkeypatch: pytest.MonkeyPatch, layout: Layout, argv: list[str], payload: dict[str, Any]) -> None:
    monkeypatch.setattr(crew_cli, "read_stdin", lambda *a, **k: json.dumps(payload))
    assert crew_cli.main(argv, layout=layout) == 0


def test_a_crew_session_end_sends_what_is_queued(home, tmp_path, crew_env, monkeypatch, capsys):
    _url, server = crew_env
    layout = Layout(tmp_path / "crew")
    key = session_key("claude-code", "sid-1")
    layout.session_file(key).parent.mkdir(parents=True, exist_ok=True)
    layout.session_file(key).write_text(json.dumps({"crew_id": "crw_1"}))
    sent: list[str] = []
    monkeypatch.setattr(crew_cli, "rpc_send", lambda _layout, op, args=None, **_: sent.append(op) or True)
    _hook(monkeypatch, layout, ["end", "--hook", "claude-code"], {"session_id": "sid-1", "reason": "other"})
    assert sent == ["end"]  # the crew end itself is unchanged
    assert _until(lambda: server.posts == ["codex:left-at-shutdown"], 15), _log(home)
    assert _until(lambda: outbox.pending(home) == [], 5)


def test_a_crew_session_start_sends_what_is_queued(home, tmp_path, crew_env, monkeypatch, capsys):
    _url, server = crew_env
    repo = make_repo(tmp_path / "repo")
    (repo / ".remembra").mkdir(exist_ok=True)
    layout = Layout(tmp_path / "crew")

    def rpc(_layout: Layout, op: str, args: Any = None, **_: Any) -> Any:
        if op == "join":
            return {"ok": True, "key": "k1", "callsign": "cc-1", "crew_id": "crw_1"}
        return {"rendered": "crew brief"} if op == "brief" else None

    monkeypatch.setattr(crew_cli, "rpc", rpc)
    monkeypatch.setattr(crew_cli, "ensure_crewd", lambda *a, **k: True)
    payload = {"session_id": "sid-2", "cwd": str(repo), "source": "startup"}
    _hook(monkeypatch, layout, ["start", "--hook", "claude-code", "--agent-pid", str(os.getpid())], payload)
    assert "crew brief" in capsys.readouterr().out  # the crew brief itself is unchanged
    assert _until(lambda: server.posts == ["codex:left-at-shutdown"], 15), _log(home)
    assert _until(lambda: outbox.pending(home) == [], 5)


def test_a_crew_hook_with_an_empty_queue_starts_nothing(home, tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("REMEMBRA_") or name in HOST_MARKERS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    layout = Layout(tmp_path / "crew")
    key = session_key("claude-code", "sid-1")
    layout.session_file(key).parent.mkdir(parents=True, exist_ok=True)
    layout.session_file(key).write_text(json.dumps({"crew_id": "crw_1"}))
    monkeypatch.setattr(crew_cli, "rpc_send", lambda *a, **k: True)
    spawned: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("no process may be started"))
    _hook(monkeypatch, layout, ["end", "--hook", "claude-code"], {"session_id": "sid-1", "reason": "other"})
    assert spawned == [] and not outbox.relay_dir(home).exists()
    assert sys.executable  # (the hook stays a no-op for the relay outbox: no file, no process)


def test_a_held_or_rejected_entry_alone_starts_no_drain(home, monkeypatch):
    """A crew hook finds only a handoff that waiting cannot deliver: no process is started for it."""
    url = "http://127.0.0.1:9"
    refusal = "HTTP 403: This API key is scoped to agent 'claude-code'; the request claims 'codex-task'."
    assert outbox.enqueue(
        home, _payload("codex-task", "held"), url=url, config_source="env", error=refusal, http_status=403, api_key=KEY
    )
    assert outbox.enqueue(home, _payload("a", "rejected"), url=url, config_source="env", error="HTTP 401: x", http_status=401)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no process may be started"))
    assert outbox.kick_drain(home, now=True, environ={"HOME": str(home)}) is False
