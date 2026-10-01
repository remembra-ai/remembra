"""HTTP tests for the WP-6 routes with authentication ENABLED (real API keys, real JWTs, real crew.db)."""

from __future__ import annotations

import time

import jwt
from fastapi import FastAPI

from remembra.api.v1 import auth, crew_tasks
from remembra.api.v1.crew_tasks import SESSION_TOKEN_HEADER
from remembra.crew.access import _walk_routes, audit_crew_routes
from remembra.crew.limits import CrewRateLimiter, set_crew_rate_limiter
from remembra.crew.schemas import ROUTES
from remembra.core.time import utcnow
from tests.crew.wp6_support import CREW, OTHER_CREW, open_db, seed_crew, seed_session, seed_zone, set_settings
from tests.security_harness import JWT_SECRET, make_settings, secure_app

API = "/api/v1"


def test_every_wp6_l0_route_is_registered_with_the_contract_access():
    app = FastAPI()
    app.include_router(crew_tasks.router, prefix=API)
    problems = audit_crew_routes(app.routes)
    assert problems == []
    registered = {(m, path) for path, r, _markers in _walk_routes(app.routes) for m in r.methods or ()}
    wp6 = [r for r in ROUTES if r.owner == "WP-6" and r.release == "L0"]
    assert len(wp6) == 20
    for r in wp6:
        assert (r.method, API + r.path) in registered, (r.method, r.path)
    not_ours = {("POST", API + "/crews/{crew_id}/tasks/from-handoff")}  # L1
    assert not (not_ours & registered)


def stale_jwt(user_id: str, minutes_ago: int = 20) -> dict[str, str]:
    issued = int(time.time() * 1000) - minutes_ago * 60_000
    payload = {
        "sub": user_id,
        "email": "o@example.com",
        "iat": issued // 1000,
        "iat_ms": issued,
        "exp": int(time.time()) + 3600,
        "type": "access",
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, JWT_SECRET, algorithm='HS256')}"}


async def _setup(h, db):
    owner = await h.create_user("owner@example.com")
    await seed_crew(db, owner=owner)
    await set_settings(db, CREW, deploy_gate={"require_pushed": False, "require_live_check": False}, wip_per_session=5)
    key, _ = await h.api_key(owner, "admin")
    session = await seed_session(db, user_id=owner)
    return owner, {"X-API-Key": key}, {"X-API-Key": key, SESSION_TOKEN_HEADER: session["_token"]}, session


