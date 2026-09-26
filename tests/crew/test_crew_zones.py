"""WP-5 zones service against a real crew.db and the real event log (hash chain verified after every test)."""

from __future__ import annotations

import json

import pytest

from remembra.crew import claims as C
from remembra.crew import zones as Z
from remembra.crew.limits import crew_limits_for_tier
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
    types,
    zone_id,
)

HUMAN = Z.Principal.human(OWNER)


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, audit = make_ops(db)
    s1, _ = await seed_session(db, "cs_one", callsign="cc-1", worktree_id="wt-a", checkout_fp="fp-a")
    try:
        yield db, ops, audit, Z.Principal.for_session(s1)
    finally:
        await assert_chain_ok(db)
        await db.close()


async def test_upload_applies_non_loosening_policy(env):
    db, ops, audit, agent = env
    res = await Z.upload_zones_file(ops, CREW, agent, yaml_text=ZONES_YML, sha="sha1", branch="main")
    assert res["result"] == "applied" and res["change_id"] is None
    rows = {r["slug"]: r for r in await Z.load_zone_rows(db.conn, CREW)}
    assert set(rows) == {"crew-policy", "app", "pos", "reports", "billing"}
    assert rows["crew-policy"]["builtin"] == 1 and rows["crew-policy"]["protected"] == 1
    assert rows["app"]["is_leaf"] == 0 and rows["pos"]["is_leaf"] == 1
    assert rows["pos"]["parent_id"] == rows["app"]["id"] and rows["pos"]["source"] == "repo"
    assert json.loads(rows["pos"]["command_patterns"]) == ["supabase db push *"]
    evs = await events(db)
    synced = [e for e in evs if e["type"] == "zone.synced"]
    assert len(synced) == 1 and synced[0]["moment"] == 1 and synced[0]["payload"]["policy_changed"] is True
    assert sum(e["type"] == "zone.created" for e in evs) == 5  # four declared + the built-in policy zone
    assert await Z.active_ignore(db.conn, CREW) == ["docs/**"]
    assert (await Z.active_commons(db.conn, CREW))[0] == {"glob": "package.json", "kind": "plain"}
    # the same sha again is a no-op; overlaps never include parent/child pairs
    assert (await Z.upload_zones_file(ops, CREW, agent, yaml_text=ZONES_YML, sha="sha1", branch="main"))["result"] == "unchanged"
    assert await db.fetchall("SELECT * FROM crew_zone_overlaps WHERE crew_id = ?", (CREW,)) == []
    crew = await db.fetchone("SELECT active_zones_sha FROM crews WHERE id = ?", (CREW,))
    assert crew is not None and crew["active_zones_sha"] == "sha1"
    assert "crew.zones_synced" in audit.actions()


async def test_overlapping_zones_are_recorded(env):
    db, ops, _, agent = env
    await Z.upload_zones_file(
        ops, CREW, agent, yaml_text="zones:\n  a: [src/**/*.ts]\n  b: [src/pos/**]\n  c: [docs/**]\n", sha="s", branch="main"
    )
    pairs = {(r["zone_a"], r["zone_b"]) for r in await db.fetchall("SELECT * FROM crew_zone_overlaps WHERE crew_id = ?", (CREW,))}
    a, b = await zone_id(db, "a"), await zone_id(db, "b")
    assert pairs == {(a, b), (b, a)}
    assert await Z.related_zone_ids(db.conn, CREW, a) == {a, b}


