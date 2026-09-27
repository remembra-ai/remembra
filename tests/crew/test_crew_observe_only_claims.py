"""Observe-only sessions cannot claim (§12): every path that grants a session a claim checks its seat.

A session that joined over the plan's live-session cap is observe-only: it gets the brief and
DO-NOT-TOUCH, is still denied by others' claims, and **cannot claim**. The refusal is
403 ``observe_only`` with the plan's upgrade hint, on explicit zone, path and resource claims,
queued claims, handover accepts, baton adopts, task claim/start/adopt and a human's assign. The
server guard does not auto-claim for it (a write in a free zone is allowed without a claim; a
write in a held zone is still denied). Once an earlier session ends, the same session has a seat
and claims normally. A serialize micro-lease is not a claim of work and stays allowed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from remembra.api.v1 import auth, crew_claims, crew_tasks, crew_zones
from remembra.cloud.plans import PLANS, PlanTier
from remembra.crew import claims as C
from remembra.crew import zones as Z
from remembra.crew.claims import SESSION_HEADER
from remembra.crew.events import CrewEventLog
from remembra.crew.limits import crew_limits_for_tier
from remembra.crew.store import now_iso
from remembra.crew.tasks import Caller, CrewServiceError, TaskService
from tests.crew.wp5_support import CREW, OWNER, ZONES_YML, make_ops, open_db, seed_crew, seed_session, zone_id
from tests.security_harness import make_settings, secure_app

FREE = crew_limits_for_tier("free")  # 3 live sessions per crew


async def _free_limits(_owner: str):  # noqa: ANN202
    return FREE


@pytest.fixture
async def env(tmp_path):
    """A Free crew with 3 seated sessions (cs_a, cs_b, cs_c) and cs_d over the cap, in cs_a's checkout."""
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db, tier="free")
    rows = {}
    for sid, n in (("cs_a", 1), ("cs_b", 2), ("cs_c", 3), ("cs_d", 4)):
        wt = "a" if sid in ("cs_a", "cs_d") else sid[-1]
        rows[sid], _ = await seed_session(db, sid, callsign=f"cc-{n}", worktree_id=f"wt-{wt}", checkout_fp=f"fp-{wt}")
    a = Z.Principal.for_session(rows["cs_a"])
    await Z.upload_zones_file(ops, CREW, a, yaml_text=ZONES_YML, sha="s1", branch="main")
    try:
        yield db, ops, {k: Z.Principal.for_session(v) for k, v in rows.items()}
    finally:
        await db.close()


def _observe_only(e: pytest.ExceptionInfo[Z.CrewOpError] | pytest.ExceptionInfo[CrewServiceError]) -> None:
    assert e.value.status == 403 and e.value.error == "observe_only", str(e.value)
    assert e.value.extra["upgrade_hint"] == "Solo allows 5 live sessions per crew."


async def _claims_of(db, sid: str) -> list[dict]:
    return await db.fetchall("SELECT * FROM crew_claims WHERE holder_session_id = ?", (sid,))


async def test_an_observe_only_session_cannot_claim_a_zone_path_or_resource(env):
    db, ops, s = env
    pos = await zone_id(db, "pos")
    for kw in (
        {"zone_id": pos},
        {"zone_id": pos, "wait": True},
        {"path_glob": "src/other/**"},
        {"resource": "deploy:vercel"},
        {"zone_id": pos, "source": "first_write"},
    ):
        with pytest.raises(Z.CrewOpError) as e:
            await C.request_claim(ops, CREW, s["cs_d"], **kw)
        _observe_only(e)
    assert await _claims_of(db, "cs_d") == []

    # a seated session claims as before
    got = await C.request_claim(ops, CREW, s["cs_c"], zone_id=pos)
    assert got.status == "granted"

    # a serialize micro-lease is not a claim of work
    lease = await C.request_claim(ops, CREW, s["cs_d"], path_glob="package.json", source="micro_lease")
    assert lease.status == "granted"

    # the seat frees up when an earlier session ends: the same session claims
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET state = 'ended' WHERE id = 'cs_a'")
    got = await C.request_claim(ops, CREW, s["cs_d"], resource="deploy:vercel")
    assert got.status == "granted"


async def test_an_observe_only_session_cannot_accept_a_handover_or_adopt_a_baton(env):
    db, ops, s = env
    pos = await zone_id(db, "pos")
    held = await C.request_claim(ops, CREW, s["cs_a"], zone_id=pos)
    await C.handover(ops, held.claim, s["cs_a"], to="cs_d")
    with pytest.raises(Z.CrewOpError) as e:
        await C.accept_handover(ops, await C.get_claim(db.conn, held.claim["id"]), s["cs_d"])
    _observe_only(e)
    await C.decline_handover(ops, await C.get_claim(db.conn, held.claim["id"]), s["cs_d"])

    # a reserved baton in its own checkout (same_checkout authorisation) is not adopted either
    await C.release_claim(ops, await C.get_claim(db.conn, held.claim["id"]), s["cs_a"], baton=True)
    baton = await C.get_claim(db.conn, held.claim["id"])
    assert baton["state"] == "reserved"
    with pytest.raises(Z.CrewOpError) as e:
        await C.adopt(ops, baton, s["cs_d"])
    _observe_only(e)
    assert await _claims_of(db, "cs_d") == []


