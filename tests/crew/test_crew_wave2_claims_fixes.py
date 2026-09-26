"""Wave-2 review fixes in claims, zones, the guard and the session-token routes (real crew.db, real event log).

* path_glob claims cannot squat zone paths or the repo root, and never decide a zone path in the guard;
* a baton (adopt, handover, task adopt) never launders reserve_for / protected / frozen;
* agent-scoped keys act only as their own agent's sessions on the claims, zones, guard and redeem routes;
* an agent cannot create or tighten a zone into a crew-wide lock (API or zones.yml upload).
"""

from __future__ import annotations

import pytest

from remembra.auth.rbac import Role
from remembra.crew import claims as C
from remembra.crew import zones as Z
from remembra.crew.claims import SESSION_HEADER
from remembra.crew.tasks import Caller, CrewServiceError, TaskService
from tests.crew.test_crew_wp5_routes import crew_http
from tests.crew.wp5_support import (
    CREW,
    OWNER,
    ZONES_YML,
    events,
    inbox,
    make_ops,
    open_db,
    seed_crew,
    seed_session,
    seed_task,
    zone_id,
)

HUMAN = Z.Principal.human(OWNER)
YML = ZONES_YML.replace(
    "commons:\n  package.json: plain\n",
    "  codexonly:\n    include: [codex/**]\n    reserve_for: codex\n  vault:\n    include: [vault/**]\n    protected: true\n"
    "commons:\n  package.json: plain\n",
)


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-1", worktree_id="wt-a", checkout_fp="fp-a")
    b, _ = await seed_session(db, "cs_b", callsign="cc-2", worktree_id="wt-b", checkout_fp="fp-b")
    # a self-declared session working in cc-1's checkout
    x, _ = await seed_session(
        db, "cs_x", callsign="rogue-1", agent_id="rogue", worktree_id="wt-a", checkout_fp="fp-a", verified=False
    )
    await Z.upload_zones_file(ops, CREW, HUMAN, yaml_text=YML, sha="s1", branch="main")
    yield db, ops, Z.Principal.for_session(a), Z.Principal.for_session(b), Z.Principal.for_session(x)
    await db.close()


async def guard(ops, who, paths):
    return await C.server_guard(ops, CREW, who, op="write", paths=list(paths))


# ---------------------------------------------------------------------------
# Finding 1: path_glob claims
# ---------------------------------------------------------------------------


async def test_agent_path_globs_cannot_cover_the_repo_or_a_zone(env):
    db, ops, a, b, x = env
    for glob in ("**", "*", "**/*.ts", "*.md", "{src,codex}/**"):
        with pytest.raises(Z.CrewOpError) as err:
            await C.request_claim(ops, CREW, x, path_glob=glob, mode="exclusive")
        assert err.value.status == 422 and err.value.error == "glob_too_broad", glob
    for glob in ("src/app/pos/cart.ts", "src/**", "codex/secret.py", "vault/**", ".remembra/zones.yml"):
        with pytest.raises(Z.CrewOpError) as err:
            await C.request_claim(ops, CREW, x, path_glob=glob, mode="exclusive")
        assert err.value.error == "zone_path", glob
    # a file no zone covers is a legitimate file claim (undeclared_policy: file_claim)
    ok = await C.request_claim(ops, CREW, x, path_glob="scripts/deploy.sh", mode="exclusive")
    assert ok.status == "granted"
    # a micro-lease is one literal commons file; anything else is refused
    ml = await C.request_claim(ops, CREW, a, path_glob="package.json", source="micro_lease")
    assert ml.status == "granted"
    with pytest.raises(Z.CrewOpError):
        await C.request_claim(ops, CREW, a, path_glob="scripts/other.sh", source="micro_lease")
    # humans keep the full claim surface
    assert (await C.request_claim(ops, CREW, HUMAN, path_glob="tools/**")).status == "granted"


async def test_a_path_glob_claim_never_decides_a_zone_path_in_the_guard(env):
    db, ops, a, b, x = env
    # a glob over everything, as an agent could make before this fix (inserted through a human claim)
    squat = await C.request_claim(ops, CREW, HUMAN, path_glob="**", mode="exclusive")
    assert squat.status == "granted"
    # the claimer's own glob never opens a reserve_for, protected or parent zone (rule 7 only outside zones)
    async with ops.log.transaction() as tx:
        await tx.conn.execute(
            "UPDATE crew_claims SET holder_kind = 'session', holder_session_id = 'cs_x', holder_agent_id = 'rogue' WHERE id = ?",
            (squat.claim["id"],),  # type: ignore[index]
        )
    # reserve_for (row 17 auto-claim refused), protected (row 3), parent zone needs a task (row 18)
    for path, rule in (("codex/secret.py", 17), ("vault/keys.txt", 3), ("src/app/index.ts", 18)):
        verdict = await guard(ops, x, [path])
        assert (verdict["decision"], verdict["rule"]) == ("deny", rule), (path, verdict)
    # outside every zone the file claim applies: the claimer is allowed (rule 7), others denied (rule 9)
    assert (await guard(ops, x, ["scripts/a.sh"]))["rule"] == 7
    readme = await guard(ops, b, ["README.md"])
    assert readme["decision"] == "deny" and readme["rule"] == 9