async def test_loosening_lands_pending_and_old_policy_stays_until_a_human_approves(env):
    db, ops, audit, agent = env
    await Z.upload_zones_file(ops, CREW, agent, yaml_text=ZONES_YML, sha="sha1", branch="main")
    pos_before = await zone_id(db, "pos")
    loosened = ZONES_YML.replace(
        '  pos:\n    title: POS section\n    parent: app\n    include: [src/app/pos/**]\n    commands: ["supabase db push *"]\n',
        "",
    ).replace("  billing:\n", "  extra:\n    include: [src/extra/**]\n  billing:\n")
    assert "pos:" not in loosened
    res = await Z.upload_zones_file(ops, CREW, agent, yaml_text=loosened, sha="sha2", branch="main")
    assert res["result"] == "pending" and res["change_id"].startswith("zch_")
    live = {r["slug"] for r in await Z.load_zone_rows(db.conn, CREW)}
    assert "pos" in live and "extra" in live  # removal held back, addition applied now
    pending = [e for e in await events(db) if e["type"] == "zone.change_pending"]
    assert pending and pending[0]["moment"] == 1 and pending[0]["payload"]["loosening"] is True
    items = await inbox(db, kind="zone_change_pending")
    assert len(items) == 1 and items[0]["state"] == "open" and items[0]["audience"] == "project"
    assert await Z.list_pending_change_ids(db.conn, CREW) == [res["change_id"]]
    change = await db.fetchone("SELECT * FROM crew_zone_changes WHERE id = ?", (res["change_id"],))
    view = await Z.decide_zone_change(ops, change, HUMAN, approve=True)
    assert view["state"] == "applied" and view["decided_by"] == OWNER
    assert "pos" not in {r["slug"] for r in await Z.load_zone_rows(db.conn, CREW)}
    archived = await db.fetchone("SELECT archived_at FROM crew_zones WHERE id = ?", (pos_before,))
    assert archived is not None and archived["archived_at"] is not None
    assert (await inbox(db, kind="zone_change_pending"))[0]["state"] == "resolved"
    assert "crew.zone_change_approved" in audit.actions()
    with pytest.raises(Z.CrewOpError) as err:
        await Z.decide_zone_change(ops, change, HUMAN, approve=True)
    assert err.value.status == 409


async def test_reject_keeps_policy_and_newer_upload_supersedes(env):
    db, ops, _, agent = env
    await Z.upload_zones_file(  # a human protects pos (an agent's protection would wait for approval)
        ops, CREW, HUMAN, yaml_text="zones:\n  pos: {include: [src/pos/**], protected: true}\n", sha="a", branch="main"
    )
    first = await Z.upload_zones_file(ops, CREW, agent, yaml_text="zones:\n  pos: [src/pos/**]\n", sha="b", branch="main")
    assert first["result"] == "pending"
    second = await Z.upload_zones_file(
        ops, CREW, agent, yaml_text="zones:\n  pos: {include: [src/pos/**], protected: false}\n", sha="c", branch="main"
    )
    assert second["result"] == "pending"
    old = await db.fetchone("SELECT state, decided_by FROM crew_zone_changes WHERE id = ?", (first["change_id"],))
    assert old == {"state": "rejected", "decided_by": Z.PENDING_SUPERSEDED_BY}
    change = await db.fetchone("SELECT * FROM crew_zone_changes WHERE id = ?", (second["change_id"],))
    await Z.decide_zone_change(ops, change, HUMAN, approve=False)
    pos = await db.fetchone("SELECT protected FROM crew_zones WHERE crew_id = ? AND slug = 'pos'", (CREW,))
    assert pos == {"protected": 1}
    decided = [e["payload"]["decision"] for e in await events(db, type_prefix="zone.change_decided")]
    assert decided == ["rejected", "rejected"]


async def test_enforcement_lowering_needs_approval_and_then_changes_settings(env):
    db, ops, _, agent = env
    res = await Z.upload_zones_file(
        ops, CREW, agent, yaml_text="enforcement: observe\nzones:\n  pos: [src/pos/**]\n", sha="e1", branch="main"
    )
    assert res["result"] == "pending"
    settings, _ = await ops.store.get_settings(CREW)
    assert settings["enforcement"] == "enforce"
    change = await db.fetchone("SELECT * FROM crew_zone_changes WHERE id = ?", (res["change_id"],))
    await Z.decide_zone_change(ops, change, HUMAN, approve=True)
    settings, version = await ops.store.get_settings(CREW)
    assert settings["enforcement"] == "observe" and version == 2
    changed = await events(db, type_prefix="crew.settings_changed")
    assert changed[-1]["payload"]["enforcement"] == "observe" and changed[-1]["actor_kind"] == "human"
    # raising it back is tightening and applies at once
    res = await Z.upload_zones_file(
        ops, CREW, agent, yaml_text="enforcement: enforce\nzones:\n  pos: [src/pos/**]\n", sha="e2", branch="main"
    )
    assert res["result"] == "applied"
    assert (await ops.store.get_settings(CREW))[0]["enforcement"] == "enforce"