async def test_session_flow_create_start_checkpoint_report(tmp_path):
    db = await open_db(tmp_path)
    try:
        async with secure_app(tmp_path, [auth.router, crew_tasks.router], state={"crew_db": db}) as h:
            owner, key_only, as_session, session = await _setup(h, db)
            zone = await seed_zone(db)
            body = {
                "title": "POS split tender",
                "zone_ids": [zone],
                "acceptance": [{"id": "c1", "text": "tests", "kind": "test", "match": "npm test -- pos", "required": True}],
                "depends_on": [],
            }
            r = await h.client.post(f"{API}/crews/{CREW}/tasks", json=body, headers=key_only)
            assert r.status_code == 403 and r.json()["detail"]["error"] == "session_required"
            r = await h.client.post(f"{API}/crews/{CREW}/tasks", json=body, headers={**key_only, SESSION_TOKEN_HEADER: "forged"})
            assert r.status_code == 401 and r.json()["detail"]["error"] == "invalid_session_token"

            idem = {**as_session, "Idempotency-Key": "create-1"}
            r = await h.client.post(f"{API}/crews/{CREW}/tasks", json=body, headers=idem)
            assert r.status_code == 201, r.text
            task = r.json()["task"]
            assert r.json()["seq"] >= 1 and task["status"] == "ready" and task["ref"] == "T-1"
            again = await h.client.post(f"{API}/crews/{CREW}/tasks", json=body, headers=idem)
            assert again.json()["replayed"] is True and again.json()["task"]["id"] == task["id"]
            conflict = await h.client.post(f"{API}/crews/{CREW}/tasks", json={**body, "title": "x"}, headers=idem)
            assert conflict.status_code == 422 and conflict.json()["detail"]["error"] == "idempotency_conflict"
            assert (await db.fetchone("SELECT COUNT(*) AS n FROM crew_tasks"))["n"] == 1
            stored = await db.fetchall("SELECT response_json FROM crew_idempotency")
            assert stored and all(session["_token"] not in s["response_json"] for s in stored)

            r = await h.client.post(f"{API}/tasks/{task['id']}/start", json={"head": "abc1234"}, headers=as_session)
            assert r.status_code == 200 and r.json()["task"]["status"] == "in_progress" and len(r.json()["claims"]) == 1

            ckp = {
                "session_id": session["id"],
                "trigger": "test",
                "facts": {
                    "tests": [{"command": "npm test -- pos", "passed": 3, "failed": 0, "observed_at": utcnow().isoformat()}]
                },
            }
            r = await h.client.post(f"{API}/crews/{CREW}/checkpoints", json=ckp, headers=as_session)
            assert r.status_code == 201 and r.json()["created"] and r.json()["checkpoint"]["facts_source"] == "relay-cli"
            r = await h.client.post(f"{API}/crews/{CREW}/checkpoints", json=ckp, headers=as_session)
            assert r.status_code == 200 and r.json()["created"] is False

            r = await h.client.get(f"{API}/crews/{CREW}/checkpoints", params={"session_id": session["id"]}, headers=key_only)
            assert r.status_code == 200 and r.json()["count"] >= 1

            r = await h.client.post(f"{API}/tasks/{task['id']}/reports", json={"summary": "done"}, headers=as_session)
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["outcome"] == "accepted" and out["task"]["status"] == "done" and out["seal"] == "tests ✓ (observed)"

            r = await h.client.get(f"{API}/tasks/{task['id']}/reports", headers=key_only)
            assert r.json()["count"] == 1 and r.json()["reports"][0]["is_current"] is True
            r = await h.client.get(f"{API}/crews/{CREW}/tasks", params={"status": "done"}, headers=key_only)
            assert [t["id"] for t in r.json()["tasks"]] == [task["id"]]
            r = await h.client.get(f"{API}/crews/{CREW}/tasks", params={"status": "bogus"}, headers=key_only)
            assert r.status_code == 422
    finally:
        await db.close()


async def test_patch_if_match_and_done_refused(tmp_path):
    db = await open_db(tmp_path)
    try:
        async with secure_app(tmp_path, [auth.router, crew_tasks.router], state={"crew_db": db}) as h:
            owner, key_only, as_session, _ = await _setup(h, db)
            r = await h.client.post(
                f"{API}/crews/{CREW}/tasks",
                json={"title": "t", "zone_ids": [], "acceptance": [], "depends_on": []},
                headers=as_session,
            )
            task = r.json()["task"]
            url = f"{API}/tasks/{task['id']}"
            assert (await h.client.patch(url, json={"title": "u"}, headers=as_session)).status_code == 428
            r = await h.client.patch(url, json={"title": "u"}, headers={**as_session, "If-Match": str(task["version"] + 1)})
            assert r.status_code == 412 and r.json()["detail"]["error"] == "version_mismatch"
            r = await h.client.patch(url, json={"status": "done"}, headers={**as_session, "If-Match": str(task["version"])})
            assert r.status_code == 409 and r.json()["detail"]["error"] == "report_required"
            r = await h.client.patch(url, json={"title": "renamed"}, headers={**as_session, "If-Match": f'"{task["version"]}"'})
            assert r.status_code == 200 and r.json()["task"]["title"] == "renamed" and r.json()["changed"] == ["title"]
            r = await h.client.patch(url, json={"title": "by key"}, headers={**key_only, "If-Match": "2"})
            assert r.status_code == 403
    finally:
        await db.close()