async def test_file_claims_count_toward_the_hoarding_alarm(env):
    db, ops, a, *_ = env
    for n in range(4):
        assert (await C.request_claim(ops, CREW, a, path_glob=f"scripts/s{n}.sh", mode="shared")).status == "granted"
    items = await inbox(db, kind="zone_hoarding")
    assert len(items) == 1 and items[0]["title"] == "cc-1 holds 4 zones or file claims"


# ---------------------------------------------------------------------------
# Finding 2: adopt / handover / task adopt keep the zone rules
# ---------------------------------------------------------------------------


async def _reserve(ops, session_id: str, reason: str = "quota") -> None:
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, session_id, reason)


async def test_adopting_a_reserved_baton_respects_reserve_for(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    cc, _ = await seed_session(db, "cs_a", callsign="cc-1", agent_id="codex", worktree_id="wt-a", checkout_fp="fp1")
    rogue, _ = await seed_session(
        db, "cs_x", callsign="rogue-1", agent_id="rogue", worktree_id="wt-a", checkout_fp="fp1", verified=False
    )
    fake_codex, _ = await seed_session(
        db, "cs_f", callsign="codex-2", agent_id="codex", worktree_id="wt-a", checkout_fp="fp1", verified=False
    )
    await Z.upload_zones_file(ops, CREW, HUMAN, yaml_text=YML, sha="s1", branch="main")
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(cc), zone_id=await zone_id(db, "codexonly"))
    assert got.status == "granted"
    await _reserve(ops, "cs_a")
    row = await C.get_claim(db.conn, got.claim["id"])  # type: ignore[index]
    for who in (rogue, fake_codex):  # same checkout, but not the key-verified agent
        with pytest.raises(Z.CrewOpError) as err:
            await C.adopt(ops, row, Z.Principal.for_session(who))
        assert err.value.status == 403 and err.value.error == "reserved_for_agent"
    assert (await C.get_claim(db.conn, row["id"]))["state"] == "reserved"
    verdict = await C.server_guard(ops, CREW, Z.Principal.for_session(rogue), op="write", paths=["codex/main.py"])
    assert verdict["decision"] == "deny"
    # the verified codex agent in the same checkout may pick it up
    real, _ = await seed_session(db, "cs_r", callsign="codex-3", agent_id="codex", worktree_id="wt-a", checkout_fp="fp1")
    res = await C.adopt(ops, row, Z.Principal.for_session(real))
    assert res["kind"] == "same_checkout" and res["claims"][0]["holder_session_id"] == "cs_r"
    await db.close()


async def test_a_protected_baton_moves_only_by_a_human_grant(env):
    db, ops, a, b, x = env
    vault = await zone_id(db, "vault")
    held = await C.request_claim(ops, CREW, HUMAN, zone_id=vault)
    row = await C.get_claim(db.conn, held.claim["id"])  # type: ignore[index]
    await C.override(ops, row, HUMAN, action="transfer", to="cs_a", reason="grant cc")
    # cc-1 cannot pass a human grant on by handover
    live = await C.get_claim(db.conn, row["id"])
    with pytest.raises(Z.CrewOpError) as err:
        await C.handover(ops, live, a, to="cs_b")
    assert err.value.error == "protected"
    await _reserve(ops, "cs_a")
    reserved = await C.get_claim(db.conn, row["id"])
    # same checkout is not a grant
    with pytest.raises(Z.CrewOpError) as err:
        await C.adopt(ops, reserved, x)
    assert err.value.status == 423 and err.value.error == "protected"
    verdict = await guard(ops, x, ["vault/keys.txt"])
    assert verdict["decision"] == "deny" and verdict["rule"] == 3
    # a brief offer is not a grant either
    async with ops.log.transaction() as tx:
        await C.record_baton_offer(ops, tx, CREW, row["id"], "cs_b", via="brief")
    with pytest.raises(Z.CrewOpError) as err:
        await C.adopt(ops, reserved, b)
    assert err.value.error == "protected"
    # a human hand-over is (it upgrades the earlier brief offer)
    async with ops.log.transaction() as tx:
        await C.record_baton_offer(ops, tx, CREW, row["id"], "cs_b", via="human")
    res = await C.adopt(ops, reserved, b)
    assert res["kind"] == "human_assign" and res["claims"][0]["holder_session_id"] == "cs_b"
    assert (await guard(ops, b, ["vault/keys.txt"]))["decision"] == "allow"


