"""Codex automations and sub-agent threads get no brief and leave no handoff.

Codex Desktop runs scheduled automations and spawns sub-agent threads, and each
one runs the relay's SessionStart / SessionEnd hooks. The hook payload does not
say what kind of thread it is; the first line of the rollout it names does
(:mod:`remembra.relay.background`). The rollouts here are synthetic
(tests/fixtures/relay/codex/threads/, shaped like Codex Desktop's own).

A sub-agent's hook payload is shaped the way live Codex sends it (codex-cli
0.155.0-alpha.16.4, tests/test_relay_codex_live.py): ``session_id`` is the
PARENT's session id (the sub-agent's session_meta ``session_id``), ``agent_id``
is the sub-agent's own thread id, and ``transcript_path`` is the sub-agent's
own rollout.

Covered: the classification of each thread kind and of rollouts that cannot be
read (missing, malformed, a first line past the read limit, not a regular
file, a device or FIFO that must not even be opened); a sub-agent sharing its
parent's session id; ``brief`` and ``close`` through ``cli.main`` with the hook payload on
stdin and every HTTP request recorded (nothing reaches the HTTP layer for a
skipped session, a user session still sends); the detached close; the
``REMEMBRA_RELAY_INCLUDE_AUTOMATIONS`` opt-out; a manual ``close`` without
``--hook``; and the real entry point in a subprocess against a local HTTP server.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.relay import background, cli, outbox
from remembra.relay.adapters import get_adapter

THREADS = Path(__file__).resolve().parent / "fixtures" / "relay" / "codex" / "threads"
SRC = str(Path(__file__).resolve().parents[1] / "src")
BRIEF_TEXT = "# Remembra brief · project widget · you are codex"
SKIPPED = {
    "automation": "automation",
    "subagent": "subagent",
    "subagent_source_only": "subagent",
}
NOT_SKIPPED = ("user", "voice_chat", "agent_created_thread", "no_thread_source", "malformed")


PARENT_SESSION = "0199d0a0-0000-7000-8000-00000000a009"  # the session the sub-agent fixtures belong to


def _session_id(kind: str) -> str:
    """The ``session_id`` Codex's hooks carry for the fixture's thread: its session_meta ``session_id``.

    That is the thread's own id, except for a sub-agent, whose hooks carry its
    parent's session id. ``malformed`` has none that parses: a fixed one.
    """
    meta = background.read_session_meta(THREADS / f"{kind}.jsonl")
    return str(meta["session_id"]) if meta else "0199d0a0-0000-7000-8000-00000000a008"


def _agent_id(kind: str) -> str | None:
    """The ``agent_id`` a sub-agent's hooks carry (its own thread id); None for any other thread."""
    meta = background.read_session_meta(THREADS / f"{kind}.jsonl")
    if not meta or meta.get("id") == meta.get("session_id"):
        return None
    return str(meta["id"])


def _skip_line(verb: str, kind: str, reason: str) -> str:
    """The relay.log line a skipped ``verb`` writes for this fixture's thread."""
    agent = _agent_id(kind)
    who = f"thread {agent} of session {_session_id(kind)}" if agent else f"session {_session_id(kind)}"
    return f"skipped {verb}: codex {reason} {who}"


def _huge_rollout(directory: Path, kind: str = "automation") -> Path:
    """A rollout whose first line is an automation's session_meta just past the read limit."""
    pad = "x" * background.FIRST_LINE_MAX_BYTES
    record = {"type": "session_meta", "payload": {"id": "huge-1", "thread_source": kind, "pad": pad}}
    path = directory / "huge.jsonl"
    path.write_text(json.dumps(record) + "\n")
    return path


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [*SKIPPED.items(), *((kind, None) for kind in NOT_SKIPPED)],
)
def test_each_thread_kind_is_read_from_the_first_line(kind, expected):
    payload = {"session_id": "s", "transcript_path": str(THREADS / f"{kind}.jsonl"), "hook_event_name": "SessionStart"}
    assert background.session_skip_reason(payload) == expected


