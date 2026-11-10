"""F11: an MCP crew seat must survive the MCP server process, and a crew call must not kill that process.

Three parts of the fault, each through the real MCP server module and a live API (``tests/crew/mcp_support.py``):

* **Long-polls.** ``crew_say(wait_s=60…120)`` and ``crew_claim(wait_s=60…300)`` held one MCP request past the
  client's 60 s request timeout; the desktop app then replaced the stdio server, which lost every seat.
* **Seat identity.** The session token lived in process memory only, so a restarted server joined as a new
  callsign while the old seat's claims stayed reserved for a dead session.
* **Looking is joining.** ``crew_status`` had no way to look at a crew without taking a seat in it.

A "restart" here is a fresh ``CrewBridge`` with a fresh ``SeatStore`` on the same directory and a new process
session id. The old process's death is ``SeatStore.close()`` (the operating system drops its file locks); one
test does it with a real child process that is killed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.crew import schemas as S
from remembra.mcp import crew as crew_mod
from remembra.mcp.crew import CrewBridge
from tests.crew.mcp_support import live_server
from tests.crew.test_mcp_crew import (
    OWNER_EMAIL,
    Clock,
    _two_agents,
    add_zone,
    agent,
    as_json,
    assert_clean,
    call,
    callsign_in,
    crew_id_of,
    project_name,
    session_row,
    use,
)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-mcp-seat")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=c))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield c
    server._client = None


class Wall:
    """Wall-clock time for the seat store (seconds since the epoch)."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def wall() -> Wall:
    return Wall()


@pytest.fixture()
def seats(tmp_path, clock, wall, monkeypatch):
    """``start(anchor="")`` begins a server process on one seat directory; ``kill()`` ends the running one."""
    from remembra.mcp.seats import SeatStore

    root = tmp_path / "seats"
    stores: list[Any] = []

    def start(anchor: str = "") -> CrewBridge:
        bridge = CrewBridge(clock=clock)
        bridge.seat_store = SeatStore(root, anchor=anchor, now=wall)
        stores.append(bridge.seat_store)
        monkeypatch.setattr(server, "_crew", bridge)
        monkeypatch.setattr(server, "_session_projects", {})
        return bridge

    def kill() -> None:
        stores[-1].close()

    yield type("Seats", (), {"start": staticmethod(start), "kill": staticmethod(kill), "root": root})
    for store in stores:
        store.close()


def rows(live, project: str) -> list[dict[str, Any]]:
    return live.crew_rows("SELECT * FROM crew_sessions WHERE crew_id = ? ORDER BY joined_at", (crew_id_of(live, project),))


def joins(live, project: str) -> int:
    crew = crew_id_of(live, project)
    return len(live.crew_rows("SELECT seq FROM crew_events WHERE crew_id = ? AND type = 'session.joined'", (crew,)))


def stored(root: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))]


# ---------------------------------------------------------------------------
# The trigger: a long-poll past the MCP client's request timeout
# ---------------------------------------------------------------------------


def test_crew_say_wait_is_cut_below_the_client_timeout_and_says_so(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "saycut")
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock, max_wait_s=1))
    use(b)
    started = time.monotonic()
    text = call("crew_say", body="anyone?", kind="question", wait_s=8)
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"the call held the MCP request for {elapsed:.1f}s"
    assert text.startswith("SENT msg_"), text
    assert "no reply yet (waited 1s" in text, text
    assert "cut to 1s" in text and "crew_status" in text, text
    assert_clean(text)
    # a wait inside the cap is passed on unchanged
    short = call("crew_say", body="again?", kind="question", wait_s=1)
    assert "no reply yet (waited 1s)" in short and "cut to" not in short, short


def test_crew_claim_wait_is_cut_and_the_claim_stays_queued(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "claimcut")
    use(a)
    assert call("crew_claim", zone="billing").startswith("GRANTED zone billing")
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock, max_wait_s=1))
    use(b)
    started = time.monotonic()
    text = call("crew_claim", zone="billing", wait_s=8)
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"the call held the MCP request for {elapsed:.1f}s"
    assert text.startswith("QUEUED zone billing (waited"), text
    assert "cut to 1s" in text, text
    crew = crew_id_of(live, project)
    assert len(live.crew_rows("SELECT id FROM crew_claims WHERE crew_id = ? AND state = 'queued'", (crew,))) == 1


