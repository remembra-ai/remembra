"""F10: inside a TEMPORARY (auto-derived) folder zone an agent claims the file it edits, not the whole folder.

A crew with no ``zones.yml`` gets one exclusive zone per top-level folder (D37). One file in
``lib/`` used to lock all of ``lib/`` for every other agent. Zones a human declared keep their
rules: there a path is claimed through its zone.
"""

from __future__ import annotations

import pytest

from remembra.crew import claims as C
from remembra.crew import zones as Z
from tests.crew.wp5_support import CREW, OWNER, assert_chain_ok, make_ops, open_db, seed_crew, seed_session

HUMAN = Z.Principal.human(OWNER, privileged=True)

TREE = {
    "name": ".",
    "files": 2,
    "children": [
        {"name": "lib", "files": 9, "children": []},
        {
            "name": "app",
            "files": 1,
            "children": [
                {"name": "api", "files": 6, "children": []},
                {"name": "portal", "files": 4, "children": []},
            ],
        },
    ],
}


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-3", worktree_id="wt-a", checkout_fp="fp-a")
    b, _ = await seed_session(db, "cs_b", callsign="codex-5", agent_id="codex", worktree_id="wt-b", checkout_fp="fp-b")
    res = await Z.put_tree(ops, CREW, Z.Principal.for_session(a), TREE)
    assert len(res["bootstrap_zone_ids"]) == 3
    try:
        yield db, ops, Z.Principal.for_session(a), Z.Principal.for_session(b)
    finally:
        await assert_chain_ok(db)
        await db.close()


async def _zones(db) -> dict[str, dict]:
    return {r["slug"]: r for r in await Z.load_zone_rows(db.conn, CREW)}


async def test_two_agents_hold_different_files_in_one_temporary_zone(env):
    db, ops, a, b = env
    zones = await _zones(db)
    assert zones["lib"]["source"] == "suggested" and zones["lib"]["include_globs"] == '["lib/**"]'
    mine = await C.request_claim(ops, CREW, a, path_glob="lib/team-access.ts")
    assert mine.status == "granted" and mine.claim["path_glob"] == "lib/team-access.ts" and mine.claim["zone_id"] is None
    theirs = await C.request_claim(ops, CREW, b, path_glob="lib/sales-research.ts")
    assert theirs.status == "granted"
    # a file that does not exist yet is claimable too, and the same file still conflicts
    assert (await C.request_claim(ops, CREW, b, path_glob="lib/brand-new.ts")).status == "granted"
    clash = await C.request_claim(ops, CREW, b, path_glob="lib/team-access.ts")
    assert clash.status == "denied" and clash.error == "conflict"
    assert clash.body()["holder"] == "cc-3" and clash.body()["claim"]["path_glob"] == "lib/team-access.ts"
    # the whole zone cannot be taken over files others hold in it, and the refusal carries the file
    whole = await C.request_claim(ops, CREW, b, zone_id=zones["lib"]["id"])
    assert whole.status == "denied" and whole.body()["claim"]["path_glob"] == "lib/team-access.ts"
    assert whole.body()["claim"]["zone_id"] is None
    # many files in one temporary zone are one area for the caps
    for n in range(6):
        assert (await C.request_claim(ops, CREW, a, path_glob=f"lib/f{n}.ts")).status == "granted"
    session = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_a'")
    assert (await C.exclusive_counts(db.conn, session))[0] == 1


async def test_a_whole_zone_claim_still_blocks_file_claims_in_it(env):
    db, ops, a, b = env
    zones = await _zones(db)
    assert (await C.request_claim(ops, CREW, a, zone_id=zones["api"]["id"])).status == "granted"
    denied = await C.request_claim(ops, CREW, b, path_glob="app/api/fulfil/route.ts")
    assert denied.status == "denied" and denied.body()["holder"] == "cc-3"
    assert denied.body()["claim"]["zone_id"] == zones["api"]["id"]