async def test_human_only_routes_and_step_up_and_audit(tmp_path):
    db = await open_db(tmp_path)
    try:
        async with secure_app(tmp_path, [auth.router, crew_tasks.router], state={"crew_db": db}) as h:
            owner, key_only, as_session, session = await _setup(h, db)
            fresh = h.jwt(owner, "owner@example.com")
            create = {"title": "t", "zone_ids": [], "acceptance": [], "depends_on": []}
            task = (await h.client.post(f"{API}/crews/{CREW}/tasks", json=create, headers=as_session)).json()["task"]
            await h.client.post(f"{API}/tasks/{task['id']}/start", headers=as_session)
            for path, body in (
                ("assign", {"to": session["id"]}),
                ("review", {"decision": "approve"}),
                ("waive", {"criterion_id": "all", "reason": "r"}),
            ):
                r = await h.client.post(f"{API}/tasks/{task['id']}/{path}", json=body, headers=as_session)
                assert r.status_code == 403 and r.json()["detail"]["error"] == "human_only", (path, r.text)
            r = await h.client.post(
                f"{API}/tasks/{task['id']}/waive",
                json={"criterion_id": "all", "reason": "shipped by hand"},
                headers=stale_jwt(owner),
            )
            assert r.status_code == 401 and r.json()["detail"]["error"] == "step_up_required"
            r = await h.client.post(
                f"{API}/tasks/{task['id']}/waive", json={"criterion_id": "all", "reason": "shipped by hand"}, headers=fresh
            )
            assert r.status_code == 200, r.text
            assert r.json()["outcome"] == "waived" and r.json()["task"]["status"] == "done"
            audit = await h.db.conn.execute("SELECT action, resource_id, user_id FROM audit_log WHERE action LIKE 'crew_%'")
            rows = [tuple(x) for x in await audit.fetchall()]
            assert rows == [("crew_report_waive", task["id"], owner)]
            # human dashboard actions need no session token; assign a fresh task to the session
            t2 = (await h.client.post(f"{API}/crews/{CREW}/tasks", json=create, headers=fresh)).json()["task"]
            r = await h.client.post(f"{API}/tasks/{t2['id']}/assign", json={"to": session["id"]}, headers=fresh)
            assert r.status_code == 200 and r.json()["task"]["owner_session_id"] == session["id"]
    finally:
        await db.close()


async def test_other_tenants_get_404_and_foreign_tokens_401(tmp_path):
    db = await open_db(tmp_path)
    try:
        async with secure_app(tmp_path, [auth.router, crew_tasks.router], state={"crew_db": db}) as h:
            owner, key_only, as_session, _ = await _setup(h, db)
            task = (
                await h.client.post(
                    f"{API}/crews/{CREW}/tasks",
                    json={"title": "t", "zone_ids": [], "acceptance": [], "depends_on": []},
                    headers=as_session,
                )
            ).json()["task"]
            intruder = await h.create_user("intruder@example.com")
            ikey, _ = await h.api_key(intruder, "admin")
            await seed_crew(db, OTHER_CREW, owner=intruder, project="theirs")
            their_session = await seed_session(db, OTHER_CREW, user_id=intruder)
            for method, url in (
                ("GET", f"/tasks/{task['id']}"),
                ("POST", f"/tasks/{task['id']}/start"),
                ("GET", f"/crews/{CREW}/tasks"),
            ):
                r = await h.client.request(
                    method, API + url, headers={"X-API-Key": ikey, SESSION_TOKEN_HEADER: their_session["_token"]}
                )
                assert r.status_code == 404 and r.json()["detail"] == {"error": "not_found", "message": "Not found."}
            # the owner cannot present another crew's session token
            r = await h.client.post(
                f"{API}/tasks/{task['id']}/start", headers={**key_only, SESSION_TOKEN_HEADER: their_session["_token"]}
            )
            assert r.status_code == 401
            restricted, _ = await h.api_key(owner, "editor", project_ids=["elsewhere"])
            r = await h.client.get(f"{API}/tasks/{task['id']}", headers={"X-API-Key": restricted})
            assert r.status_code == 404
    finally:
        await db.close()


async def test_adopt_is_rate_limited_per_session(tmp_path):
    db = await open_db(tmp_path)
    set_crew_rate_limiter(CrewRateLimiter("memory://"))
    try:
        async with secure_app(
            tmp_path, [auth.router, crew_tasks.router], state={"crew_db": db}, settings=make_settings(rate_limit_enabled=True)
        ) as h:
            owner, key_only, as_session, _ = await _setup(h, db)
            task = (
                await h.client.post(
                    f"{API}/crews/{CREW}/tasks",
                    json={"title": "t", "zone_ids": [], "acceptance": [], "depends_on": []},
                    headers=as_session,
                )
            ).json()["task"]
            codes = [(await h.client.post(f"{API}/tasks/{task['id']}/adopt", headers=as_session)).status_code for _ in range(7)]
            assert codes[:6] == [409] * 6 and codes[6] == 429
    finally:
        set_crew_rate_limiter(None)
        await db.close()