async def test_invalid_file_and_zone_cap(env):
    db, ops, _, agent = env
    with pytest.raises(Z.CrewOpError) as err:
        await Z.upload_zones_file(ops, CREW, agent, yaml_text="zones:\n  crew-policy: [x/**]\n", sha="x", branch="main")
    assert err.value.status == 422 and err.value.extra["problems"]
    ops.limits = crew_limits_for_tier("free")
    many = "zones:\n" + "".join(f"  z{i}: [d{i}/**]\n" for i in range(ops.limits.max_zones + 1))
    with pytest.raises(Z.CrewOpError) as err:
        await Z.upload_zones_file(ops, CREW, agent, yaml_text=many, sha="many", branch="main")
    assert err.value.status == 409 and err.value.error == "zone_cap"
    assert [r["slug"] for r in await Z.load_zone_rows(db.conn, CREW)] == []  # rolled back entirely
    # replacing zones at the cap is fine: only growth past the cap is refused
    full = "zones:\n" + "".join(f"  z{i}: [d{i}/**]\n" for i in range(ops.limits.max_zones))
    assert (await Z.upload_zones_file(ops, CREW, agent, yaml_text=full, sha="full", branch="main"))["result"] == "applied"
    swapped = full.replace("  z0: [d0/**]\n", "  other: [o/**]\n")
    with pytest.raises(Z.CrewOpError) as err:  # removal waits for approval, so the new zone would pass the cap now
        await Z.upload_zones_file(ops, CREW, agent, yaml_text=swapped, sha="swap", branch="main")
    assert err.value.error == "zone_cap"
    renamed = full.replace("  z0: [d0/**]\n", "  z0: [d0/**, d0b/**]\n")
    assert (await Z.upload_zones_file(ops, CREW, agent, yaml_text=renamed, sha="grow", branch="main"))["result"] == "applied"


async def test_server_zone_crud_and_human_only_loosening(env):
    db, ops, audit, agent = env
    with pytest.raises(Z.CrewOpError) as err:  # an agent cannot create a protected zone (zone squat)
        await Z.create_zone(ops, CREW, agent, {"slug": "pos", "include": ["src/pos/**"], "protected": True})
    assert err.value.status == 403 and err.value.error == "human_only"
    detail = await Z.create_zone(ops, CREW, HUMAN, {"slug": "pos", "include": ["src/pos/**"], "protected": True})
    assert detail["source"] == "dashboard" and detail["protected"] is True
    with pytest.raises(Z.CrewOpError) as err:
        await Z.create_zone(ops, CREW, agent, {"slug": "pos", "include": ["x/**"]})
    assert err.value.error == "zone_exists"
    zone = await Z.get_zone(db.conn, detail["id"])
    with pytest.raises(Z.CrewOpError) as err:
        await Z.patch_zone(ops, zone, agent, {"protected": False}, if_match=zone["version"])
    assert err.value.status == 403 and err.value.error == "human_only"
    with pytest.raises(Z.CrewOpError) as err:
        await Z.patch_zone(ops, zone, HUMAN, {"protected": False}, if_match=zone["version"] + 5)
    assert err.value.status == 412
    with pytest.raises(Z.CrewOpError) as err:  # narrowing a protected zone is loosening
        await Z.patch_zone(ops, zone, agent, {"exclude": ["src/pos/tmp/**"]}, if_match=zone["version"])
    assert err.value.error == "human_only"
    ok = await Z.patch_zone(
        ops, zone, agent, {"title": "Point of sale", "include": ["src/pos/**", "src/pos2/**"]}, if_match=zone["version"]
    )
    assert ok["applied"] and ok["zone"]["title"] == "Point of sale"
    zone = await Z.get_zone(db.conn, detail["id"])
    done = await Z.patch_zone(ops, zone, HUMAN, {"protected": False}, if_match=zone["version"])
    assert done["zone"]["protected"] is False
    with pytest.raises(Z.CrewOpError) as err:
        await Z.archive_zone(ops, zone, agent)
    assert err.value.error == "human_only"
    claim = await C.request_claim(ops, CREW, agent, zone_id=detail["id"], source="mcp")
    assert claim.status == "granted"
    archived = await Z.archive_zone(ops, await Z.get_zone(db.conn, detail["id"]), HUMAN)
    assert archived["applied"] and archived["released_claims"] == [claim.claim["id"]]  # type: ignore[index]
    assert "crew.zone_archived" in audit.actions()
    # a builtin zone is never editable
    builtin = await db.fetchone("SELECT * FROM crew_zones WHERE crew_id = ? AND builtin = 1", (CREW,))
    with pytest.raises(Z.CrewOpError) as err:
        await Z.patch_zone(ops, builtin, HUMAN, {"title": "x"}, if_match=builtin["version"])
    assert err.value.status == 423