async def test_only_literal_files_in_unrestricted_temporary_zones(env):
    db, ops, a, b = env
    zones = await _zones(db)
    for glob in ("lib/**", "lib/*.ts", "lib/{a,b}.ts"):
        with pytest.raises(Z.CrewOpError) as err:
            await C.request_claim(ops, CREW, a, path_glob=glob)
        assert err.value.error == "zone_path", glob
    # a zone a human protected or froze keeps the zone rules, temporary or not
    await db.conn.execute("UPDATE crew_zones SET protected = 1 WHERE id = ?", (zones["portal"]["id"],))
    await db.conn.execute("UPDATE crew_zones SET frozen_by = ?, frozen_until = NULL WHERE id = ?", (OWNER, zones["api"]["id"]))
    await db.conn.commit()
    for path in ("app/portal/page.tsx", "app/api/x/route.ts"):
        with pytest.raises(Z.CrewOpError) as err:
            await C.request_claim(ops, CREW, a, path_glob=path)
        assert err.value.error == "zone_path", path


async def test_declared_zones_keep_claim_the_zone(env):
    db, ops, a, b = env
    assert (await C.request_claim(ops, CREW, a, path_glob="lib/a.ts")).status == "granted"
    await Z.upload_zones_file(ops, CREW, a, yaml_text="zones:\n  portal: [app/portal/**]\n", sha="r", branch="main")
    zones = await _zones(db)
    assert zones["portal"]["source"] == "repo"
    with pytest.raises(Z.CrewOpError) as err:
        await C.request_claim(ops, CREW, b, path_glob="app/portal/page.tsx")
    assert err.value.error == "zone_path" and "claim the zone instead" in err.value.message


# ---------------------------------------------------------------------------
# The gate (hook sessions, and crew_guard on the server) follows the same rule
# ---------------------------------------------------------------------------


def _gate_snap(*, features: bool = True, source: str = "suggested"):
    from remembra.crew import schemas as S
    from tests.crew.gatecore_support import find, snapshot

    snap = snapshot()
    # a bootstrap zone: one flat folder, exclusive leaf, auto-claimed
    lib = {**find(snap["zones"], "zn_invoices"), "id": "zn_lib", "slug": "lib", "title": "lib", "parent_id": None}
    snap["zones"].append({**lib, "include_globs": ["lib/**"], "source": source})
    if features:
        snap["features"] = list(S.SNAPSHOT_FEATURES)
    return snap


def _file_claim(snap, claim_id: str, path: str, holder: str, **over):
    from tests.crew.gatecore_support import find

    base = find(snap["claims"], "clm_pos")
    row = {**base, "id": claim_id, "zone_id": None, "path_glob": path, "holder_session_id": holder, "task_id": None, **over}
    snap["claims"].append(row)
    return row


NEW = {"file_path": "/w/yaadbooks-c/lib/new.ts", "content": "x"}
OTHER = {"file_path": "/w/yaadbooks-c/lib/other.ts", "content": "x"}


def test_first_write_in_a_temporary_zone_claims_the_file_not_the_folder():
    from remembra.crew import gatecore as G
    from tests.crew.gatecore_support import run

    calls: list = []
    v = run("Write", NEW, snap=_gate_snap(), claim=lambda r: calls.append(r) or "granted")
    assert (v.rule, v.decision, v.variant) == (17, "allow", "auto_claimed")
    assert calls == [G.ClaimRequest(None, None, "exclusive", path_glob="lib/new.ts")]


def test_an_older_server_or_a_declared_zone_or_no_server_keeps_the_zone_claim():
    from remembra.crew import gatecore as G
    from tests.crew.gatecore_support import run

    zone_req = [G.ClaimRequest("zn_lib", "lib", "exclusive")]
    for snap, kw in (
        (_gate_snap(features=False), {}),  # a 0.17.5 server refuses a path inside a zone
        (_gate_snap(source="repo"), {}),  # a zone a human declared
        (_gate_snap(), {"server_reachable": False}),  # offline: the local arbiter decides whole zones
    ):
        calls: list = []
        v = run("Write", NEW, snap=snap, claim=lambda r, c=calls: c.append(r) or "granted", **kw)
        assert v.decision == "allow" and calls == zone_req, (v.variant, calls)
    for field, value in (("protected", True), ("reserve_for", "codex"), ("frozen_by", "u_mani")):
        snap = _gate_snap()
        next(z for z in snap["zones"] if z["id"] == "zn_lib")[field] = value
        calls = []
        run("Write", NEW, snap=snap, claim=lambda r, c=calls: c.append(r) or "granted")
        assert not any(r.path_glob for r in calls), field


