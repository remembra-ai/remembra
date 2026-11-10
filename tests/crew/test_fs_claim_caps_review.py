"""F4 follow-up: what the area count must not let through, and the refusal body's contract.

- A literal path with no file extension may be a directory, and a directory claim covers its
  whole subtree: it is its own area, never a share of its parent's (or of the repo root's).
- The 409 ``claim_cap`` body fits the published ``CrewError`` shape (message of at most 500
  characters, a declared ``cap`` object).
- A session that holds nothing may take one whole request (a task's zones, a baton group)
  under the per-agent cap, still bounded by the per-session cap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from remembra.crew import claims as C
from remembra.crew import gatecore as G
from remembra.crew import schemas as S
from remembra.crew import zones as Z
from tests.crew.test_fs_claim_caps import WRITE_INVOICE, _codex, _gate_snapshot, _path
from tests.crew.wp5_support import assert_chain_ok, make_ops, open_db, seed_crew

REPO = Path(__file__).resolve().parents[2]


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


async def _counts(db, sid: str) -> tuple[int, int]:
    return await C.exclusive_counts(db.conn, await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (sid,)))


# ---------------------------------------------------------------------------
# Directory claims: one area each
# ---------------------------------------------------------------------------


async def test_top_level_directory_claims_use_one_slot_each(env):
    """``src``, ``lib``, ``app``, ``docs`` ... used to share the one area ``dir:.`` and lock the whole repo."""
    db, ops = env
    a, b = await _codex(db, "cs_a", "codex-1"), await _codex(db, "cs_b", "codex-2")
    got = [await _path(ops, a, d) for d in ("src", "lib", "app", "docs", "tests", "scripts")]
    assert [o.status for o in got] == ["granted"] * 3 + ["denied"] * 3
    assert {o.error for o in got[3:]} == {"claim_cap"} and got[3].body()["cap"]["cap"] == "session"
    assert await _counts(db, "cs_a") == (3, 3)
    # what the session could not take is free for the others
    assert (await _path(ops, b, "docs/guide/intro.md")).status == "granted"
    assert (await _path(ops, b, "src/deep/new_file.py")).error == "conflict"


async def test_nested_directory_claims_use_one_slot_each(env):
    db, ops = env
    a = await _codex(db, "cs_a", "codex-1")
    got = [await _path(ops, a, d) for d in ("app/api", "app/portal", "app/lib", "app/admin")]
    assert [o.status for o in got] == ["granted", "granted", "granted", "denied"] and got[3].error == "claim_cap"
    assert await _counts(db, "cs_a") == (3, 3)


async def test_a_directory_and_the_files_inside_it_are_one_area(env):
    db, ops = env
    a = await _codex(db, "cs_a", "codex-1")
    for glob in ("public/brand", "public/brand/logo.svg", "public/brand/**", "public/brand/mark.png"):
        assert (await _path(ops, a, glob)).status in ("granted", "existing"), glob
    assert await _counts(db, "cs_a") == (1, 1)
    # the job the first fix was for still fits: many files in two folders
    for i in range(6):
        assert (await _path(ops, a, f"app/portal/f{i}.tsx")).status == "granted"
    assert (await _path(ops, a, "app/layout.tsx")).status == "granted"
    assert await _counts(db, "cs_a") == (3, 3)


def test_claim_folder_never_folds_a_possible_directory_into_its_parent():
    f = G.claim_folder
    assert [f(g) for g in ("src", "lib", "app", ".github", "Makefile")] == ["src", "lib", "app", ".github", "Makefile"]
    assert f("app/api") == "app/api" and f("app/api/") == "app/api" and f("public/brand") == "public/brand"
    # a name with a file extension is a file: it counts under its folder, as before
    assert f("app/api/route.ts") == "app/api" and f("app/.env.example") == "app" and f("README.md") == "."
    assert f("app/portal/**") == "app/portal" and f("app/portal/*.tsx") == "app/portal" and f("app/{a,b}/x.ts") == "app"
    assert f("") == "."
    zones: list = []
    assert C.claim_unit({"path_glob": "src"}, zones) == "dir:src" != C.claim_unit({"path_glob": "lib"}, zones)
    assert C.claim_unit({"path_glob": "app/api"}, zones) == C.claim_unit({"path_glob": "app/api/route.ts"}, zones)


def test_gate_counts_directory_claims_one_area_each():
    """The hook gate mirrors the server: three directory claims are the per-session cap, so no auto-claim."""
    from tests.crew.gatecore_support import run

    snap, base = _gate_snapshot()
    mine = {**base, "resource": None, "holder_session_id": "cs_c", "state": "active", "reserved_for": None}
    for n, d in enumerate(("scripts", "tools", "bin")):
        snap["claims"].append({**mine, "id": f"clm_d{n}", "path_glob": d})
    calls: list = []
    v = run("Write", WRITE_INVOICE, snap=snap, claim=lambda r: calls.append(r) or "granted")
    assert (v.rule, v.decision, v.variant) == (17, "deny", "claim_required") and calls == []
    assert "you hold 3 of 3 exclusive claims, the per-session limit" in (v.reason or "")


# ---------------------------------------------------------------------------
# The refusal body fits the published CrewError shape
# ---------------------------------------------------------------------------

LONG = ["apps/storefront/components/checkout", "apps/storefront/components/catalog", "packages/design-system/tokens"]


def _contract_errors(body: dict) -> list[str]:
    # ``seq`` rides on every claim outcome since before this fix and is not part of this finding
    return S.validate({k: v for k, v in body.items() if k != "seq"}, S.ERROR_BODY)


async def test_the_per_session_refusal_fits_the_error_contract_with_long_paths(env):
    db, ops = env
    a = await _codex(db, "cs_a", "codex-1")
    paths = [f"{LONG[0]}/file-number-{i}.component.tsx" for i in range(4)] + [f"{LONG[1]}/a.tsx", f"{LONG[2]}/b.ts"]
    for p in paths:
        assert (await _path(ops, a, p)).status == "granted"
    denied = await _path(ops, a, "zz/x.ts")
    body = denied.body()
    assert denied.http_status == 409 and body["error"] == "claim_cap"
    assert _contract_errors(body) == []
    msg = body["message"]
    assert len(msg) <= 500 and len(body["cap"]["message"]) <= 500
    # shortened, not cut off: the numbers, the count left out, and the advice are all still there
    assert "you hold 3 of 3 exclusive areas" in msg and "more)" in msg and f"path {paths[0]}" in msg
    assert 'crew_claim(action="release")' in msg and "max_exclusive_claims_per_session" in msg
    assert len(body["cap"]["holding"]) == 6  # the full list stays in the detail


async def test_the_per_agent_refusal_and_the_adopt_refusal_fit_the_error_contract(env):
    db, ops = env
    sessions = [await _codex(db, f"cs_{n}", f"codex-{n}") for n in range(1, 4)]
    first = [(await _path(ops, sessions[0], f"{LONG[0]}/{folder}/x.ts")).claim for folder in "abc"]
    # adopt adds its own sentence to the message: still within the contract
    await C.release_claim(ops, first[0], sessions[0], baton=True)
    assert (await _path(ops, sessions[0], f"{LONG[2]}/deep/x.ts")).status == "granted"
    with pytest.raises(Z.CrewOpError) as err:
        await C.adopt(ops, first[0], sessions[0])
    body = err.value.body()
    assert body["error"] == "claim_cap" and body["cap"]["cap"] == "session"
    assert body["message"].endswith("Then adopt this baton.")
    assert _contract_errors(body) == [] and len(body["message"]) <= 500
    # the per-agent refusal
    for folder in "abc":
        assert (await _path(ops, sessions[1], f"{LONG[1]}/{folder}/x.ts")).status == "granted"
    c3 = sessions[2]
    assert (await _path(ops, c3, "lib/x.ts")).status == "granted"
    denied = await _path(ops, c3, "db/x.sql")
    assert denied.body()["cap"]["cap"] == "agent" and _contract_errors(denied.body()) == []
    assert "codex-1 x3" in denied.body()["message"] and len(denied.body()["message"]) <= 500


def test_the_cap_object_is_in_the_published_openapi():
    doc = json.loads((REPO / "docs/crew/openapi.json").read_text())
    schemas = doc["components"]["schemas"]
    assert schemas["CrewError"]["properties"]["cap"] == {"$ref": "#/components/schemas/ClaimCapDetail"}
    assert "cap" not in schemas["CrewError"]["required"]  # optional: only a claim_cap refusal carries it
    detail = schemas["ClaimCapDetail"]
    assert detail["properties"]["cap"]["enum"] == ["session", "agent"]
    assert {"limit", "held_by_you", "held_by_agent", "holders", "holding", "message"} <= set(detail["properties"])
    assert (REPO / "docs/crew/openapi.json").read_text() == S.render_openapi()


# ---------------------------------------------------------------------------
# A session that holds nothing may take one whole request under the per-agent cap
# ---------------------------------------------------------------------------


async def _fill_fleet(db, ops) -> None:
    for sid, sign, folders in (("cs_5", "codex-5", "abc"), ("cs_3", "codex-3", "def")):
        who = await _codex(db, sid, sign)
        for folder in folders:
            assert (await _path(ops, who, f"{folder}/x.ts")).status == "granted"


async def test_cap_breach_lets_a_session_holding_nothing_take_one_whole_request(env):
    db, ops = env
    await _fill_fleet(db, ops)
    await _codex(db, "cs_4", "codex-4")
    session = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_4'")
    settings = {"max_exclusive_claims_per_session": 3, "max_exclusive_claims_per_agent": 6}
    two = [{"zone_id": "zn_1"}, {"zone_id": "zn_2"}]
    assert await C.cap_breach(db.conn, session, settings, two) is None
    assert await C.cap_breach(db.conn, session, settings, two + [{"zone_id": "zn_3"}]) is None
    # the per-session cap still bounds the request
    four = await C.cap_breach(db.conn, session, settings, two + [{"zone_id": "zn_3"}, {"zone_id": "zn_4"}])
    assert four is not None and (four["cap"], four["held_by_you"], four["needed"]) == ("session", 0, 4)


async def test_a_session_holding_nothing_starts_a_two_zone_task_with_the_fleet_at_the_cap(tmp_path):
    """Through the real task-start path: the fleet holds the per-agent 6, a fresh sibling starts a task of two zones."""
    from remembra.crew.tasks import Caller, CrewServiceError, TaskService
    from tests.crew import wp6_support as W

    db = await W.open_db(tmp_path)
    try:
        await W.seed_crew(db)
        svc = TaskService(W.event_log(db))
        human = Caller.for_human("u_owner", privileged=True)
        zones = [await W.seed_zone(db, slug=f"z{i}", includes=[f"z{i}/**"]) for i in range(7)]
        now = W.now_iso()
        fleet = [await W.seed_session(db, callsign=f"codex-{n}", agent_id="codex") for n in (1, 2)]
        async with db.transaction():
            for n, s in enumerate(fleet):
                for folder in "abc":
                    await db.conn.execute(
                        """INSERT INTO crew_claims (id, crew_id, path_glob, mode, holder_kind, holder_session_id,
                               holder_user_id, holder_agent_id, state, source, epoch, created_at, updated_at)
                           VALUES (?, ?, ?, 'exclusive', 'session', ?, ?, ?, 'active', 'mcp', 1, ?, ?)""",
                        (f"clm_{n}{folder}", W.CREW, f"w{n}{folder}/x.ts", s["id"], s["user_id"], s["agent_id"], now, now),
                    )
        await W.set_settings(db, W.CREW, wip_per_session=5)
        fresh = await W.seed_session(db, callsign="codex-3", agent_id="codex")
        assert await C.exclusive_counts(db.conn, fresh) == (0, 6)

        def task(title, ids):
            return svc.create(W.CREW, human, {"title": title, "zone_ids": ids, "acceptance": [], "depends_on": []})

        two = (await task("two", zones[:2])).task
        await svc.claim(W.CREW, two["id"], Caller.for_session(fresh))
        assert await C.exclusive_counts(db.conn, fresh) == (2, 8)
        # it now holds areas, so the per-agent cap applies to it like to everyone else
        one = (await task("one", zones[2:3])).task
        with pytest.raises(CrewServiceError) as e:
            await svc.claim(W.CREW, one["id"], Caller.for_session(fresh))
        assert (e.value.status, e.value.error, e.value.extra["cap"]["cap"]) == (409, "claim_cap", "agent")
        assert len(e.value.message) <= 500
        # and a request larger than the per-session cap is refused even for a session that holds nothing
        other = await W.seed_session(db, callsign="codex-4", agent_id="codex")
        four = (await task("four", zones[3:7])).task
        with pytest.raises(CrewServiceError) as e:
            await svc.claim(W.CREW, four["id"], Caller.for_session(other))
        assert (e.value.error, e.value.extra["cap"]["cap"], e.value.extra["cap"]["held_by_you"]) == ("claim_cap", "session", 0)
    finally:
        await db.close()
