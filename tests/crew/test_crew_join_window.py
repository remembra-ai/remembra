"""The join window (seen live 2026-09-28 against production, through the Claude Code 2.1.168 hooks).

Session A held zone ``api`` exclusively. Session B joined from its own worktree. B's turn line already
said ``DO NOT TOUCH: zone api → cc-4``, yet B's PreToolUse Edit on ``src/api/server.py`` exited 0 with
empty stdout (allow) at +0.2, +3.4, +6.5 and +9.7 s after B joined, and was denied only at +12.8 s.

Root cause: the gate knows a checkout only from the local snapshot's ``checkouts``, and a path in no
known checkout is rule 0 (allow). crewd put a new session's checkout there only as part of a successful
``GET /crews/{id}/snapshot``. The server allows that read once per 10 s per API key, agent and crew
(the ``snapshot`` bucket), which every session of that agent on the host shares; B's join came within
10 s of the previous read (A's), got 429, and ``sync_snapshot`` returned without writing anything. B's
own checkout stayed unknown until the gate's staleness refresh (snapshot older than 15 s) fetched a new
one. The turn line was right all along because it reads only the claims, which the old snapshot had.

These tests run the real vendored gate as a subprocess against a real crewd socket and the real crew
routers, with the real crew rate limiter on where the scenario needs it.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import schemas as S
from remembra.crew.limits import get_crew_rate_limiter
from remembra.relay.crew import crewd as crewd_mod
from remembra.relay.crew import gate as gate_mod
from remembra.relay.crew import outbox as O
from remembra.relay.crew.crewd import Crewd, Resp, Unreachable
from remembra.relay.crew.gate import Layout, read_json, session_key, vendor_gate, write_json
from remembra.relay.crew.snapshot import verify
from tests.crew.test_wp9_gate import arun_gate, pretool
from tests.crew.wp9_support import Server, add_worktree, crew_server, git, make_repo, new_crewd, peer


@pytest.fixture()
def sleeper() -> Iterator[int]:
    """Session A's agent: a live process outside this test's process tree (the gate runs as our child, as B)."""
    proc = subprocess.Popen(["sleep", "600"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _join(sid: str, cwd: Path, pid: int) -> dict[str, Any]:
    return {"adapter": "claude-code", "client_session_id": sid, "cwd": str(cwd), "agent_pid": pid, "source": "startup"}


async def _crewd(tmp_path: Path, srv: Server, home: str, alive: set[int]) -> tuple[Layout, Crewd]:
    layout = Layout(tmp_path / home)
    vendor_gate(layout)
    d = new_crewd(layout, srv, alive=alive)
    await d.start_server()
    return layout, d


def _zone(d: Crewd, crew_id: str, slug: str) -> dict[str, Any]:
    return next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == slug)


def _snapshot_reads(d: Crewd) -> list[int]:
    """Record the status of every snapshot read crewd makes (the real request still runs)."""
    statuses: list[int] = []
    real = d.request

    async def request(api: Any, method: str, path: str, **kw: Any) -> Resp:
        resp = await real(api, method, path, **kw)
        if method == "GET" and path.endswith("/snapshot"):
            statuses.append(resp.status)
        return resp

    d.request = request  # type: ignore[method-assign]
    return statuses


def _edit(sid: str, checkout: Path, rel: str) -> dict[str, Any]:
    return pretool(sid, checkout, "Edit", {"file_path": str(checkout / rel), "old_string": "1", "new_string": "2"})


def _decision(res: Any) -> tuple[str, str]:
    """``(decision, reason)`` of one PreToolUse run; allow is exit 0 with empty stdout."""
    assert res.returncode == 0, res.stderr
    if not res.stdout.strip():
        return "allow", ""
    assert S.validate_hook_stdout("PreToolUse", res.stdout) == [], res.stdout
    out = json.loads(res.stdout)["hookSpecificOutput"]
    return str(out.get("permissionDecision") or "allow"), str(out.get("permissionDecisionReason") or "")


async def _a_holds_pos(d: Crewd, repo: Path, holder_pid: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """A joins in ``repo`` and holds ``pos`` exclusively; the local snapshot shows the hold (as live)."""
    a = await d.join(peer(holder_pid), _join("sess-a", repo, holder_pid))
    assert a["ok"], a
    pos = _zone(d, a["crew_id"], "pos")
    assert (await d.claim(peer(holder_pid), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
    await d.drain()
    return a, pos


async def test_first_write_after_join_is_denied_while_the_snapshot_read_is_rate_limited(tmp_path, sleeper):
    """The live sequence: A holds pos and the snapshot shows it; B joins inside the 10 s snapshot window."""
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path, rate_limits=True) as srv:
        layout, d = await _crewd(tmp_path, srv, "home", {sleeper})
        reads = _snapshot_reads(d)
        a, pos = await _a_holds_pos(d, repo, sleeper)
        crew_id = a["crew_id"]
        get_crew_rate_limiter().reset()  # 10 s later: the next read shows A's hold (A's own join read did, live)
        assert await d.sync_snapshot(crew_id)
        before = read_json(layout.snapshot_file(crew_id))
        assert any(c["zone_id"] == pos["id"] and c["holder_session_id"] == a["session_id"] for c in before["claims"])

        # B joins from its own worktree at once: the snapshot read of its join is refused (1 per 10 s)
        b = await d.join(peer(me), _join("sess-b", wt_b, me))
        assert b["ok"], b
        assert reads[-1] == 429, reads  # the production condition, not a vacuous pass
        after_join = read_json(layout.snapshot_file(crew_id))

        # B's turn line names A's hold (it did live too) ...
        turn = await arun_gate(
            layout,
            "turn",
            {"session_id": "sess-b", "cwd": str(wt_b), "hook_event_name": "UserPromptSubmit", "prompt": "edit split.ts"},
            cwd=wt_b,
        )
        assert "DO NOT TOUCH: zone pos" in turn.stdout, turn.stdout
        # ... and B's very first PreToolUse in that zone is denied, naming the holder
        decision, reason = _decision(await arun_gate(layout, "pretool", _edit("sess-b", wt_b, "src/app/pos/split.ts"), cwd=wt_b))
        assert decision == "deny", reason
        assert 'zone "pos"' in reason and a["callsign"] in reason
        # a path nobody holds stays allowed (B auto-claims it)
        decision, reason = _decision(
            await arun_gate(layout, "pretool", _edit("sess-b", wt_b, "src/app/reports/export.ts"), cwd=wt_b)
        )
        assert decision == "allow", reason
        # why: the join put B's checkout into the local snapshot although the server read failed; the
        # server part and its age are still the last successful read's, so the gate's refresh rules hold
        snap = after_join
        assert snap is not None
        assert sorted((c["toplevel"], c["session_id"]) for c in snap["checkouts"]) == sorted(
            [(str(repo), a["session_id"]), (str(wt_b), b["session_id"])]
        )
        assert snap["synced_at"] == before["synced_at"] and snap["skew_s"] == before["skew_s"]
        assert verify(snap, d.hmac_key)


@pytest.mark.parametrize("failure", ["unreachable", "server_error"])
async def test_join_publishes_its_checkout_when_the_snapshot_read_fails(tmp_path, sleeper, monkeypatch, failure):
    """A server that times out or fails on the snapshot read right after the join: the same rule holds."""
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _crewd(tmp_path, srv, "home", {sleeper})
        a, _pos = await _a_holds_pos(d, repo, sleeper)
        assert await d.sync_snapshot(a["crew_id"])
        real_call = crewd_mod.Api.call

        async def call(self: Any, method: str, path: str, **kw: Any) -> Resp:
            if method == "GET" and path.endswith("/snapshot"):
                if failure == "unreachable":
                    raise Unreachable(f"GET {path}: TimeoutError")
                return Resp(503, {"detail": {"error": "unavailable"}}, {})
            return await real_call(self, method, path, **kw)

        monkeypatch.setattr(crewd_mod.Api, "call", call)
        b = await d.join(peer(me), _join("sess-b", wt_b, me))
        assert b["ok"], b
        snap = read_json(layout.snapshot_file(a["crew_id"]))
        assert str(wt_b) in {c["toplevel"] for c in snap["checkouts"]}
        decision, reason = _decision(await arun_gate(layout, "pretool", _edit("sess-b", wt_b, "src/app/pos/split.ts"), cwd=wt_b))
        assert decision == "deny", reason
        assert a["callsign"] in reason
        # the local arbiter grants a free zone while the server cannot be read
        decision, reason = _decision(
            await arun_gate(layout, "pretool", _edit("sess-b", wt_b, "src/app/reports/export.ts"), cwd=wt_b)
        )
        assert decision == "allow", reason


async def test_an_advisory_session_joining_in_the_window_is_fenced_at_once(tmp_path, sleeper):
    """An agent without a pre-write hook (Codex, advisory) is held back by the read-only fence, which the
    join must apply from what is known locally too, not only after a successful server read."""
    repo = make_repo(tmp_path / "repo")
    wt_x = add_worktree(repo, tmp_path / "wt-codex", "codex-work")
    async with crew_server(tmp_path, rate_limits=True) as srv:
        layout = Layout(tmp_path / "home")
        d = new_crewd(layout, srv, alive={sleeper, 51002})
        reads = _snapshot_reads(d)
        a, _pos = await _a_holds_pos(d, repo, sleeper)
        get_crew_rate_limiter().reset()
        assert await d.sync_snapshot(a["crew_id"])
        x = await d.join(peer(51002), {**_join("sess-x", wt_x, 51002), "adapter": "codex", "agent_id": "codex"})
        assert x["ok"] and reads[-1] == 429, (x, reads)
        assert d.sessions[x["key"]]["enforcement"] == "advisory"
        target = wt_x / "src/app/pos/split.ts"
        assert stat.S_IMODE(target.stat().st_mode) & 0o222 == 0, "A's zone is not fenced in the advisory checkout"
        assert os.access(wt_x / "src/app/reports/export.ts", os.W_OK)
        assert os.access(repo / "src/app/pos/split.ts", os.W_OK)  # the holder's own checkout is never fenced
        d.fence.restore(str(wt_x))


async def test_gate_uses_the_session_file_when_the_snapshot_does_not_list_its_checkout(tmp_path, sleeper):
    """A snapshot written before the new checkout was published (an older crewd, or crewd stopped between
    writing the session file and the snapshot): the caller's own checkout still is not "outside"."""
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _crewd(tmp_path, srv, "home", {sleeper})
        a, _pos = await _a_holds_pos(d, repo, sleeper)
        await d.join(peer(me), _join("sess-b", wt_b, me))
        crew_id = a["crew_id"]
        path = layout.snapshot_file(crew_id)
        snap = read_json(path)
        assert snap is not None
        snap["checkouts"] = [c for c in snap["checkouts"] if c["toplevel"] != str(wt_b)]
        write_json(path, snap)

        decision, reason = _decision(await arun_gate(layout, "pretool", _edit("sess-b", wt_b, "src/app/pos/split.ts"), cwd=wt_b))
        assert decision == "deny", reason
        assert a["callsign"] in reason
        decision, reason = _decision(
            await arun_gate(layout, "pretool", _edit("sess-b", wt_b, "src/app/reports/export.ts"), cwd=wt_b)
        )
        assert decision == "allow", reason
        # the commit gate reads the same snapshot: a commit into A's zone from B's checkout is refused
        (wt_b / "src/app/pos/split.ts").write_text("export const split = 3;\n")
        git(wt_b, "add", "src/app/pos/split.ts")
        result = await asyncio.to_thread(gate_mod.git_gate_precommit, layout, str(wt_b))
        assert result.allowed is False and a["callsign"] in (result.reason or ""), result


