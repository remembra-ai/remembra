"""WP-8 crew core API over real HTTP: resolve, list, get, settings patch, snapshot, events, members,
per-agent page, timeline and batons.

Real FastAPI app (security harness, authentication ON), real API keys and JWTs, a real
``crew.db`` migrated by ``CREW_MIGRATIONS`` and the real WP-2 event log and bus.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from remembra.api.v1 import auth, crews
from remembra.crew import schemas
from remembra.crew.access import _walk_routes, audit_crew_routes
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.db import CrewDatabase
from remembra.crew.events import Actor, CrewEventLog
from remembra.crew.limits import CrewRateLimiter, set_crew_rate_limiter
from remembra.crew.store import crew_id_for
from tests.crew import wp8_seed as seed
from tests.security_harness import make_settings, secure_app

PROJECT = "yaadbooks"


@asynccontextmanager
async def crew_api(tmp_path: Any, **settings: Any):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    bus = CrewBus(loader=db_loader(db))
    published: list[dict[str, Any]] = []
    bus.subscribe(published.append)
    events = CrewEventLog(db, bus)
    try:
        async with secure_app(
            tmp_path,
            [auth.router, crews.router],
            settings=make_settings(**settings),
            state={"crew_db": db, "crew_events": events},
        ) as h:
            yield h, db, events, published
    finally:
        await db.close()


async def _owner(h: Any, email: str = "owner@example.com") -> tuple[str, dict[str, str], dict[str, str]]:
    uid = await h.create_user(email)
    key, _ = await h.api_key(uid, "editor")
    return uid, {"X-API-Key": key}, h.jwt(uid, email)


async def _world(db: CrewDatabase, events: CrewEventLog, owner: str) -> dict[str, str]:
    """A crew with three sessions, two zones, a reserved baton offered to codex-1 and history."""
    crew_id = await seed.crew(db, owner, PROJECT)
    await seed.zone(db, crew_id, "zn_pos", "pos", globs=["src/app/pos/**"], title="POS section")
    await seed.zone(db, crew_id, "zn_reports", "reports", globs=["src/app/reports/**"])
    await seed.zone(db, crew_id, "zn_billing", "billing", globs=["src/app/billing/**"], frozen_by=owner, frozen_note="Mani edits")
    await seed.session(db, crew_id, "cs_a", user_id=owner, callsign="cc-1", current_task_id="tsk_14")
    await seed.session(
        db,
        crew_id,
        "cs_b",
        user_id=owner,
        callsign="cc-2",
        state="quota_blocked",
        state_reason="billing_error",
        limit_level="exhausted",
        limit_source="reported",
        active_minutes_ago=12,
    )
    await seed.session(db, crew_id, "cs_c", user_id=owner, callsign="codex-1", agent_id="codex")
    await seed.session(
        db, crew_id, "cs_old", user_id=owner, callsign="cc-3", state="ended", joined_minutes_ago=3000, ended_minutes_ago=2900
    )
    await seed.task(
        db, crew_id, "tsk_14", 14, "Split tender payments", status="in_progress", owner_session_id="cs_a", phase="POS"
    )
    await seed.task(db, crew_id, "tsk_12", 12, "Invoice PDF", status="stalled", owner_session_id="cs_b", phase="Reports")
    await seed.task(db, crew_id, "tsk_9", 9, "Receipts", status="done", phase="POS")
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_task_deps (crew_id, task_id, depends_on_id) VALUES (?, 'tsk_14', 'tsk_9')", (crew_id,)
        )
    await seed.claim(db, crew_id, "clm_pos", holder="cs_a", zone_id="zn_pos", task_id="tsk_14")
    await seed.claim(
        db,
        crew_id,
        "clm_rep",
        holder="cs_b",
        zone_id="zn_reports",
        task_id="tsk_12",
        state="reserved",
        reserve_reason="quota",
        baton_ref="refs/remembra/baton/T-12/7",
    )
    await seed.claim(db, crew_id, "clm_old", holder="cs_old", zone_id=None, resource="deploy:vercel", state="released")
    await seed.offer(db, crew_id, "off_1", "clm_rep", "cs_c", "tsk_12")
    await seed.checkpoint(
        db,
        crew_id,
        "ckp_b1",
        "cs_b",
        facts={"dirty_files": ["a.ts", "b.ts", "c.ts"], "unpushed_count": 2, "tests": [{"command": "pdf.spec", "passed": False}]},
        task_id="tsk_12",
    )
    await seed.checkpoint(db, crew_id, "ckp_c1", "cs_c", facts={}, trigger="turn", minutes_ago=1)
    await seed.report(
        db,
        crew_id,
        "rpt_12",
        "tsk_12",
        "cs_b",
        sections={"next": ["fix margin calc, push"]},
        baton_ref="refs/remembra/baton/T-12/7",
    )
    await seed.baton(
        db, crew_id, "bat_1", to_session="cs_c", from_session="cs_old", task_id="tsk_9", brief_text="CREW yaadbooks brief"
    )
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_collisions
                 (id, crew_id, kind, severity, subject, zone_id, session_a, session_b, state, created_at)
               VALUES ('col_1', ?, 'same_file', 'medium', 'src/app/pos/cart.ts', 'zn_pos', 'cs_a', 'cs_c', 'open', ?)""",
            (crew_id, seed.ts(3)),
        )
        await db.conn.execute(
            """INSERT INTO crew_decisions
                 (id, crew_id, number, title, decision, state, source, decided_by_kind, decided_by, created_at)
               VALUES ('dec_7', ?, 7, 'GCT rounding', 'half-up per line', 'in_force', 'direct', 'human', ?, ?),
                      ('dec_8', ?, 8, 'Agent idea', 'use floats', 'proposed', 'direct', 'agent', 'cc-1', ?),
                      ('dec_1', ?, 1, 'Old', 'x', 'superseded', 'direct', 'human', ?, ?)""",
            (crew_id, owner, seed.ts(9), crew_id, seed.ts(8), crew_id, owner, seed.ts(99)),
        )
        for iid, audience, recipient, kind, state in (
            ("inb_1", "project", None, "baton_available", "open"),
            ("inb_2", "crew", None, "baton_reserved", "open"),
            ("inb_3", "project", None, "review_report", "resolved"),
            ("inb_4", "session", "cs_c", "mention", "open"),
            ("inb_5", "session", "cs_c", "mention", "seen"),
        ):
            await db.conn.execute(
                """INSERT INTO crew_inbox_items
                     (id, crew_id, audience, recipient, kind, title, state, dedupe_key, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'x', ?, ?, ?, ?)""",
                (iid, crew_id, audience, recipient, kind, state, iid, seed.ts(2), seed.ts(2)),
            )
        await db.conn.execute(
            """INSERT INTO crew_footprints (crew_id, session_id, path, state, attribution, first_at, last_at)
               VALUES (?, 'cs_a', 'src/app/pos/split.ts', 'dirty', 'certain', ?, ?),
                      (?, 'cs_a', 'src/app/pos/old.ts', 'landed', 'certain', ?, ?)""",
            (crew_id, seed.ts(3), seed.ts(3), crew_id, seed.ts(3), seed.ts(3)),
        )
        await db.conn.execute(
            """INSERT INTO crew_zone_files (crew_id, yaml_sha, compiled, commons, ignore)
               VALUES (?, 'abc', '{}', ?, ?)""",
            (crew_id, json.dumps([{"glob": "package.json", "kind": "serialize"}, "README.md"]), json.dumps(["docs/**"])),
        )
        await db.conn.execute(
            "INSERT INTO crew_zone_changes (id, crew_id, yaml_sha, diff, loosening, state, created_at)"
            " VALUES ('zch_1', ?, 'def', '{}', 1, 'pending', ?)",
            (crew_id, seed.ts(1)),
        )
    await events.emit(
        crew_id=crew_id,
        type="session.state_changed",
        actor=Actor.system(),
        payload={"from": "active", "to": "quota_blocked", "reason": "billing_error", "quiet_reason": None},
        summary="cc-2 quota_blocked",
        refs={"session_id": "cs_b"},
    )
    return {"crew_id": crew_id}