async def test_a_frozen_zone_baton_is_not_adopted(env):
    db, ops, a, b, x = env
    pos = await zone_id(db, "pos")
    got = await C.request_claim(ops, CREW, a, zone_id=pos)
    await _reserve(ops, "cs_a", "lost")
    await Z.freeze_zone(ops, await Z.get_zone(db.conn, pos), HUMAN, reason="owner edits", until=None)
    reserved = await C.get_claim(db.conn, got.claim["id"])  # type: ignore[index]
    with pytest.raises(Z.CrewOpError) as err:
        await C.adopt(ops, reserved, x)
    assert err.value.error == "frozen"


async def test_task_adopt_respects_reserve_for(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    cc, _ = await seed_session(db, "cs_a", callsign="codex-1", agent_id="codex", worktree_id="wt-a", checkout_fp="fp1")
    rogue, _ = await seed_session(
        db, "cs_x", callsign="rogue-1", agent_id="rogue", worktree_id="wt-a", checkout_fp="fp1", verified=False
    )
    await Z.upload_zones_file(ops, CREW, HUMAN, yaml_text=YML, sha="s1", branch="main")
    await seed_task(db, "tsk_1", 1, status="in_progress")
    async with db.transaction():
        await db.conn.execute(
            "UPDATE crew_tasks SET owner_session_id = 'cs_a', owner_user_id = ?, owner_agent_id = 'codex',"
            " started_at = created_at WHERE id = 'tsk_1'",
            (OWNER,),
        )
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(cc), zone_id=await zone_id(db, "codexonly"), task_id="tsk_1")
    assert got.status == "granted"
    tasks = TaskService(ops.log)
    await tasks.release(CREW, "tsk_1", Caller.for_session(cc), baton=True)
    async with ops.log.transaction() as tx:
        await C.record_baton_offer(ops, tx, CREW, got.claim["id"], "cs_x", via="brief")  # type: ignore[index]
    with pytest.raises(CrewServiceError) as err:
        await tasks.adopt(CREW, "tsk_1", Caller.for_session(rogue))
    assert err.value.status == 403 and err.value.error == "reserved_for_agent"
    task = await db.fetchone("SELECT status, owner_session_id FROM crew_tasks WHERE id = 'tsk_1'")
    assert task == {"status": "stalled", "owner_session_id": "cs_a"}
    await db.close()


# ---------------------------------------------------------------------------
# Finding 4: agent-scoped keys are bound to their own agent's sessions
# ---------------------------------------------------------------------------


