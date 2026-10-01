"""WP-9 runtime pieces on their own, with real files, real git and real processes (no server).

outbox (spool/flush/ordering/backoff), snapshot writer (skew, HMAC, settings subset), baton refs
(capture rules, push/fetch across clones, deletions, retention), local arbiter, read-only fence,
transcript detector, zones.yml compiler and upload rule, and crewd helpers (peer credentials on a
real unix socket, process ancestry, TTY check, stall classification, tree snapshot, supervision
units rendered for a temp HOME).
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from remembra.crew import schemas as S
from remembra.relay.crew import baton as B
from remembra.relay.crew import detector as D
from remembra.relay.crew import outbox as O
from remembra.relay.crew import snapshot as SN
from remembra.relay.crew import zonescompile as ZC
from remembra.relay.crew.arbiter import Arbiter, LocalSessionRef
from remembra.relay.crew.crewd import (
    ancestors,
    build_tree,
    classify_stall,
    find_agent_pid,
    githook_state,
    has_controlling_tty,
    launchd_plist,
    peer_credentials,
    systemd_unit,
    unit_path,
)
from remembra.relay.crew.fence import Fence, zones_to_fence
from remembra.relay.crew.gate import Layout
from tests.crew.wp9_support import ZONES_YML, add_worktree, git, make_repo

# ---------------------------------------------------------------------------
# outbox
# ---------------------------------------------------------------------------


async def test_outbox_spools_privately_and_flushes_in_order_per_kind(tmp_path):
    box = tmp_path / "outbox"
    p1 = O.spool(box, "event", {"n": 1}, session_key="k", now=100.0)
    O.spool(box, "event", {"n": 2}, session_key="k", now=101.0)
    O.spool(box, "checkpoint", {"n": 3}, session_key="k", now=102.0)
    assert stat.S_IMODE(p1.stat().st_mode) == 0o600 and stat.S_IMODE(box.stat().st_mode) == 0o700
    seen: list[int] = []

    async def send(e: O.Entry) -> str:
        seen.append(e.body["n"])
        return "retry" if e.kind == "event" else "done"

    res = await O.flush(box, send, now=200.0)
    # the first event retries and holds back the second event, but not the checkpoint
    assert seen == [1, 3] and res.retried == 1 and res.sent == 1 and res.skipped == 1
    left = O.read_entries(box)
    assert [e.body["n"] for e in left] == [1, 2] and left[0].attempts == 1 and left[0].next_attempt_at > 200.0
    # backoff: nothing is retried before next_attempt_at; afterwards both go in order
    seen.clear()

    async def ok(e: O.Entry) -> str:
        seen.append(e.body["n"])
        return "done"

    await O.flush(box, ok, now=201.0)
    assert seen == []
    await O.flush(box, ok, now=1000.0)
    assert seen == [1, 2] and O.read_entries(box) == []


async def test_outbox_named_records_replace_and_old_records_drop(tmp_path):
    box = tmp_path / "outbox"
    name = O.claim_record_name("k1", "zn_pos")
    O.spool(box, "claim", {"zone_id": "zn_pos", "try": 1}, session_key="k1", name=name)
    O.spool(box, "claim", {"zone_id": "zn_pos", "try": 2}, session_key="k1", name=name)
    assert [e.body["try"] for e in O.pending_claims(box, "k1")] == [2]
    assert O.pending_claim_zone_ids(box, "k1") == {"zn_pos"} and O.pending_claim_zone_ids(box, "other") == set()
    O.spool(box, "event", {"x": 1}, now=time.time() - O.MAX_AGE_S - 10)

    async def never(e: O.Entry) -> str:
        raise AssertionError("an expired record is never sent")

    res = await O.flush(box, never, kinds=("event",))
    assert res.dropped == 1
    with pytest.raises(ValueError):
        O.spool(box, "nope", {})
    # a half-written temp file is ignored
    (box / ".x.tmp").write_text("{")
    (box / "broken.json").write_text("{")
    assert [e.kind for e in O.read_entries(box)] == ["claim"]


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_local_snapshot_skew_settings_and_hmac(tmp_path):
    server = {
        "crew": {"id": "crw_0123456789abcdef", "enforcement": "observe"},
        "server_time": "2026-09-25T20:00:00.000Z",
        "zones": [{"id": "zn_1", "source": "suggested"}],
        "claims": [],
    }
    local_now = SN.parse_ts("2026-09-25T20:00:07.000Z").timestamp()
    snap = SN.build_local_snapshot(
        server,
        settings={"auto_claim": False, "lease_ttl_s": 900, "other": 1},
        host_id="hst_1",
        checkouts=[{"toplevel": "/r"}],
        local_now=local_now,
        hmac_key=b"k" * 32,
    )
    assert snap["skew_s"] == 7.0 and snap["synced_at"] == server["server_time"]
    assert snap["settings"]["enforcement"] == "observe" and snap["settings"]["auto_claim"] is False
    assert snap["settings"]["lease_ttl_s"] == 900 and "other" not in snap["settings"]
    assert snap["bootstrap_zones"] is True
    assert SN.verify(snap, b"k" * 32) and not SN.verify({**snap, "claims": [{}]}, b"k" * 32)
    # the server clock: 10 s after sync on the local clock is 10 s of snapshot age
    assert SN.age_s(snap, local_now + 10) == pytest.approx(10.0)
    path = SN.snapshot_path(tmp_path, "crw_0123456789abcdef")
    SN.write_snapshot(path, snap)
    assert SN.load_snapshot(path) == snap and stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# baton refs
# ---------------------------------------------------------------------------


def test_baton_capture_rules_and_clean_tree(tmp_path):
    repo = make_repo(tmp_path / "repo")
    assert B.create_baton_ref(repo, "T-9") is None  # clean: nothing to save
    (repo / "src/app/pos/split.ts").write_text("changed\n")
    git(repo, "add", "src/app/pos/split.ts")  # staged
    (repo / "src/app/pos/tender.ts").write_text("unstaged\n")
    (repo / "README.md").unlink()  # deleted
    (repo / "new.txt").write_text("untracked\n")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "big.bin").write_bytes(b"0" * (B.MAX_FILE_BYTES + 1))
    (repo / ".gitignore").write_text("ignored.log\n")
    (repo / "ignored.log").write_text("x\n")
    index_before = (repo / ".git" / "index").read_bytes()
    baton = B.create_baton_ref(repo, "T-9")
    assert baton is not None and baton.ref == "refs/remembra/baton/T-9/1" and baton.seq == 1
    assert sorted(baton.skipped) == [".env", "big.bin"]
    files = git(repo, "ls-tree", "-r", "--name-only", baton.ref).splitlines()
    assert "new.txt" in files and ".gitignore" in files and "README.md" not in files
    assert ".env" not in files and "big.bin" not in files and "ignored.log" not in files
    assert git(repo, "show", f"{baton.ref}:src/app/pos/tender.ts") == "unstaged"
    # the real index and the working tree are untouched
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert (repo / "src/app/pos/tender.ts").read_text() == "unstaged\n"
    assert git(repo, "rev-parse", f"{baton.ref}^") == git(repo, "rev-parse", "HEAD")
    again = B.create_baton_ref(repo, "T-9")
    assert again is not None and again.seq == 2
    with pytest.raises(ValueError):
        B.create_baton_ref(repo, "../../evil")


def test_baton_push_fetch_and_restore_on_another_clone_with_deletions(tmp_path):
    remote = tmp_path / "remote.git"
    repo = make_repo(tmp_path / "repo", remote=remote)
    (repo / "src/app/pos/split.ts").write_text("wip\n")
    (repo / "README.md").unlink()
    git(repo, "commit", "-qam", "unpushed work")
    (repo / "src/app/pos/tender.ts").write_text("dirty\n")
    baton = B.create_baton_ref(repo, "T-4")
    assert baton is not None and len(baton.unpushed) == 1
    assert B.push_baton(repo, baton, "origin") is True
    assert f"{B.BATON_HEADS_PREFIX}/T-4/1" in git(remote, "for-each-ref", "--format=%(refname)")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "O")
    result = B.restore_baton(other, baton.ref, remote="origin")
    assert result.restored, result
    assert (other / "src/app/pos/tender.ts").read_text() == "dirty\n"
    assert (other / "src/app/pos/split.ts").read_text() == "wip\n"  # the unpushed commit came with the baton
    assert not (other / "README.md").exists()
    assert git(other, "rev-parse", "HEAD") == baton.parent
    # retention: 7 days after release the ref is deleted locally and on the remote
    registry = {baton.ref: {"closed_at": time.time() - B.REF_TTL_S - 1}}
    assert B.expired(registry, time.time()) == [baton.ref]
    B.delete_baton(repo, baton.ref, remote="origin")
    assert baton.ref not in B.list_batons(repo)
    assert baton.ref not in git(remote, "for-each-ref", "--format=%(refname)")


def test_restore_in_a_second_worktree_uses_a_new_branch_when_the_branch_is_taken(tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt = add_worktree(repo, tmp_path / "wt", "other")
    (repo / "README.md").write_text("main moved on\n")
    git(repo, "commit", "-qam", "main moves")
    (repo / "src/app/pos/split.ts").write_text("from main\n")
    baton = B.create_baton_ref(repo, "T-2")
    assert baton is not None
    res = B.restore_baton(wt, baton.ref)
    assert res.restored and res.branch == "remembra/T-2-1"  # main is checked out in the first worktree
    assert (wt / "src/app/pos/split.ts").read_text() == "from main\n"
    assert git(wt, "diff", "--cached", "--name-only") == ""  # unstaged
    assert git(wt, "rev-parse", "HEAD") == baton.parent and (wt / "README.md").read_text() == "main moved on\n"
    # when the worktree is already at the baton's parent, the files land on its current branch
    wt2 = add_worktree(repo, tmp_path / "wt2", "third")
    git(wt2, "reset", "-q", "--hard", baton.parent)
    res2 = B.restore_baton(wt2, baton.ref)
    assert res2.restored and res2.branch == "third"


# ---------------------------------------------------------------------------
# local arbiter
# ---------------------------------------------------------------------------


def test_arbiter_local_decisions_dead_holders_and_replay(tmp_path):
    alive = {1, 2, 3}
    arb = Arbiter(tmp_path / "arbiter.json", is_alive=lambda pid: pid in alive)
    a = LocalSessionRef("cs_a", 1, "/repo")
    b = LocalSessionRef("cs_b", 2, "/wt-b")
    a2 = LocalSessionRef("cs_a2", 3, "/repo")
    snap = {
        "zones": [{"id": "zn_pos", "slug": "pos"}, {"id": "zn_pay", "slug": "pay", "fail_closed": True}],
        "claims": [
            {"id": "clm_1", "zone_id": "zn_pos", "holder_session_id": "cs_a", "state": "active", "mode": "exclusive", "epoch": 3}
        ],
    }
    arb.seed("crw_x", snap, {"cs_a": a, "cs_b": b})
    # seeded hold: B is denied regardless of snapshot age; A keeps its own
    assert arb.decide("crw_x", "zn_pos", b).result == "conflict"
    assert arb.decide("crw_x", "zn_pos", a).result == "granted"
    # a new local grant, persisted, pending replay
    assert arb.decide("crw_x", "zn_pay", b, fail_closed=True).result == "granted"
    assert Arbiter(tmp_path / "arbiter.json", is_alive=lambda p: True).pending_replay("crw_x")[0][0] == "zn_pay"
    # B dies: the fail_closed zone becomes adopt-only (reserved); another checkout cannot take it
    alive.discard(2)
    assert arb.decide("crw_x", "zn_pay", a).result == "conflict"
    # A dies: pos (not fail_closed) is freed for another local session
    alive.discard(1)
    assert arb.decide("crw_x", "zn_pos", a2).result == "granted"
    # replay results: a lost race removes the local grant
    arb.lost_race("crw_x", "zn_pos")
    assert "zn_pos" not in arb.leases("crw_x")
    arb.confirm("crw_x", "zn_pay", claim_id="clm_2", epoch=1)
    assert arb.pending_replay("crw_x") == []


def test_arbiter_reserved_fail_closed_zone_is_adoptable_from_the_same_checkout(tmp_path):
    alive = {1, 2}
    arb = Arbiter(tmp_path / "arbiter.json", is_alive=lambda pid: pid in alive)
    a = LocalSessionRef("cs_a", 1, "/repo")
    same = LocalSessionRef("cs_n", 2, "/repo")
    assert arb.decide("crw", "zn", a, fail_closed=True).result == "granted"
    alive.discard(1)
    assert arb.mark_dead("crw", "cs_a") == ["zn"]
    d = arb.decide("crw", "zn", same)
    assert d.result == "granted" and d.reason == "adopted"


# ---------------------------------------------------------------------------
# read-only fence
# ---------------------------------------------------------------------------


def test_fence_blocks_writes_and_restores_modes(tmp_path):
    wt = make_repo(tmp_path / "wt")
    (wt / "src/app/pos/tender.ts").chmod(0o640)
    fence = Fence(tmp_path / "fence")
    zone = {"id": "zn_pos", "slug": "pos", "include_globs": ["src/app/pos/**"], "exclude_globs": []}
    res = fence.apply(str(wt), [zone], case_insensitive=False)
    assert sorted(res.fenced) == ["src/app/pos", "src/app/pos/split.ts", "src/app/pos/tender.ts"]
    assert stat.S_IMODE((wt / "src/app/pos/tender.ts").stat().st_mode) == 0o440
    with pytest.raises(PermissionError):
        (wt / "src/app/pos/split.ts").write_text("apply_patch would fail\n")
    with pytest.raises(PermissionError):
        (wt / "src/app/pos/new.ts").write_text("cannot create either\n")
    (wt / "src/app/reports/export.ts").write_text("outside the zone is fine\n")
    record = json.loads(next((tmp_path / "fence").glob("*.json")).read_text())
    assert record["entries"]["src/app/pos/tender.ts"]["mode"] == 0o640
    # the zone is released: apply with no zones restores exactly the recorded modes
    res = fence.apply(str(wt), [], case_insensitive=False)
    assert sorted(res.restored) == ["src/app/pos", "src/app/pos/split.ts", "src/app/pos/tender.ts"]
    assert stat.S_IMODE((wt / "src/app/pos/tender.ts").stat().st_mode) == 0o640
    (wt / "src/app/pos/split.ts").write_text("writable again\n")
    assert list((tmp_path / "fence").glob("*.json")) == []


def test_fence_sweep_restores_after_a_crash_and_zone_selection(tmp_path):
    wt = make_repo(tmp_path / "wt")
    fence = Fence(tmp_path / "fence")
    fence.apply(str(wt), [{"include_globs": ["src/app/pos/**"]}], case_insensitive=False)
    assert Fence(tmp_path / "fence").sweep().restored  # a fresh crewd after a crash
    (wt / "src/app/pos/split.ts").write_text("ok\n")
    snap = {
        "zones": [{"id": "z1", "slug": "pos"}, {"id": "z2", "slug": "rep"}, {"id": "z3", "slug": "w"}],
        "claims": [
            {"zone_id": "z1", "holder_session_id": "cs_a", "state": "active", "mode": "exclusive"},
            {"zone_id": "z2", "holder_session_id": "cs_a", "state": "reserved", "mode": "exclusive", "reserved_for": "cs_x"},
            {"zone_id": "z3", "holder_session_id": "cs_a", "state": "active", "mode": "shared"},
        ],
    }
    assert [z["slug"] for z in zones_to_fence(snap, "cs_x")] == ["pos"]  # reserved for x itself, shared: not fenced
    assert [z["slug"] for z in zones_to_fence(snap, "cs_y")] == ["pos", "rep"]


# ---------------------------------------------------------------------------
# transcript detector
# ---------------------------------------------------------------------------


def _jl(path: Path, *objs: dict) -> None:
    with path.open("a") as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n")


def test_detector_codex_limit_then_quiet(tmp_path):
    t = tmp_path / "rollout.jsonl"
    _jl(
        t,
        {"timestamp": "2026-09-25T20:00:00Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        {
            "timestamp": "2026-09-25T20:01:00Z",
            "type": "event_msg",
            "payload": {"type": "error", "message": "You've hit your usage limit. Upgrade to Pro or try again in 3 hours."},
        },
    )
    base = SN.parse_ts("2026-09-25T20:01:00Z").timestamp()
    state = D.scan(t, "codex", D.TailState(), now=base)
    assert state.limit_at == base and "usage limit" in (state.limit_text or "")
    assert D.evaluate(state, now=base + 120, process_alive=True).reason == "waiting"
    assert D.evaluate(state, now=base + 301, process_alive=True).detected
    assert D.evaluate(state, now=base + 301, process_alive=False).detected is False  # dead = orphan path
    # tool activity after the message (hook activity) cancels it
    assert D.evaluate(state, now=base + 400, process_alive=True, last_hook_activity_at=base + 10).detected is False
    # incremental tail: new lines only; a later tool call clears the condition
    _jl(t, {"timestamp": "2026-09-25T20:02:00Z", "type": "event_msg", "payload": {"type": "exec_command_begin"}})
    state = D.scan(t, "codex", state, now=base + 60)
    assert D.evaluate(state, now=base + 900, process_alive=True).reason == "activity_after_limit"


def test_detector_claude_usage_limit_prefixes_and_plain_429(tmp_path):
    t = tmp_path / "t.jsonl"
    _jl(
        t,
        {
            "type": "assistant",
            "timestamp": "2026-09-25T20:00:00Z",
            "message": {
                "content": [
                    {"type": "text", "text": "API Error: Request rejected (429) · This request would exceed your rate limit."}
                ]
            },
        },
    )
    state = D.scan(t, "claude-code", D.TailState(), now=0)
    assert state.limit_at is None  # a transient 429 is not a usage limit (S0)
    _jl(
        t,
        {
            "type": "assistant",
            "timestamp": "2026-09-25T20:05:00Z",
            "message": {"content": [{"type": "text", "text": "You've hit your session limit · resets 3pm"}]},
        },
    )
    state = D.scan(t, "claude-code", state, now=0)
    assert state.limit_text and state.limit_text.startswith("You've hit your")


# ---------------------------------------------------------------------------
# zones.yml compiler and upload rule
# ---------------------------------------------------------------------------


def test_zones_upload_rule_default_branch_only(tmp_path):
    repo = make_repo(tmp_path / "repo")
    plan = ZC.plan_upload(str(repo), "main", None)
    assert plan.upload and plan.file is not None and plan.file.source == "default_branch"
    assert {z["slug"] for z in plan.result.compiled["zones"]} == {"pos", "reports"}
    assert ZC.plan_upload(str(repo), "main", plan.result.sha).reason == "unchanged"
    # an agent's uncommitted edit (loosening) is never uploaded
    (repo / ".remembra/zones.yml").write_text("version: 1\nzones: {}\n")
    assert ZC.plan_upload(str(repo), "main", plan.result.sha).reason == "unchanged"
    # nor a commit on a feature branch
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "commit", "-qam", "drop zones")
    assert ZC.plan_upload(str(repo), "main", plan.result.sha).reason == "unchanged"
    # a human push at a TTY uploads the working copy
    human = ZC.plan_upload(str(repo), "main", plan.result.sha, human_push=True, branch="feature")
    assert human.upload and human.file.source == "working_copy"
    # invalid YAML is reported, never uploaded
    bad = ZC.compile_text("zones:\n  pos: &a [x]\n  rep: *a\n")
    assert bad.ok is False and bad.errors
    body = ZC.upload_body(plan)
    assert set(body) == {"yaml", "sha", "branch"} and not S.validate(body, S.REQUEST_SHAPES["ZonesFile"])
    written = ZC.write_compiled(tmp_path, "crw_abc", plan.file, plan.result)
    assert json.loads(written.read_text())["ok"] is True
    assert ZONES_YML.startswith("version: 1")


# ---------------------------------------------------------------------------
# crewd helpers
# ---------------------------------------------------------------------------


def test_peer_credentials_on_a_real_unix_socket(tmp_path):
    path = f"/tmp/rc-test-{os.getpid()}.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        child = subprocess.Popen(
            [sys.executable, "-c", f"import socket,time;s=socket.socket(socket.AF_UNIX);s.connect({path!r});time.sleep(2)"]
        )
        conn, _ = srv.accept()
        peer = peer_credentials(conn)
        assert peer.pid == child.pid and peer.uid == os.getuid()
        chain = ancestors(child.pid)
        assert chain[0] == child.pid and os.getpid() in chain
        conn.close()
        child.kill()
        child.wait()
    finally:
        srv.close()
        os.unlink(path)


def test_find_agent_pid_skips_shells_and_prefers_the_adapter_binary():
    table = {500: (400, "python3.11"), 400: (300, "sh"), 300: (200, "claude"), 200: (1, "zsh")}
    assert find_agent_pid(500, "claude-code", table) == 300
    table2 = {500: (400, "python3.11"), 400: (300, "sh"), 300: (200, "node"), 200: (1, "zsh")}
    assert find_agent_pid(500, "claude-code", table2) == 300
    assert has_controlling_tty(os.getpid()) in (True, False)
    detached = subprocess.Popen(["sleep", "5"], start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        assert has_controlling_tty(detached.pid) is False
    finally:
        detached.kill()
        detached.wait()


def test_classify_stall_follows_d14():
    assert classify_stall("billing_error", None) == "quota"
    assert classify_stall("rate_limit", "You've hit your weekly limit") == "quota"
    assert classify_stall("rate_limit", "Server is temporarily limiting requests (not your usage limit)") == "transient"
    assert classify_stall("authentication_failed", "") == "auth"
    assert classify_stall("oauth_org_not_allowed", "") == "auth"
    assert classify_stall("server_error", "") == "transient"
    assert classify_stall("detected_limit", "") == "quota"


def test_tree_snapshot_is_names_and_counts_within_limits():
    files = [f"d{i}/sub{j}/deep/deeper/f{k}.ts" for i in range(30) for j in range(20) for k in range(2)]
    tree = build_tree(files)
    assert S.validate_tree(tree) == []
    assert tree["files"] == len(files)
    assert "f0.ts" not in json.dumps(tree)


def test_supervision_units_render_for_a_temp_home(tmp_path):
    home = tmp_path / "home"
    plist = plistlib.loads(launchd_plist(home, python="/opt/py/bin/python3").encode())
    assert plist["Label"] == "dev.remembra.crewd" and plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == ["/opt/py/bin/python3", "-m", "remembra.relay.crew.crewd", "--wait-lock"]
    assert plist["EnvironmentVariables"]["HOME"] == str(home)
    assert str(unit_path(home, "Darwin")).startswith(str(home))
    unit = systemd_unit(home, python="/opt/py/bin/python3")
    assert "Restart=always" in unit and "--wait-lock" in unit
    assert str(unit_path(home, "Linux")) == str(home / ".config/systemd/user/remembra-crewd.service")
    if sys.platform == "darwin":
        path = tmp_path / "unit.plist"
        path.write_text(launchd_plist(home))
        assert subprocess.run(["plutil", "-lint", str(path)], capture_output=True).returncode == 0


def test_single_instance_lock_and_supervisor_wait(tmp_path):
    from remembra.relay.crew.crewd import acquire_lock

    layout = Layout(tmp_path / "home")
    fd = acquire_lock(layout, wait=False)
    assert fd is not None
    # a second (hook-spawned) instance exits at once
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "from remembra.relay.crew.crewd import acquire_lock;"
            "from remembra.relay.crew.gate import Layout;from pathlib import Path;import sys;"
            f"sys.exit(0 if acquire_lock(Layout(Path({str(layout.home)!r})), wait=False) is None else 3)",
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=30,
    )
    assert res.returncode == 0
    os.close(fd)
    assert acquire_lock(layout, wait=False) is not None


def test_githook_state_plain_husky_and_lefthook(tmp_path):
    repo = make_repo(tmp_path / "repo")
    assert githook_state(str(repo)) == ("missing", ["pre-commit", "prepare-commit-msg", "pre-push"])
    hooks = repo / ".git" / "hooks"
    for h in ("pre-commit", "prepare-commit-msg", "pre-push"):
        (hooks / h).write_text(f"#!/bin/sh\nexec python -I ~/.remembra/crew/bin/crew-gate.py x {S.CREW_HOOK_MARKER}\n")
    assert githook_state(str(repo))[0] == "ok"
    # Husky v9 moved core.hooksPath: the gate is chained through .husky/<hook>
    (repo / ".husky" / "_").mkdir(parents=True)
    git(repo, "config", "core.hooksPath", ".husky/_")
    assert githook_state(str(repo))[0] == "missing"
    for h in ("pre-commit", "prepare-commit-msg", "pre-push"):
        (repo / ".husky" / h).write_text(f"npm test\n. ~/.remembra/crew/bin/husky.sh {S.CREW_HOOK_MARKER}\n")
    assert githook_state(str(repo))[0] == "chained"
    git(repo, "config", "--unset", "core.hooksPath")
    for h in ("pre-commit", "prepare-commit-msg", "pre-push"):
        (hooks / h).unlink()
    (repo / "lefthook-local.yml").write_text(
        f"pre-commit:\n  commands:\n    crew:\n      run: crew-gate precommit {S.CREW_HOOK_MARKER}\n"
    )
    assert githook_state(str(repo))[0] == "chained"


def test_clean_unpushed_baton_restores_committed_work_on_another_clone(tmp_path):
    remote = tmp_path / "remote.git"
    repo = make_repo(tmp_path / "repo", remote=remote)
    original_head = git(repo, "rev-parse", "HEAD")
    assert B.create_baton_ref(repo, "T-5") is None  # fully published and clean
    (repo / "src/app/pos/split.ts").write_text("committed but unpushed\n")
    git(repo, "commit", "-qam", "local work")
    saved_head = git(repo, "rev-parse", "HEAD")
    assert B.is_clean(repo)
    index_before = (repo / ".git" / "index").read_bytes()
    baton = B.create_baton_ref(repo, "T-5")
    assert baton is not None
    assert baton.parent == saved_head and baton.unpushed == [saved_head]
    assert baton.dirty_files == []
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert B.is_clean(repo) and git(repo, "rev-parse", "HEAD") == saved_head
    assert B.push_baton(repo, baton, "origin")
    assert git(remote, "rev-parse", "refs/heads/main") == original_head
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)], check=True, capture_output=True)
    result = B.restore_baton(other, baton.ref, remote="origin")
    assert result.restored, result
    assert git(other, "rev-parse", "HEAD") == saved_head
    assert (other / "src/app/pos/split.ts").read_text() == "committed but unpushed\n"
    assert B.is_clean(other)