async def test_repo_zone_edits_return_an_export_patch(env):
    db, ops, _, agent = env
    await Z.upload_zones_file(ops, CREW, agent, yaml_text=ZONES_YML, sha="sha1", branch="main")
    pos = await Z.get_zone(db.conn, await zone_id(db, "pos"))
    res = await Z.patch_zone(ops, pos, HUMAN, {"title": "Till"}, if_match=pos["version"])
    assert res["applied"] is False and "+    title: Till" in res["export_patch"] and "title: Till" in res["yaml"]
    assert (await Z.get_zone(db.conn, pos["id"]))["title"] == "POS section"  # unchanged until the repo changes
    res = await Z.archive_zone(ops, pos, HUMAN)
    assert res["applied"] is False and "-  pos:" in res["export_patch"]


async def test_zones_file_takes_over_a_dashboard_zone_and_flags_loosening(env):
    db, ops, _, agent = env
    await Z.create_zone(ops, CREW, HUMAN, {"slug": "pos", "include": ["src/pos/**"], "protected": True})
    res = await Z.upload_zones_file(ops, CREW, agent, yaml_text="zones:\n  pos: [src/pos/**]\n", sha="t", branch="main")
    assert res["result"] == "pending"
    row = await db.fetchone("SELECT source, protected FROM crew_zones WHERE crew_id = ? AND slug = 'pos'", (CREW,))
    assert row == {"source": "repo", "protected": 1}


async def test_freeze_and_unfreeze(env):
    db, ops, audit, agent = env
    await Z.upload_zones_file(ops, CREW, agent, yaml_text=ZONES_YML, sha="sha1", branch="main")
    pos = await Z.get_zone(db.conn, await zone_id(db, "reports"))
    s2, _ = await seed_session(db, "cs_two", callsign="cc-2")
    other = Z.Principal.for_session(s2)
    queued = await C.request_claim(ops, CREW, other, zone_id=pos["id"], wait=True)
    assert queued.status == "granted"  # nobody holds it yet
    await C.release_claim(ops, queued.claim, other)  # type: ignore[arg-type]
    res = await Z.freeze_zone(ops, pos, HUMAN, reason="Mani is editing reports himself", until=None)
    assert res["zone"]["frozen_by"] == OWNER and res["claim"]["holder_kind"] == "human"
    with pytest.raises(Z.CrewOpError) as err:
        await C.request_claim(ops, CREW, agent, zone_id=pos["id"])
    assert err.value.status == 423 and err.value.error == "frozen"
    waiting = await C.request_claim(ops, CREW, Z.Principal.human("u_admin2"), zone_id=pos["id"], wait=True)
    assert waiting.status == "queued"
    evs = await events(db)
    assert any(e["type"] == "zone.frozen" and e["moment"] == 1 for e in evs)
    assert any(e["type"] == "human.override" and e["payload"]["action"] == "freeze" for e in evs)
    await Z.unfreeze_zone(ops, await Z.get_zone(db.conn, pos["id"]), HUMAN, reason="done")
    human_claim = await db.fetchone("SELECT state FROM crew_claims WHERE id = ?", (res["claim"]["id"],))
    assert human_claim == {"state": "released"}
    promoted = await db.fetchone("SELECT state FROM crew_claims WHERE id = ?", (waiting.claim["id"],))  # type: ignore[index]
    assert promoted == {"state": "active"}
    assert {"crew.zone_frozen", "crew.zone_unfrozen"} <= set(audit.actions())
    with pytest.raises(Z.CrewOpError):
        await Z.freeze_zone(ops, pos, HUMAN, reason="x", until="2000-01-01T00:00:00Z")