def test_sub_agent_fixtures_carry_their_parent_session_id():
    """As in Codex: a sub-agent's session_meta ``session_id`` is its parent's, its ``id`` its own."""
    for kind in ("subagent", "subagent_source_only"):
        meta = background.read_session_meta(THREADS / f"{kind}.jsonl")
        assert meta is not None and meta["session_id"] == PARENT_SESSION and meta["id"] != PARENT_SESSION
        assert meta["source"]["subagent"]["thread_spawn"]["parent_thread_id"] == PARENT_SESSION


def test_a_later_record_never_changes_the_kind(tmp_path):
    """Only the first line decides. A heartbeat automation posts its turns into an existing thread
    (one someone started, or a voice chat): that thread keeps its brief and handoff."""
    for kind in ("user", "voice_chat"):
        thread = tmp_path / f"{kind}.jsonl"
        later = {"type": "session_meta", "payload": {"id": "later", "thread_source": "automation"}}
        thread.write_text((THREADS / f"{kind}.jsonl").read_text() + json.dumps(later) + "\n")
        assert background.session_skip_reason({"transcript_path": str(thread)}) is None, kind


def test_a_parent_thread_id_alone_marks_a_sub_agent():
    assert background.background_kind({"thread_source": "user", "parent_thread_id": "p-1"}) == "subagent"
    assert background.background_kind({"parent_thread_id": "  "}) is None
    assert background.background_kind({"thread_source": " Automation "}) == "automation"
    assert background.background_kind({"source": {"vscode": {}}, "thread_source": "realtime_voice"}) is None
    assert background.background_kind({"source": "subagent"}) is None  # only the object form names a sub-agent


