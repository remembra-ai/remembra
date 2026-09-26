"""WP-5 server guard (§5.2 through the shared gatecore) and human bypass codes (D34)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from remembra.crew import bypass as B
from remembra.crew import claims as C
from remembra.crew import collisions as CO
from remembra.crew import schemas as S
from remembra.crew import zones as Z
from remembra.crew.store import now_iso
from tests.crew.wp5_support import (
    CREW,
    OWNER,
    ZONES_YML,
    assert_chain_ok,
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


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, audit = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-1", worktree_id="wt-a")
    b, _ = await seed_session(db, "cs_b", callsign="cc-2", worktree_id="wt-b")
    yml = ZONES_YML.replace(
        "commons:\n  package.json: plain\n",
        "  vault:\n    include: [vault/**]\n    protected: true\ncommons:\n  package.json: plain\n  yarn.lock: serialize\n",
    )
    # a human sets up the protected zone (an agent upload would hold it for approval)
    await Z.upload_zones_file(ops, CREW, HUMAN, yaml_text=yml, sha="s1", branch="main")
    try:
        yield db, ops, audit, Z.Principal.for_session(a), Z.Principal.for_session(b)
    finally:
        await assert_chain_ok(db)
        await db.close()


async def guard(ops, who, op="write", paths=(), **kw):
    return await C.server_guard(ops, CREW, who, op=op, paths=list(paths), **kw)


async def test_auto_claim_then_exclusive_deny_with_coalesced_block_event(env):
    db, ops, _, a, b = env
    res = await guard(ops, a, paths=["src/app/pos/cart.ts"])
    assert res["decision"] == "allow" and res["rule"] in (7, 17)
    assert [c["zone_id"] for c in res["auto_claimed"]] == [await zone_id(db, "pos")]
    claim = await db.fetchone("SELECT source, holder_session_id FROM crew_claims WHERE zone_id = ?", (await zone_id(db, "pos"),))
    assert claim == {"source": "first_write", "holder_session_id": "cs_a"}
    again = await guard(ops, a, paths=["src/app/pos/tender.ts"])
    assert again["decision"] == "allow" and again["rule"] == 7 and again["auto_claimed"] == []
    denied = await guard(ops, b, paths=["src/app/pos/cart.ts"])
    assert denied["decision"] == "deny" and denied["rule"] == 9
    reason = denied["reasons"][0]
    assert reason.startswith("BLOCKED by Remembra Crew") and "cc-1" in reason and '"pos"' in reason and len(reason) <= 450
    assert denied["blockers"][0]["holder_callsign"] == "cc-1"
    await guard(ops, b, paths=["src/app/pos/other.ts"])
    blocked = await events(db, type_prefix="guard.blocked")
    assert len(blocked) == 1  # coalesced per (session, zone) per 5 min
    assert (
        blocked[0]["payload"]["surface"] == "server"
        and blocked[0]["payload"]["zone"] == "pos"
        and blocked[0]["payload"]["holder"] == "cc-1"
    )


async def test_policy_and_tamper_rows(env):
    db, ops, _, a, _b = env
    res = await guard(ops, a, paths=[".remembra/zones.yml"])
    assert res["decision"] == "deny" and res["rule"] == 2
    res2 = await guard(ops, a, op="command", command_tokens=["git", "commit", "--no-verify", "-m", "x"])
    assert res2["decision"] == "deny" and res2["rule"] == 2
    tampers = await events(db, type_prefix="guard.tamper_blocked")
    assert len(tampers) == 2 and all(t["moment"] == 1 and t["payload"]["surface"] == "mcp" for t in tampers)
    assert await inbox(db, kind="tamper_blocked")
    protected = await guard(ops, a, paths=["vault/secret.txt"])
    assert protected["decision"] == "deny" and protected["rule"] == 3


async def test_commands_mcp_services_parent_ignore_and_undeclared(env):
    db, ops, _, a, b = env
    await C.request_claim(ops, CREW, a, zone_id=await zone_id(db, "pos"))
    cmd = await guard(ops, b, op="command", command_tokens=["supabase", "db", "push"])
    assert cmd["decision"] == "deny"
    await C.request_claim(ops, CREW, a, resource="schema:main")
    mcp = await guard(ops, b, op="mcp", mcp_tool="mcp__supabase__apply_migration")
    assert mcp["decision"] == "deny" and mcp["rule"] == 14
    read = await guard(ops, b, op="mcp", mcp_tool="mcp__supabase__list_tables")
    assert read["decision"] == "allow" and read["rule"] == 0
    parent = await guard(ops, b, paths=["src/app/index.ts"])
    assert parent["decision"] == "deny" and parent["rule"] == 18
    assert (await guard(ops, b, paths=["docs/readme.md"]))["rule"] == 6
    undeclared = await guard(ops, b, paths=["README.md"])
    assert undeclared["decision"] == "allow" and undeclared["rule"] == 19
    with pytest.raises(Z.CrewOpError):
        await guard(ops, b, paths=["../outside.txt"])
    with pytest.raises(Z.CrewOpError):
        await guard(ops, b, op="command")
    with pytest.raises(Z.CrewOpError):
        await guard(ops, b, op="mcp", mcp_tool="write_file")


async def test_reserved_variants_show_adopt_only_to_offered_sessions(env):
    db, ops, _, a, b = env
    await seed_task(db, "tsk_12", 12)
    got = await C.request_claim(ops, CREW, a, zone_id=await zone_id(db, "pos"), task_id="tsk_12")
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "quota")
    res = await guard(ops, b, paths=["src/app/pos/cart.ts"])
    assert res["decision"] == "deny" and res["rule"] == 10 and res["variant"] == "reserved_not_offered"
    assert "adopt" not in res["reasons"][0]
    async with ops.log.transaction() as tx:
        await C.record_baton_offer(ops, tx, CREW, got.claim["id"], "cs_b")  # type: ignore[index]
    res = await guard(ops, b, paths=["src/app/pos/cart.ts"])
    assert res["variant"] == "reserved_offered" and "remembra-crew adopt T-12" in res["reasons"][0]


async def test_clobber_paused_observe_and_off(env):
    db, ops, _, a, _b = env
    twin, _ = await seed_session(db, "cs_twin", callsign="cc-3", worktree_id="wt-a")
    async with ops.log.transaction() as tx:
        await CO.record_footprints(ops, tx, CREW, a.session, [{"path": "README.md", "state": "dirty", "attribution": "certain"}])
    clob = await guard(ops, Z.Principal.for_session(twin), paths=["README.md"])
    assert clob["decision"] == "deny" and clob["rule"] == 5
    paused, _ = await seed_session(db, "cs_p", callsign="cc-4", state="paused")
    assert (await guard(ops, Z.Principal.for_session(paused), paths=["README.md"]))["rule"] == 1
    await C.request_claim(ops, CREW, a, zone_id=await zone_id(db, "reports"))
    await ops.store.patch_settings(CREW, {"enforcement": "observe"}, if_match=1)
    warn = await guard(ops, Z.Principal.for_session(twin), paths=["src/app/reports/x.ts"])
    assert warn["decision"] == "warn" and warn["rule"] == 9
    would = await events(db, type_prefix="guard.blocked")
    assert would[-1]["payload"]["decision"] == "would_deny"
    await ops.store.patch_settings(CREW, {"enforcement": "off"}, if_match=2)
    off = await guard(ops, Z.Principal.for_session(twin), paths=["src/app/reports/x.ts"])
    assert off == {**off, "decision": "allow", "variant": "crew_off"}


async def test_serialize_commons_take_a_micro_lease(env):
    db, ops, _, a, b = env
    res = await guard(ops, a, paths=["yarn.lock"])
    assert res["decision"] == "allow" and res["rule"] == 12 and "micro_lease" in res["effects"]
    lease = await db.fetchone("SELECT * FROM crew_claims WHERE path_glob = 'yarn.lock'")
    assert lease is not None and lease["source"] == "micro_lease" and lease["holder_session_id"] == "cs_a"
    other = await guard(ops, b, paths=["yarn.lock"])
    assert other["decision"] == "deny" and other["rule"] == 9


# ---------------------------------------------------------------------------
# Bypass codes
# ---------------------------------------------------------------------------


async def test_issue_and_redeem_bypass_code_once(env):
    db, ops, audit, a, b = env
    issued = await B.issue_code(ops, CREW, HUMAN, session_id="cs_b", scope="push", minutes=15)
    import re

    assert re.fullmatch(S.BYPASS_CODE_PATTERN, issued["code"])
    stored = await db.fetchone("SELECT * FROM crew_bypass_codes WHERE id = ?", (issued["code_id"],))
    assert stored is not None and issued["code"] not in str(stored) and stored["code_hash"] == B.hash_code(issued["code"])
    with pytest.raises(Z.CrewOpError) as err:
        await B.redeem_code(ops, a.session, issued["code"], surface="prepush")  # wrong session
    assert err.value.status == 403 and err.value.error == "invalid_code"
    used = await B.redeem_code(ops, b.session, issued["code"].lower(), surface="prepush")
    assert used["ok"] and used["scope"] == "push"
    ev = (await events(db, type_prefix="guard.bypass_used"))[0]
    assert ev["moment"] == 1 and ev["payload"]["code_id"] == issued["code_id"]
    assert await inbox(db, kind="bypass_used")
    with pytest.raises(Z.CrewOpError):
        await B.redeem_code(ops, b.session, issued["code"], surface="prepush")  # single use
    assert {"crew.bypass_issued", "crew.bypass_used"} <= set(audit.actions())


async def test_bypass_code_validation_and_expiry(env):
    db, ops, _, _a, b = env
    for kwargs in (
        {"session_id": "cs_b", "scope": "push", "minutes": 16},
        {"session_id": "cs_b", "scope": "push", "minutes": 0},
        {"session_id": "cs_b", "scope": "Push It", "minutes": 5},
        {"session_id": "cs_nope", "scope": "push", "minutes": 5},
    ):
        with pytest.raises(Z.CrewOpError) as err:
            await B.issue_code(ops, CREW, HUMAN, **kwargs)
        assert err.value.status == 422
    with pytest.raises(Z.CrewOpError):
        await B.issue_code(ops, CREW, b, session_id="cs_b", scope="push", minutes=5)
    issued = await B.issue_code(ops, CREW, HUMAN, session_id="cs_b", scope="commit", minutes=1)
    await db.conn.execute(
        "UPDATE crew_bypass_codes SET expires_at = ? WHERE id = ?",
        (now_iso(C.utcnow() - timedelta(seconds=1)), issued["code_id"]),
    )
    await db.conn.commit()
    with pytest.raises(Z.CrewOpError):
        await B.redeem_code(ops, b.session, issued["code"], surface="precommit")
    with pytest.raises(Z.CrewOpError):
        await B.redeem_code(ops, b.session, "not-a-code", surface="precommit")