async def test_the_guard_never_auto_claims_for_an_observe_only_session(env):
    db, ops, s = env
    # a write in a free zone: allowed, no claim made
    out = await C.server_guard(ops, CREW, s["cs_d"], op="write", paths=["src/app/reports/r.ts"])
    assert out["decision"] == "allow" and out["auto_claimed"] == [], out
    assert await _claims_of(db, "cs_d") == []
    # a seated session auto-claims the same way as before
    out = await C.server_guard(ops, CREW, s["cs_b"], op="write", paths=["src/billing/b.ts"])
    assert out["decision"] == "allow" and [c["zone_id"] for c in out["auto_claimed"]] == [await zone_id(db, "billing")]
    # and others' claims still stop the observe-only session
    out = await C.server_guard(ops, CREW, s["cs_d"], op="write", paths=["src/billing/b.ts"])
    assert out["decision"] == "deny", out


async def _task(db, number: int, *, status: str, zones: list[str], owner: str | None = None) -> str:
    tid = f"tsk_{number:026d}"
    now = now_iso()
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_tasks (id, crew_id, number, title, status, zone_ids, owner_session_id, owner_user_id,"
            " owner_agent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid,
                CREW,
                number,
                f"task {number}",
                status,
                json.dumps(zones),
                owner,
                OWNER if owner else None,
                "claude-code" if owner else None,
                now,
                now,
            ),
        )
    return tid


async def test_an_observe_only_session_cannot_claim_start_adopt_or_be_assigned_a_task(env):
    db, ops, s = env
    tasks = TaskService(CrewEventLog(db, None), limits_for=_free_limits)
    reports = await zone_id(db, "reports")
    d = Caller.for_session(s["cs_d"].session)
    t1 = await _task(db, 1, status="ready", zones=[reports])
    for act in (tasks.claim, tasks.start):
        with pytest.raises(CrewServiceError) as e:
            await act(CREW, t1, d)
        _observe_only(e)
    stalled = await _task(db, 2, status="stalled", zones=[], owner="cs_a")
    with pytest.raises(CrewServiceError) as e:
        await tasks.adopt(CREW, stalled, d)  # same checkout as cs_a: authorised, but no seat
    _observe_only(e)
    with pytest.raises(CrewServiceError) as e:
        await tasks.assign(CREW, t1, Caller.for_human(OWNER, privileged=True), "cs_d")
    _observe_only(e)
    assert await _claims_of(db, "cs_d") == []
    # a seated session starts it and gets its zone
    res = await tasks.start(CREW, t1, Caller.for_session(s["cs_c"].session))
    assert res.task["status"] == "in_progress" and [c["zone_id"] for c in res.extra["claims"]] == [reports]


class _FreeMeter:
    async def get_account(self, _user_id: str):  # noqa: ANN202
        return SimpleNamespace(limits=PLANS[PlanTier.FREE])


async def test_claim_guard_and_task_routes_refuse_an_observe_only_session_on_the_owners_plan(tmp_path):
    db = await open_db(tmp_path)
    try:
        async with secure_app(
            tmp_path,
            [auth.router, crew_zones.router, crew_claims.router, crew_tasks.router],
            state={"crew_db": db, "usage_meter": _FreeMeter()},
            settings=make_settings(),
        ) as h:
            owner = await h.create_user("owner@example.com", password="Str0ng!Passw0rd")
            await seed_crew(db, CREW, owner=owner)
            key, _ = await h.api_key(owner, "admin")
            tokens = {}
            for sid, n in (("cs_a", 1), ("cs_b", 2), ("cs_c", 3), ("cs_d", 4)):
                _, tokens[sid] = await seed_session(db, sid, user_id=owner, callsign=f"cc-{n}", worktree_id=f"wt-{sid}")
            a = {"X-API-Key": key, SESSION_HEADER: tokens["cs_a"]}
            d = {"X-API-Key": key, SESSION_HEADER: tokens["cs_d"]}
            base = f"/api/v1/crews/{CREW}"
            res = await h.client.put(f"{base}/zones/file", json={"yaml": ZONES_YML, "sha": "s1", "branch": "main"}, headers=a)
            assert res.status_code == 200, res.text
            pos = await zone_id(db, "pos")
            body = {"zone_id": pos, "mode": "exclusive", "wait": False, "source": "mcp"}
            res = await h.client.post(f"{base}/claims", json=body, headers=d)
            assert res.status_code == 403, res.text
            detail = res.json()["detail"]
            assert detail["error"] == "observe_only" and detail["upgrade_hint"] == "Solo allows 5 live sessions per crew."
            res = await h.client.post(f"{base}/claims", json={**body, "zone_id": None, "resource": "deploy:vercel"}, headers=d)
            assert res.status_code == 403 and res.json()["detail"]["error"] == "observe_only", res.text
            res = await h.client.post(
                f"{base}/guard", json={"session_id": "cs_d", "op": "write", "paths": ["src/app/reports/r.ts"]}, headers=d
            )
            assert res.status_code == 200 and res.json()["decision"] == "allow" and res.json()["auto_claimed"] == [], res.text
            t1 = await _task(db, 1, status="ready", zones=[await zone_id(db, "reports")])
            res = await h.client.post(
                f"/api/v1/tasks/{t1}/start", json={}, headers={**d, "X-Remembra-Crew-Session": tokens["cs_d"]}
            )
            assert res.status_code == 403 and res.json()["detail"]["error"] == "observe_only", res.text
            assert await db.fetchall("SELECT id FROM crew_claims WHERE holder_session_id = 'cs_d'") == []
            # the first seated session still claims
            res = await h.client.post(f"{base}/claims", json=body, headers=a)
            assert res.status_code == 201, res.text
    finally:
        await db.close()