async def test_first_session_on_a_host_without_a_snapshot_asks_the_server(tmp_path, sleeper):
    """B is this host's first session of the crew and its join's snapshot read is refused: there is no
    local snapshot at all. The gate asks the server's guard through crewd within its deadline."""
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path, rate_limits=True) as srv:
        layout_a, da = await _crewd(tmp_path, srv, "host-a", {sleeper})
        a, _pos = await _a_holds_pos(da, repo, sleeper)
        crew_id = a["crew_id"]
        get_crew_rate_limiter().reset()
        assert await da.sync_snapshot(crew_id)
        # another host, same key and agent: the same snapshot bucket
        layout_b, db = await _crewd(tmp_path, srv, "host-b", set())
        reads = _snapshot_reads(db)
        b = await db.join(peer(me), _join("sess-b", wt_b, me))
        assert b["ok"] and b["crew_id"] == crew_id, b
        assert reads and set(reads) == {429}, reads
        assert not layout_b.snapshot_file(crew_id).exists()

        decision, reason = _decision(
            await arun_gate(layout_b, "pretool", _edit("sess-b", wt_b, "src/app/pos/split.ts"), cwd=wt_b)
        )
        assert decision == "deny", reason
        assert a["callsign"] in reason
        decision, reason = _decision(
            await arun_gate(layout_b, "pretool", _edit("sess-b", wt_b, "src/app/reports/export.ts"), cwd=wt_b)
        )
        assert decision == "allow", reason
        held = await srv.rows(
            "SELECT holder_session_id FROM crew_claims WHERE state = 'active' AND holder_session_id = ?", (b["session_id"],)
        )
        assert held, "the server guard auto-claims the free zone for B, as the local gate would"