def test_unreadable_rollouts_are_never_skipped_and_never_raise(tmp_path):
    reason = background.session_skip_reason
    assert reason({"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert reason({"transcript_path": str(tmp_path)}) is None  # a directory
    assert reason({}) is None and reason({"transcript_path": ""}) is None and reason({"transcript_path": 7}) is None
    assert reason({"transcript_path": "bad\x00path"}) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n" + (THREADS / "automation.jsonl").read_text())
    not_utf8 = tmp_path / "latin1.jsonl"
    not_utf8.write_bytes(b'{"type":"session_meta","payload":{"thread_source":"automation","x":"\xff"}}\n')
    deep = tmp_path / "deep.jsonl"
    deep.write_text("[" * 200_000 + "\n")
    other = tmp_path / "other.jsonl"
    other.write_text('{"type":"event_msg","payload":{"thread_source":"automation"}}\n')
    not_object = tmp_path / "list.jsonl"
    not_object.write_text('[{"type":"session_meta"}]\n')
    for path in (empty, blank, not_utf8, deep, other, not_object):
        assert reason({"transcript_path": str(path)}) is None, path.name


def test_the_first_line_is_read_up_to_the_limit_and_no_further(tmp_path, monkeypatch):
    huge = _huge_rollout(tmp_path)
    read: list[int] = []
    real_read = os.read

    def counting_read(fd: int, n: int) -> bytes:
        chunk = real_read(fd, n)
        read.append(len(chunk))
        return chunk

    monkeypatch.setattr(background.os, "read", counting_read)
    assert background.session_skip_reason({"transcript_path": str(huge)}) is None
    assert sum(read) == background.FIRST_LINE_MAX_BYTES + 1  # one byte past the limit shows it is too long
    assert huge.stat().st_size > sum(read)

    at_limit = tmp_path / "at-limit.jsonl"
    head = '{"type":"session_meta","payload":{"thread_source":"automation","pad":"'
    tail = '"}}'
    pad = "x" * (background.FIRST_LINE_MAX_BYTES - len(head) - len(tail))
    at_limit.write_text(head + pad + tail + "\nmore\n")
    assert background.session_skip_reason({"transcript_path": str(at_limit)}) == "automation"
    one_line = tmp_path / "one-line.jsonl"  # no newline at all: the whole file is the first line
    one_line.write_text((THREADS / "automation.jsonl").read_text().split("\n")[0])
    assert background.session_skip_reason({"transcript_path": str(one_line)}) == "automation"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_is_not_read_and_does_not_block(tmp_path):
    fifo = tmp_path / "rollout.jsonl"
    os.mkfifo(fifo)
    started = time.monotonic()
    assert background.session_skip_reason({"transcript_path": str(fifo)}) is None
    assert time.monotonic() - started < 2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs and /dev/null")
def test_anything_but_a_regular_file_is_refused_before_it_is_opened(tmp_path, monkeypatch):
    """Opening a device reaches its driver (a serial port resets its board, a watchdog arms): never open one."""
    fifo = tmp_path / "rollout.fifo"
    os.mkfifo(fifo)
    to_device = tmp_path / "rollout-link.jsonl"
    to_device.symlink_to(os.devnull)
    opened: list[str] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(background.os, "open", recording_open)
    for path in (os.devnull, "/dev/zero", str(fifo), str(to_device), str(tmp_path)):
        assert background.session_skip_reason({"transcript_path": path}) is None, path
    assert opened == []
    automation = str(THREADS / "automation.jsonl")
    assert background.session_skip_reason({"transcript_path": automation}) == "automation"
    assert opened == [automation]  # a regular file is opened, once


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_swapped_in_after_the_check_is_not_read_and_does_not_block(tmp_path, monkeypatch):
    """The open is non-blocking and the opened file is checked again: a FIFO put in place between the two is refused.

    The stat is made to report a regular file; the FIFO holds an automation's
    session_meta line, which a read would find. It is not read.
    """
    fifo = tmp_path / "rollout.jsonl"
    os.mkfifo(fifo)
    regular = os.stat(THREADS / "automation.jsonl")
    real_stat = os.stat

    def swapped_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        return regular if os.fspath(path) == str(fifo) else real_stat(path, *args, **kwargs)

    monkeypatch.setattr(background.os, "stat", swapped_stat)
    started = time.monotonic()
    assert background.read_first_line(fifo) is None  # no writer: refused, not waited on
    writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # a writer holding an automation's first line
    try:
        os.write(writer, (THREADS / "automation.jsonl").read_bytes().split(b"\n")[0] + b"\n")
        assert background.session_skip_reason({"transcript_path": str(fifo)}) is None
        assert background.read_first_line(fifo) is None
    finally:
        os.close(writer)
    assert time.monotonic() - started < 2


def test_include_automations_reads_the_environment():
    on = background.include_automations
    assert all(on({"REMEMBRA_RELAY_INCLUDE_AUTOMATIONS": v}) for v in ("1", "true", "YES", " on "))
    assert not any(on({"REMEMBRA_RELAY_INCLUDE_AUTOMATIONS": v}) for v in ("", "0", "false", "no"))
    assert not on({})


def test_only_adapters_that_read_codex_rollouts_skip(tmp_path):
    payload = {"session_id": "s", "transcript_path": str(THREADS / "automation.jsonl")}
    claude = get_adapter("claude-code")
    codex = get_adapter("codex")
    assert claude is not None and codex is not None
    assert background.skip_hook_session(claude, payload, "brief", tmp_path) is None
    assert background.skip_hook_session(codex, payload, "brief", tmp_path, environ={}) == "automation"
    opted_in = {"REMEMBRA_RELAY_INCLUDE_AUTOMATIONS": "1"}
    assert background.skip_hook_session(codex, payload, "brief", tmp_path, environ=opted_in) is None
    sub = {"session_id": "t", "transcript_path": str(THREADS / "subagent.jsonl")}
    assert background.skip_hook_session(codex, sub, "close", tmp_path, environ=opted_in) == "subagent"
    # The live shape: the parent's session id, the sub-agent's own thread id as agent_id.
    live = {**sub, "session_id": "parent-1", "agent_id": " sub-1 ", "agent_type": "default"}
    assert background.skip_hook_session(codex, live, "brief", tmp_path) == "subagent"
    same = {**sub, "agent_id": "t"}  # an agent_id equal to the session id adds nothing
    assert background.skip_hook_session(codex, same, "brief", tmp_path) == "subagent"
    odd = {**sub, "agent_id": 7}  # not a string: ignored
    assert background.skip_hook_session(codex, odd, "brief", tmp_path) == "subagent"
    long = {**sub, "session_id": "p" * 60, "agent_id": "a" * 60}
    assert background.skip_hook_session(codex, long, "close", tmp_path) == "subagent"
    log = outbox.log_path(tmp_path).read_text().splitlines()
    assert [line.split(" ", 2)[2] for line in log] == [
        "skipped brief: codex automation session s",
        "skipped close: codex subagent session t",
        "skipped brief: codex subagent thread sub-1 of session parent-1",
        "skipped brief: codex subagent session t",
        "skipped brief: codex subagent session t",
        f"skipped close: codex subagent thread {'a' * 40} of session {'p' * 40}",
    ]


# ---------------------------------------------------------------------------
# brief / close through cli.main, every HTTP request recorded
# ---------------------------------------------------------------------------


class Recorder:
    """Stands in for the server: records every request the CLI makes."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        with self.lock:
            self.requests.append((request.method, request.url.path, {**dict(request.url.params), **body}))
        if request.url.path == "/api/v1/session/brief":
            return httpx.Response(200, json={"rendered": BRIEF_TEXT})
        if request.url.path == "/api/v1/session/close":
            return httpx.Response(200, json={"handoff_id": "h-1", "project_id": "widget", "headline": "ok"})
        return httpx.Response(200, json={})

    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.requests]


@pytest.fixture()
def relay_env(monkeypatch, tmp_path) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_background_test")
    monkeypatch.setenv("REMEMBRA_URL", "http://relay.test")
    for var in (
        "REMEMBRA_AGENT_ID",
        "REMEMBRA_PROJECT",
        "REMEMBRA_RELAY_PROJECT",
        "REMEMBRA_SESSION_ID",
        "REMEMBRA_RELAY_INCLUDE_AUTOMATIONS",
    ):
        monkeypatch.delenv(var, raising=False)
    recorder = Recorder()

    def client(self: cli.Context, config=None, agent=None, budget=None) -> httpx.Client:
        return httpx.Client(base_url=(config or self.config).url, transport=httpx.MockTransport(recorder.handle))

    monkeypatch.setattr(cli.Context, "client", client)
    spawned: list[dict[str, Any]] = []

    def spawn(args: Any, payload: dict[str, Any]) -> bool:
        spawned.append(payload)
        return True

    monkeypatch.setattr(cli, "spawn_detached_close", spawn)
    return {"home": home, "work": work, "http": recorder, "spawned": spawned}


def _payload(env: dict[str, Any], transcript: Path, event: str, session_id: str, agent_id: str | None = None) -> str:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": str(env["work"]),
        "hook_event_name": event,
    }
    if agent_id:  # a sub-agent's hook, as live Codex sends it
        payload["agent_id"] = agent_id
        payload["agent_type"] = "default"
    if event == "SessionStart":
        payload["source"] = "startup"
    elif event == "SessionEnd":
        payload["reason"] = "other"
    elif event == "UserPromptSubmit":
        payload["prompt"] = "Run the nightly check."
    return json.dumps(payload)


def _main(monkeypatch, capsys, argv: list[str], stdin: str) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def _log(home: Path) -> list[str]:
    path = outbox.log_path(home)
    return [line.split(" ", 2)[2] for line in path.read_text().splitlines()] if path.exists() else []


def _transcript(kind: str, tmp_path: Path) -> Path:
    if kind == "huge":
        return _huge_rollout(tmp_path)
    if kind == "missing":
        return tmp_path / "rollout-missing.jsonl"
    return THREADS / f"{kind}.jsonl"


BRIEF = ["brief", "--hook", "codex", "--agent", "codex"]
# --next: the session hands something off (a close with nothing recorded sends nothing at all).
CLOSE = ["close", "--hook", "codex", "--agent", "codex", "--next", "carry on"]


@pytest.mark.parametrize(("kind", "reason"), list(SKIPPED.items()))
def test_skipped_session_brief_prints_nothing_and_sends_nothing(relay_env, monkeypatch, capsys, kind, reason):
    sid, agent = _session_id(kind), _agent_id(kind)
    stdin = _payload(relay_env, THREADS / f"{kind}.jsonl", "SessionStart", sid, agent)
    code, out, err = _main(monkeypatch, capsys, BRIEF, stdin)
    assert (code, out, err) == (0, "", "")
    assert relay_env["http"].requests == []
    assert _log(relay_env["home"]) == [_skip_line("brief", kind, reason)]
    # No session state and no "brief delivered" marker: nothing recorded for this session.
    assert not (relay_env["home"] / ".remembra" / "relay" / "sessions").exists()

    # The per-prompt hook (brief --once) skips it too.
    prompt = _payload(relay_env, THREADS / f"{kind}.jsonl", "UserPromptSubmit", sid, agent)
    assert _main(monkeypatch, capsys, [*BRIEF, "--once"], prompt) == (0, "", "")
    assert relay_env["http"].requests == []
    assert _log(relay_env["home"]) == [_skip_line("brief", kind, reason)] * 2
    assert not (relay_env["home"] / ".remembra" / "relay" / "sessions").exists()


@pytest.mark.parametrize(("kind", "reason"), list(SKIPPED.items()))
def test_skipped_session_close_sends_queues_and_detaches_nothing(relay_env, monkeypatch, capsys, kind, reason):
    sid = _session_id(kind)
    stdin = _payload(relay_env, THREADS / f"{kind}.jsonl", "SessionEnd", sid, _agent_id(kind))
    assert _main(monkeypatch, capsys, CLOSE, stdin) == (0, "", "")  # the hook as Codex runs it: detaches when it goes on
    assert relay_env["spawned"] == []
    assert _main(monkeypatch, capsys, [*CLOSE, "--foreground"], stdin) == (0, "", "")  # the detached child's path
    assert relay_env["http"].requests == []
    assert outbox.pending(relay_env["home"]) == []
    assert _log(relay_env["home"]) == [_skip_line("close", kind, reason)] * 2

    # Without a key a close is queued; a skipped one is not.
    monkeypatch.delenv("REMEMBRA_API_KEY")
    assert _main(monkeypatch, capsys, [*CLOSE, "--foreground"], stdin) == (0, "", "")
    assert outbox.pending(relay_env["home"]) == []
    code, out, err = _main(monkeypatch, capsys, [*CLOSE, "--dry-run"], stdin)
    assert (code, out) == (0, "") and f"close skipped: a codex {reason} session leaves no handoff" in err


@pytest.mark.parametrize("kind", [*NOT_SKIPPED, "missing", "huge"])
def test_other_sessions_still_get_their_brief_and_handoff(relay_env, monkeypatch, capsys, tmp_path, kind):
    sid = f"user-session-{kind}"
    transcript = _transcript(kind, tmp_path)
    code, out, err = _main(monkeypatch, capsys, BRIEF, _payload(relay_env, transcript, "SessionStart", sid))
    assert code == 0 and out.strip() == BRIEF_TEXT, err
    assert relay_env["http"].calls() == [("GET", "/api/v1/session/brief")]
    assert relay_env["http"].requests[0][2]["session_id"] == sid

    end = _payload(relay_env, transcript, "SessionEnd", sid)
    assert _main(monkeypatch, capsys, CLOSE, end)[0] == 0
    assert [p["session_id"] for p in relay_env["spawned"]] == [sid]  # handed to the detached child
    code, out, err = _main(monkeypatch, capsys, [*CLOSE, "--foreground"], end)
    assert code == 0 and out == "", err
    assert relay_env["http"].calls() == [("GET", "/api/v1/session/brief"), ("POST", "/api/v1/session/close")]
    close = relay_env["http"].requests[1][2]
    assert close["session_id"] == sid and close["agent_id"] == "codex" and close["end_reason"] == "other"
    assert not any(line.startswith("skipped") for line in _log(relay_env["home"]))


def test_include_automations_opt_out(relay_env, monkeypatch, capsys):
    monkeypatch.setenv("REMEMBRA_RELAY_INCLUDE_AUTOMATIONS", "1")
    sid = _session_id("automation")
    automation = THREADS / "automation.jsonl"
    code, out, _ = _main(monkeypatch, capsys, BRIEF, _payload(relay_env, automation, "SessionStart", sid))
    assert code == 0 and out.strip() == BRIEF_TEXT
    code, out, _ = _main(monkeypatch, capsys, [*CLOSE, "--foreground"], _payload(relay_env, automation, "SessionEnd", sid))
    assert relay_env["http"].calls() == [("GET", "/api/v1/session/brief"), ("POST", "/api/v1/session/close")]
    assert relay_env["http"].requests[1][2]["session_id"] == sid

    # Sub-agent threads stay skipped: their parent's handoff covers the work.
    sub, agent = _session_id("subagent"), _agent_id("subagent")
    subagent = THREADS / "subagent.jsonl"
    start, end = (_payload(relay_env, subagent, event, sub, agent) for event in ("SessionStart", "SessionEnd"))
    assert _main(monkeypatch, capsys, BRIEF, start) == (0, "", "")
    assert _main(monkeypatch, capsys, [*CLOSE, "--foreground"], end) == (0, "", "")
    assert len(relay_env["http"].requests) == 2
    assert _log(relay_env["home"]) == [_skip_line(verb, "subagent", "subagent") for verb in ("brief", "close")]


def test_a_sub_agent_sharing_its_parent_session_id_leaves_the_parent_alone(relay_env, monkeypatch, capsys, tmp_path):
    """Live Codex sends a sub-agent's hooks with its PARENT's session id: the skip decides by the rollout.

    The sub-agent's prompt hook is skipped and records nothing for the shared
    session id, so the parent still gets its brief; the sub-agent's close
    sends nothing and the parent's close still sends its handoff.
    """
    sub_rollout, agent = THREADS / "subagent.jsonl", _agent_id("subagent")
    assert agent is not None and _session_id("subagent") == PARENT_SESSION
    parent_rollout = tmp_path / "parent.jsonl"  # the parent: a thread someone started, with the shared id
    parent_rollout.write_text((THREADS / "user.jsonl").read_text().replace("00000000a004", PARENT_SESSION[-12:]))
    assert (background.read_session_meta(parent_rollout) or {}).get("id") == PARENT_SESSION
    sub_line = f"thread {agent} of session {PARENT_SESSION}"

    sub_prompt = _payload(relay_env, sub_rollout, "UserPromptSubmit", PARENT_SESSION, agent)
    assert _main(monkeypatch, capsys, [*BRIEF, "--once"], sub_prompt) == (0, "", "")
    assert relay_env["http"].requests == []
    assert _log(relay_env["home"]) == [f"skipped brief: codex subagent {sub_line}"]
    assert not cli.brief_delivered(relay_env["home"], "codex", PARENT_SESSION)

    parent_prompt = _payload(relay_env, parent_rollout, "UserPromptSubmit", PARENT_SESSION)
    code, out, err = _main(monkeypatch, capsys, [*BRIEF, "--once"], parent_prompt)
    assert code == 0 and out.strip() == BRIEF_TEXT, err
    assert relay_env["http"].calls() == [("GET", "/api/v1/session/brief")]
    assert relay_env["http"].requests[0][2]["session_id"] == PARENT_SESSION
    # A later sub-agent prompt in the same session: still no brief, nothing sent.
    assert _main(monkeypatch, capsys, [*BRIEF, "--once"], sub_prompt) == (0, "", "")

    sub_end = _payload(relay_env, sub_rollout, "SessionEnd", PARENT_SESSION, agent)
    assert _main(monkeypatch, capsys, CLOSE, sub_end) == (0, "", "")
    assert _main(monkeypatch, capsys, [*CLOSE, "--foreground"], sub_end) == (0, "", "")
    assert relay_env["spawned"] == [] and len(relay_env["http"].requests) == 1
    assert _log(relay_env["home"])[-2:] == [f"skipped close: codex subagent {sub_line}"] * 2

    parent_end = _payload(relay_env, parent_rollout, "SessionEnd", PARENT_SESSION)
    assert _main(monkeypatch, capsys, CLOSE, parent_end)[0] == 0
    assert [p["session_id"] for p in relay_env["spawned"]] == [PARENT_SESSION]
    code, out, err = _main(monkeypatch, capsys, [*CLOSE, "--foreground"], parent_end)
    assert code == 0 and out == "", err
    assert relay_env["http"].calls() == [("GET", "/api/v1/session/brief"), ("POST", "/api/v1/session/close")]
    assert relay_env["http"].requests[1][2]["session_id"] == PARENT_SESSION
    assert all(PARENT_SESSION not in line or sub_line in line for line in _log(relay_env["home"]))


def test_manual_close_without_a_hook_is_unaffected(relay_env, monkeypatch, capsys):
    """``remembra-relay close`` typed by hand (no --hook) sends, even given an automation's rollout."""
    work = str(relay_env["work"])
    argv = ["close", "--agent", "codex", "--cwd", work, "--transcript", str(THREADS / "automation.jsonl"), "--next", "n"]
    code, out, _ = _main(monkeypatch, capsys, argv, "")
    assert code == 0 and "Remembra handoff h-1" in out
    assert relay_env["http"].calls() == [("POST", "/api/v1/session/close")]
    assert relay_env["http"].requests[0][2]["session_id"] == _session_id("automation")  # read from the rollout
    code, out, _ = _main(monkeypatch, capsys, ["brief", "--agent", "codex", "--cwd", work], "")
    assert code == 0 and BRIEF_TEXT in out
    assert _log(relay_env["home"]) == []


# ---------------------------------------------------------------------------
# The real entry point in a subprocess, against a local HTTP server
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_server() -> Iterator[dict[str, Any]]:
    seen: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _answer(self, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            seen.append(("GET", self.path.split("?")[0]))
            self._answer({"rendered": BRIEF_TEXT} if self.path.startswith("/api/v1/session/brief") else {})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append(("POST", self.path))
            self._answer({"handoff_id": "h-1", "project_id": "widget", "headline": "ok"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_address[1]}", "seen": seen}
    finally:
        server.shutdown()
        server.server_close()


def _relay(home: Path, url: str, argv: list[str], stdin: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": SRC,
        "REMEMBRA_URL": url,
        "REMEMBRA_API_KEY": "rem_background_test",
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-m", "remembra.relay.cli", *argv], input=stdin, capture_output=True, text=True, env=env, timeout=60
    )


def _wait_for(predicate: Any, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def test_real_entry_point_skips_an_automation_and_serves_a_user_session(stub_server, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    env = {"work": work}
    url, seen = stub_server["url"], stub_server["seen"]
    auto_id, user_id = _session_id("automation"), _session_id("user")
    automation, user = THREADS / "automation.jsonl", THREADS / "user.jsonl"

    brief = _relay(home, url, BRIEF, _payload(env, automation, "SessionStart", auto_id))
    close = _relay(home, url, CLOSE, _payload(env, automation, "SessionEnd", auto_id))
    assert (brief.returncode, brief.stdout, brief.stderr) == (0, "", "")
    assert (close.returncode, close.stdout, close.stderr) == (0, "", "")
    assert not _wait_for(lambda: bool(seen), timeout=2.0), seen
    assert not (home / ".remembra" / "relay" / "last-detached-close.log").exists()  # no detached child started
    assert _log(home) == [f"skipped {verb}: codex automation session {auto_id}" for verb in ("brief", "close")]

    brief = _relay(home, url, BRIEF, _payload(env, user, "SessionStart", user_id))
    assert brief.returncode == 0 and brief.stdout.strip() == BRIEF_TEXT, brief.stderr
    close = _relay(home, url, CLOSE, _payload(env, user, "SessionEnd", user_id))
    assert close.returncode == 0 and close.stdout == ""
    assert _wait_for(lambda: ("POST", "/api/v1/session/close") in seen), seen  # sent by the detached child
    assert seen == [("GET", "/api/v1/session/brief"), ("POST", "/api/v1/session/close")]

    opted_in = _relay(
        home, url, BRIEF, _payload(env, automation, "SessionStart", auto_id), REMEMBRA_RELAY_INCLUDE_AUTOMATIONS="1"
    )
    assert opted_in.returncode == 0 and opted_in.stdout.strip() == BRIEF_TEXT
    assert seen[-1] == ("GET", "/api/v1/session/brief")
