"""F35: a slow crewd is not a dead crewd, and the start hook must say what is true.

- crewd runs on the latency path of every tool call, so its LaunchAgent must not be in macOS's Background band;
- a crewd that accepts the connection but answers late is never respawned and is not reported as "did not start";
- a join that crewd completes after the hook stopped waiting leaves the session enforced: the hook must not
  print "Crew rules are not active", and it recovers the callsign and the environment exports.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.crew import cli, install
from remembra.relay.crew.gate import Layout, session_key
from tests.crew.wp9_support import make_repo

HANG = object()
SID = "s-late"


class SlowCrewd:
    """A unix-socket stand-in for crewd: ``answer(op, args)`` returns a reply, ``None`` (close) or HANG."""

    def __init__(self, layout: Layout, answer: Callable[[str, dict[str, Any]], Any]) -> None:
        self.path = layout.socket
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            self.path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(16)
        self.answer = answer
        self.ops: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        self.sock.settimeout(0.05)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return  # a bare connect (liveness probe): nothing to answer
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0])
            self.ops.append(req["op"])
            reply = self.answer(req["op"], req.get("args") or {})
            if reply is HANG:
                self.stop.wait(30)
                return
            if reply is not None:
                try:
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass  # the client stopped waiting

    def close(self) -> None:
        self.stop.set()
        self.sock.close()
        self.thread.join(2)
        if self.path.exists():
            self.path.unlink()


def _session(layout: Layout, **changes: Any) -> dict[str, Any]:
    key = session_key("claude-code", SID)
    sess: dict[str, Any] = {
        "key": key,
        "adapter": "claude-code",
        "client_session_id": SID,
        "crew_id": "crew_1",
        "session_id": "cs_late",
        "callsign": "cc-3",
        "member_key": "mem_3",
        "project_id": "proj_late",
        "enforcement": "enforced",
        "observe_only": False,
        "githook_state": "ok",
        "ended": False,
    }
    sess.update(changes)
    return sess


def _write_session(layout: Layout, **changes: Any) -> None:
    sess = _session(layout, **changes)
    layout.sessions.mkdir(parents=True, exist_ok=True)
    layout.session_file(sess["key"]).write_text(json.dumps(sess))


def _start(layout: Layout, cwd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[str, float]:
    monkeypatch.setattr(cli, "read_stdin", lambda *a, **k: json.dumps({"session_id": SID, "cwd": str(cwd), "source": "startup"}))
    capsys.readouterr()
    t = time.monotonic()
    code = cli.main(["start", "--hook", "claude-code", "--agent-pid", str(os.getpid())], layout=layout)
    elapsed = time.monotonic() - t
    assert code == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"], elapsed


@pytest.fixture
def no_respawn(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every attempt to start a crewd instead of starting one."""
    spawned: list[str] = []

    def fake_respawn(_layout: Layout, *, force: bool = False) -> bool:
        spawned.append("respawn")
        return True

    real_popen = cli.subprocess.Popen

    def popen(argv: Any, *a: Any, **k: Any) -> Any:
        if "remembra.relay.crew.crewd" in list(argv):
            spawned.append("popen")
            raise OSError("no crewd in tests")
        return real_popen(argv, *a, **k)

    monkeypatch.setattr(cli, "respawn_crewd", fake_respawn)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return spawned


# -- 1. the supervisor unit -----------------------------------------------------------------


def test_launchd_unit_is_not_in_the_background_band(tmp_path):
    """``ProcessType: Background`` runs crewd and its ps/git children at the lowest priority: under load a
    36 ms ``ps`` took seconds, longer than the hooks wait."""
    body = plistlib.loads(
        install.render_launchd_plist(tmp_path, ["/opt/remembra/bin/remembra-crewd"], "/opt/py/bin/python3").encode()
    )
    assert body["ProcessType"] == "Interactive"
    assert body["KeepAlive"] is True and body["RunAtLoad"] is True and body["ThrottleInterval"] == 10
    assert body["ProgramArguments"] == ["/opt/remembra/bin/remembra-crewd"]


def test_connect_rewrites_an_installed_background_unit(tmp_path):
    """An already installed Background unit differs from the rendered text, so connect rewrites and reloads it."""
    home = tmp_path / "home"
    path = install.launchd_plist_path(home)
    path.parent.mkdir(parents=True)
    program = ["/opt/remembra/bin/remembra-crewd"]
    new = install.render_launchd_plist(home, program, "/opt/py/bin/python3")
    old = new.replace("<string>Interactive</string>", "<string>Background</string>")
    assert old != new and plistlib.loads(old.encode())["ProcessType"] == "Background"
    path.write_text(old)
    opts = install.ConnectOptions(home=home, platform="darwin", uid=501, crewd_command=program, python="/opt/py/bin/python3")
    section = install.Section("service")
    plan = install.InstallPlan([section])
    install._plan_service(opts, section, plan, remove=False)
    (op,) = section.ops
    assert op.changed and op.after == new
    assert [c.argv[:2] for c in plan.post_commands] == [("launchctl", "bootout"), ("launchctl", "bootstrap")]


