"""F4: the exclusive-claim caps refuse truthfully and never starve a session that holds nothing.

Server side (``crew/claims.py``, ``crew/tasks.py``): what counts toward the caps, which cap fired,
who holds what, and the paths that used to skip the caps (re-taking an own baton).
"""

from __future__ import annotations

import pytest

from remembra.crew import claims as C
from remembra.crew import zones as Z
from tests.crew.wp5_support import CREW, OWNER, assert_chain_ok, events, make_ops, open_db, seed_crew, seed_session

HUMAN = Z.Principal.human(OWNER, privileged=True)


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    try:
        yield db, ops
    finally:
        await assert_chain_ok(db)
        await db.close()


async def _codex(db, sid: str, callsign: str) -> Z.Principal:
    row, _ = await seed_session(db, sid, callsign=callsign, agent_id="codex", worktree_id=f"wt-{sid}", checkout_fp=f"fp-{sid}")
    return Z.Principal.for_session(row)


async def _path(ops, who, glob: str, **kw):
    return await C.request_claim(ops, CREW, who, path_glob=glob, **kw)


async def test_a_session_holding_nothing_is_never_refused_its_first_area_by_the_per_agent_cap(env):
    """The transcript case: codex-5 x3 and codex-3 x3 fill the per-agent 6; codex-4 holds nothing."""
    db, ops = env
    c5, c3, c4 = await _codex(db, "cs_5", "codex-5"), await _codex(db, "cs_3", "codex-3"), await _codex(db, "cs_4", "codex-4")
    for folder in ("a", "b", "c"):
        assert (await _path(ops, c5, f"{folder}/x.ts")).status == "granted"
    for folder in ("d", "e", "f"):
        assert (await _path(ops, c3, f"{folder}/x.ts")).status == "granted"
    first = await _path(ops, c4, "lib/model.ts")
    assert first.status == "granted", first.body()
    # more files in the folder it already holds are the same area: no new slot is used
    assert (await _path(ops, c4, "lib/assets.ts")).status == "granted"
    # a second area is the per-agent cap, and the refusal says so with numbers and holders
    second = await _path(ops, c4, "app/api/assets/route.ts")
    assert second.status == "denied" and second.error == "claim_cap" and second.http_status == 409
    body = second.body()
    cap = body["cap"]
    assert cap["cap"] == "agent" and cap["setting"] == "max_exclusive_claims_per_agent"
    assert cap["limit"] == 6 and cap["held_by_you"] == 1 and cap["held_by_agent"] == 7 and cap["agent_id"] == "codex"
    assert {(h["callsign"], h["count"]) for h in cap["holders"]} == {("codex-5", 3), ("codex-3", 3), ("codex-4", 1)}
    assert "codex-5 x3" in body["message"] and "codex-3 x3" in body["message"] and "per-agent" in body["message"].lower()
    assert "max_exclusive_claims_per_agent" in body["message"]
    assert body["blockers"] == [] and body["error"] == "claim_cap"  # what a 0.17.5 client reads is unchanged


async def test_the_per_session_refusal_names_the_cap_the_number_and_what_the_session_holds(env):
    db, ops = env
    a = await _codex(db, "cs_a", "codex-1")
    for folder in ("a", "b", "c"):
        assert (await _path(ops, a, f"{folder}/x.ts")).status == "granted"
    denied = await _path(ops, a, "d/x.ts")
    assert denied.status == "denied" and denied.error == "claim_cap"
    cap = denied.body()["cap"]
    assert cap["cap"] == "session" and cap["setting"] == "max_exclusive_claims_per_session"
    assert cap["limit"] == 3 and cap["held_by_you"] == 3
    assert sorted(h["target"] for h in cap["holding"]) == ["path a/x.ts", "path b/x.ts", "path c/x.ts"]
    msg = denied.body()["message"]
    assert "3 of 3" in msg and "path a/x.ts" in msg and "max_exclusive_claims_per_session" in msg
    assert [e["payload"]["blockers"] for e in await events(db, type_prefix="claim.denied")] == [[]]


async def test_files_in_one_folder_are_one_area_so_a_thirteen_path_request_is_granted_whole(env):
    """13 paths used to give 3 GRANTED and 10 REFUSED (one slot per file)."""
    db, ops = env
    a = await _codex(db, "cs_a", "codex-1")
    paths = [f"app/portal/f{i}.tsx" for i in range(10)] + ["app/layout.tsx", "app/page.tsx", "app/globals.css"]
    outcomes = [await _path(ops, a, p) for p in paths]
    assert [o.status for o in outcomes] == ["granted"] * 13
    session = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_a'")
    assert await C.exclusive_counts(db.conn, session) == (2, 2)  # app/portal and app
    # the cap still bounds the number of areas
    assert (await _path(ops, a, "lib/x.ts")).status == "granted"
    assert (await _path(ops, a, "db/x.sql")).error == "claim_cap"


