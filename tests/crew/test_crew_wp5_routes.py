"""WP-5 routes over real HTTP: real API keys and JWTs (auth enabled), real crew.db, route-table audit."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import jwt

from remembra.api.v1 import auth, crew_claims, crew_zones
from remembra.crew import limits as crew_limits
from remembra.crew.access import audit_crew_routes
from remembra.crew.claims import SESSION_HEADER
from remembra.crew.limits import CrewRateLimiter
from remembra.crew.schemas import ROUTES
from tests.crew.wp5_support import ZONES_YML, assert_chain_ok, events, open_db, seed_crew, seed_session, seed_task
from tests.security_harness import JWT_SECRET, make_settings, secure_app

CREW = "crw_0000000000000a01"
FOREIGN = "crw_0000000000000f0f"
PASSWORD = "Str0ng!Passw0rd"
WP5 = [r for r in ROUTES if r.owner == "WP-5"]


@asynccontextmanager
async def crew_http(tmp_path: Any, **settings: Any):
    db = await open_db(tmp_path)
    try:
        async with secure_app(
            tmp_path,
            [auth.router, crew_zones.router, crew_claims.router],
            state={"crew_db": db},
            settings=make_settings(**settings),
        ) as h:
            owner = await h.create_user("owner@example.com", password=PASSWORD)
            await seed_crew(db, CREW, owner=owner)
            intruder = await h.create_user("intruder@example.com", password=PASSWORD)
            await seed_crew(db, FOREIGN, owner=intruder, project="theirs")
            key, _ = await h.api_key(owner, "admin")
            login = await h.client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
            assert login.status_code == 200, login.text
            jwt_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            _, tok_a = await seed_session(db, "cs_a", crew_id=CREW, user_id=owner, callsign="cc-1", worktree_id="wt-a")
            _, tok_b = await seed_session(db, "cs_b", crew_id=CREW, user_id=owner, callsign="cc-2", worktree_id="wt-b")
            ctx = {
                "owner": owner,
                "key": key,
                "jwt": jwt_headers,
                "a": {"X-API-Key": key, SESSION_HEADER: tok_a},
                "b": {"X-API-Key": key, SESSION_HEADER: tok_b},
                "intruder_key": (await h.api_key(intruder, "admin"))[0],
            }
            yield h, db, ctx
            await assert_chain_ok(db)
    finally:
        await db.close()


def _stale(user_id: str) -> dict[str, str]:
    issued = int(time.time() * 1000) - 20 * 60_000
    token = jwt.encode(
        {
            "sub": user_id,
            "email": "owner@example.com",
            "iat": issued // 1000,
            "iat_ms": issued,
            "exp": int(time.time()) + 3600,
            "type": "access",
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def test_every_wp5_route_is_registered_with_the_contract_access(tmp_path):
    async with crew_http(tmp_path) as (h, _db, _ctx):
        assert audit_crew_routes(h.app.router.routes, WP5, require_all=True) == []
        assert len(WP5) == 29


async def test_zones_claims_and_guard_over_http(tmp_path):
    async with crew_http(tmp_path) as (h, db, ctx):
        c = h.client
        base = f"/api/v1/crews/{CREW}"
        res = await c.put(
            f"{base}/zones/file", json={"yaml": ZONES_YML, "sha": "s1", "branch": "main"}, headers={"X-API-Key": ctx["key"]}
        )
        assert res.status_code == 401 and res.json()["detail"]["error"] == "session_required"
        res = await c.put(f"{base}/zones/file", json={"yaml": ZONES_YML, "sha": "s1", "branch": "main"}, headers=ctx["a"])
        assert res.status_code == 200 and res.json()["result"] == "applied"
        bad = await c.put(f"{base}/zones/file", json={"yaml": "zones: [", "sha": "s2", "branch": "main"}, headers=ctx["a"])
        assert bad.status_code == 422 and bad.json()["detail"]["error"] == "invalid_zones_file"
        extra = await c.put(f"{base}/zones/file", json={"yaml": "", "sha": "s2", "branch": "main", "x": 1}, headers=ctx["a"])
        assert extra.status_code == 422
        zones = (await c.get(f"{base}/zones", headers=ctx["a"])).json()["zones"]
        pos = next(z for z in zones if z["slug"] == "pos")
        claim_body = {"zone_id": pos["id"], "mode": "exclusive", "wait": False, "source": "mcp"}
        got = await c.post(f"{base}/claims", json=claim_body, headers={**ctx["a"], "Idempotency-Key": "k1"})
        assert got.status_code == 201, got.text
        replay = await c.post(f"{base}/claims", json=claim_body, headers={**ctx["a"], "Idempotency-Key": "k1"})
        assert replay.status_code == 201 and replay.json()["claim"]["id"] == got.json()["claim"]["id"]
        clash = await c.post(
            f"{base}/claims", json={**claim_body, "mode": "shared"}, headers={**ctx["a"], "Idempotency-Key": "k1"}
        )
        assert clash.status_code == 422 and clash.json()["detail"]["error"] == "idempotency_conflict"
        assert len(await events(db, type_prefix="claim.granted")) == 1
        denied = await c.post(f"{base}/claims", json=claim_body, headers=ctx["b"])
        assert denied.status_code == 409
        assert (
            denied.json()["detail"]["error"] == "conflict" and denied.json()["detail"]["blockers"][0]["holder_callsign"] == "cc-1"
        )
        queued = await c.post(f"{base}/claims", json={**claim_body, "wait": True, "wait_s": 1}, headers=ctx["b"])
        assert queued.status_code == 202 and queued.json()["status"] == "queued" and queued.json()["waited_s"] == 1
        g = await c.post(
            f"{base}/guard", json={"session_id": "cs_b", "op": "write", "paths": ["src/app/pos/cart.ts"]}, headers=ctx["b"]
        )
        assert g.status_code == 200 and g.json()["decision"] == "deny" and g.json()["rule"] == 9
        mismatch = await c.post(f"{base}/guard", json={"session_id": "cs_a", "op": "write", "paths": ["x"]}, headers=ctx["b"])
        assert mismatch.status_code == 422
        listed = (await c.get(f"{base}/claims", headers=ctx["a"])).json()["claims"]
        assert {cl["state"] for cl in listed} == {"active", "queued"}
        claim_id = got.json()["claim"]["id"]
        rel = await c.post(f"/api/v1/claims/{claim_id}/release", json={"baton": False}, headers=ctx["b"])
        assert rel.status_code == 403 and rel.json()["detail"]["error"] == "not_holder"
        rel = await c.post(f"/api/v1/claims/{claim_id}/release", json={}, headers=ctx["a"])
        assert rel.status_code == 200 and rel.json()["claim"]["state"] == "released"
        m = await c.post(f"{base}/match", json={"paths": ["src/app/pos/x.ts"]}, headers=ctx["a"])
        assert m.json()["paths"][0]["leaf_zone"] == "pos"
        export = await c.get(f"{base}/zones/export", headers=ctx["a"])
        assert "pos:" in export.json()["yaml"]
        tree = {"name": ".", "files": 1, "children": [{"name": "src", "files": 2, "children": []}]}
        assert (await c.put(f"{base}/tree", json={"tree": tree}, headers=ctx["a"])).status_code == 200
        sug = await c.post(f"{base}/zones/suggest", json={}, headers=ctx["a"])
        assert sug.status_code == 200 and sug.json()["zones"][0]["slug"] == "src"


async def test_tenancy_404_and_human_only_routes(tmp_path):
    async with crew_http(tmp_path) as (h, db, ctx):
        c = h.client
        await c.put(f"/api/v1/crews/{CREW}/zones/file", json={"yaml": ZONES_YML, "sha": "s1", "branch": "main"}, headers=ctx["a"])
        pos = await db.fetchone("SELECT id, version FROM crew_zones WHERE slug = 'pos'")
        assert pos is not None
        for url, method in (
            (f"/api/v1/crews/{CREW}/zones", "GET"),
            (f"/api/v1/zones/{pos['id']}", "DELETE"),
            (f"/api/v1/crews/{CREW}/claims", "GET"),
        ):
            res = await c.request(method, url, headers={"X-API-Key": ctx["intruder_key"]})
            assert res.status_code == 404, (url, res.text)
        res = await c.get(f"/api/v1/crews/{FOREIGN}/zones", headers=ctx["a"])
        assert res.status_code == 404
        # admin API key with a live session token is still never human (D27)
        human_only = [
            ("POST", f"/api/v1/zones/{pos['id']}/freeze", {"reason": "x"}),
            ("POST", f"/api/v1/zones/{pos['id']}/unfreeze", {"reason": "x"}),
            ("POST", f"/api/v1/crews/{CREW}/bypass-codes", {"session_id": "cs_b", "scope": "push", "minutes": 5}),
        ]
        for method, url, body in human_only:
            res = await c.request(method, url, json=body, headers=ctx["a"])
            assert res.status_code == 403 and res.json()["detail"]["error"] == "human_only", url
        # zone PATCH needs If-Match
        res = await c.patch(f"/api/v1/zones/{pos['id']}", json={"title": "x"}, headers=ctx["a"])
        assert res.status_code == 428
        res = await c.patch(
            f"/api/v1/zones/{pos['id']}", json={"title": "Till"}, headers={**ctx["a"], "If-Match": str(pos["version"])}
        )
        assert res.status_code == 200 and res.json()["applied"] is False and "Till" in res.json()["export_patch"]
        # freeze with a login works; unfreeze needs a fresh login (step-up)
        res = await c.post(f"/api/v1/zones/{pos['id']}/freeze", json={"reason": "mine"}, headers=ctx["jwt"])
        assert res.status_code == 200 and res.json()["zone"]["frozen_by"] == ctx["owner"]
        res = await c.post(f"/api/v1/zones/{pos['id']}/unfreeze", json={"reason": "done"}, headers=_stale(ctx["owner"]))
        assert res.status_code == 401 and res.json()["detail"]["error"] == "step_up_required"
        res = await c.post(f"/api/v1/zones/{pos['id']}/unfreeze", json={"reason": "done"}, headers=ctx["jwt"])
        assert res.status_code == 200 and res.json()["zone"]["frozen_by"] is None


async def test_pending_change_approval_and_bypass_codes_over_http(tmp_path):
    async with crew_http(tmp_path) as (h, db, ctx):
        c = h.client
        base = f"/api/v1/crews/{CREW}"
        await c.put(
            f"{base}/zones/file",
            json={"yaml": "zones:\n  pos: {include: [p/**], protected: true}\n", "sha": "a", "branch": "main"},
            headers=ctx["a"],
        )
        res = await c.put(
            f"{base}/zones/file", json={"yaml": "zones:\n  pos: [p/**]\n", "sha": "b", "branch": "main"}, headers=ctx["a"]
        )
        change_id = res.json()["change_id"]
        assert res.json()["result"] == "pending"
        pending = await c.get(f"{base}/zone-changes?state=pending", headers=ctx["a"])
        assert [x["id"] for x in pending.json()["changes"]] == [change_id]
        res = await c.post(f"/api/v1/zone-changes/{change_id}/approve", headers=ctx["a"])
        assert res.status_code == 403
        res = await c.post(f"/api/v1/zone-changes/{change_id}/approve", headers=_stale(ctx["owner"]))
        assert res.status_code == 401
        res = await c.post(f"/api/v1/zone-changes/{change_id}/approve", headers=ctx["jwt"])
        assert res.status_code == 200 and res.json()["state"] == "applied"
        row = await db.fetchone("SELECT protected FROM crew_zones WHERE slug = 'pos'")
        assert row == {"protected": 0}
        audit = await h.db.conn.execute("SELECT action FROM audit_log WHERE action LIKE 'crew.%'")
        assert "crew.zone_change_approved" in {r[0] for r in await audit.fetchall()}
        issued = await c.post(
            f"{base}/bypass-codes", json={"session_id": "cs_b", "scope": "push", "minutes": 5}, headers=ctx["jwt"]
        )
        assert issued.status_code == 201 and issued.json()["code"].startswith("RCB-")
        code = issued.json()["code"]
        wrong = await c.post("/api/v1/bypass-codes/redeem", json={"code": code, "session_id": "cs_b"}, headers=ctx["a"])
        assert wrong.status_code == 401  # token of cs_a does not name cs_b
        ok = await c.post("/api/v1/bypass-codes/redeem", json={"code": code, "session_id": "cs_b"}, headers=ctx["b"])
        assert ok.status_code == 200 and ok.json()["scope"] == "push"
        again = await c.post("/api/v1/bypass-codes/redeem", json={"code": code, "session_id": "cs_b"}, headers=ctx["b"])
        assert again.status_code == 403 and again.json()["detail"]["error"] == "invalid_code"


async def test_adopt_rate_limit_and_collision_routes(tmp_path):
    limiter = CrewRateLimiter("memory://")
    crew_limits.set_crew_rate_limiter(limiter)
    try:
        async with crew_http(tmp_path, rate_limit_enabled=True) as (h, db, ctx):
            c = h.client
            base = f"/api/v1/crews/{CREW}"
            await c.put(f"{base}/zones/file", json={"yaml": ZONES_YML, "sha": "s1", "branch": "main"}, headers=ctx["a"])
            pos = await db.fetchone("SELECT id FROM crew_zones WHERE slug = 'pos'")
            await seed_task(db, "tsk_1", 1)
            got = await c.post(
                f"{base}/claims",
                json={"zone_id": pos["id"], "mode": "exclusive", "wait": False, "source": "task", "task_id": "tsk_1"},
                headers=ctx["a"],
            )
            claim_id = got.json()["claim"]["id"]
            await db.conn.execute("UPDATE crew_claims SET state = 'reserved', reserve_reason = 'quota' WHERE id = ?", (claim_id,))
            await db.conn.commit()
            codes = []
            for _ in range(7):
                res = await c.post(f"/api/v1/claims/{claim_id}/adopt", json={}, headers=ctx["b"])
                codes.append(res.status_code)
            assert codes[:6] == [403] * 6 and codes[6] == 429  # not offered, then the 6/h adopt bucket
            # collisions: list, ack by a party, dismiss only by a human
            from remembra.crew import collisions as CO
            from remembra.crew import zones as Z
            from remembra.crew.events import CrewEventLog

            ops = Z.CrewOps(CrewEventLog(db, None))
            a = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_a'")
            b = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_b'")
            async with ops.log.transaction() as tx:
                await CO.record_footprints(ops, tx, CREW, a, [{"path": "src/x.ts", "state": "dirty", "attribution": "certain"}])
                await CO.record_footprints(ops, tx, CREW, b, [{"path": "src/x.ts", "state": "dirty", "attribution": "certain"}])
            cols = (await c.get(f"{base}/collisions", headers=ctx["a"])).json()["collisions"]
            assert [x["kind"] for x in cols] == ["same_file"]
            cid = cols[0]["id"]
            assert (await c.post(f"/api/v1/collisions/{cid}/ack", headers=ctx["a"])).json()["state"] == "acknowledged"
            assert (await c.post(f"/api/v1/collisions/{cid}/dismiss", json={}, headers=ctx["a"])).status_code == 403
            res = await c.post(f"/api/v1/collisions/{cid}/dismiss", json={"reason": "fine"}, headers=ctx["jwt"])
            assert res.status_code == 200 and res.json()["state"] == "dismissed"
    finally:
        crew_limits.set_crew_rate_limiter(None)


async def test_handover_and_override_over_http(tmp_path):
    async with crew_http(tmp_path) as (h, db, ctx):
        c = h.client
        base = f"/api/v1/crews/{CREW}"
        await c.put(f"{base}/zones/file", json={"yaml": ZONES_YML, "sha": "s1", "branch": "main"}, headers=ctx["a"])
        pos = await db.fetchone("SELECT id FROM crew_zones WHERE slug = 'pos'")
        got = await c.post(
            f"{base}/claims", json={"zone_id": pos["id"], "mode": "exclusive", "wait": False, "source": "mcp"}, headers=ctx["a"]
        )
        cid = got.json()["claim"]["id"]
        assert (await c.post(f"/api/v1/claims/{cid}/handover", json={}, headers=ctx["a"])).status_code == 422
        res = await c.post(f"/api/v1/claims/{cid}/handover", json={"to": "cs_b"}, headers=ctx["a"])
        assert res.status_code == 200 and res.json()["claim"]["state"] == "offered"
        assert (await c.post(f"/api/v1/claims/{cid}/decline", headers=ctx["b"])).json()["claim"]["state"] == "active"
        await c.post(f"/api/v1/claims/{cid}/handover", json={"to": "cs_b"}, headers=ctx["a"])
        res = await c.post(f"/api/v1/claims/{cid}/accept", headers=ctx["b"])
        assert res.status_code == 200 and res.json()["claim"]["holder_session_id"] == "cs_b"
        body = {"action": "revoke", "reason": "stop"}
        assert (await c.post(f"/api/v1/claims/{cid}/override", json=body, headers=ctx["a"])).status_code == 403
        assert (await c.post(f"/api/v1/claims/{cid}/override", json=body, headers=_stale(ctx["owner"]))).status_code == 401
        res = await c.post(f"/api/v1/claims/{cid}/override", json=body, headers=ctx["jwt"])
        assert res.status_code == 200 and res.json()["claim"]["state"] == "revoked"
        assert (await events(db, type_prefix="human.override"))[-1]["moment"] == 1
