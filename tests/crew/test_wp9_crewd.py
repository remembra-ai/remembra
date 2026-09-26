"""WP-9 crewd against the real crew and relay routers (real crew.db, real event log, auth on, real API key).

Covers: host registration and join (session file without a token, token only under keys/),
snapshot writer (checkouts, settings, HMAC), zones.yml upload from the default branch,
auto-claim with the server as authority, heartbeat (leases, deltas, footprints), checkpoints,
the git delta with attribution, stall with a baton ref, SessionEnd with a baton ref and leave,
orphan detection, adopt with work restore, the local arbiter while the server is unreachable
with replay on reconnect, the outbox, and peer authentication.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from remembra.crew import schemas as S
from remembra.relay.crew import baton as B
from remembra.relay.crew import outbox as O
from remembra.relay.crew.crewd import Unreachable
from remembra.relay.crew.gate import Layout, read_json
from remembra.relay.crew.snapshot import verify
from tests.crew.wp9_support import add_worktree, crew_server, git, make_repo, new_crewd, peer

A_PID, B_PID, C_PID = 41001, 41002, 41003


def _join_args(sid: str, cwd: Path, pid: int, adapter: str = "claude-code") -> dict:
    return {
        "adapter": adapter,
        "agent_id": adapter,
        "client_session_id": sid,
        "cwd": str(cwd),
        "agent_pid": pid,
        "source": "startup",
    }


async def test_join_writes_session_file_snapshot_and_uploads_zones(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        res = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        assert res["ok"], res
        crew_id = res["crew_id"]
        # session file: identity and checkout, never a token
        sess = read_json(layout.session_file(res["key"]))
        assert sess["session_id"] == res["session_id"] and sess["toplevel"] == str(repo)
        raw = layout.session_file(res["key"]).read_text()
        token = (layout.keys / f"{res['key']}.token").read_text()
        assert token and token not in raw
        assert stat.S_IMODE((layout.keys / f"{res['key']}.token").stat().st_mode) == 0o600
        # the zones.yml committed on main was uploaded and is in the snapshot
        snap = read_json(layout.snapshot_file(crew_id))
        assert {z["slug"] for z in snap["zones"]} >= {"pos", "reports"}
        assert snap["checkouts"] == [
            {
                "toplevel": str(repo),
                "worktree_id": sess["worktree_id"],
                "git_common_dir": sess["git_common_dir"],
                "case_insensitive": sess["case_insensitive"],
                "session_id": res["session_id"],
                "default_branch": "main",
            }
        ]
        assert snap["settings"]["enforcement"] == "enforce"
        assert verify(snap, d.hmac_key)
        assert not S.validate(snap, S.LOCAL_SNAPSHOT), S.validate(snap, S.LOCAL_SNAPSHOT)
        # a snapshot rewritten by someone else is detected and restored by the integrity check
        tampered = {**snap, "claims": []}
        layout.snapshot_file(crew_id).write_text(json.dumps(tampered))
        d.check_gate()
        assert read_json(layout.snapshot_file(crew_id))["hmac"] == snap["hmac"]
        # host registered once, host token only in keys/
        hosts = read_json(layout.keys / "hosts.json")
        assert len(hosts) == 1
        types = await srv.event_types(crew_id)
        assert "session.joined" in types and "zone.synced" in types


async def test_auto_claim_heartbeat_checkpoint_and_attribution(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID, B_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        b = await d.join(peer(B_PID), _join_args("sess-b", wt_b, B_PID))
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        # A auto-claims pos (server authority); B's auto-claim conflicts and names A
        got = await d.claim(peer(A_PID), {"key": a["key"], "zone_id": pos["id"], "mode": "exclusive"})
        assert got["result"] == "granted", got
        lost = await d.claim(peer(B_PID), {"key": b["key"], "zone_id": pos["id"], "mode": "exclusive"})
        assert lost["result"] == "conflict" and lost["winner_session_id"] == a["session_id"]
        # a peer outside B's process tree cannot claim as B
        denied = await d.claim(peer(A_PID), {"key": b["key"], "zone_id": pos["id"]})
        assert denied["ok"] is False and denied["error"] == "not_your_session"
        # activity: A edits split.ts (certain footprint); heartbeat renews the lease and sends it
        (repo / "src/app/pos/split.ts").write_text("export const split = 2;\n")
        await d.activity(peer(A_PID), {"key": a["key"], "phase": "start", "tool": "Edit", "paths": ["src/app/pos/split.ts"]})
        await d.activity(peer(A_PID), {"key": a["key"], "phase": "end", "tool": "Edit", "paths": ["src/app/pos/split.ts"]})
        results = await d.heartbeat()
        assert set(results.values()) == {"ok"}
        fps = await srv.rows("SELECT path, attribution, state FROM crew_footprints WHERE session_id = ?", (a["session_id"],))
        assert fps == [{"path": "src/app/pos/split.ts", "attribution": "certain", "state": "dirty"}]
        snap = d.snapshots[crew_id]
        mine = next(c for c in snap["claims"] if c["holder_session_id"] == a["session_id"])
        assert mine["lease_expires_at"] and d.sessions[a["key"]]["lease_expires_at"]
        # checkpoint with git facts (relay-cli source because the session is a hook session)
        res = await d.checkpoint(d.sessions[a["key"]], "turn", force=True)
        assert res["ok"] and res["status"] == 201, res
        ck = await srv.rows("SELECT trigger, facts, facts_source FROM crew_checkpoints WHERE session_id = ?", (a["session_id"],))
        assert ck[0]["facts_source"] == "relay-cli"
        assert "src/app/pos/split.ts" in json.loads(ck[0]["facts"])["dirty"]
        # an unchanged turn does not checkpoint again
        again = await d.checkpoint(d.sessions[a["key"]], "turn")
        assert again.get("skipped") == "unchanged"


async def test_git_delta_attribution_in_a_shared_checkout(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID, B_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        b = await d.join(peer(B_PID), _join_args("sess-b", repo, B_PID))
        assert b["shared_checkout"] is True
        # A edits split.ts with its file tool (certain); while A's tool call is in flight, B runs a
        # Bash command that changes nothing it can be blamed for -> no footprint for B (E2E-e)
        await d.activity(peer(A_PID), {"key": a["key"], "phase": "start", "tool": "Edit", "paths": ["src/app/pos/split.ts"]})
        (repo / "src/app/pos/split.ts").write_text("export const split = 3;\n")
        await d.activity(peer(B_PID), {"key": b["key"], "phase": "start", "tool": "Bash", "verb": "python"})
        await d.activity(peer(B_PID), {"key": b["key"], "phase": "end", "tool": "Bash", "verb": "python", "read_only": False})
        await d.activity(peer(A_PID), {"key": a["key"], "phase": "end", "tool": "Edit", "paths": ["src/app/pos/split.ts"]})
        assert "src/app/pos/split.ts" not in d.pending_footprints.get(b["key"], {})
        # a file only B's command could have written is attributed to B as probable
        await asyncio.sleep(0.3)
        await d.activity(peer(B_PID), {"key": b["key"], "phase": "start", "tool": "Bash", "verb": "touch"})
        (repo / "notes.txt").write_text("b\n")
        await d.activity(peer(B_PID), {"key": b["key"], "phase": "end", "tool": "Bash", "verb": "touch", "read_only": False})
        assert d.pending_footprints[b["key"]]["notes.txt"]["attribution"] == "probable"


async def test_stall_creates_baton_ref_and_reserves(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(A_PID), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        (repo / "src/app/pos/tender.ts").write_text("export const tender = 42;\n")
        (repo / "src/app/pos/new.ts").write_text("export const n = 1;\n")
        res = await d.stall(
            peer(A_PID), {"key": a["key"], "error": "billing_error", "last_assistant_message": "Credit balance is too low"}
        )
        assert res["ok"] and res["kind"] == "quota", res
        ref = res["baton_ref"]
        assert ref and ref.startswith(f"refs/remembra/baton/{a['session_id']}/")
        # the ref holds the uncommitted and untracked work; the working tree and index are untouched
        assert git(repo, "show", f"{ref}:src/app/pos/tender.ts") == "export const tender = 42;"
        assert git(repo, "show", f"{ref}:src/app/pos/new.ts") == "export const n = 1;"
        assert git(repo, "status", "--porcelain").splitlines() == ["M src/app/pos/tender.ts", "?? src/app/pos/new.ts"]
        assert git(repo, "diff", "--cached", "--name-only") == ""
        claims = await srv.rows("SELECT state, reserve_reason, baton_ref FROM crew_claims WHERE zone_id = ?", (pos["id"],))
        assert claims == [{"state": "reserved", "reserve_reason": "quota", "baton_ref": ref}]
        types = await srv.event_types(crew_id)
        assert "session.quota_blocked" in types
        # a transient 429 is checkpoint-only: no baton
        assert (
            await d.stall(None, {"key": a["key"], "error": "rate_limit", "last_assistant_message": "Request rejected (429)"})
        )["kind"] == "transient"


async def test_end_with_dirty_tree_leaves_with_baton_and_marks_ended(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        pos = next(z for z in d.snapshots[a["crew_id"]]["zones"] if z["slug"] == "pos")
        await d.claim(peer(A_PID), {"key": a["key"], "zone_id": pos["id"]})
        (repo / "src/app/pos/split.ts").write_text("dirty\n")
        res = await d.end(peer(A_PID), {"key": a["key"], "reason": "logout"}, grace=False)
        assert res["ok"] and res["baton_ref"], res
        rows = await srv.rows("SELECT state, end_reason FROM crew_sessions WHERE id = ?", (a["session_id"],))
        assert rows[0]["state"] == "ended"
        claims = await srv.rows("SELECT state, reserve_reason, baton_ref FROM crew_claims WHERE zone_id = ?", (pos["id"],))
        assert claims[0]["state"] == "reserved" and claims[0]["baton_ref"] == res["baton_ref"]
        # ended: the gate's fast exit (session file marked ended), token gone
        assert read_json(layout.session_file(a["key"]))["ended"] is True
        assert not (layout.keys / f"{a['key']}.token").exists()
        assert a["key"] not in d.sessions
        # the relay handoff was recorded too (step 1)
        assert "handoff.created" in await srv.event_types(a["crew_id"])


async def test_orphan_is_detected_and_leaves_process_exited(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    alive = {A_PID}
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive=alive)
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        (repo / "src/app/pos/split.ts").write_text("half done\n")
        assert await d.liveness() == []
        alive.discard(A_PID)  # kill -9
        assert await d.liveness() == [a["key"]]
        rows = await srv.rows("SELECT state, state_reason FROM crew_sessions WHERE id = ?", (a["session_id"],))
        assert rows == [{"state": "lost", "state_reason": "process_exited"}]
        assert "session.lost" in await srv.event_types(a["crew_id"])
        refs = B.list_batons(repo)
        assert refs and refs[0].startswith(f"refs/remembra/baton/{a['session_id']}/")


async def test_adopt_restores_the_saved_work_in_another_worktree(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID, C_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        # A creates and starts T-1 on pos, commits once, leaves tender.ts dirty, then runs out of credits
        tok_a = d.tokens[a["key"]]
        api = d.api_for(d.sessions[a["key"]])
        created = await api.call(
            "POST",
            f"/crews/{crew_id}/tasks",
            session_token=tok_a,
            json_body={"title": "POS split tender", "zone_ids": [pos["id"]], "acceptance": [], "depends_on": []},
        )
        assert created.status == 201, created.body
        task = created.body["task"]
        started = await api.call(
            "POST", f"/tasks/{task['id']}/start", session_token=tok_a, json_body={"head": git(repo, "rev-parse", "HEAD")}
        )
        assert started.ok, started.body
        await d.sync_snapshot(crew_id)
        assert d.sessions[a["key"]]["task_ref"] == "T-1"
        (repo / "src/app/pos/tender.ts").write_text("export const tender = 'wip';\n")
        stalled = await d.stall(peer(A_PID), {"key": a["key"], "error": "billing_error"})
        assert stalled["baton_ref"].startswith("refs/remembra/baton/T-1/")
        # C starts in its own worktree: the brief offers the baton; adopt restores A's file
        c = await d.join(peer(C_PID), _join_args("sess-c", wt_c, C_PID))
        assert [o.get("task_id") for o in c["batons_offered"]] == [task["id"]]
        res = await d.adopt(peer(C_PID), {"task": "T-1"})
        assert res["ok"], res
        assert res["restore"]["restored"] is True, res
        assert (wt_c / "src/app/pos/tender.ts").read_text() == "export const tender = 'wip';\n"
        # restored as unstaged working-tree changes on a branch at A's HEAD
        assert git(wt_c, "status", "--porcelain") == "M src/app/pos/tender.ts"
        claims = await srv.rows("SELECT holder_session_id, epoch, state FROM crew_claims WHERE zone_id = ?", (pos["id"],))
        assert claims == [{"holder_session_id": c["session_id"], "epoch": 2, "state": "active"}]
        assert "baton.passed" in await srv.event_types(crew_id)


async def test_adopt_refuses_to_restore_into_a_dirty_tree(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_c = add_worktree(repo, tmp_path / "wt-c", "c-work")
    (wt_c / "mine.txt").write_text("c's own work\n")
    ref = None
    (repo / "src/app/pos/tender.ts").write_text("saved\n")
    baton = B.create_baton_ref(repo, "T-3")
    assert baton is not None
    ref = baton.ref
    result = B.restore_baton(wt_c, ref, retry_command="remembra-crew adopt T-3 --restore-only")
    assert result.restored is False and result.reason == "dirty_tree"
    assert result.next_command == "remembra-crew adopt T-3 --restore-only"
    assert (wt_c / "src/app/pos/tender.ts").read_text() == "export const tender = 1;\n"
    assert not S.DESTRUCTIVE_COMMAND_RES[0].search(result.next_command or "")


async def test_local_arbiter_while_unreachable_then_replay(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID, B_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        b = await d.join(peer(B_PID), _join_args("sess-b", wt_b, B_PID))
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        real = d.request

        async def down(api, method, path, **kw):
            await d.note_result(api, None)
            raise Unreachable("network cut")

        d.request = down  # type: ignore[method-assign]
        assert set((await d.heartbeat()).values()) == {"unreachable"}
        assert d.server_reachable is False
        # same-host conflicts are decided locally: A gets pos, B is denied naming A
        assert (await d.claim(peer(A_PID), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        lost = await d.claim(peer(B_PID), {"key": b["key"], "zone_id": pos["id"]})
        assert lost == {"ok": True, "result": "conflict", "winner_session_id": a["session_id"], "source": "local_arbiter"}
        # the local grant is visible to every local gate at once
        snap = read_json(layout.snapshot_file(crew_id))
        assert any(c["zone_id"] == pos["id"] and c["source"] == "local_arbiter" for c in snap["claims"])
        assert read_json(layout.status_file)["server_reachable"] is False
        # reconnect: the grant is replayed to the server (source local_arbiter) and confirmed
        d.request = real  # type: ignore[method-assign]
        await d.on_reconnect()
        rows = await srv.rows("SELECT holder_session_id, source, state FROM crew_claims WHERE zone_id = ?", (pos["id"],))
        assert rows == [{"holder_session_id": a["session_id"], "source": "local_arbiter", "state": "active"}]
        assert d.arbiter.pending_replay(crew_id) == []


async def test_outbox_replayed_twice_creates_no_duplicates(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        a = await d.join(peer(A_PID), _join_args("sess-a", repo, A_PID))
        sess = d.sessions[a["key"]]
        body = {"session_id": a["session_id"], "trigger": "precompact", "facts": {"head": git(repo, "rev-parse", "HEAD")}}
        rec = {"method": "POST", "path": f"/crews/{a['crew_id']}/checkpoints", "json": body}
        O.spool(layout.outbox, "checkpoint", rec, session_key=a["key"], crew_id=a["crew_id"], idem="ckp-fixed-1")
        O.spool(layout.outbox, "checkpoint", rec, session_key=a["key"], crew_id=a["crew_id"], idem="ckp-fixed-1")
        result = await d.flush_outbox()
        assert result.sent == 2 and O.read_entries(layout.outbox) == []
        rows = await srv.rows(
            "SELECT id FROM crew_checkpoints WHERE session_id = ? AND trigger = 'precompact'", (a["session_id"],)
        )
        assert len(rows) == 1
        assert sess["session_id"] == a["session_id"]


async def test_join_refuses_an_agent_pid_outside_the_callers_tree(tmp_path):
    repo = make_repo(tmp_path / "repo")
    layout = Layout(tmp_path / "home")
    async with crew_server(tmp_path) as srv:
        d = new_crewd(layout, srv, alive={A_PID})
        from remembra.relay.crew.crewd import CrewdError

        with pytest.raises(CrewdError) as e:
            await d.join(peer(B_PID), _join_args("sess-a", repo, A_PID))
        assert e.value.code == "agent_pid_not_ancestor"
        assert os.listdir(layout.sessions) == []