def test_one_wait_budget_covers_every_target_of_a_claim(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "budget")
    use(a)
    call("crew_claim", zone="billing")
    call("crew_claim", zone="pos")
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock, max_wait_s=2))
    use(b)
    started = time.monotonic()
    text = call("crew_claim", paths=["src/app/billing/invoice.ts", "src/app/pos/cart.ts"], wait_s=2)
    elapsed = time.monotonic() - started
    assert text.count("QUEUED") == 2, text
    assert elapsed < 3.5, f"two targets waited {elapsed:.1f}s on a 2s budget"


def test_the_wait_cap_defaults_under_sixty_seconds_and_reads_the_environment():
    assert 0 < crew_mod.MAX_WAIT_S < 60 - crew_mod.HTTP_GRACE_S + 1
    assert CrewBridge().max_wait_s == crew_mod.MAX_WAIT_S
    assert server._max_wait_s(None) == crew_mod.MAX_WAIT_S
    assert server._max_wait_s("") == crew_mod.MAX_WAIT_S
    assert server._max_wait_s("not a number") == crew_mod.MAX_WAIT_S
    assert server._max_wait_s("90") == 90
    assert server._max_wait_s("0") == 1 and server._max_wait_s("100000") == S.CLAIM_WAIT_MAX_S


# ---------------------------------------------------------------------------
# Seat identity across a restart
# ---------------------------------------------------------------------------


def test_a_restarted_stdio_process_resumes_its_seat_and_its_claims(live, seats):
    project = project_name("resume")
    seats.start()
    use(agent(live, "claude-desktop", "process-one", project))
    first = call("crew_status", project_id=project)
    me = callsign_in(first)
    assert "RESUMED" not in first
    add_zone(live, project, "pos", "src/app/pos/**")
    assert call("crew_claim", zone="pos").startswith("GRANTED zone pos")
    seats.kill()  # the client timed out and replaced the server process
    seats.start()
    use(agent(live, "claude-desktop", "process-two", project))  # a new process: a new random session id
    again = call("crew_status", project_id=project)
    assert callsign_in(again) == me, again
    # no conversation id reaches a desktop app's server: the answer says whose seat this is and what to do
    assert "RESUMED the seat this agent's previous MCP server process held on this machine" in again, again
    assert "holding zone pos" in again, again
    assert_clean(again)
    sessions = rows(live, project)
    assert [(r["callsign"], r["session_id"], r["state"]) for r in sessions] == [(me, "process-one", "active")]
    assert joins(live, project) == 1
    crew = crew_id_of(live, project)
    claims = live.crew_rows("SELECT holder_session_id, state FROM crew_claims WHERE crew_id = ?", (crew,))
    assert claims == [{"holder_session_id": sessions[0]["id"], "state": "active"}]
    # the resumed seat works: it can release what it held, and nothing collides with a previous callsign
    assert call("crew_claim", action="release", zone="pos").startswith("RELEASED zone pos")
    assert "COLLISIONS" not in call("crew_checkpoint", files_changed=["src/app/pos/cart.ts"])


def test_a_write_as_the_first_call_after_a_restart_resumes_too(live, seats):
    project = project_name("resumewrite")
    seats.start()
    use(agent(live, "codex", "w-one", project))
    me = callsign_in(call("crew_status", project_id=project))
    seats.kill()
    seats.start()
    use(agent(live, "codex", "w-two", project))
    said = call("crew_say", body="back")
    assert said.startswith("SENT msg_") and said.endswith(f"(in CREW {project} as {me})"), said
    assert len(rows(live, project)) == 1


def test_a_live_process_is_never_taken_over(live, seats):
    project = project_name("liveowner")
    seats.start()
    use(agent(live, "codex", "thread-one", project))
    first = callsign_in(call("crew_status", project_id=project))
    seats.start()  # a second server process while the first still runs (another Codex thread)
    use(agent(live, "codex", "thread-two", project))
    second_text = call("crew_status", project_id=project)
    second = callsign_in(second_text)
    assert second != first and "RESUMED" not in second_text
    assert sorted(r["session_id"] for r in rows(live, project)) == ["thread-one", "thread-two"]
    assert len(stored(seats.root)) == 2