async def test_claim_unit_keys():
    zones = [{"id": "zn_lib", "include_globs": '["lib/**"]'}]
    key = C.claim_unit
    assert key({"zone_id": "zn_1"}, zones) == "zone:zn_1"
    assert key({"resource": "deploy:vercel"}, zones) == "resource:deploy:vercel"
    assert key({"path_glob": "lib/a.ts"}, zones) == "zone:zn_lib"  # a file inside a zone counts as that zone
    assert key({"path_glob": "app/portal/a.tsx"}, zones) == key({"path_glob": "app/portal/b.tsx"}, zones) == "dir:app/portal"
    assert key({"path_glob": "app/portal/**"}, zones) == "dir:app/portal"
    # a literal path with no file extension may be a directory: its own area, shared with the files inside it
    assert key({"path_glob": "app/portal"}, zones) == "dir:app/portal"
    assert key({"path_glob": "README.md"}, zones) == "dir:."
    assert key({"path_glob": None, "zone_id": None, "resource": None}, zones) == "dir:."


async def test_retaking_an_own_baton_no_longer_skips_the_caps(env):
    """3 live claims plus 3 re-taken batons used to give one session 6, the whole per-agent budget."""
    db, ops = env
    a, b = await _codex(db, "cs_a", "codex-1"), await _codex(db, "cs_b", "codex-2")
    first = [(await _path(ops, a, f"{folder}/x.ts")).claim for folder in ("a", "b", "c")]
    for claim in first:
        assert (await C.release_claim(ops, claim, a, baton=True))["claim"]["state"] == "reserved"
    for folder in ("d", "e", "f"):
        assert (await _path(ops, a, f"{folder}/x.ts")).status == "granted"
    retake = await _path(ops, a, "a/x.ts")
    assert retake.status == "denied" and retake.error == "claim_cap" and retake.body()["cap"]["cap"] == "session"
    with pytest.raises(Z.CrewOpError) as err:
        await C.adopt(ops, first[1], a)
    assert err.value.error == "claim_cap" and err.value.extra["cap"]["cap"] == "session"
    assert "3 of 3" in err.value.message
    session = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_a'")
    assert await C.exclusive_counts(db.conn, session) == (3, 3)
    # the sibling that holds nothing is served
    assert (await _path(ops, b, "g/x.ts")).status == "granted"
    # with a slot free the baton is re-taken as before
    live = await db.fetchone("SELECT * FROM crew_claims WHERE holder_session_id = 'cs_a' AND path_glob = 'd/x.ts'")
    await C.release_claim(ops, live, a)
    assert (await _path(ops, a, "a/x.ts")).status == "retaken"


async def test_accepting_a_handover_over_the_cap_says_which_cap(env):
    db, ops = env
    a, b = await _codex(db, "cs_a", "codex-1"), await _codex(db, "cs_b", "codex-2")
    for folder in ("a", "b", "c"):
        assert (await _path(ops, a, f"{folder}/x.ts")).status == "granted"
    offered = (await _path(ops, b, "d/x.ts")).claim
    await C.handover(ops, offered, b, to="cs_a")
    with pytest.raises(Z.CrewOpError) as err:
        await C.accept_handover(ops, offered, a)
    assert err.value.status == 409 and err.value.error == "claim_cap"
    assert err.value.extra["cap"]["cap"] == "session" and err.value.extra["cap"]["limit"] == 3
    assert err.value.body()["cap"]["held_by_you"] == 3


# ---------------------------------------------------------------------------
# Task start: the same count and the same refusal as a plain claim
# ---------------------------------------------------------------------------


