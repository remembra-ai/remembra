"""WP-9 ``crew-gate.py`` end to end: the vendored gate as a real subprocess (``python -I``), talking to a
real crewd socket server (peer-authenticated) that talks to the real crew routers.

Covers the fast exit, vendoring and integrity (restore on tamper), PreToolUse decisions (auto-claim
through crewd, deny naming the holder, foreign checkout, tamper, crew-policy, read-only fast exits),
the stdout contracts, PostToolUse hand-off, UserPromptSubmit digest, the Stop block, the git gates
(pre-commit, prepare-commit-msg trailer, pre-push) and gate latency.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from remembra.crew import schemas as S
from remembra.relay.crew import gate as gate_mod
from remembra.relay.crew import outbox as O
from remembra.relay.crew.gate import Layout, read_json, session_key, vendor_gate, verify_gate
from tests.crew.wp9_support import add_worktree, crew_server, git, make_repo, new_crewd, peer

PY = sys.executable


def run_gate(
    layout: Layout,
    sub: str,
    payload: dict | None = None,
    *,
    cwd: Path | None = None,
    args: list[str] | None = None,
    stdin: str | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "PYTHON"))}
    env["HOME"] = str(layout.home)
    env.update(extra_env or {})
    return subprocess.run(
        [PY, "-I", str(layout.gate_script), sub, "--hook", "claude-code", *(args or [])],
        input=stdin if stdin is not None else (json.dumps(payload) if payload is not None else ""),
        capture_output=True,
        text=True,
        cwd=str(cwd or layout.home),
        env=env,
        timeout=30,
    )


async def arun_gate(*a, **kw) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(run_gate, *a, **kw)


def pretool(sid: str, cwd: Path, tool: str, tool_input: dict) -> dict:
    return {
        "session_id": sid,
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "permission_mode": "default",
        "transcript_path": "/dev/null",
    }


# ---------------------------------------------------------------------------
# Vendoring, integrity, fast exit
# ---------------------------------------------------------------------------


def test_vendored_gate_is_private_stdlib_only_and_sealed(tmp_path):
    layout = Layout(tmp_path / "home")
    script = vendor_gate(layout, crewd_argv=[PY, "-m", "remembra.relay.crew.crewd"])
    assert script == layout.gate_script
    first = script.read_text().splitlines()[0]
    assert gate_mod.HEADER_RE.match(first)
    check = verify_gate(layout)
    assert check.ok and check.installed
    for p in [layout.bin, layout.lib, *layout.lib.rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))
    # runs with no site-packages at all: the vendored copy is self-contained stdlib
    env = {"HOME": str(layout.home), "PATH": "/usr/bin:/bin"}
    res = subprocess.run([PY, "-I", "-S", str(script), "version"], capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 0 and res.stdout.strip() == f"crew-gate v{gate_mod.GATE_VERSION}", res.stderr
    res = subprocess.run(
        [PY, "-I", "-S", str(script), "pretool", "--hook", "claude-code"],
        input=json.dumps({"session_id": "x", "tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert res.returncode == 0 and res.stdout == ""


def test_gate_fast_exit_without_a_crew_session(tmp_path):
    layout = Layout(tmp_path / "home")
    vendor_gate(layout)
    payload = pretool("no-such-session", tmp_path, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"})
    # in-process: the no-session exit returns before importing the decision core (<10 ms budget)
    import io

    old_stdin = sys.stdin
    timings = []
    for _ in range(20):
        sys.stdin = io.StringIO(json.dumps(payload))
        t = time.perf_counter()
        assert gate_mod.main(["pretool", "--hook", "claude-code"], layout=layout) == 0
        timings.append(time.perf_counter() - t)
    sys.stdin = old_stdin
    assert statistics.median(timings) < 0.010, timings
    res = run_gate(layout, "pretool", payload)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    for sub in ("posttool", "turn", "stop", "precompact"):
        res = run_gate(layout, sub, {"session_id": "no-such-session"})
        assert (res.returncode, res.stdout) == (0, "")


def test_tampered_gate_is_detected_and_restored_by_crewd(tmp_path):
    from remembra.relay.crew.crewd import Crewd

    layout = Layout(tmp_path / "home")
    vendor_gate(layout)
    core = layout.lib / "remembra" / "crew" / "gatecore.py"
    core.write_text(core.read_text() + "\n# an agent was here\n")
    assert verify_gate(layout).ok is False
    d = Crewd(layout, restore_gate=True)
    d.load_state()
    res = d.check_gate()
    assert res["restored"] is True and verify_gate(layout).ok
    assert "an agent was here" not in core.read_text()
    assert d.events_log[-1]["type"] == "gate.tampered"
    # a changed header is caught too
    text = layout.gate_script.read_text().replace("sha256:", "sha256:0", 1)[:-1]
    layout.gate_script.write_text(text)
    assert verify_gate(layout).ok is False


# ---------------------------------------------------------------------------
# PreToolUse / PostToolUse / UserPromptSubmit / Stop against a live crewd
# ---------------------------------------------------------------------------


@pytest.fixture()
def sleeper():
    proc = subprocess.Popen(["sleep", "600"])
    yield proc.pid
    proc.kill()
    proc.wait()


async def _start_crewd(tmp_path, srv, alive=()):
    layout = Layout(tmp_path / "home")
    vendor_gate(layout)
    d = new_crewd(layout, srv, alive=set(alive))
    await d.start_server()
    return layout, d


async def test_pretool_decisions_through_the_real_gate(tmp_path, sleeper):
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={sleeper})
        # A: an agent process elsewhere on this host; B: this test process tree (the gate runs as our child)
        a = await d.join(
            peer(sleeper), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": sleeper}
        )
        b = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-b", "cwd": str(wt_b), "agent_pid": me})
        crew_id = a["crew_id"]
        # A writes into pos first: the gate auto-claims through crewd and allows (empty stdout)
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(sleeper), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(crew_id)

        # B writes into pos in its own worktree: denied, naming the holder, valid PreToolUse JSON
        res = await arun_gate(
            layout,
            "pretool",
            pretool("sess-b", wt_b, "Write", {"file_path": str(wt_b / "src/app/pos/split.ts"), "content": "x"}),
            cwd=wt_b,
        )
        assert res.returncode == 0, res.stderr
        assert S.validate_hook_stdout("PreToolUse", res.stdout) == [], res.stdout
        out = json.loads(res.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert 'zone "pos"' in out["permissionDecisionReason"] and a["callsign"] in out["permissionDecisionReason"]
        assert '"allow"' not in res.stdout
        assert (wt_b / "src/app/pos/split.ts").read_text() == "export const split = 1;\n"

        # B writes into A's checkout directly: foreign checkout (rule 4)
        res = await arun_gate(
            layout,
            "pretool",
            pretool(
                "sess-b",
                wt_b,
                "Edit",
                {"file_path": str(repo / "src/app/reports/export.ts"), "old_string": "1", "new_string": "2"},
            ),
            cwd=wt_b,
        )
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

        # tamper and crew-policy: denied in every mode
        res = await arun_gate(
            layout, "pretool", pretool("sess-b", wt_b, "Bash", {"command": "git commit --no-verify -m x"}), cwd=wt_b
        )
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        res = await arun_gate(
            layout,
            "pretool",
            pretool(
                "sess-b", wt_b, "Edit", {"file_path": str(wt_b / ".remembra/zones.yml"), "old_string": "pos", "new_string": "x"}
            ),
            cwd=wt_b,
        )
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        res = await arun_gate(
            layout, "pretool", pretool("sess-b", wt_b, "Bash", {"command": "pkill -f remembra-crewd"}), cwd=wt_b
        )
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

        # read-only Bash, reads and B's own zone: allowed with empty stdout
        for tool, tin in (("Bash", {"command": "ls -la && git status"}), ("Read", {"file_path": str(wt_b / "README.md")})):
            res = await arun_gate(layout, "pretool", pretool("sess-b", wt_b, tool, tin), cwd=wt_b)
            assert (res.returncode, res.stdout) == (0, "")
        res = await arun_gate(
            layout,
            "pretool",
            pretool("sess-b", wt_b, "Write", {"file_path": str(wt_b / "src/app/reports/export.ts"), "content": "y"}),
            cwd=wt_b,
        )
        assert (res.returncode, res.stdout) == (0, ""), res.stdout
        await d.drain()
        claims = await srv.rows(
            "SELECT zone_id, holder_session_id, source FROM crew_claims WHERE state = 'active' ORDER BY created_at"
        )
        reports = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "reports")
        assert {"zone_id": reports["id"], "holder_session_id": b["session_id"], "source": "first_write"} in claims

        # the denies became client events (guard.blocked / guard.tamper_blocked) spooled for the server
        types = [e.body["type"] for e in O.read_entries(layout.outbox, ("event",))]
        assert "guard.blocked" in types and "guard.tamper_blocked" in types
        for e in O.read_entries(layout.outbox, ("event",)):
            assert S.validate_client_event({"id": "evt_test_1234", "type": e.body["type"], "payload": e.body["payload"]}) == []


async def test_posttool_turn_and_stop(tmp_path):
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        a = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": me})
        crew_id = a["crew_id"]
        # PostToolUse after an Edit: hands off to crewd (≤100 ms budget), footprint recorded as certain
        (repo / "src/app/reports/export.ts").write_text("export const x = 2;\n")
        t = time.perf_counter()
        res = await arun_gate(
            layout,
            "posttool",
            {
                "session_id": "sess-a",
                "cwd": str(repo),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src/app/reports/export.ts")},
                "tool_response": {},
            },
        )
        elapsed = time.perf_counter() - t
        assert (res.returncode, res.stdout) == (0, "")
        assert elapsed < 1.0
        await d.drain()
        assert d.pending_footprints[a["key"]]["src/app/reports/export.ts"]["attribution"] == "certain"
        # a commit seen by PostToolUse: activity.commit spooled, commit checkpoint recorded
        git(repo, "commit", "-qam", "reports")
        res = await arun_gate(
            layout,
            "posttool",
            {
                "session_id": "sess-a",
                "cwd": str(repo),
                "tool_name": "Bash",
                "tool_input": {"command": "git commit -qam reports"},
                "tool_response": {"stdout": ""},
            },
        )
        await d.drain()
        ev = [e.body for e in O.read_entries(layout.outbox, ("event",)) if e.body["type"] == "activity.commit"]
        assert ev and ev[0]["payload"]["files"] == ["src/app/reports/export.ts"]
        assert await srv.rows(
            "SELECT trigger FROM crew_checkpoints WHERE session_id = ? AND trigger = 'commit'", (a["session_id"],)
        )
        # a test run: counts only, verdict tracked
        res = await arun_gate(
            layout,
            "posttool",
            {
                "session_id": "sess-a",
                "cwd": str(repo),
                "tool_name": "Bash",
                "tool_input": {"command": "pytest -q tests/pos"},
                "tool_response": {"stdout": "....\n4 passed in 0.1s\n"},
            },
        )
        await d.drain()
        assert d.sessions[a["key"]]["tests"]["pytest -q tests/pos"]["verdict"] == "pass"

        # UserPromptSubmit: first turn prints the YOU line; unchanged digest prints nothing
        prompt = {"session_id": "sess-a", "cwd": str(repo), "prompt": "hi"}
        res = await arun_gate(layout, "turn", prompt)
        assert S.validate_hook_stdout("UserPromptSubmit", res.stdout) == [], res.stdout
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "YOU:" in ctx and len(ctx) <= 600
        res = await arun_gate(layout, "turn", prompt)
        assert res.stdout == ""

        # Stop (D16): an in_progress task with a commit and a "done" message blocks once, with ids only
        tok = d.tokens[a["key"]]
        api = d.api_for(d.sessions[a["key"]])
        reports = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "reports")
        created = await api.call(
            "POST",
            f"/crews/{crew_id}/tasks",
            session_token=tok,
            json_body={"title": "Reports export", "zone_ids": [reports["id"]], "acceptance": [], "depends_on": []},
        )
        task = created.body["task"]
        assert (await api.call("POST", f"/tasks/{task['id']}/start", session_token=tok, json_body={})).ok
        await d.sync_snapshot(crew_id)
        d.sessions[a["key"]]["task_commits"] = {task["id"]: 1}
        d.persist(a["key"])
        stop = {
            "session_id": "sess-a",
            "cwd": str(repo),
            "stop_hook_active": False,
            "last_assistant_message": "All tests pass. The task is done.",
        }
        res = await arun_gate(layout, "stop", stop)
        assert S.validate_hook_stdout("Stop", res.stdout) == [], res.stdout
        reason = json.loads(res.stdout)["reason"]
        assert f"T-{task['number']}" in reason and "crew_report" in reason
        assert (await arun_gate(layout, "stop", stop)).stdout == ""  # once per task per session
        assert (await arun_gate(layout, "stop", {**stop, "stop_hook_active": True})).stdout == ""


async def test_git_gates_precommit_trailer_and_prepush(tmp_path, sleeper):
    remote = tmp_path / "remote.git"
    repo = make_repo(tmp_path / "repo", remote=remote)
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={sleeper})
        a = await d.join(
            peer(sleeper), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": sleeper}
        )
        b = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-b", "cwd": str(wt_b), "agent_pid": me})
        crew_id = a["crew_id"]
        pos = next(z for z in d.snapshots[crew_id]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(sleeper), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(crew_id)
        hooks = Path(git(wt_b, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        hooks.mkdir(parents=True, exist_ok=True)
        env_home = f'HOME="{layout.home}"'
        for hook, sub in (("pre-commit", "precommit"), ("prepare-commit-msg", 'trailer "$1"'), ("pre-push", "prepush")):
            (hooks / hook).write_text(f'#!/bin/sh\n{env_home} exec "{PY}" -I "{layout.gate_script}" {sub} # remembra-crew\n')
            (hooks / hook).chmod(0o755)
        assert d.sessions[b["key"]]["githook_state"] in ("missing", "unknown")
        d.check_githooks()
        assert d.sessions[b["key"]]["githook_state"] == "ok"

        def commit(cwd, *args, env=None):
            return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **(env or {})})

        # a commit touching POS (held by A) is refused at commit time, with the §5.2 reason
        (wt_b / "src/app/pos/split.ts").write_text("export const split = 99;\n")
        commit(wt_b, "add", "-A")
        res = await asyncio.to_thread(commit, wt_b, "commit", "-m", "touch pos")
        assert res.returncode != 0, res.stdout
        assert "BLOCKED by Remembra Crew" in res.stderr and "pos" in res.stderr
        assert git(wt_b, "rev-list", "--count", "main..HEAD") == "0"
        # a commit outside held zones goes through and carries the member trailer
        commit(wt_b, "reset", "-q")
        commit(wt_b, "checkout", "--", "src/app/pos/split.ts")
        (wt_b / "src/app/reports/export.ts").write_text("export const x = 5;\n")
        commit(wt_b, "add", "-A")
        res = await asyncio.to_thread(commit, wt_b, "commit", "-m", "reports work")
        assert res.returncode == 0, res.stderr
        assert f"Remembra-Member: {b['member_key']}" in git(wt_b, "log", "-1", "--format=%B")
        # a commit that skipped the pre-commit hook still cannot be pushed (pre-push checks the range)
        (wt_b / "src/app/pos/tender.ts").write_text("export const tender = 7;\n")
        commit(wt_b, "add", "-A")
        res = await asyncio.to_thread(commit, wt_b, "-c", "core.hooksPath=/dev/null", "commit", "-m", "sneaky")
        assert res.returncode == 0, res.stderr
        res = await asyncio.to_thread(commit, wt_b, "push", "-q", "origin", "b")
        assert res.returncode != 0 and "pos" in res.stderr, res.stderr
        assert "refs/heads/b" not in git(remote, "for-each-ref", "--format=%(refname)")
        await d.drain()
        types = [e.body["type"] for e in O.read_entries(layout.outbox, ("event",))]
        assert types.count("guard.blocked") >= 2
        assert {e.body["payload"]["surface"] for e in O.read_entries(layout.outbox, ("event",))} >= {"precommit", "prepush"}


async def test_gate_latency_with_a_fresh_snapshot(tmp_path):
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        a = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": me})
        pos = next(z for z in d.snapshots[a["crew_id"]]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(me), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(a["crew_id"])
        payload = pretool("sess-a", repo, "Write", {"file_path": str(repo / "src/app/pos/split.ts"), "content": "x"})
        times = []
        for _ in range(40):
            t = time.perf_counter()
            res = await arun_gate(layout, "pretool", payload)
            times.append(time.perf_counter() - t)
            assert (res.returncode, res.stdout) == (0, "")
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        # whole subprocess including interpreter start; the spec's p95 ≤90 ms applies with a CI guard of 2x
        assert p95 <= 0.18, f"p95 {p95 * 1000:.0f} ms, cold {times[-1] * 1000:.0f} ms"
        assert read_json(layout.session_file(session_key("claude-code", "sess-a")))["session_id"] == a["session_id"]


# ---------------------------------------------------------------------------
# Urgent items, news, rewake, fencing horizon, human bypass
# ---------------------------------------------------------------------------


async def test_urgent_items_news_and_rewake(tmp_path, sleeper):
    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={sleeper})
        a = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": me})
        b = await d.join(
            peer(sleeper), {"adapter": "claude-code", "client_session_id": "sess-b", "cwd": str(wt_b), "agent_pid": sleeper}
        )
        # a human (dashboard login) asks A for a checkpoint: delivered as inject_text on A's heartbeat
        res = await srv.h.client.post(
            f"/api/v1/sessions/{a['session_id']}/request-checkpoint",
            json={"reason": "now"},
            headers=srv.h.jwt(srv.owner, "owner@example.com"),
        )
        assert res.status_code == 200, res.text
        await d.heartbeat()
        assert "checkpoint" in d.sessions[a["key"]]["inject"]["text"]
        # asyncRewake waiter: exit 2 with the item on stderr, once
        woke = await arun_gate(layout, "wake", {"session_id": "sess-a", "cwd": str(repo)})
        assert woke.returncode == 2 and "checkpoint" in woke.stderr and woke.stdout == ""
        assert (await arun_gate(layout, "wake", {"session_id": "sess-a", "cwd": str(repo)})).returncode == 0
        await d.drain()
        assert d.sessions[a["key"]]["inject_delivered"] == d.sessions[a["key"]]["inject"]["id"]
        # a second human item arrives: now delivered mid-turn as PreToolUse additionalContext on an allowed write
        d.sessions[a["key"]]["inject"] = {"id": "manual-2", "text": "Crew: Mani paused zone review; hold pushes."}
        d.persist(a["key"])
        res = await arun_gate(
            layout, "pretool", pretool("sess-a", repo, "Write", {"file_path": str(repo / "README.md"), "content": "x"})
        )
        assert S.validate_hook_stdout("PreToolUse", res.stdout) == [], res.stdout
        hso = json.loads(res.stdout)["hookSpecificOutput"]
        assert "permissionDecision" not in hso and "hold pushes" in hso["additionalContext"]
        # news: B runs out of credits; A's next turn names it (server template, ids and callsigns only)
        await d.stall(peer(sleeper), {"key": b["key"], "error": "billing_error"})
        await d.heartbeat()
        turn = await arun_gate(layout, "turn", {"session_id": "sess-a", "cwd": str(repo), "prompt": "next"})
        ctx = json.loads(turn.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "NEW:" in ctx and b["callsign"] in ctx


async def test_fencing_horizon_and_server_outage(tmp_path):
    repo = make_repo(tmp_path / "repo")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        a = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": me})
        pos = next(z for z in d.snapshots[a["crew_id"]]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(me), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(a["crew_id"])
        write = pretool("sess-a", repo, "Write", {"file_path": str(repo / "src/app/pos/split.ts"), "content": "x"})
        assert (await arun_gate(layout, "pretool", write)).stdout == ""
        # the lease could not be renewed: 30 s before expiry is past the 60 s fence margin (D31)
        from remembra.relay.crew.snapshot import format_ts

        snap = read_json(layout.snapshot_file(a["crew_id"]))
        for c in snap["claims"]:
            if c["zone_id"] == pos["id"]:
                c["lease_expires_at"] = format_ts(time.time() - snap["skew_s"] + 30)
        layout.snapshot_file(a["crew_id"]).write_text(json.dumps(snap))
        d.server_reachable, d.server_outage = False, False
        d.write_status()
        res = await arun_gate(layout, "pretool", write)
        out = json.loads(res.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and "reconnecting" in out["permissionDecisionReason"]
        # a server outage (5xx and /health failing) keeps the holder's own claims writable
        d.server_outage = True
        d.write_status()
        assert (await arun_gate(layout, "pretool", write)).stdout == ""


async def test_note_result_tells_network_loss_from_server_outage(tmp_path):
    from remembra.relay.crew.crewd import Resp

    repo = make_repo(tmp_path / "repo")
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv)
        api = d.api_for_cfg(srv.config_loader()("claude-code", None), "claude-code")
        await d.note_result(api, Resp(503, {}))
        assert (d.server_reachable, d.server_outage) == (False, False)  # /health answers: not an outage
        real = api.call

        async def health_down(method, path, **kw):
            if path == "/health":
                return Resp(500, {})
            return await real(method, path, **kw)

        api.call = health_down  # type: ignore[method-assign]
        await d.note_result(api, Resp(503, {}))
        assert (d.server_reachable, d.server_outage) == (False, True)
        assert read_json(layout.status_file)["server_outage"] is True
        await d.note_result(api, None)
        assert (d.server_reachable, d.server_outage) == (False, False)
        await d.note_result(api, Resp(200, {}))
        assert d.server_reachable is True
        assert repo.exists()


async def test_human_bypass_is_single_use_and_not_for_agents(tmp_path, sleeper, monkeypatch):
    from remembra.relay.crew import crewd as crewd_mod
    from remembra.relay.crew.crewd import CrewdError, Peer

    repo = make_repo(tmp_path / "repo")
    wt_b = add_worktree(repo, tmp_path / "wt-b", "b")
    me = os.getpid()
    async with crew_server(tmp_path) as srv:
        layout, d = await _start_crewd(tmp_path, srv, alive={sleeper})
        a = await d.join(
            peer(sleeper), {"adapter": "claude-code", "client_session_id": "sess-a", "cwd": str(repo), "agent_pid": sleeper}
        )
        b = await d.join(peer(me), {"adapter": "claude-code", "client_session_id": "sess-b", "cwd": str(wt_b), "agent_pid": me})
        pos = next(z for z in d.snapshots[a["crew_id"]]["zones"] if z["slug"] == "pos")
        assert (await d.claim(peer(sleeper), {"key": a["key"], "zone_id": pos["id"]}))["result"] == "granted"
        await d.sync_snapshot(a["crew_id"])
        human = Peer(424242, os.getuid(), (424242, 1))
        # no controlling terminal (an agent's Bash tool): refused
        with pytest.raises(CrewdError) as e:
            await d.bypass(human, {"session": b["callsign"]})
        assert e.value.code == "tty_required"
        monkeypatch.setattr(crewd_mod, "has_controlling_tty", lambda pid: True)
        # a process inside a session's tree cannot grant itself one, even with a TTY
        with pytest.raises(CrewdError) as e:
            await d.bypass(peer(me), {"session": b["callsign"]})
        assert e.value.code == "agent_process"
        # server reachable: a human-issued code is required
        with pytest.raises(CrewdError) as e:
            await d.bypass(human, {"session": b["callsign"]})
        assert e.value.code == "code_required"
        d.server_reachable = False
        granted = await d.bypass(human, {"session": b["callsign"], "minutes": 5})
        assert granted["ok"] and granted["via"] == "offline_tty"
        write = pretool("sess-b", wt_b, "Write", {"file_path": str(wt_b / "src/app/pos/split.ts"), "content": "x"})
        assert (await arun_gate(layout, "pretool", write, cwd=wt_b)).stdout == ""  # used once
        res = await arun_gate(layout, "pretool", write, cwd=wt_b)
        assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        audit = [json.loads(line) for line in (layout.log_dir / "audit.jsonl").read_text().splitlines()]
        assert [r["action"] for r in audit] == ["bypass_granted", "bypass_used"]