def test_a_real_process_holding_a_seat_blocks_takeover_until_it_dies(tmp_path, wall):
    from remembra.mcp.seats import SeatStore

    root = tmp_path / "seats"
    code = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from remembra.mcp.seats import SeatRecord, SeatStore
        store = SeatStore(Path(sys.argv[1]), now=lambda: float(sys.argv[2]))
        store.save(SeatRecord(pool="pool", anchor="", project_id="p", agent_id="codex", client_session_id="child",
                              crew_id="crw_1", session_id="cs_1", callsign="codex-1", token="rcs_child", verified=False))
        print("held", flush=True)
        time.sleep(60)
        """
    )
    env = {**os.environ, "PYTHONPATH": str(Path(crew_mod.__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen([sys.executable, "-c", code, str(root), str(wall.now)], stdout=subprocess.PIPE, env=env, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        mine = SeatStore(root, now=wall)
        assert list(mine.take("pool")) == []  # its owner is alive
        assert len(stored(root)) == 1
        child.kill()
        child.wait(10)
        taken = list(mine.take("pool"))
        assert [(r.client_session_id, r.token) for r in taken] == [("child", "rcs_child")]
        other = SeatStore(root, now=wall)
        assert list(other.take("pool")) == []  # and now this process owns it
        mine.close()
        other.close()
    finally:
        child.kill()
        child.wait(10)
        if child.stdout is not None:
            child.stdout.close()


def test_an_unanchored_seat_is_not_resumed_once_the_server_would_have_marked_it_lost(live, seats, wall):
    project = project_name("stale")
    seats.start()
    use(agent(live, "codex", "stale-one", project))
    first = callsign_in(call("crew_status", project_id=project))
    seats.kill()
    wall.now += 56 * 60
    seats.start()
    use(agent(live, "codex", "stale-two", project))
    text = call("crew_status", project_id=project)
    assert callsign_in(text) != first and "RESUMED" not in text
    assert [r["client_session_id"] for r in stored(seats.root)] == ["stale-two"]  # the stale record is gone


def test_a_fixed_session_id_resumes_its_own_seat_hours_later_and_no_other(live, seats, wall):
    project = project_name("anchored")
    seats.start(anchor="id:conversation-1")
    use(agent(live, "claude-code", "conversation-1", project))
    first = callsign_in(call("crew_status", project_id=project))
    seats.kill()
    wall.now += 3 * 3600
    seats.start(anchor="id:conversation-2")  # another conversation of the same agent
    use(agent(live, "claude-code", "conversation-2", project))
    other = call("crew_status", project_id=project)
    assert callsign_in(other) != first and "RESUMED" not in other
    seats.kill()
    seats.start(anchor="id:conversation-1")  # the first conversation's server comes back
    use(agent(live, "claude-code", "conversation-1", project))
    back = call("crew_status", project_id=project)
    assert callsign_in(back) == first and "RESUMED after an MCP server restart (this conversation's seat" in back, back
    assert sorted(r["session_id"] for r in rows(live, project)) == ["conversation-1", "conversation-2"]  # no ".r" seat


def test_a_seat_is_resumed_only_for_the_same_server_key_agent_and_project(live, seats):
    project, other_project = project_name("pool"), project_name("poolother")
    seats.start()
    use(agent(live, "codex", "pool-one", project))
    first = callsign_in(call("crew_status", project_id=project))
    seats.kill()
    seats.start()
    use(agent(live, "claude-desktop", "pool-two", project))  # another agent
    assert "RESUMED" not in call("crew_status", project_id=project)
    other_key = live.key(live.user(f"{project}@example.com"), "admin")
    use(agent(live, "codex", "pool-three", project, key=other_key))  # another account
    assert "RESUMED" not in call("crew_status", project_id=project)
    use(agent(live, "codex", "pool-four", other_project))  # another project
    assert "RESUMED" not in call("crew_status", project_id=other_project)
    use(agent(live, "codex", "pool-five", project))  # the same agent, key and project
    back = call("crew_status", project_id=project)
    assert callsign_in(back) == first and "RESUMED" in back, back


def test_a_stored_token_the_server_refuses_falls_back_to_a_fresh_seat(live, seats):
    project = project_name("refused")
    seats.start()
    use(agent(live, "codex", "refused-one", project))
    first = callsign_in(call("crew_status", project_id=project))
    seats.kill()
    for path in seats.root.glob("*.json"):
        record = json.loads(path.read_text())
        record["token"] = "rcs_not_the_token"
        path.write_text(json.dumps(record))
    (seats.root / "garbage.json").write_text("{not json")
    seats.start()
    use(agent(live, "codex", "refused-two", project))
    text = call("crew_status", project_id=project)
    assert callsign_in(text) != first and "RESUMED" not in text, text
    assert [r["client_session_id"] for r in stored_ok(seats.root)] == ["refused-two"]


def stored_ok(root: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(root.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def test_close_session_forgets_the_stored_seat(live, seats):
    project = project_name("closed")
    seats.start()
    use(agent(live, "codex", "closed-one", project))
    first = callsign_in(call("crew_status", project_id=project))
    assert len(stored(seats.root)) == 1
    closed = as_json(call("close_session", end_reason="done", project_id=project))
    assert closed["crew"]["session_left"] is True
    assert stored(seats.root) == []
    seats.kill()
    seats.start()
    use(agent(live, "codex", "closed-two", project))
    text = call("crew_status", project_id=project)
    assert "RESUMED" not in text and callsign_in(text) != first
    assert session_row(live, project, first)["state"] == "ended"


def test_the_seat_file_is_private_and_the_token_never_reaches_the_agent(live, seats):
    project = project_name("private")
    seats.start()
    use(agent(live, "codex", "private-one", project))
    text = call("crew_status", project_id=project)
    assert "rcs_" not in text
    assert stat.S_IMODE(seats.root.stat().st_mode) == 0o700
    files = list(seats.root.glob("*.json"))
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600
    record = json.loads(files[0].read_text())
    assert record["token"].startswith("rcs_") and record["callsign"] == callsign_in(text)
    assert live.state["key"] not in files[0].read_text()  # the API key is never written


def test_without_a_seat_store_a_restart_still_joins_as_a_new_seat(live, clock, monkeypatch):
    """Remote transports and in-process use keep the old behaviour: tokens stay in memory only."""
    project = project_name("nostore")
    assert CrewBridge().seat_store is None
    use(agent(live, "codex", "nostore-one", project))
    first = callsign_in(call("crew_status", project_id=project))
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock))
    use(agent(live, "codex", "nostore-two", project))
    assert callsign_in(call("crew_status", project_id=project)) != first


def test_the_stdio_server_enables_the_store_under_the_home_directory(tmp_path, monkeypatch):
    from remembra.mcp.seats import SeatStore

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.delenv("REMEMBRA_MCP_SEAT_STORE", raising=False)
    monkeypatch.delenv("REMEMBRA_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    server._enable_seat_store("streamable-http")
    assert server._crew.seat_store is None  # a remote server holds other people's sessions: nothing on disk
    server._enable_seat_store("stdio")
    store = server._crew.seat_store
    assert isinstance(store, SeatStore) and store.root == tmp_path / ".remembra" / "mcp" / "seats" and store.anchor == ""
    monkeypatch.setenv("REMEMBRA_SESSION_ID", "fixed")
    server._enable_seat_store("stdio")
    assert server._crew.seat_store.anchor == "id:fixed"
    monkeypatch.delenv("REMEMBRA_SESSION_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "conv-9")
    server._enable_seat_store("stdio")
    assert server._crew.seat_store.anchor == "conversation:conv-9"
    monkeypatch.setenv("REMEMBRA_MCP_SEAT_STORE", "0")
    server._enable_seat_store("stdio")
    assert server._crew.seat_store is None


# ---------------------------------------------------------------------------
# Looking without joining
# ---------------------------------------------------------------------------


def test_crew_status_join_false_shows_the_crew_without_taking_a_seat(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "lookonly")
    use(a)
    assert call("crew_claim", zone="billing").startswith("GRANTED zone billing")
    before = rows(live, project)
    events = joins(live, project)
    use(agent(live, "gemini", "auditor", "configured-default"))
    text = call("crew_status", project_id=project, join=False)
    assert text.startswith(f"CREW {project} "), text
    assert a_sign in text and "billing" in text, text
    assert "NOT JOINED" in text and "YOU: " not in text, text
    assert_clean(text)
    assert rows(live, project) == before and joins(live, project) == events
    # it does not become the current project either: the next crew call is not routed there
    assert server._crew.pointed_projects(server._crew_caller()) == []
    # a session that already has a seat gets its YOU line, and still makes no join
    use(a)
    seen = session_row(live, project, a_sign)["last_seen_at"]
    time.sleep(1.1)
    mine = call("crew_status", project_id=project, join=False)
    assert f"YOU: {a_sign} (" in mine and "holding zone billing" in mine, mine
    assert session_row(live, project, a_sign)["last_seen_at"] == seen and joins(live, project) == events


def test_crew_status_join_is_in_the_contract():
    spec = next(t for t in S.MCP_TOOLS if t.name == "crew_status")
    param = next(p for p in spec.params if p.name == "join")
    assert (param.type, param.default, param.required) == ("bool", True, False)
    assert S.validate_mcp_call("crew_status", {"join": False}) == []
    assert S.validate_mcp_call("crew_status", {"join": "no"}) == ["join: expected bool"]


# ---------------------------------------------------------------------------
# The real server process over stdio, killed and replaced as a desktop app does
# ---------------------------------------------------------------------------


class StdioServer:
    """``python -m remembra.mcp.server`` on stdio, spoken to in JSON-RPC lines."""

    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "remembra.mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=cwd,
        )
        self._id = 0
        init = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        self._request("initialize", init)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, body: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(body) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            assert line, "the MCP server closed its stdout"
            message = json.loads(line)  # stdout carries JSON-RPC only
            if message.get("id") == self._id:
                return dict(message)

    def tool(self, name: str, **arguments: Any) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})["result"]
        return str(result["content"][0]["text"])

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(10)
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe is not None:
                pipe.close()


def test_a_killed_stdio_server_is_replaced_by_one_with_the_same_callsign(live, tmp_path):
    project = project_name("stdio")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "CLAUDE_CODE_"))},
        "PYTHONPATH": str(Path(crew_mod.__file__).resolve().parents[2]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(home),
        "REMEMBRA_URL": live.url,
        "REMEMBRA_API_KEY": live.state["key"],
        "REMEMBRA_AGENT_ID": "claude-desktop",
        "REMEMBRA_PROJECT": project,
        "REMEMBRA_MCP_TRANSPORT": "stdio",
    }
    first = StdioServer(env, tmp_path)
    try:
        status = first.tool("crew_status", project_id=project)
        me = callsign_in(status)
        add_zone(live, project, "pos", "src/app/pos/**")
        assert first.tool("crew_claim", zone="pos", project_id=project).startswith("GRANTED zone pos")
    finally:
        first.kill()  # what the desktop app does to the server after a request times out
    seat_dir = home / ".remembra" / "mcp" / "seats"
    assert len(list(seat_dir.glob("*.json"))) == 1
    second = StdioServer(env, tmp_path)
    try:
        again = second.tool("crew_status", project_id=project)
        assert callsign_in(again) == me and "RESUMED" in again and "holding zone pos" in again, again
        assert "rcs_" not in again
        released = second.tool("crew_claim", action="release", zone="pos", project_id=project)
        assert released.startswith("RELEASED zone pos"), released
    finally:
        second.kill()
    assert [(r["callsign"], r["state"]) for r in rows(live, project)] == [(me, "active")]
    assert joins(live, project) == 1
    # with the store switched off, the next process is a new seat, as before
    third = StdioServer({**env, "REMEMBRA_MCP_SEAT_STORE": "0"}, tmp_path)
    try:
        fresh = third.tool("crew_status", project_id=project)
        assert callsign_in(fresh) != me and "RESUMED" not in fresh, fresh
    finally:
        third.kill()