# -- 2. slow is not down ------------------------------------------------------------------


def test_a_busy_crewd_is_never_respawned(tmp_path, no_respawn):
    layout = Layout(tmp_path / "home")
    fake = SlowCrewd(layout, lambda op, args: HANG)
    try:
        t = time.monotonic()
        assert cli.ensure_crewd(layout, wait=0.6) is False  # it did not answer a ping…
        elapsed = time.monotonic() - t
        assert cli.crewd_listening(layout) is True  # …but it is running
    finally:
        fake.close()
    assert no_respawn == [], no_respawn
    assert 1.0 <= elapsed < 2.0, elapsed
    # one patient ping after the first, not a burst of short ones (each costs crewd a process-table read)
    assert fake.ops == ["ping", "ping"], fake.ops


def test_a_slow_ping_is_waited_out(tmp_path, no_respawn):
    layout = Layout(tmp_path / "home")

    def answer(op: str, args: dict[str, Any]) -> Any:
        time.sleep(0.9)  # longer than the first 0.5 s ping, inside the wait
        return {"ok": True}

    fake = SlowCrewd(layout, answer)
    try:
        assert cli.ensure_crewd(layout, wait=2.0) is True
    finally:
        fake.close()
    assert no_respawn == []


def test_a_dead_crewd_is_started(tmp_path, no_respawn):
    layout = Layout(tmp_path / "home")
    assert cli.crewd_listening(layout) is False
    assert cli.ensure_crewd(layout, wait=0.4) is False
    assert no_respawn == ["respawn"]


def test_a_stale_socket_file_counts_as_down(tmp_path, no_respawn):
    layout = Layout(tmp_path / "home")
    fake = SlowCrewd(layout, lambda op, args: {"ok": True})
    fake.stop.set()
    fake.sock.close()  # the socket file stays behind, nobody listens (crewd was killed)
    fake.thread.join(2)
    assert layout.socket.exists()
    assert cli.crewd_listening(layout) is False
    assert cli.ensure_crewd(layout, wait=0.3) is False
    assert no_respawn == ["respawn"]


def test_crewd_started_by_the_hook_is_found(tmp_path, monkeypatch):
    layout = Layout(tmp_path / "home")
    started: list[SlowCrewd] = []

    def fake_respawn(_layout: Layout, *, force: bool = False) -> bool:
        started.append(SlowCrewd(layout, lambda op, args: {"ok": True}))
        return True

    monkeypatch.setattr(cli, "respawn_crewd", fake_respawn)
    try:
        assert cli.ensure_crewd(layout, wait=2.0) is True
    finally:
        for f in started:
            f.close()
    assert len(started) == 1


# -- 3. the start hook says what is true ---------------------------------------------------------


def test_busy_crewd_is_not_reported_as_not_started(tmp_path, monkeypatch, capsys, no_respawn):
    """Pings time out but crewd is alive and answers the join: the session joins normally."""
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    monkeypatch.setattr(cli, "CREWD_START_WAIT_S", 0.5)

    def answer(op: str, args: dict[str, Any]) -> Any:
        if op == "ping":
            return HANG
        if op == "join":
            _write_session(layout)
            return {
                "ok": True,
                **{
                    k: v
                    for k, v in _session(layout).items()
                    if k
                    in ("key", "crew_id", "session_id", "callsign", "member_key", "project_id", "observe_only", "githook_state")
                },
            }
        if op == "brief":
            return {"ok": True, "rendered": "BRIEF for cc-3"}
        return None

    fake = SlowCrewd(layout, answer)
    try:
        text, _ = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "did not start" not in text and "not active" not in text, text
    assert "BRIEF for cc-3" in text
    assert "join" in fake.ops and no_respawn == []


def test_late_join_is_reported_as_active_and_recovered(tmp_path, monkeypatch, capsys, no_respawn):
    """crewd persists the session, then answers after the hook stopped waiting (the 2026-11-10 incident)."""
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    env_file = tmp_path / "claude.env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    monkeypatch.setattr(cli, "START_DEADLINE_S", 1.5)
    monkeypatch.setattr(cli, "START_HARD_S", 3.5, raising=False)

    def answer(op: str, args: dict[str, Any]) -> Any:
        if op == "ping":
            return {"ok": True}
        if op == "join":
            time.sleep(1.6)  # the session file lands after the client gave up on the reply
            _write_session(layout)
            return HANG
        if op == "brief":
            return {"ok": True, "rendered": "BRIEF for cc-3"}
        return None

    fake = SlowCrewd(layout, answer)
    try:
        text, elapsed = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "not active" not in text and "unavailable" not in text, text
    assert "crew rules are active" in text.lower(), text
    assert "BRIEF for cc-3" in text, text  # the brief is fetched with the time that is left
    assert fake.ops.count("join") == 1, fake.ops  # never sent twice
    assert elapsed < 3.5, elapsed
    exports = env_file.read_text()
    assert 'export REMEMBRA_PROJECT_ID="proj_late"' in exports and 'export REMEMBRA_MEMBER="mem_3"' in exports