TREE = {
    "name": ".",
    "files": 2,
    "children": [
        {
            "name": "src",
            "files": 0,
            "children": [
                {"name": "pos", "files": 7, "children": []},
                {"name": "reports", "files": 3, "children": []},
            ],
        },
        {"name": "docs", "files": 4, "children": []},
    ],
}


async def test_no_zone_bootstrap_applies_suggestions_when_the_crew_goes_multi(env):
    db, ops, _, agent = env
    res = await Z.put_tree(ops, CREW, agent, TREE)
    assert res == {"node_count": 5, "bootstrap_zone_ids": []}  # only one live session
    await seed_session(db, "cs_two", callsign="cc-2")
    async with ops.log.transaction() as tx:
        applied = await Z.maybe_bootstrap(ops, tx, CREW)
    assert len(applied) == 2
    rows = {r["slug"]: r for r in await Z.load_zone_rows(db.conn, CREW)}
    assert rows["pos"]["source"] == "suggested" and json.loads(rows["pos"]["include_globs"]) == ["src/pos/**"]
    assert await Z.bootstrap_active(db.conn, CREW) is True
    sa = [e for e in await events(db) if e["type"] == "zone.suggested_applied"]
    assert sa and sa[0]["payload"]["undo_available"] is True
    async with ops.log.transaction() as tx:
        assert await Z.maybe_bootstrap(ops, tx, CREW) == []  # idempotent
    # a claim on a suggested zone keeps it alive when the repo policy arrives; the other is retired
    claim = await C.request_claim(ops, CREW, agent, zone_id=rows["pos"]["id"])
    assert claim.status == "granted"
    await Z.upload_zones_file(ops, CREW, agent, yaml_text="zones:\n  billing: [src/billing/**]\n", sha="r", branch="main")
    live = {r["slug"]: r["source"] for r in await Z.load_zone_rows(db.conn, CREW)}
    assert live == {"crew-policy": "builtin", "pos": "suggested", "billing": "repo"}
    assert await Z.bootstrap_active(db.conn, CREW) is False


async def test_bootstrap_off_and_suggest(env):
    db, ops, _, agent = env
    await ops.store.patch_settings(CREW, {"no_zone_bootstrap": "off"}, if_match=1)
    await seed_session(db, "cs_two", callsign="cc-2")
    res = await Z.put_tree(ops, CREW, agent, TREE)
    assert res["bootstrap_zone_ids"] == []
    sug = await Z.suggest(db.conn, CREW)
    assert [z["slug"] for z in sug["zones"]] == ["pos", "reports"] and "src/pos/**" in sug["yaml"]
    with pytest.raises(Z.CrewOpError):
        await Z.put_tree(ops, CREW, agent, {"name": "a/b", "files": 1})


async def test_builtin_zone_is_restored_and_match(env):
    db, ops, _, agent = env
    await Z.upload_zones_file(ops, CREW, agent, yaml_text=ZONES_YML, sha="sha1", branch="main")
    await db.conn.execute("UPDATE crew_zones SET protected = 0, archived_at = 'x' WHERE crew_id = ? AND builtin = 1", (CREW,))
    await db.conn.commit()
    async with ops.log.transaction() as tx:
        row = await Z.ensure_policy_zone(tx, CREW)
    assert row["protected"] == 1 and row["archived_at"] is None
    m = await Z.match(
        db.conn,
        CREW,
        paths=["src/app/pos/cart.ts", ".remembra/zones.yml", "docs/a.md", "package.json", "/etc/x"],
        command_tokens=["supabase", "db", "push"],
        mcp_tool="mcp__vercel__deploy_to_vercel",
    )
    by = {p["path"]: p for p in m["paths"]}
    assert by["src/app/pos/cart.ts"]["zones"] == ["app", "pos"] and by["src/app/pos/cart.ts"]["leaf_zone"] == "pos"
    assert by[".remembra/zones.yml"]["crew_policy"] is True
    assert by["docs/a.md"]["ignored"] is True
    assert by["package.json"]["commons"] == {"glob": "package.json", "kind": "plain"}
    assert by["/etc/x"]["error"]
    assert m["command"]["zones"] == ["pos"]
    assert m["mcp"]["kind"] == "services"
    exported = await Z.export(db.conn, CREW)
    assert exported["zones"] == 4 and "pos:" in exported["yaml"]
    assert "zone.created" in await types(db)