async def test_an_agent_scoped_key_cannot_act_as_another_agents_session(tmp_path):
    async with crew_http(tmp_path) as (h, db, ctx):
        created = await h.keys.create_key(user_id=ctx["owner"], name="codex-key", agent_id="codex")
        await h.roles.assign_role(created.id, Role("editor"))
        # cs_a is claude-code; the codex key carries cs_a's token
        hdr = {"X-API-Key": created.key, SESSION_HEADER: ctx["a"][SESSION_HEADER]}
        base = f"/api/v1/crews/{CREW}"
        r = await h.client.post(
            f"{base}/claims", headers=hdr, json={"path_glob": "scripts/x.sh", "mode": "exclusive", "wait": False}
        )
        assert r.status_code == 401, r.text
        r = await h.client.post(f"{base}/guard", headers=hdr, json={"session_id": "cs_a", "op": "write", "paths": ["a.ts"]})
        assert r.status_code == 401, r.text
        r = await h.client.post(f"{base}/zones", headers=hdr, json={"slug": "tools", "include": ["tools/**"]})
        assert r.status_code == 401, r.text
        r = await h.client.post(
            "/api/v1/bypass-codes/redeem",
            headers=hdr,
            json={"session_id": "cs_a", "code": "RCB-AAAAA-BBBBB", "surface": "prepush"},
        )
        assert r.status_code == 401, r.text
        assert await db.fetchone("SELECT id FROM crew_claims WHERE path_glob = 'scripts/x.sh'") is None
        # the same key with a codex session works
        _, tok = await seed_session(db, "cs_c", crew_id=CREW, user_id=ctx["owner"], callsign="codex-1", agent_id="codex")
        ok = await h.client.post(
            f"{base}/claims",
            headers={"X-API-Key": created.key, SESSION_HEADER: tok},
            json={"path_glob": "scripts/x.sh", "mode": "exclusive", "wait": False, "source": "mcp"},
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["claim"]["holder_agent_id"] == "codex"


# ---------------------------------------------------------------------------
# Finding 5: agents cannot squat the crew with zones
# ---------------------------------------------------------------------------


async def test_an_agent_cannot_create_a_crew_wide_lock_zone(env):
    db, ops, a, b, x = env
    for body in (
        {"slug": "lockall", "include": ["**"], "protected": True},
        {"slug": "lockall", "include": ["**"]},
        {"slug": "mine", "include": ["tools/**"], "protected": True},
        {"slug": "mine", "include": ["tools/**"], "reserve_for": "rogue"},
        {"slug": "mine", "include": ["tools/**"], "fail_closed": True},
        {"slug": "over", "include": ["src/**"]},  # lays itself over app/pos/reports/billing
        {"slug": "inside", "include": ["src/app/pos/x/**"]},  # inside pos
    ):
        with pytest.raises(Z.CrewOpError) as err:
            await Z.create_zone(ops, CREW, x, body)
        assert err.value.status == 403 and err.value.error == "human_only", body
    assert await db.fetchone("SELECT id FROM crew_zones WHERE slug IN ('lockall','mine','over','inside')") is None
    for path in ("README.md", "src/app/pos/cart.ts"):
        assert (await guard(ops, a, [path]))["rule"] != 3
    # a plain zone over free paths is fine; tightening it later is not
    tools = await Z.create_zone(ops, CREW, x, {"slug": "tools", "include": ["tools/**"]})
    zone = await Z.get_zone(db.conn, tools["id"])
    with pytest.raises(Z.CrewOpError) as err:
        await Z.patch_zone(ops, zone, x, {"protected": True}, if_match=int(zone["version"]))
    assert err.value.error == "human_only"
    with pytest.raises(Z.CrewOpError):
        await Z.patch_zone(ops, zone, x, {"include": ["**"]}, if_match=int(zone["version"]))
    # a human does all of it
    await Z.patch_zone(ops, zone, HUMAN, {"protected": True}, if_match=int(zone["version"]))
    assert (await Z.get_zone(db.conn, tools["id"]))["protected"] == 1
    assert (await Z.create_zone(ops, CREW, HUMAN, {"slug": "lockall", "include": ["**"]}))["slug"] == "lockall"


async def test_an_agent_zones_file_upload_holds_tightening_for_a_human(env):
    db, ops, a, b, x = env
    squat = YML.replace(
        "commons:\n  package.json: plain\n",
        "  lockall:\n    include: ['**']\n    protected: true\n  tools:\n    include: [tools/**]\n"
        "commons:\n  package.json: plain\n",
    ).replace("  billing:\n    include: [src/billing/**]\n", "  billing:\n    include: [src/billing/**]\n    fail_closed: true\n")
    res = await Z.upload_zones_file(ops, CREW, x, yaml_text=squat, sha="s2", branch="main")
    assert res["result"] == "pending"
    held = {i["target"]: i for i in res["diff"]["items"] if i["loosening"]}
    assert set(held) == {"zone:lockall", "zone:billing"}
    assert held["zone:lockall"]["reason"] == "agent-set protected needs a human"
    # the harmless part applied, the lock did not
    assert await db.fetchone("SELECT id FROM crew_zones WHERE slug = 'lockall' AND archived_at IS NULL") is None
    assert (await Z.get_zone(db.conn, await zone_id(db, "tools")))["protected"] == 0
    assert (await Z.get_zone(db.conn, await zone_id(db, "billing")))["fail_closed"] == 0
    assert (await guard(ops, a, ["README.md"]))["rule"] != 3
    assert [i["kind"] for i in await inbox(db, kind="zone_change_pending")] == ["zone_change_pending"]
    # the human approves: the full file applies
    change = await db.fetchone("SELECT * FROM crew_zone_changes WHERE id = ?", (res["change_id"],))
    await Z.decide_zone_change(ops, change, HUMAN, approve=True)
    assert (await Z.get_zone(db.conn, await zone_id(db, "lockall")))["protected"] == 1
    assert (await Z.get_zone(db.conn, await zone_id(db, "billing")))["fail_closed"] == 1
    # the same file uploaded by a human applies at once
    assert "zone.change_pending" in [e["type"] for e in await events(db)]