async def test_no_snapshot_and_no_crewd_still_allows(tmp_path):
    """Nothing to ask (crewd down) and nothing known: the write is allowed and recorded (never brick the agent)."""
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _crewd(tmp_path, srv, "home", set())
        a = await d.join(peer(me), _join("sess-a", repo, me))
        await d.drain()
        await d.shutdown()
        srv.daemons.remove(d)
        layout.snapshot_file(a["crew_id"]).unlink()
        write_json(layout.crewd_cmd, {"argv": ["/usr/bin/true"]})  # no real crewd may be respawned by this test
        res = await arun_gate(layout, "pretool", _edit("sess-a", repo, "src/app/pos/split.ts"), cwd=repo)
        assert (res.returncode, res.stdout) == (0, ""), res.stdout
        assert "no crew snapshot yet" in res.stderr
        spooled = [e.body for e in O.read_entries(layout.outbox, ("event",))]
        assert any(e["type"] == "gate.deadline" and e["payload"]["stage"] == "no_snapshot" for e in spooled), spooled
        assert read_json(layout.session_file(session_key("claude-code", "sess-a")))["session_id"] == a["session_id"]


async def test_a_refused_snapshot_read_is_retried_when_the_server_allows(tmp_path):
    """429 carries Retry-After: crewd reads again then, instead of waiting for a stale gate or the heartbeat."""
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout = Layout(tmp_path / "home")
        d = new_crewd(layout, srv)
        a = await d.join(peer(me), _join("sess-a", repo, me))
        crew_id = a["crew_id"]
        real = d.request
        refused: list[str] = []

        async def request(api: Any, method: str, path: str, **kw: Any) -> Resp:
            if method == "GET" and path.endswith("/snapshot") and not refused:
                refused.append(path)
                body = {"detail": {"error": "rate_limited", "retry_after_s": 1}}
                return Resp(429, body, {"retry-after": "1"})
            return await real(api, method, path, **kw)

        d.request = request  # type: ignore[method-assign]
        before = d.snapshots[crew_id]["synced_at"]
        await asyncio.sleep(0.01)
        assert await d.sync_snapshot(crew_id) is False and refused
        for _ in range(60):
            if d.snapshots[crew_id]["synced_at"] != before:
                break
            await asyncio.sleep(0.05)
        assert d.snapshots[crew_id]["synced_at"] != before, "crewd did not read again after Retry-After"
        assert read_json(layout.snapshot_file(crew_id))["synced_at"] == d.snapshots[crew_id]["synced_at"]
        # a retry still waiting is cancelled at shutdown (nothing left running)
        refused.clear()
        assert await d.sync_snapshot(crew_id) is False
        pending = list(d.resyncs.values())
        assert pending
        await d.shutdown()
        srv.daemons.remove(d)
        await asyncio.sleep(0)
        assert all(t.done() for t in pending)