async def test_task_start_counts_areas_like_a_plain_claim_and_names_the_cap(tmp_path):
    from remembra.crew.tasks import Caller, CrewServiceError, TaskService
    from tests.crew import wp6_support as W

    db = await W.open_db(tmp_path)
    try:
        await W.seed_crew(db)
        svc = TaskService(W.event_log(db))
        s = await W.seed_session(db)
        human = Caller.for_human("u_owner", privileged=True)
        zones = [await W.seed_zone(db, slug=f"z{i}", includes=[f"z{i}/**"]) for i in range(4)]
        # five file claims in one folder are one area: two zones still fit under the cap of 3
        now = W.now_iso()
        async with db.transaction():
            for i in range(5):
                await db.conn.execute(
                    """INSERT INTO crew_claims (id, crew_id, path_glob, mode, holder_kind, holder_session_id, holder_user_id,
                           holder_agent_id, state, source, epoch, created_at, updated_at)
                       VALUES (?, ?, ?, 'exclusive', 'session', ?, ?, ?, 'active', 'mcp', 1, ?, ?)""",
                    (f"clm_file{i}", W.CREW, f"scripts/f{i}.sh", s["id"], s["user_id"], s["agent_id"], now, now),
                )
        await W.set_settings(db, W.CREW, wip_per_session=5)
        two = (await svc.create(W.CREW, human, {"title": "two", "zone_ids": zones[:2], "acceptance": [], "depends_on": []})).task
        await svc.claim(W.CREW, two["id"], Caller.for_session(s))
        one = (await svc.create(W.CREW, human, {"title": "one", "zone_ids": zones[2:3], "acceptance": [], "depends_on": []})).task
        with pytest.raises(CrewServiceError) as e:
            await svc.claim(W.CREW, one["id"], Caller.for_session(s))
        assert (e.value.status, e.value.error) == (409, "claim_cap")
        cap = e.value.extra["cap"]
        assert cap["cap"] == "session" and cap["limit"] == 3 and cap["held_by_you"] == 3
        assert "Per-session claim limit reached: you hold 3 of 3" in e.value.message
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The hook gate counts the same areas and words the refusal truthfully
# ---------------------------------------------------------------------------


def _gate_snapshot():
    from tests.crew.gatecore_support import find, snapshot

    snap = snapshot()
    base = find(snap["claims"], "clm_db")
    return snap, base


WRITE_INVOICE = {"file_path": "/w/yaadbooks-c/src/app/invoices/new.ts", "content": "x"}


def test_gate_counts_files_in_one_folder_once_and_ignores_batons_left_for_anyone():
    from tests.crew.gatecore_support import run

    snap, base = _gate_snapshot()
    mine = {**base, "resource": None, "holder_session_id": "cs_c", "state": "active", "reserved_for": None}
    for n in range(5):  # five files in one folder: one area
        snap["claims"].append({**mine, "id": f"clm_f{n}", "path_glob": f"scripts/f{n}.sh"})
    for n in range(3):  # batons left for anyone use no slot (the server does not count them either)
        snap["claims"].append({**mine, "id": f"clm_b{n}", "path_glob": f"tools{n}/x.sh", "state": "reserved"})
    calls: list = []
    v = run("Write", WRITE_INVOICE, snap=snap, claim=lambda r: calls.append(r) or "granted")
    assert (v.rule, v.decision) == (17, "allow") and len(calls) == 1
    # three areas is the cap: no auto-claim, and the text states the numbers and says claims, not zones
    snap["claims"].append({**mine, "id": "clm_g1", "path_glob": "tools/a.sh"})
    snap["claims"].append({**mine, "id": "clm_g2", "path_glob": "bin/a.sh", "state": "reserved", "reserved_for": "cs_c"})
    calls.clear()
    v = run("Write", WRITE_INVOICE, snap=snap, claim=lambda r: calls.append(r) or "granted")
    assert (v.rule, v.decision, v.variant) == (17, "deny", "claim_required") and calls == []
    assert "you hold 3 of 3 exclusive claims, the per-session limit" in (v.reason or "")
    assert "exclusive zones" not in (v.reason or "")


def test_gate_does_not_tell_a_session_under_its_own_cap_to_release_claims_it_does_not_hold():
    """A server refusal by the per-agent cap reaches the gate as ``cap``; the caller holds nothing."""
    from tests.crew.gatecore_support import run

    v = run("Write", WRITE_INVOICE, claim=lambda r: "cap")
    assert (v.rule, v.decision, v.variant) == (17, "deny", "claim_cap")
    reason = v.reason or ""
    assert "per-agent claim limit is reached by other sessions with your agent id; you hold 0 of your 3" in reason
    assert "max_exclusive_claims_per_agent" in reason and "you already hold" not in reason
    assert len(reason) <= 450


def test_claim_folder():
    from remembra.crew import gatecore as G

    assert G.claim_folder("app/portal/a.tsx") == "app/portal"
    assert G.claim_folder("app/portal/**") == "app/portal"
    assert G.claim_folder("app/{a,b}/x.ts") == "app"
    assert G.claim_folder("README.md") == "." and G.claim_folder("") == "." and G.claim_folder("*.md") == "."