def test_late_join_without_a_brief_still_names_the_seat(tmp_path, monkeypatch, capsys, no_respawn):
    """The session was already joined (a compact or resume start) and crewd answers nothing in time."""
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    _write_session(layout)
    monkeypatch.setattr(cli, "START_DEADLINE_S", 1.2)
    monkeypatch.setattr(cli, "START_HARD_S", 2.0, raising=False)
    fake = SlowCrewd(layout, lambda op, args: {"ok": True} if op == "ping" else HANG)
    try:
        text, elapsed = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "not active" not in text and "unavailable" not in text, text
    assert "cc-3" in text and "crew_1" in text and "crew rules are active" in text.lower(), text
    assert "remembra-crew status" in text
    assert elapsed < 2.6, elapsed


def test_unconfirmed_join_on_a_busy_crewd_is_not_called_inactive(tmp_path, monkeypatch, capsys, no_respawn):
    """crewd took the join and has not finished it when the hook has to return: say so, do not claim either way."""
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    monkeypatch.setattr(cli, "START_DEADLINE_S", 1.2)
    monkeypatch.setattr(cli, "START_HARD_S", 1.8, raising=False)
    fake = SlowCrewd(layout, lambda op, args: {"ok": True} if op == "ping" else HANG)
    try:
        text, elapsed = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "Crew rules are not active" not in text, text
    assert "busy" in text and "remembra-crew status" in text, text
    assert fake.ops.count("join") == 1
    assert elapsed < 2.4, elapsed


@pytest.mark.parametrize(
    "changes",
    [
        {"ended": True},  # a closed session's file
        {"crew_id": None},
        {"session_id": ""},
        {"client_session_id": "someone-else"},
    ],
)
def test_a_dead_session_file_is_not_taken_for_a_join(tmp_path, monkeypatch, capsys, no_respawn, changes):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    _write_session(layout, **changes)
    monkeypatch.setattr(cli, "START_DEADLINE_S", 1.2)
    monkeypatch.setattr(cli, "START_HARD_S", 1.6, raising=False)
    fake = SlowCrewd(layout, lambda op, args: {"ok": True} if op == "ping" else HANG)
    try:
        text, _ = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "crew rules are active" not in text.lower(), text


def test_malformed_session_file_is_ignored(tmp_path, monkeypatch, capsys, no_respawn):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    layout.sessions.mkdir(parents=True)
    layout.session_file(session_key("claude-code", SID)).write_text("[not a session")
    monkeypatch.setattr(cli, "START_DEADLINE_S", 1.2)
    monkeypatch.setattr(cli, "START_HARD_S", 1.6, raising=False)
    fake = SlowCrewd(layout, lambda op, args: {"ok": True} if op == "ping" else HANG)
    try:
        text, _ = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "crew rules are active" not in text.lower() and "busy" in text, text


def test_crewd_that_dies_during_the_join_is_reported_as_down(tmp_path, monkeypatch, capsys, no_respawn):
    """No session file and nobody listening any more: crew rules really are off, and the notice says so."""
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    monkeypatch.setattr(cli, "START_DEADLINE_S", 1.2)
    monkeypatch.setattr(cli, "START_HARD_S", 1.8, raising=False)
    holder: list[SlowCrewd] = []

    def answer(op: str, args: dict[str, Any]) -> Any:
        if op == "ping":
            return {"ok": True}
        # crewd is gone before the join's connection closes. The accept loop is joined first: closing a
        # listening socket another thread is still polling leaves it accepting for up to one poll (50 ms)
        # on Linux, and the client's liveness probe then found a "running" crewd.
        fake = holder[0]
        fake.stop.set()
        fake.thread.join(5)
        fake.sock.close()
        fake.path.unlink(missing_ok=True)
        return None  # connection closed without a reply

    holder.append(SlowCrewd(layout, answer))
    try:
        text, _ = _start(layout, repo, monkeypatch, capsys)
    finally:
        holder[0].close()
    assert "Crew rules are not active for this session" in text and "no answer from crewd" in text, text


def test_a_refused_join_keeps_its_reason(tmp_path, monkeypatch, capsys, no_respawn):
    """An explicit refusal from crewd is not a late join, even when an older session file exists."""
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    _write_session(layout)

    def answer(op: str, args: dict[str, Any]) -> Any:
        return {"ok": True} if op == "ping" else {"ok": False, "error": "plan_limit", "message": "seat limit reached"}

    fake = SlowCrewd(layout, answer)
    try:
        text, _ = _start(layout, repo, monkeypatch, capsys)
    finally:
        fake.close()
    assert "seat limit reached" in text and "Crew rules are not active for this session" in text