def test_file_claims_inside_a_temporary_zone_decide_the_write():
    from tests.crew.gatecore_support import run

    snap = _gate_snap()
    _file_claim(snap, "clm_mine", "lib/new.ts", "cs_c")
    _file_claim(snap, "clm_theirs", "lib/other.ts", "cs_a")
    calls: list = []
    mine = run("Write", NEW, snap=snap, claim=lambda r: calls.append(r) or "granted")
    assert (mine.rule, mine.decision) == (7, "allow") and calls == []  # own file claim, no second claim
    theirs = run("Write", OTHER, snap=snap, claim=lambda r: calls.append(r) or "granted")
    assert theirs.decision == "deny" and calls == []
    assert "lib/other.ts is claimed EXCLUSIVELY by cc-1" in (theirs.reason or "")
    assert 'zone "lib"' not in (theirs.reason or "")
    # offline the file claims in the snapshot still decide; only a new claim falls back to the zone
    assert run("Write", OTHER, snap=snap, claim=lambda r: "granted", server_reachable=False).decision == "deny"
    assert run("Write", NEW, snap=snap, claim=lambda r: "conflict", server_reachable=False).rule == 7
    # without the feature (an older server) a path claim inside a zone decides nothing, as before
    old = _gate_snap(features=False)
    _file_claim(old, "clm_theirs", "lib/other.ts", "cs_a")
    v = run("Write", OTHER, snap=old, claim=lambda r: "granted")
    assert (v.rule, v.decision) == (17, "allow")


def test_a_whole_zone_holder_still_blocks_and_the_refusals_name_the_file():
    from tests.crew.gatecore_support import find, run

    snap = _gate_snap()
    snap["claims"].append({**find(snap["claims"], "clm_pos"), "id": "clm_inv", "zone_id": "zn_lib", "task_id": None})
    held = run("Write", NEW, snap=snap, claim=lambda r: "granted")
    assert (held.rule, held.decision) == (9, "deny") and 'zone "lib", held EXCLUSIVELY by cc-1' in (held.reason or "")
    lost = run("Write", NEW, snap=_gate_snap(), claim=lambda r: G_result("conflict", "cs_a"))
    assert (lost.rule, lost.decision, lost.variant) == (17, "deny", "claim_conflict")
    assert "lib/new.ts was just claimed by cc-1. Do not edit it." in (lost.reason or "")
    assert "Do not edit files in zone" not in (lost.reason or "")
    off = _gate_snap()
    off["settings"]["auto_claim"] = False
    need = run("Write", NEW, snap=off, claim=lambda r: "granted")
    assert (need.rule, need.decision, need.variant) == (17, "deny", "claim_required")
    assert 'call crew_claim(paths=["lib/new.ts"])' in (need.reason or "")


def G_result(result: str, winner: str):
    from remembra.crew import gatecore as G

    return G.ClaimResult(result, winner_session_id=winner)


def test_many_files_in_one_temporary_zone_use_one_slot_of_the_cap():
    from tests.crew.gatecore_support import run

    snap = _gate_snap()
    for n in range(5):
        _file_claim(snap, f"clm_f{n}", f"lib/f{n}.ts", "cs_c")
    _file_claim(snap, "clm_s", "scripts/a.sh", "cs_c")
    _file_claim(snap, "clm_t", "tools/a.sh", "cs_c")
    calls: list = []
    v = run("Write", NEW, snap=snap, claim=lambda r: calls.append(r) or "granted")  # 3 areas held, this one included
    assert (v.rule, v.decision) == (17, "allow") and len(calls) == 1


# ---------------------------------------------------------------------------
# crew_guard on the server (advisory MCP sessions): auto-claims the file for real
# ---------------------------------------------------------------------------