# ---------------------------------------------------------------------------
# Route contract
# ---------------------------------------------------------------------------


def test_router_honours_the_access_contract_for_every_wp8_route():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(crews.router, prefix="/api/v1")
    assert audit_crew_routes(app.routes) == []
    registered = {(m, path) for path, r, _ in _walk_routes(app.routes) for m in r.methods}
    wanted = {
        (r.method, "/api/v1" + r.path)
        for r in schemas.ROUTES
        if r.owner == "WP-8" and r.release == "L0" and r.access != "existing"
    }
    assert wanted <= registered, wanted - registered


# ---------------------------------------------------------------------------
# Resolve and list
# ---------------------------------------------------------------------------


async def test_resolve_is_read_only_and_404s_without_a_crew(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _jwt = await _owner(h)
        res = await h.client.post("/api/v1/crews/resolve", json={"project_id": PROJECT}, headers=key)
        assert res.status_code == 404 and res.json()["detail"] == {"error": "not_found", "message": "Not found."}
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM crews"))["n"] == 0  # never creates

        ids = await _world(db, events, owner)
        res = await h.client.post("/api/v1/crews/resolve", json={"project_id": PROJECT}, headers=key)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["crew"]["id"] == ids["crew_id"] == crew_id_for(owner, PROJECT)
        assert body["crew"]["mode"] == "multi"  # cs_a, cs_b (quota_blocked), cs_c are live
        assert body["role"] == "owner" and "crew:read" in body["permissions"]
        assert "crew:admin" not in body["permissions"]  # an API key is never human (D27)
        assert schemas.validate(body["crew"], schemas.CREW_VIEW) == []


async def test_resolve_by_location_goes_through_the_project_registry_without_binding(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        await _world(db, events, owner)
        remote = "https://github.com/mani/yaadbooks.git"
        res = await h.client.post("/api/v1/crews/resolve", json={"git_remote": remote}, headers=key)
        assert res.status_code == 200, res.text
        assert res.json()["project_id"] == PROJECT and res.json()["resolution"]["project_id"] == PROJECT
        # read-only: the location was not recorded
        cursor = await h.db.conn.execute("SELECT COUNT(*) FROM project_fingerprints")
        assert (await cursor.fetchone())[0] == 0
        res = await h.client.post("/api/v1/crews/resolve", json={}, headers=key)
        assert res.status_code == 422


async def test_resolve_and_list_hide_other_tenants_and_restricted_projects(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        await _world(db, events, owner)
        intruder = await h.create_user("intruder@example.com")
        intruder_key, _ = await h.api_key(intruder, "admin")
        restricted, _ = await h.api_key(owner, "editor", project_ids=["elsewhere"])
        for headers in ({"X-API-Key": intruder_key}, {"X-API-Key": restricted}):
            res = await h.client.post("/api/v1/crews/resolve", json={"project_id": PROJECT}, headers=headers)
            assert res.status_code == 404, res.text
            listing = await h.client.get("/api/v1/crews", headers=headers)
            assert listing.status_code == 200 and listing.json()["crews"] == []
        viewer_key, _ = await h.api_key(owner, "viewer", scopes=["memory:recall"])
        res = await h.client.get("/api/v1/crews", headers={"X-API-Key": viewer_key})
        assert res.status_code == 403  # no crew:read on the credential


async def test_list_crews_carries_counts_lanes_and_phase_progress(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        await seed.crew(db, owner, "quiet-project")
        res = await h.client.get("/api/v1/crews", headers=key)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 2
        first, second = body["crews"]
        assert first["crew"]["id"] == ids["crew_id"]  # needs-you first
        assert first["needs_you"] == 1 and first["crew_inbox"] == 1 and first["live"] == 3
        assert {s["callsign"] for s in first["live_sessions"]} == {"cc-1", "cc-2", "codex-1"}
        assert first["tasks_by_status"] == {"in_progress": 1, "stalled": 1, "done": 1}
        assert {p["phase"]: (p["done"], p["total"]) for p in first["phases"]} == {"POS": (1, 2), "Reports": (0, 1)}
        assert first["last_event_at"] is not None and first["moments_24h"] == 0
        assert second["crew"]["project_id"] == "quiet-project" and second["live"] == 0
        for item in body["crews"]:
            assert schemas.validate(item["crew"], schemas.CREW_VIEW) == []
            for s in item["live_sessions"]:
                assert schemas.validate(s, schemas.SESSION_VIEW) == []


# ---------------------------------------------------------------------------
# Get and settings patch (H, step-up, If-Match)
# ---------------------------------------------------------------------------


async def test_get_crew_returns_settings_and_version_etag(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        res = await h.client.get(f"/api/v1/crews/{ids['crew_id']}", headers=key)
        assert res.status_code == 200, res.text
        body = res.json()
        assert res.headers["etag"] == '"1"' and body["settings_version"] == 1
        assert body["settings"]["enforcement"] == "enforce" and body["settings"]["require_report_for_done"] is True
        assert body["role"] == "owner" and body["human"] is False and body["members"] == 1


async def test_settings_patch_is_human_only_step_up_and_versioned(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, published):
        owner, key, jwt = await _owner(h)
        ids = await _world(db, events, owner)
        url = f"/api/v1/crews/{ids['crew_id']}"
        patch = {"settings": {"enforcement": "observe", "checkpoint": {"interval_s": 900}}}

        res = await h.client.patch(url, json=patch, headers={**key, "If-Match": '"1"'})
        assert res.status_code == 403 and res.json()["detail"]["error"] == "human_only"

        res = await h.client.patch(url, json=patch, headers=jwt)
        assert res.status_code == 428 and res.json()["detail"]["error"] == "if_match_required"

        res = await h.client.patch(url, json=patch, headers={**jwt, "If-Match": '"7"'})
        assert res.status_code == 412 and res.json()["detail"]["current_version"] == 1

        res = await h.client.patch(url, json={"settings": {"require_report_for_done": False}}, headers={**jwt, "If-Match": '"1"'})
        assert res.status_code == 422 and "require_report_for_done" in res.json()["detail"]["errors"]

        seq_before = (await db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (ids["crew_id"],)))["last_seq"]
        res = await h.client.patch(url, json=patch, headers={**jwt, "If-Match": '"1"'})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["settings_version"] == 2 and res.headers["etag"] == '"2"'
        assert body["settings"]["enforcement"] == "observe" and body["settings"]["checkpoint"]["interval_s"] == 900
        assert body["changed_keys"] == ["checkpoint.interval_s", "enforcement"]
        assert body["seq"] == seq_before + 1 and body["crew"]["enforcement"] == "observe"
        event = await db.fetchone("SELECT * FROM crew_events WHERE crew_id = ? AND seq = ?", (ids["crew_id"], body["seq"]))
        assert event["type"] == "crew.settings_changed" and event["actor_kind"] == "human" and event["moment"] == 1
        assert json.loads(event["payload"]) == {
            "settings_version": 2,
            "changed_keys": ["checkpoint.interval_s", "enforcement"],
            "enforcement": "observe",
        }
        assert published and published[-1]["type"] == "crew.settings_changed"  # published after COMMIT
        audit = await h.db.get_audit_logs(user_id=owner)
        assert any(a["action"] == "crew.settings_changed" and a["resource_id"] == ids["crew_id"] for a in audit)

        # an identical patch changes nothing: no version bump, no event
        res = await h.client.patch(url, json=patch, headers={**jwt, "If-Match": '"2"'})
        assert res.status_code == 200 and res.json()["settings_version"] == 2 and res.json()["seq"] is None


async def test_settings_patch_needs_a_fresh_login(tmp_path):
    import time

    import jwt as pyjwt

    from tests.security_harness import JWT_SECRET

    async with crew_api(tmp_path) as (h, db, events, _):
        owner, _, _ = await _owner(h)
        ids = await _world(db, events, owner)
        issued = int(time.time() * 1000) - 20 * 60_000
        stale = pyjwt.encode(
            {
                "sub": owner,
                "email": "owner@example.com",
                "iat": issued // 1000,
                "iat_ms": issued,
                "exp": int(time.time()) + 3600,
                "type": "access",
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        res = await h.client.patch(
            f"/api/v1/crews/{ids['crew_id']}",
            json={"settings": {"enforcement": "observe"}},
            headers={"Authorization": f"Bearer {stale}", "If-Match": '"1"'},
        )
        assert res.status_code == 401 and res.json()["detail"]["error"] == "step_up_required"
        assert (await db.fetchone("SELECT settings_version FROM crews"))["settings_version"] == 1


# ---------------------------------------------------------------------------
# Snapshot and events polling
# ---------------------------------------------------------------------------


async def test_snapshot_matches_the_contract_and_holds_exactly_the_live_state(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        res = await h.client.get(f"/api/v1/crews/{ids['crew_id']}/snapshot", headers=key)
        assert res.status_code == 200, res.text
        snap = res.json()
        assert schemas.validate(snap, schemas.SNAPSHOT) == []
        assert snap["as_of_seq"] == snap["crew"]["last_seq"] == 1 and res.headers["etag"] == snap["etag"]
        # live sessions plus the recently relevant ones; cs_old ended 48 h ago and holds nothing live
        assert [s["id"] for s in snap["sessions"]] == ["cs_a", "cs_b", "cs_c"]
        assert {c["id"] for c in snap["claims"]} == {"clm_pos", "clm_rep"}  # released claims are not live
        assert {z["slug"] for z in snap["zones"]} == {"pos", "reports", "billing"}
        assert snap["commons"] == [{"glob": "package.json", "kind": "serialize"}, {"glob": "README.md", "kind": "plain"}]
        assert snap["ignore"] == ["docs/**"]
        assert [t["id"] for t in snap["tasks"]] == ["tsk_12", "tsk_14"]  # done task left out
        assert next(t for t in snap["tasks"] if t["id"] == "tsk_14")["depends_on"] == ["tsk_9"]
        assert [c["id"] for c in snap["collisions"]] == ["col_1"]
        assert {d["id"]: d["state"] for d in snap["decisions"]} == {"dec_7": "in_force", "dec_8": "proposed"}
        assert snap["offers"] == [
            {"id": "off_1", "claim_id": "clm_rep", "task_id": "tsk_12", "to_session": "cs_c", "via": "brief"}
        ]
        assert snap["footprints"] == [
            {
                "session_id": "cs_a",
                "worktree_id": None,
                "path": "src/app/pos/split.ts",
                "state": "dirty",
                "attribution": "certain",
            }
        ]
        assert snap["inbox_counts"] == {"project": 1, "crew": 1}
        assert snap["pending_zone_changes"] == ["zch_1"]
        reserved = next(c for c in snap["claims"] if c["id"] == "clm_rep")
        assert reserved["reserve_reason"] == "quota" and reserved["baton_ref"] == "refs/remembra/baton/T-12/7"
        text = json.dumps(snap)
        assert "token_hash" not in text and "0" * 64 not in text  # never a token or token hash


async def test_snapshot_etag_304_and_changes_when_state_changes(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        url = f"/api/v1/crews/{ids['crew_id']}/snapshot"
        first = await h.client.get(url, headers=key)
        etag = first.headers["etag"]
        res = await h.client.get(url, headers={**key, "If-None-Match": etag})
        assert res.status_code == 304 and res.headers["etag"] == etag and res.content == b""
        async with db.transaction():  # a heartbeat-style change without an event still changes the ETag
            await db.conn.execute("UPDATE crew_sessions SET last_activity_at = ? WHERE id = 'cs_a'", (seed.ts(0),))
        res = await h.client.get(url, headers={**key, "If-None-Match": etag})
        assert res.status_code == 200 and res.headers["etag"] != etag


async def test_snapshot_rate_limit_is_per_agent_session_not_for_the_dashboard(tmp_path):
    set_crew_rate_limiter(CrewRateLimiter("memory://"))
    try:
        async with crew_api(tmp_path, rate_limit_enabled=True) as (h, db, events, _):
            owner, key, jwt = await _owner(h)
            ids = await _world(db, events, owner)
            url = f"/api/v1/crews/{ids['crew_id']}/snapshot"
            assert (await h.client.get(url, headers={**key, "X-Remembra-Agent-Id": "claude-code"})).status_code == 200
            res = await h.client.get(url, headers={**key, "X-Remembra-Agent-Id": "claude-code"})
            assert res.status_code == 429 and res.json()["detail"]["error"] == "rate_limited"
            assert int(res.headers["retry-after"]) >= 1
            # another agent on the same key has its own bucket; the dashboard login is not limited here
            assert (await h.client.get(url, headers={**key, "X-Remembra-Agent-Id": "codex"})).status_code == 200
            for _ in range(3):
                assert (await h.client.get(url, headers=jwt)).status_code == 200
    finally:
        set_crew_rate_limiter(None)


async def test_events_polling_pages_by_seq_with_etag(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        for i in range(4):
            await events.emit(
                crew_id=ids["crew_id"],
                type="session.stuck",
                actor=Actor.system(),
                payload={"signal": f"s{i}", "stuck": True},
                summary="cc-1 stuck",
                refs={"session_id": "cs_a"},
            )
        url = f"/api/v1/crews/{ids['crew_id']}/events"
        res = await h.client.get(url, params={"since_seq": 0, "limit": 2}, headers=key)
        assert res.status_code == 200, res.text
        body = res.json()
        assert [e["seq"] for e in body["events"]] == [1, 2] and body["has_more"] is True and body["last_seq"] == 5
        for e in body["events"]:
            assert schemas.validate_envelope(e) == []
        res = await h.client.get(url, params={"since_seq": 2}, headers=key)
        assert [e["seq"] for e in res.json()["events"]] == [3, 4, 5] and res.json()["has_more"] is False
        etag = res.headers["etag"]
        assert etag == '"5"'
        res = await h.client.get(url, params={"since_seq": 5}, headers={**key, "If-None-Match": etag})
        assert res.status_code == 304


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def _teammates(h, owner: str, *users: str) -> str:
    """A team the owner created and ``users`` joined (by accepting its invite)."""
    from remembra.teams.manager import TeamManager

    teams = TeamManager(h.db)
    team = await teams.create_team("Crew team", owner, max_seats=10)
    for user in users:
        await teams.add_member(team["id"], user, invited_by=owner)
    return str(team["id"])


async def test_members_add_change_remove_are_human_only_and_audited(tmp_path, monkeypatch):
    from remembra.api.v1 import websocket

    revoked: list[dict[str, Any]] = []

    async def spy_revoke(**kwargs: Any) -> int:
        revoked.append(kwargs)
        return 0

    monkeypatch.setattr(websocket.connection_manager, "revoke", spy_revoke)
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, jwt = await _owner(h)
        ids = await _world(db, events, owner)
        mate = await h.create_user("mate@example.com")
        await _teammates(h, owner, mate)
        url = f"/api/v1/crews/{ids['crew_id']}/members"

        res = await h.client.post(url, json={"user_id": mate, "role": "member"}, headers=key)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "human_only"
        res = await h.client.post(url, json={"user_id": "u_nobody", "role": "member"}, headers=jwt)
        assert res.status_code == 422 and res.json()["detail"]["error"] == "unknown_user"
        res = await h.client.post(url, json={"user_id": mate, "role": "boss"}, headers=jwt)
        assert res.status_code == 422

        res = await h.client.post(url, json={"user_id": mate, "role": "member"}, headers=jwt)
        assert res.status_code == 200, res.text
        assert res.json()["member"]["role"] == "member" and res.json()["previous_role"] is None
        listing = (await h.client.get(url, headers=key)).json()
        assert {m["user_id"]: m["role"] for m in listing["members"]} == {owner: "owner", mate: "member"}

        # the member now sees the crew (read), but cannot manage members
        mate_jwt = h.jwt(mate, "mate@example.com")
        assert (await h.client.get(f"/api/v1/crews/{ids['crew_id']}", headers=mate_jwt)).json()["role"] == "member"
        res = await h.client.post(url, json={"user_id": mate, "role": "admin"}, headers=mate_jwt)
        assert res.status_code == 403

        res = await h.client.post(url, json={"user_id": mate, "role": "viewer"}, headers=jwt)
        assert res.status_code == 200 and res.json()["previous_role"] == "member"
        assert revoked[-1] == {"user_id": mate, "crew_id": ids["crew_id"], "reason": "crew role changed"}

        res = await h.client.request("DELETE", f"{url}/{owner}", headers=jwt)
        assert res.status_code == 409 and res.json()["detail"]["error"] == "crew_owner"
        res = await h.client.post(url, json={"user_id": owner, "role": "member"}, headers=jwt)
        assert res.status_code == 409

        res = await h.client.request("DELETE", f"{url}/{mate}", headers=jwt)
        assert res.status_code == 200 and res.json()["removed"] == mate
        assert revoked[-1]["reason"] == "removed from crew"
        assert (await h.client.get(f"/api/v1/crews/{ids['crew_id']}", headers=mate_jwt)).status_code == 404
        assert (await h.client.request("DELETE", f"{url}/{mate}", headers=jwt)).status_code == 404

        actions = [a["action"] for a in await h.db.get_audit_logs(user_id=owner)]
        assert {"crew.member_added", "crew.member_role_changed", "crew.member_removed"} <= set(actions)


async def test_crew_admin_cannot_grant_owner_or_admin(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, _, _ = await _owner(h)
        ids = await _world(db, events, owner)
        admin_user = await h.create_user("admin@example.com")
        other = await h.create_user("other@example.com")
        await _teammates(h, owner, admin_user, other)
        async with db.transaction():
            await db.conn.execute(
                "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, 'admin', ?)",
                (ids["crew_id"], admin_user, seed.ts(1)),
            )
        admin_jwt = h.jwt(admin_user, "admin@example.com")
        url = f"/api/v1/crews/{ids['crew_id']}/members"
        assert (await h.client.post(url, json={"user_id": other, "role": "member"}, headers=admin_jwt)).status_code == 200
        res = await h.client.post(url, json={"user_id": other, "role": "admin"}, headers=admin_jwt)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "crew_role_required"
        assert (await h.client.request("DELETE", f"{url}/{admin_user}", headers=admin_jwt)).status_code == 403


async def test_a_new_member_must_have_joined_the_owners_team_and_the_plan_must_include_teammates(tmp_path):
    """Adding someone to a crew puts the crew in their inbox and alerts: it needs their consent (they
    joined the owner's team, which only an accepted invite does) and a plan with crew teammates."""
    from remembra.cloud.plans import PlanTier
    from remembra.crew.limits import crew_limits_for_tier

    async with crew_api(tmp_path) as (h, db, events, _):
        owner, _key, jwt = await _owner(h)
        ids = await _world(db, events, owner)
        stranger = await h.create_user("stranger@example.com")
        mate = await h.create_user("mate2@example.com")
        url = f"/api/v1/crews/{ids['crew_id']}/members"

        res = await h.client.post(url, json={"user_id": stranger, "role": "admin"}, headers=jwt)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "not_a_teammate", res.text
        other_owner = await h.create_user("elsewhere@example.com")
        await _teammates(h, other_owner, stranger)  # a team the crew owner is not in proves nothing
        res = await h.client.post(url, json={"user_id": stranger, "role": "member"}, headers=jwt)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "not_a_teammate"
        cursor = await db.conn.execute("SELECT COUNT(*) FROM crew_members WHERE user_id = ?", (stranger,))
        assert (await cursor.fetchone())[0] == 0

        await _teammates(h, owner, mate)
        tiers: dict[str, PlanTier] = {owner: PlanTier.FREE}

        class Meter:
            async def get_account(self, user_id: str):  # noqa: ANN202
                from types import SimpleNamespace

                from remembra.cloud.plans import get_plan

                return SimpleNamespace(limits=get_plan(tiers[user_id]))

        h.app.state.usage_meter = Meter()
        for tier in (PlanTier.FREE, PlanTier.PRO):
            tiers[owner] = tier
            assert crew_limits_for_tier(tier).teammates is False
            res = await h.client.post(url, json={"user_id": mate, "role": "member"}, headers=jwt)
            assert res.status_code == 402 and res.json()["detail"]["error"] == "plan_required", (tier, res.text)
        tiers[owner] = PlanTier.TEAM
        res = await h.client.post(url, json={"user_id": mate, "role": "member"}, headers=jwt)
        assert res.status_code == 200, res.text
        # a role change of an existing member is not a new add
        tiers[owner] = PlanTier.FREE
        res = await h.client.post(url, json={"user_id": mate, "role": "viewer"}, headers=jwt)
        assert res.status_code == 200 and res.json()["previous_role"] == "member", res.text


# ---------------------------------------------------------------------------
# Per-agent page, timeline, batons
# ---------------------------------------------------------------------------


async def test_agent_page_has_sessions_checkpoints_batons_claims_and_tasks(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        res = await h.client.get(f"/api/v1/crews/{ids['crew_id']}/agents/claude-code", headers=key)
        assert res.status_code == 200, res.text
        page = res.json()
        assert page["agent_id"] == "claude-code" and page["verified"] is True
        assert [s["callsign"] for s in page["current_sessions"]] == ["cc-2", "cc-1"] or {
            s["callsign"] for s in page["current_sessions"]
        } == {"cc-1", "cc-2"}
        assert {s["callsign"] for s in page["sessions"]} == {"cc-1", "cc-2", "cc-3"}
        assert [c["id"] for c in page["checkpoints"]] == ["ckp_b1"]
        assert [b["id"] for b in page["batons_out"]] == ["bat_1"] and page["batons_out"][0]["to_callsign"] == "codex-1"
        assert page["batons_out"][0]["brief_text"] == "CREW yaadbooks brief"
        assert {c["id"] for c in page["claims"]} == {"clm_pos", "clm_rep"}
        assert {t["id"] for t in page["tasks"]} == {"tsk_14", "tsk_12"}
        assert {e["callsign"]: e["before_write"] for e in page["enforcement"]} == {"cc-1": "enforced", "cc-2": "enforced"}
        for s in page["sessions"] + page["current_sessions"]:
            assert schemas.validate(s, schemas.SESSION_VIEW) == []
        for c in page["claims"]:
            assert schemas.validate(c, schemas.CLAIM_VIEW) == []

        codex = (await h.client.get(f"/api/v1/crews/{ids['crew_id']}/agents/codex", headers=key)).json()
        assert [b["id"] for b in codex["batons_in"]] == ["bat_1"] and codex["batons_in"][0]["from_callsign"] == "cc-3"
        assert (await h.client.get(f"/api/v1/crews/{ids['crew_id']}/agents/gemini", headers=key)).status_code == 404
        assert (await h.client.get(f"/api/v1/crews/{ids['crew_id']}/agents/bad%20id", headers=key)).status_code == 404


async def test_agent_timeline_window_and_batons_filter(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, key, _ = await _owner(h)
        ids = await _world(db, events, owner)
        base = f"/api/v1/crews/{ids['crew_id']}"
        res = await h.client.get(f"{base}/agents/claude-code/timeline", headers=key)
        assert res.status_code == 200, res.text
        entries = res.json()["sessions"]
        assert [e["session"]["callsign"] for e in entries][:1] in (["cc-1"], ["cc-2"])
        cc2 = next(e for e in entries if e["session"]["callsign"] == "cc-2")
        assert [c["id"] for c in cc2["checkpoints"]] == ["ckp_b1"]
        old = next(e for e in entries if e["session"]["callsign"] == "cc-3")
        assert [b["id"] for b in old["batons_out"]] == ["bat_1"]
        # a window after cc-3 ended leaves it out
        res = await h.client.get(f"{base}/agents/claude-code/timeline", params={"from": seed.ts(60)}, headers=key)
        assert {e["session"]["callsign"] for e in res.json()["sessions"]} == {"cc-1", "cc-2"}
        assert (
            await h.client.get(f"{base}/agents/claude-code/timeline", params={"from": "yesterday"}, headers=key)
        ).status_code == 422

        res = await h.client.get(f"{base}/batons", headers=key)
        assert res.status_code == 200 and [b["id"] for b in res.json()["batons"]] == ["bat_1"]
        assert res.json()["batons"][0]["from_callsign"] == "cc-3" and res.json()["batons"][0]["to_callsign"] == "codex-1"
        assert (await h.client.get(f"{base}/batons", params={"task_id": "tsk_9"}, headers=key)).json()["count"] == 1
        assert (await h.client.get(f"{base}/batons", params={"task_id": "tsk_14"}, headers=key)).json()["count"] == 0
        other = await seed.crew(db, owner, "other")
        await seed.task(db, other, "tsk_x", 1, "foreign")
        res = await h.client.get(f"{base}/batons", params={"task_id": "tsk_x"}, headers=key)
        assert res.status_code == 422 and res.json()["detail"]["error"] == "cross_crew_reference"


@pytest.mark.parametrize("path", ["", "/snapshot", "/events", "/members", "/agents/claude-code", "/batons"])
async def test_every_read_route_is_404_for_another_tenant(tmp_path, path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, _, _ = await _owner(h)
        ids = await _world(db, events, owner)
        intruder = await h.create_user("intruder@example.com")
        intruder_key, _ = await h.api_key(intruder, "admin")
        res = await h.client.get(f"/api/v1/crews/{ids['crew_id']}{path}", headers={"X-API-Key": intruder_key})
        assert res.status_code == 404 and res.json()["detail"] == {"error": "not_found", "message": "Not found."}


async def test_mutations_replay_their_response_for_a_repeated_idempotency_key(tmp_path):
    async with crew_api(tmp_path) as (h, db, events, _):
        owner, _, jwt = await _owner(h)
        ids = await _world(db, events, owner)
        url = f"/api/v1/crews/{ids['crew_id']}"
        headers = {**jwt, "If-Match": '"1"', "Idempotency-Key": "patch-1"}
        first = await h.client.patch(url, json={"settings": {"wip_per_session": 2}}, headers=headers)
        assert first.status_code == 200, first.text
        seq = (await db.fetchone("SELECT last_seq FROM crews"))["last_seq"]
        # a retry after success (If-Match now stale) replays the stored response: no 412, no second event
        again = await h.client.patch(url, json={"settings": {"wip_per_session": 2}}, headers=headers)
        assert again.status_code == 200 and again.json() == first.json() and again.headers["etag"] == '"2"'
        assert (await db.fetchone("SELECT last_seq FROM crews"))["last_seq"] == seq
        res = await h.client.patch(url, json={"settings": {"wip_per_session": 3}}, headers=headers)
        assert res.status_code == 422 and res.json()["detail"]["error"] == "idempotency_conflict"

        mate = await h.create_user("mate@example.com")
        await _teammates(h, owner, mate)
        members = f"{url}/members"
        add = {**jwt, "Idempotency-Key": "add-1"}
        first = await h.client.post(members, json={"user_id": mate, "role": "member"}, headers=add)
        assert first.status_code == 200 and first.json()["previous_role"] is None
        again = await h.client.post(members, json={"user_id": mate, "role": "member"}, headers=add)
        assert again.json() == first.json()  # not "previous_role": "member"
        remove = {**jwt, "Idempotency-Key": "rm-1"}
        assert (await h.client.request("DELETE", f"{members}/{mate}", headers=remove)).status_code == 200
        replay = await h.client.request("DELETE", f"{members}/{mate}", headers=remove)
        assert replay.status_code == 200 and replay.json() == {"crew_id": ids["crew_id"], "removed": mate}
        stored = await db.fetchall("SELECT principal, response_json FROM crew_idempotency")
        assert len(stored) == 3 and all(r["principal"].startswith(f"user:{owner}:") for r in stored)
