"""WP-4 REST routes over real HTTP (ASGI): auth enabled, real API keys and dashboard JWTs,
real crew.db, the production router. Covers host tokens, join token rules, the batched
heartbeat, leave/stall session tokens, the human-only session actions (D27) with their
audit rows, the session list, idempotency and the §6 access contract.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from remembra.api.v1 import auth, crew_sessions
from remembra.auth.rbac import Role
from remembra.crew.access import audit_crew_routes
from remembra.crew.db import CrewDatabase
from remembra.crew.hosts import HOST_TOKEN_HEADER
from remembra.crew.schemas import ROUTES
from remembra.crew.sessions import SESSION_TOKEN_HEADER
from remembra.crew.store import crew_id_for
from tests.security_harness import secure_app

PASSWORD = "Str0ng!Passw0rd"


@asynccontextmanager
async def crew_http(tmp_path):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    try:
        async with secure_app(tmp_path, [auth.router, crew_sessions.router], state={"crew_db": db}) as h:
            yield h, db
    finally:
        await db.close()


async def _login(h, email):
    res = await h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _join_body(session_id, **kw):
    body = {
        "project_id": "yaadbooks",
        "agent_id": "claude-code",
        "session_id": session_id,
        "adapter": "claude-code",
        "client_kind": "hook",
        "checkout_fp": "fp-a",
        "worktree_id": "wt-a",
        "branch": "main",
        "head": "abc1234",
        "source": "startup",
    }
    body.update(kw)
    return body


async def _owner(h, email="owner@example.com"):
    uid = await h.create_user(email, password=PASSWORD)
    key, _ = await h.api_key(uid, "admin")
    return uid, {"X-API-Key": key}


async def test_host_register_and_rotate(tmp_path):
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        res = await h.client.post(
            "/api/v1/crew/hosts/register", json={"host_label": "mbp1", "platform": "darwin", "crewd_version": "1"}, headers=key
        )
        assert res.status_code == 201, res.text
        host_id, token = res.json()["host_id"], res.json()["host_token"]
        stored = await db.fetchone("SELECT token_hash, user_id FROM crew_hosts WHERE id = ?", (host_id,))
        assert stored["user_id"] == uid and token not in json.dumps(stored)
        bad = await h.client.post(
            "/api/v1/crew/hosts/register",
            json={"host_label": "My-MacBook.local", "platform": "d", "crewd_version": "1"},
            headers=key,
        )
        assert bad.status_code == 422 and bad.json()["detail"]["error"] == "validation"
        res = await h.client.post(f"/api/v1/crew/hosts/{host_id}/rotate", headers={**key, HOST_TOKEN_HEADER: "rch_nope"})
        assert res.status_code == 401 and res.json()["detail"]["error"] == "host_token_invalid"
        res = await h.client.post(f"/api/v1/crew/hosts/{host_id}/rotate", headers={**key, HOST_TOKEN_HEADER: token})
        assert res.status_code == 200 and res.json()["host_token"] != token
        # another user cannot rotate it even with the right token
        _, other = await _owner(h, "other@example.com")
        res = await h.client.post(
            f"/api/v1/crew/hosts/{host_id}/rotate", headers={**other, HOST_TOKEN_HEADER: res.json()["host_token"]}
        )
        assert res.status_code == 401
        viewer_uid = await h.create_user("viewer@example.com", password=PASSWORD)
        vkey, _ = await h.api_key(viewer_uid, "viewer")
        res = await h.client.post(
            "/api/v1/crew/hosts/register",
            json={"host_label": "mbp9", "platform": "d", "crewd_version": "1"},
            headers={"X-API-Key": vkey},
        )
        assert res.status_code == 403


async def test_join_token_rules_and_identity_over_http(tmp_path):
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        reg = (
            await h.client.post(
                "/api/v1/crew/hosts/register",
                json={"host_label": "mbp1", "platform": "darwin", "crewd_version": "1"},
                headers=key,
            )
        ).json()
        hdr = {**key, HOST_TOKEN_HEADER: reg["host_token"]}
        res = await h.client.post("/api/v1/crews/join", json=_join_body("s-1", host_id=reg["host_id"]), headers=hdr)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["crew_id"] == crew_id_for(uid, "yaadbooks") and body["crew_created"] and body["callsign"] == "cc-1"
        assert body["session"]["agent_verified"] is False  # unscoped key: self-declared
        token = body["session_token"]
        again = await h.client.post("/api/v1/crews/join", json=_join_body("s-1"), headers=key)
        assert again.status_code == 409 and again.json()["detail"]["error"] == "session_exists"
        again = await h.client.post(
            "/api/v1/crews/join", json=_join_body("s-1", source="compact"), headers={**key, SESSION_TOKEN_HEADER: token}
        )
        assert again.status_code == 200 and again.json()["session_token"] is None
        rot = await h.client.post(
            "/api/v1/crews/join", json=_join_body("s-1", host_id=reg["host_id"], source="resume"), headers=hdr
        )
        assert rot.status_code == 200 and rot.json()["token_rotated"] and rot.json()["session_token"] not in (None, token)
        rows = await db.fetchall("SELECT response_json FROM crew_idempotency")
        assert all("rcs_" not in r["response_json"] for r in rows)
        # a key scoped to agent codex is key-verified; it cannot claim another agent name
        scoped = await h.keys.create_key(user_id=uid, name="codex", agent_id="codex")
        await h.roles.assign_role(scoped.id, Role("editor"))
        res = await h.client.post(
            "/api/v1/crews/join", json=_join_body("s-2", agent_id="codex", adapter="codex"), headers={"X-API-Key": scoped.key}
        )
        assert res.status_code == 201 and res.json()["session"]["agent_verified"] is True and res.json()["callsign"] == "codex-1"
        res = await h.client.post("/api/v1/crews/join", json=_join_body("s-3"), headers={"X-API-Key": scoped.key})
        assert res.status_code == 403
        # a project-restricted key sees no crew outside its projects, and cannot create one
        restricted, _ = await h.api_key(uid, "editor", project_ids=["elsewhere"])
        res = await h.client.post("/api/v1/crews/join", json=_join_body("s-4"), headers={"X-API-Key": restricted})
        assert res.status_code == 404 and res.json()["detail"] == {"error": "not_found", "message": "Not found."}
        # binding a host needs that host's token
        res = await h.client.post("/api/v1/crews/join", json=_join_body("s-5", host_id=reg["host_id"]), headers=key)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "host_token_invalid"
        res = await h.client.post("/api/v1/crews/join", json={"agent_id": "x", "session_id": "q"}, headers=key)
        assert res.status_code == 422


async def test_join_by_locator_resolves_read_only(tmp_path):
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        body = _join_body("s-1")
        body.pop("project_id")
        body["locator"] = "git@github.com:mani/yaadbooks.git"
        res = await h.client.post("/api/v1/crews/join", json=body, headers=key)
        assert res.status_code == 201, res.text
        crew = await db.fetchone("SELECT project_id FROM crews WHERE id = ?", (res.json()["crew_id"],))
        assert crew["project_id"] == "yaadbooks"
        bound = await h.db.conn.execute("SELECT COUNT(*) FROM project_fingerprints WHERE user_id = ?", (uid,))
        assert (await bound.fetchone())[0] == 0  # resolution never binds a location
        body.pop("locator")
        res = await h.client.post("/api/v1/crews/join", json=body, headers=key)
        assert res.status_code == 422


async def test_heartbeat_leave_and_stall_over_http(tmp_path):
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        reg = (
            await h.client.post(
                "/api/v1/crew/hosts/register",
                json={"host_label": "mbp1", "platform": "darwin", "crewd_version": "1"},
                headers=key,
            )
        ).json()
        hdr = {**key, HOST_TOKEN_HEADER: reg["host_token"]}
        joined = (await h.client.post("/api/v1/crews/join", json=_join_body("s-1", host_id=reg["host_id"]), headers=hdr)).json()
        sid, token = joined["session_id"], joined["session_token"]
        hb = {
            "batch_id": "batch-1",
            "sessions": [
                {
                    "session_id": sid,
                    "token": token,
                    "alive": True,
                    "activity_age_s": 3,
                    "last_action": {"tool": "Edit", "path_rel": "src/a.ts", "verb": None, "age_s": 3},
                    "calls_since_checkpoint": 1,
                    "limit": None,
                    "footprints": [],
                    "cursor": 0,
                    "githook_state": "ok",
                }
            ],
        }
        assert (await h.client.post("/api/v1/crew/heartbeat", json=hb, headers=key)).status_code == 401
        res = await h.client.post("/api/v1/crew/heartbeat", json=hb, headers=hdr)
        assert res.status_code == 200, res.text
        first = res.json()
        assert first["per_session"][sid]["state"] == "active" and "replayed" not in first
        res = await h.client.post("/api/v1/crew/heartbeat", json=hb, headers=hdr)
        assert res.json()["replayed"] is True and res.json()["per_session"] == first["per_session"]
        stored = await db.fetchall("SELECT response_json FROM crew_idempotency")
        assert stored and all(token not in r["response_json"] for r in stored)
        bad = {
            **hb,
            "batch_id": "batch-2",
            "sessions": [{**hb["sessions"][0], "last_action": {"tool": "Edit", "path_rel": "/etc/x", "age_s": 1}}],
        }
        assert (await h.client.post("/api/v1/crew/heartbeat", json=bad, headers=hdr)).status_code == 422

        stall = {
            "error": "billing_error",
            "error_details_class": "credits",
            "facts": {"uncommitted_files": ["a"]},
            "baton_ref": "refs/remembra/baton/cs_x/1",
        }
        res = await h.client.post(f"/api/v1/sessions/{sid}/stall", json=stall, headers={**key, SESSION_TOKEN_HEADER: "rcs_wrong"})
        assert res.status_code == 401 and res.json()["detail"]["error"] == "session_token_invalid"
        _, other = await _owner(h, "other@example.com")
        res = await h.client.post(f"/api/v1/sessions/{sid}/stall", json=stall, headers={**other, SESSION_TOKEN_HEADER: token})
        assert res.status_code == 404
        res = await h.client.post(f"/api/v1/sessions/{sid}/stall", json=stall, headers={**key, SESSION_TOKEN_HEADER: token})
        assert res.status_code == 200 and res.json()["state"] == "quota_blocked"

        leave = {"reason": "logout", "facts": {}, "summary": None, "baton": False}
        idem = {**key, SESSION_TOKEN_HEADER: token, "Idempotency-Key": "leave-1"}
        res = await h.client.post(f"/api/v1/sessions/{sid}/leave", json=leave, headers=idem)
        assert res.status_code == 200 and res.json()["state"] == "ended" and res.json()["already"] is False
        replay = await h.client.post(f"/api/v1/sessions/{sid}/leave", json=leave, headers=idem)
        assert replay.json() == res.json()
        conflict = await h.client.post(f"/api/v1/sessions/{sid}/leave", json={**leave, "reason": "other"}, headers=idem)
        assert conflict.status_code == 422 and conflict.json()["detail"]["error"] == "idempotency_conflict"


async def test_human_only_session_actions_and_audit(tmp_path):
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        jwt = await _login(h, "owner@example.com")
        sid = (await h.client.post("/api/v1/crews/join", json=_join_body("s-1"), headers=key)).json()["session_id"]
        for action in ("pause", "resume", "request-checkpoint", "release-all"):
            res = await h.client.post(f"/api/v1/sessions/{sid}/{action}", json={"reason": "r"}, headers=key)
            assert res.status_code == 403 and res.json()["detail"]["error"] == "human_only", action
            res = await h.client.post(f"/api/v1/sessions/{sid}/{action}", json={"reason": f"by hand {action}"}, headers=jwt)
            assert res.status_code == 200, (action, res.text)
        cur = await h.db.conn.execute(
            "SELECT action, user_id, resource_id FROM audit_log WHERE action LIKE 'crew.%' ORDER BY timestamp"
        )
        rows = [tuple(r) for r in await cur.fetchall()]
        assert [r[0] for r in rows] == ["crew.pause", "crew.resume", "crew.request_checkpoint", "crew.release_all"]
        assert {r[1] for r in rows} == {uid} and {r[2] for r in rows} == {sid}
        # a member who is not owner/admin cannot, and another tenant sees nothing
        mate = await h.create_user("mate@example.com", password=PASSWORD)
        await db.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, 'member', '2026-09-25T00:00:00.000Z')",
            (crew_id_for(uid, "yaadbooks"), mate),
        )
        await db.conn.commit()
        res = await h.client.post(
            f"/api/v1/sessions/{sid}/pause", json={"reason": "x"}, headers=await _login(h, "mate@example.com")
        )
        assert res.status_code == 403 and res.json()["detail"]["error"] == "crew_role_required"
        await h.create_user("stranger@example.com", password=PASSWORD)
        res = await h.client.post(
            f"/api/v1/sessions/{sid}/pause", json={"reason": "x"}, headers=await _login(h, "stranger@example.com")
        )
        assert res.status_code == 404


async def test_list_sessions_route(tmp_path):
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        crew = crew_id_for(uid, "yaadbooks")
        await h.client.post("/api/v1/crews/join", json=_join_body("s-1"), headers=key)
        await h.client.post("/api/v1/crews/join", json=_join_body("s-2", agent_id="codex", adapter="codex"), headers=key)
        res = await h.client.get(f"/api/v1/crews/{crew}/sessions", params={"state": "live"}, headers=key)
        assert res.status_code == 200 and sorted(s["callsign"] for s in res.json()["sessions"]) == ["cc-1", "codex-1"]
        assert (await h.client.get(f"/api/v1/crews/{crew}/sessions", params={"state": "nope"}, headers=key)).status_code == 422
        _, other = await _owner(h, "other@example.com")
        res = await h.client.get(f"/api/v1/crews/{crew}/sessions", headers=other)
        assert res.status_code == 404


async def test_routes_follow_the_access_contract(tmp_path):
    async with crew_http(tmp_path) as (h, _db):
        problems = audit_crew_routes(h.app.routes)
        assert problems == []
        registered = {(m, r.path) for r in crew_sessions.router.routes for m in r.methods}
        wp4 = {(r.method, r.path) for r in ROUTES if r.owner == "WP-4"}
        assert wp4 == registered


async def test_heartbeat_and_join_rate_limits(tmp_path):
    from remembra.crew.limits import CrewRateLimiter, set_crew_rate_limiter
    from tests.security_harness import make_settings

    set_crew_rate_limiter(CrewRateLimiter("memory://"))
    try:
        db = CrewDatabase(str(tmp_path / "crew.db"))
        await db.init_schema()
        try:
            async with secure_app(
                tmp_path,
                [auth.router, crew_sessions.router],
                state={"crew_db": db},
                settings=make_settings(rate_limit_enabled=True),
            ) as h:
                uid, key = await _owner(h)
                reg = (
                    await h.client.post(
                        "/api/v1/crew/hosts/register",
                        json={"host_label": "mbp1", "platform": "darwin", "crewd_version": "1"},
                        headers=key,
                    )
                ).json()
                hdr = {**key, HOST_TOKEN_HEADER: reg["host_token"]}
                codes = [
                    (
                        await h.client.post("/api/v1/crew/heartbeat", json={"batch_id": f"b{i}", "sessions": []}, headers=hdr)
                    ).status_code
                    for i in range(3)
                ]
                assert codes == [200, 200, 429]  # 2/min per host token (§11.2)
                res = await h.client.post("/api/v1/crew/heartbeat", json={"batch_id": "b9", "sessions": []}, headers=hdr)
                assert res.json()["detail"]["error"] == "rate_limited" and int(res.headers["Retry-After"]) >= 1
                statuses = [
                    (await h.client.post("/api/v1/crews/join", json=_join_body(f"s-{i}"), headers=key)).status_code
                    for i in range(30)
                ]
                assert statuses.count(429) == 1 and statuses[-1] == 429  # 30/min per user shared with host registration
        finally:
            await db.close()
    finally:
        set_crew_rate_limiter(None)