async def test_server_guard_auto_claims_the_file_and_lets_a_second_agent_into_the_folder(env):
    db, ops, a, b = env
    first = await C.server_guard(ops, CREW, a, op="write", paths=["lib/team-access.ts"])
    assert first["decision"] == "allow", first
    assert [(c["path_glob"], c["zone_id"]) for c in first["auto_claimed"]] == [("lib/team-access.ts", None)]
    second = await C.server_guard(ops, CREW, b, op="write", paths=["lib/sales-research.ts"])
    assert second["decision"] == "allow", second
    assert [c["path_glob"] for c in second["auto_claimed"]] == ["lib/sales-research.ts"]
    blocked = await C.server_guard(ops, CREW, b, op="write", paths=["lib/team-access.ts"])
    assert blocked["decision"] == "deny" and "cc-3" in " ".join(blocked["reasons"])
    rows = await db.fetchall("SELECT path_glob, zone_id, source FROM crew_claims WHERE crew_id = ? ORDER BY created_at", (CREW,))
    assert [dict(r) for r in rows] == [
        {"path_glob": "lib/team-access.ts", "zone_id": None, "source": "first_write"},
        {"path_glob": "lib/sales-research.ts", "zone_id": None, "source": "first_write"},
    ]


async def test_the_snapshot_tells_the_gate_the_server_takes_file_claims_in_temporary_zones(env):
    from remembra.crew import schemas as S
    from remembra.crew.core import CrewCore

    db, ops, a, b = env
    session = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_a'")
    assert (await C.guard_snapshot(db.conn, CREW, session))["features"] == ["temp_zone_file_claims"]
    snap = await CrewCore(db).snapshot(CREW)
    assert snap["features"] == ["temp_zone_file_claims"] and S.validate(snap, S.SNAPSHOT) == []
    # a 0.17.5 snapshot (no features) is still a valid snapshot
    assert S.validate({k: v for k, v in snap.items() if k != "features"}, S.SNAPSHOT) == []


async def test_a_task_cannot_take_a_zone_over_files_another_session_holds_in_it(tmp_path):
    """Task start claims whole zones: it used to look at zone claims only, so it would have been granted
    the folder on top of another session's file."""
    from remembra.crew.tasks import Caller, CrewServiceError, TaskService
    from tests.crew import wp6_support as W

    db = await W.open_db(tmp_path)
    try:
        await W.seed_crew(db)
        svc = TaskService(W.event_log(db))
        a = await W.seed_session(db, callsign="cc-3")
        b = await W.seed_session(db, callsign="codex-5", agent_id="codex")
        human = Caller.for_human("u_owner", privileged=True)
        lib = await W.seed_zone(db, slug="lib", includes=["lib/**"])
        api = await W.seed_zone(db, slug="api", includes=["app/api/**"])
        now = W.now_iso()
        async with db.transaction():
            await db.conn.execute(
                """INSERT INTO crew_claims (id, crew_id, path_glob, mode, holder_kind, holder_session_id, holder_user_id,
                       holder_agent_id, state, source, epoch, created_at, updated_at)
                   VALUES ('clm_file', ?, 'lib/team-access.ts', 'exclusive', 'session', ?, ?, ?, 'active', 'mcp', 1, ?, ?)""",
                (W.CREW, a["id"], a["user_id"], a["agent_id"], now, now),
            )
        task = (await svc.create(W.CREW, human, {"title": "t", "zone_ids": [api, lib], "acceptance": [], "depends_on": []})).task
        with pytest.raises(CrewServiceError) as e:
            await svc.claim(W.CREW, task["id"], Caller.for_session(b))
        assert (e.value.status, e.value.error) == (409, "conflict")
        assert "cc-3 holds lib/team-access.ts inside zone lib" in e.value.message
        assert [x["claim_id"] for x in e.value.extra["blockers"]] == ["clm_file"]
        held = await db.fetchall("SELECT id FROM crew_claims WHERE holder_session_id = ?", (b["id"],))
        assert held == []  # all or nothing: api was not taken either
        # the holder of the file may take the zone around it, and a watch never conflicts
        mine = (await svc.create(W.CREW, human, {"title": "m", "zone_ids": [lib], "acceptance": [], "depends_on": []})).task
        await svc.claim(W.CREW, mine["id"], Caller.for_session(a))
    finally:
        await db.close()
