"""Integration of the Wave-2 crew routers (WP-4 … WP-8) as main.py mounts them.

A session token is issued by WP-4 (``/crews/join``) and must be accepted, in the one
session-token header, by every other router that acts as a crew session: WP-5 claims,
WP-6 tasks, WP-7 channel and WP-4 leave. After the session leaves, the same token is
refused everywhere. Runs over real HTTP with auth enabled, a real API key, a real
crew.db, the real event log and the hash chain.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from typing import Any

from remembra.api.v1 import auth
from remembra.api.v1 import crew_tasks as crew_tasks_api
from remembra.crew import channel, claims, sessions
from remembra.crew.bus import CrewBus
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog, verify_crew_chain
from remembra.main import CREW_ROUTER_MODULES
from tests.security_harness import secure_app

HEADER = "X-Remembra-Crew-Session"


def test_every_crew_router_reads_the_session_token_from_one_header():
    assert sessions.SESSION_TOKEN_HEADER == HEADER  # WP-4 (join, leave, stall)
    assert claims.SESSION_HEADER == HEADER  # WP-5 (zones, claims, guard)
    assert crew_tasks_api.SESSION_TOKEN_HEADER == HEADER  # WP-6 (tasks, reports, checkpoints)
    assert channel.SESSION_HEADER == HEADER  # WP-7 (channel, inbox)
    assert not hasattr(channel, "TOKEN_HEADER")  # the session id is never a second credential
    # all hash the token the same way, so a token issued by join verifies everywhere
    token = "rcs_example"
    assert claims.hash_session_token(token) == channel.token_hash(token) == sessions.hash_token(token)


@asynccontextmanager
async def crew_app(tmp_path: Any):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    bus = CrewBus()
    log = CrewEventLog(db, bus)
    routers = [importlib.import_module(name).router for name in CREW_ROUTER_MODULES]
    try:
        async with secure_app(tmp_path, [auth.router, *routers], state={"crew_db": db, "crew_bus": bus, "crew_events": log}) as h:
            yield h, db, log
    finally:
        await db.close()


async def test_a_token_from_join_acts_on_claims_tasks_and_channel_until_the_session_leaves(tmp_path):
    async with crew_app(tmp_path) as (h, db, log):
        owner = await h.create_user("owner@example.com")
        key, _ = await h.api_key(owner, "admin")
        k = {"X-API-Key": key}

        joined = await h.client.post(
            "/api/v1/crews/join",
            json={
                "project_id": "yaadbooks",
                "agent_id": "claude-code",
                "session_id": "s-1",
                "adapter": "claude-code",
                "client_kind": "hook",
                "checkout_fp": "fp-a",
                "worktree_id": "wt-a",
                "branch": "main",
                "head": "abc1234",
                "source": "startup",
            },
            headers=k,
        )
        assert joined.status_code == 201, joined.text
        crew_id, sid, token = joined.json()["crew_id"], joined.json()["session_id"], joined.json()["session_token"]
        s = {**k, HEADER: token}
        forged = {**k, HEADER: "rcs_forged"}

        # WP-5 claims
        claim = {"resource": "schema:main", "mode": "exclusive", "wait": False, "source": "mcp"}
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/claims", json=claim, headers=forged)).status_code == 401
        res = await h.client.post(f"/api/v1/crews/{crew_id}/claims", json=claim, headers=s)
        assert res.status_code == 201, res.text
        assert res.json()["claim"]["holder_session_id"] == sid

        # WP-6 tasks
        task = {"title": "POS split tender", "zone_ids": [], "acceptance": [], "depends_on": []}
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/tasks", json=task, headers=forged)).status_code == 401
        res = await h.client.post(f"/api/v1/crews/{crew_id}/tasks", json=task, headers=s)
        assert res.status_code == 201, res.text

        # WP-7 channel
        msg = {"kind": "note", "body": "starting on the schema", "client_msg_id": "m1"}
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/messages", json=msg, headers=forged)).status_code == 401
        res = await h.client.post(f"/api/v1/crews/{crew_id}/messages", json=msg, headers=s)
        assert res.status_code in (200, 201), res.text
        assert res.json()["message"]["author_session_id"] == sid

        # WP-8 snapshot sees WP-4's session and WP-5's claim
        snap = (await h.client.get(f"/api/v1/crews/{crew_id}/snapshot", headers=k)).json()
        assert [x["id"] for x in snap["sessions"]] == [sid]
        assert [c["holder_session_id"] for c in snap["claims"]] == [sid]

        # WP-4 leave with the same header; afterwards the token is dead on every router
        leave = {"reason": "logout", "facts": {}, "summary": None, "baton": False}
        res = await h.client.post(f"/api/v1/sessions/{sid}/leave", json=leave, headers=s)
        assert res.status_code == 200 and res.json()["state"] == "ended", res.text
        claim2 = {**claim, "resource": "schema:other"}
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/claims", json=claim2, headers=s)).status_code == 401
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/tasks", json=task, headers=s)).status_code == 401
        msg2 = {**msg, "client_msg_id": "m2"}
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/messages", json=msg2, headers=s)).status_code == 401

        report = await verify_crew_chain(db.conn, crew_id)
        assert report.ok, report.errors
