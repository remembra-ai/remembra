"""The §13.3 E2E variants b–h, model-free (WP-15).

Same machinery as ``test_three_agents.py``: a real crew-mode server, real crewd, the gate and CLI
installed by the real ``connect`` into a temp HOME, fake agent processes running the installed hook
commands with S0-shaped payloads. Each variant gets its own world.

E2E-c (orphan, up to 60 s) and E2E-f (fencing, a 120 s lease) are slow; they run when
``REMEMBRA_CREW_SLOW=1`` (the crew-e2e workflow sets it).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from tests.crew.e2e.harness import FakeAgent, World, git, wait_for

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
slow = pytest.mark.skipif(os.environ.get("REMEMBRA_CREW_SLOW") != "1", reason="slow variant: set REMEMBRA_CREW_SLOW=1")


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        w.close()


def of_type(events: list[dict[str, Any]], etype: str, **match: Any) -> list[dict[str, Any]]:
    def dig(obj: Any, dotted: str) -> Any:
        for part in dotted.split("."):
            obj = obj.get(part) if isinstance(obj, dict) else None
        return obj

    return [e for e in events if e["type"] == etype and all(dig(e, k) == v for k, v in match.items())]


def crew_of(agent: FakeAgent) -> str:
    return str(agent.local_session()["crew_id"])


def sid_of(agent: FakeAgent) -> str:
    return str(agent.local_session()["session_id"])


def zone_id(world: World, crew: str, slug: str) -> str:
    return next(z["id"] for z in world.snapshot(crew)["zones"] if z["slug"] == slug)


def human_task(world: World, crew: str, title: str, zones: list[str], key: str) -> dict[str, Any]:
    with world.human() as h:
        r = h.post(
            f"/crews/{crew}/tasks",
            json={"title": title, "zone_ids": zones, "acceptance": [], "depends_on": []},
            headers={"Idempotency-Key": key},
        )
    assert r.status_code == 201, r.text
    return dict(r.json()["task"])


def denied(result: dict[str, Any]) -> str:
    assert result["denied"], json.dumps(result)[:2000]
    return str(result["reason"])


def allowed(result: dict[str, Any]) -> dict[str, Any]:
    assert not result["denied"], result.get("reason")
    return result


# ---------------------------------------------------------------------------
# E2E-h: MCP writes
# ---------------------------------------------------------------------------


def test_e2e_h_mcp_writes_are_gated(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = crew_of(a)
    feed = world.feed(crew)
    held = a.cli("claim", "schema:main")
    assert held.returncode == 0 and held.stdout.startswith("GRANTED schema:main"), held.stdout + held.stderr
    allowed(a.write("src/app/pos/split.ts", "export const split = 3;\n"))  # A auto-claims POS

    b = world.agent("B", wts["wt-b"])
    b.start()
    migration = {"project_id": "prj", "name": "add_tip", "query": "alter table t add column tip int"}
    reason = denied(b.mcp("mcp__supabase__apply_migration", migration))
    assert "schema:main" in reason and "cc-1" in reason, reason
    ddl = denied(b.mcp("mcp__supabase__execute_sql", {"project_id": "prj", "query": "drop table t"}))
    assert "schema:main" in ddl, ddl
    fs = denied(b.mcp("mcp__filesystem__write_file", {"path": str(wts["wt-b"] / "src/app/pos/split.ts"), "content": "x"}))
    assert 'zone "pos"' in fs and "cc-1" in fs, fs
    # reads and non-DDL SQL are not gated
    allowed(b.mcp("mcp__supabase__list_tables", {"project_id": "prj"}))
    allowed(b.mcp("mcp__supabase__execute_sql", {"project_id": "prj", "query": "select 1"}))
    allowed(b.mcp("mcp__filesystem__write_file", {"path": str(wts["wt-b"] / "src/app/reports/new.ts"), "content": "x"}))
    assert wait_for(lambda: len(of_type(feed.events(), "guard.blocked", **{"actor.id": sid_of(b)})) >= 1, timeout=15)


# ---------------------------------------------------------------------------
# E2E-d: C starts in A's worktree → auto-adopt, no command
# ---------------------------------------------------------------------------


def test_e2e_d_same_checkout_auto_adopts(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = crew_of(a)
    feed = world.feed(crew)
    pos = zone_id(world, crew, "pos")
    human_task(world, crew, "POS split tender", [pos], "d-t1")
    assert "T-1 in_progress" in allowed(a.bash("remembra-crew task start T-1"))["response"]["stdout"]
    allowed(a.write("src/app/pos/tender.ts", "export const tender = 5; // wip\n"))
    a.stop_failure("billing_error", "Credit balance is too low")
    a.kill()
    assert wait_for(lambda: of_type(feed.events(), "claim.reserved", **{"payload.claim.zone_id": pos}), timeout=10)

    c = world.agent("C", wts["wt-a"])  # the same checkout as A
    brief = c.start()
    c_sid = sid_of(c)
    assert "YOUR BATON" not in brief and "remembra-crew adopt" not in brief, brief  # nothing to run: already adopted
    ev = wait_for(lambda: of_type(feed.events(), "baton.passed", **{"payload.to_session": c_sid}), timeout=10)
    assert ev and ev[-1]["payload"]["kind"] == "same_checkout" and ev[-1]["payload"]["from_session"] == sid_of_a(feed), ev
    adopted = of_type(feed.events(), "claim.adopted", **{"payload.claim.zone_id": pos})
    assert adopted and adopted[-1]["payload"]["claim"]["holder_session_id"] == c_sid, adopted
    assert adopted[-1]["payload"]["cross_checkout"] is False
    allowed(c.edit("src/app/pos/tender.ts", "// wip", "// done"))  # no deny, no adopt command
    assert (wts["wt-a"] / "src/app/pos/tender.ts").read_text() == "export const tender = 5; // done\n"
    snap = world.snapshot(crew)
    t1 = next(t for t in snap["tasks"] if t["number"] == 1)
    assert t1["owner_session_id"] == c_sid, t1


def sid_of_a(feed: Any) -> str:
    return str(next(e for e in feed.events() if e["type"] == "session.joined")["actor"]["id"])


# ---------------------------------------------------------------------------
# E2E-b: B as Codex (advisory, unverified) in its own worktree
# ---------------------------------------------------------------------------


def test_e2e_b_codex_fence_git_gate_and_post_hoc_collision(world: World) -> None:
    wts = world.setup(("wt-a", "wt-b", "wt-c"), "--include-unverified", "--agent", "claude-code", "--agent", "codex")
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = crew_of(a)
    feed = world.feed(crew)
    allowed(a.write("src/app/pos/split.ts", "export const split = 3;\n"))  # A holds POS

    b = world.agent("B", wts["wt-b"], adapter="codex")
    brief = b.start()
    assert "DO NOT TOUCH: zone pos → cc-1" in brief, brief
    assert b.local_session()["enforcement"] == "advisory" and b.local_session()["callsign"] == "codex-1"
    b_sid = sid_of(b)
    split = wts["wt-b"] / "src/app/pos/split.ts"
    before = split.read_text()

    # the read-only fence: apply_patch (a write the Codex hooks never see) cannot touch POS in B's worktree
    assert wait_for(lambda: not (split.stat().st_mode & 0o222), timeout=15), oct(split.stat().st_mode)
    blocked = b.write_raw(split, "codex patch\n")
    assert blocked == {**blocked, "written": False, "exception": "PermissionError"}, blocked
    assert split.read_text() == before
    free = wts["wt-b"] / "src/app/reports/export.ts"
    assert free.stat().st_mode & 0o200  # only held zones are fenced

    # removed by hand: the write lands, the delta records it, the commit gate refuses, the server detects it
    split.chmod(split.stat().st_mode | stat.S_IWUSR)
    assert b.write_raw(split, "codex patch\n")["written"] is True
    allowed(b.bash("git add -A"))
    head = git(wts["wt-b"], "rev-parse", "HEAD")
    commit = allowed(b.bash("git commit -qm 'pos by codex'"))
    assert commit["ok"] is False and "BLOCKED by Remembra Crew" in commit["response"]["stderr"], commit["response"]
    assert "held EXCLUSIVELY by cc-1" in commit["response"]["stderr"]
    assert git(wts["wt-b"], "rev-parse", "HEAD") == head  # nothing was committed
    assert b.cli("renew").returncode == 0  # heartbeat now (footprints travel with it)

    def breach() -> list[dict[str, Any]]:
        with world.human() as h:
            items = h.get(f"/crews/{crew}/collisions").json()["collisions"]
        return [
            c for c in items if c["kind"] == "exclusive_breach" and c["session_b"] == sid_of_a(feed) and c["session_a"] == b_sid
        ]

    found = wait_for(breach, timeout=20)
    assert found and found[0]["subject"] == "src/app/pos/split.ts" and found[0]["attribution"] == "probable", found
    blocks = of_type(feed.events(), "guard.blocked", **{"actor.id": b_sid})
    assert any(e["payload"]["surface"] == "precommit" for e in blocks), blocks


# ---------------------------------------------------------------------------
# E2E-e: A and B in one checkout; B's unparseable command is not blamed for A's edit
# ---------------------------------------------------------------------------


def test_e2e_e_shared_checkout_is_not_misattributed(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = crew_of(a)
    feed = world.feed(crew)
    b = world.agent("B", wts["wt-a"])  # the same checkout
    assert "another session works in this same checkout" in b.start()
    b_sid = sid_of(b)
    allowed(a.write("src/app/pos/split.ts", "export const split = 4;\n"))  # A holds POS and edits it
    odd = b.bash("echo $(( 1 +")  # unparseable: the gate cannot tell what it touches
    assert not odd["denied"]
    stop = b.stop()
    assert all(not (h.get("stdout") or "").strip() for h in stop if not h.get("async")), stop  # no Stop block for B
    assert b.cli("renew").returncode == 0
    time.sleep(1.0)
    with world.human() as h:
        collisions = h.get(f"/crews/{crew}/collisions").json()["collisions"]
    assert not [c for c in collisions if b_sid in (c["session_a"], c["session_b"])], collisions
    assert "src/app/pos/split.ts" not in (b.local_session().get("known_footprints") or [])
    assert not of_type(feed.events(), "collision.detected")


# ---------------------------------------------------------------------------
# E2E-g: no zones.yml → temporary zones when the second agent joins
# ---------------------------------------------------------------------------


def test_e2e_g_no_zone_bootstrap(tmp_path: Path) -> None:
    w = World(tmp_path, zones=None)
    try:
        wts = w.setup()
        w.layout.root.mkdir(parents=True, exist_ok=True)
        (w.layout.root / "config.json").write_text(json.dumps({"crew_all_repos": True}))  # the owner's opt-in
        a = w.agent("A", wts["wt-a"])
        assert "CREW" in a.start()
        crew = crew_of(a)
        feed = w.feed(crew)
        assert [z for z in w.snapshot(crew)["zones"] if z["source"] != "builtin"] == []
        b = w.agent("B", wts["wt-b"])
        brief = b.start()
        assert "TEMPORARY ZONES" in brief, brief
        applied = wait_for(lambda: of_type(feed.events(), "zone.suggested_applied"), timeout=10)
        assert applied and applied[0]["payload"]["undo_available"] is True
        zones = {z["slug"]: z for z in w.snapshot(crew)["zones"] if z["source"] == "suggested"}
        assert {"pos", "reports"} <= set(zones), zones
        allowed(a.write("src/app/pos/split.ts", "export const split = 5;\n"))  # A works in its feature folder
        reason = denied(b.write("src/app/pos/split.ts", "export const split = 6;\n"))  # enforced at once
        assert 'zone "pos"' in reason and "cc-1" in reason, reason
        allowed(b.write("src/app/reports/export.ts", "export const exportCsv = () => 'b';\n"))
    finally:
        w.close()


# ---------------------------------------------------------------------------
# E2E-c: kill -9 with no StopFailure → the orphan path
# ---------------------------------------------------------------------------


@slow
def test_e2e_c_orphan_kill_9(world: World) -> None:
    wts = world.setup()
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = crew_of(a)
    feed = world.feed(crew)
    pos = zone_id(world, crew, "pos")
    task = human_task(world, crew, "POS split tender", [pos], "c-t1")
    assert "T-1 in_progress" in allowed(a.bash("remembra-crew task start T-1"))["response"]["stdout"]
    allowed(a.write("src/app/pos/split.ts", "export const split = 7;\n"))
    allowed(a.bash("git add -A && git commit -qm 'pos: split'"))
    sha = git(wts["wt-a"], "rev-parse", "HEAD")
    allowed(a.write("src/app/pos/tender.ts", "export const tender = 7; // wip\n"))
    a_sid = sid_of(a)
    t0 = time.time()
    a.kill()  # no StopFailure, no SessionEnd

    def orphaned() -> bool:
        ev = feed.events()
        return all(of_type(ev, t) for t in ("session.lost", "baton.ref_created", "claim.reserved", "task.stalled"))

    assert wait_for(orphaned, timeout=60), " ".join(feed.types())
    assert time.time() - t0 <= 60
    ev = feed.events()
    lost = of_type(ev, "session.lost", **{"refs.session_id": a_sid})
    assert lost and lost[0]["payload"]["reason"] == "process_exited", lost
    ref = of_type(ev, "baton.ref_created")[0]["payload"]
    assert ref["dirty_files"] == 1, ref  # crewd saved tender.ts
    assert "tender.ts" in git(wts["main"], "show", "--stat", "--format=", ref["ref"])
    reserved = of_type(ev, "claim.reserved", **{"payload.claim.zone_id": pos})
    assert reserved and reserved[0]["payload"]["reason"] == "lost", reserved
    with world.human() as h:
        reports = h.get(f"/tasks/{task['id']}/reports").json()["reports"]
    stalled = next(r for r in reports if r["kind"] == "stalled")
    assert stalled["facts_source"] == "server-inferred" and stalled["baton_ref"] == ref["ref"], stalled
    assert sha in stalled["commits"], stalled
    handoff = of_type(ev, "handoff.created")
    assert handoff and handoff[-1]["payload"]["end_reason"] == "orphaned", handoff


# ---------------------------------------------------------------------------
# E2E-f: A's network is cut; fencing at the horizon; a human hands POS to C
# ---------------------------------------------------------------------------


@slow
def test_e2e_f_fencing_when_the_network_is_cut(world: World) -> None:
    import httpx

    wts = world.setup()
    proxy = world.through_proxy()  # A's machine reaches the API only through this link
    a = world.agent("A", wts["wt-a"])
    a.start()
    crew = crew_of(a)
    feed = world.feed(crew)
    with world.human() as h:
        v = h.get(f"/crews/{crew}").json()["settings_version"]
        r = h.patch(f"/crews/{crew}", json={"settings": {"lease_ttl_s": 120}}, headers={"If-Match": str(v)})
        assert r.status_code == 200, r.text
    allowed(a.write("src/app/pos/split.ts", "export const split = 8;\n"))
    assert a.cli("renew").returncode == 0  # a fresh lease (120 s) with the new setting
    pos = zone_id(world, crew, "pos")
    claim = next(c for c in world.snapshot(crew)["claims"] if c["zone_id"] == pos)
    a_sid = sid_of(a)

    proxy.cut()
    t_cut = time.time()
    edits, reconnecting = 0, None
    while time.time() - t_cut < 150:
        res = a.write("src/app/pos/split.ts", f"export const split = {100 + edits};\n")
        if res["denied"]:
            reconnecting = res["reason"]
            break
        edits += 1
        time.sleep(5)
    assert reconnecting and "reconnecting" in reconnecting, reconnecting
    fenced_after = time.time() - t_cut
    assert fenced_after <= 75, fenced_after  # at the horizon (lease − 60 s), not at expiry

    # the server reserves POS for A at lease expiry (the reaper sweeps every 30 s)
    def reserved_for_a() -> bool:
        c = next((c for c in world.snapshot(crew)["claims"] if c["id"] == claim["id"]), None)
        return bool(c and c["state"] == "reserved" and c["reserved_for"] == a_sid)

    assert wait_for(reserved_for_a, timeout=120, interval=2), [c for c in world.snapshot(crew)["claims"]]
    held = next(c for c in world.snapshot(crew)["claims"] if c["id"] == claim["id"])
    assert held["reserve_reason"] == "offline", held  # host silence is not a stall (§10.1): only A may re-take it

    # C works on another machine (its own host and crewd; here its API calls are made directly)
    assert world.server is not None
    key = {"X-API-Key": world.server.key}
    with httpx.Client(base_url=world.server.url + "/api/v1", headers=key, timeout=30) as api:
        host = api.post(
            "/crew/hosts/register", json={"host_label": "otherhost", "platform": "linux", "crewd_version": "1.0.0"}
        ).json()
        body = {
            "project_id": world.snapshot(crew)["crew"]["project_id"],
            "agent_id": "claude-code",
            "session_id": "c-remote",
            "adapter": "claude-code",
            "client_kind": "hook",
            "host_id": host["host_id"],
            "checkout_fp": "fp-remote",
            "worktree_id": "wt-remote",
            "branch": "main",
            "head": git(wts["main"], "rev-parse", "HEAD"),
            "source": "startup",
        }
        join = api.post("/crews/join", headers={"X-Remembra-Host-Token": host["host_token"]}, json=body)
        assert join.status_code in (200, 201), join.text
        c_sid = join.json()["session_id"]
    with world.human() as h:
        r = h.post(f"/claims/{claim['id']}/override", json={"action": "transfer", "to": c_sid, "reason": "A is offline"})
        assert r.status_code == 200, r.text
    assert of_type(feed.events(), "claim.transferred") or wait_for(
        lambda: of_type(feed.events(), "claim.transferred"), timeout=10
    )

    proxy.restore()
    assert a.cli("renew").returncode == 0  # reconnect: heartbeat + snapshot
    again = a.write("src/app/pos/split.ts", "export const split = 999;\n")
    assert again["denied"], again  # denied at once: C holds POS now (epoch moved on)
    with world.human() as h:
        collisions = h.get(f"/crews/{crew}/collisions").json()["collisions"]
    assert not [c for c in collisions if c["kind"] == "stale_epoch_write"], collisions
