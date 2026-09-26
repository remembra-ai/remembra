"""Sub-agent identity (owner decision on continuity gap analysis open question 1).

A sub-agent is its own crew session, linked by ``parent_session_id`` to the live
session of the same account in the same crew that started it. What it claims and
owns is attributed to it (holder, owner, event actor), and its parent stays
accountable: every event the sub-agent causes names the parent, the parent may
release its claims and act on its tasks, and a parent that ends takes its running
sub-agents with it. Heartbeats and leave work per sub-agent. Snapshots, the gate
templates and the dashboard show sub-agents nested under their parent.

The first test runs over real HTTP with the production crew routers, real auth,
a real crew.db and the hash chain; the rest drive the same services directly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from remembra.crew import collisions  # noqa: F401  (registers the heartbeat footprint sink)
from remembra.crew import gatecore as G
from remembra.crew import views
from remembra.crew.closeout import on_relay_close
from remembra.crew.events import verify_crew_chain
from remembra.crew.hosts import HOST_TOKEN_HEADER
from remembra.crew.sessions import SUB_AGENT_END_REASON, JoinRequest, get_session
from remembra.crew.store import crew_id_for, is_accountable_for
from tests.crew.sessions_support import OWNER, PROJECT, add_claim, add_task, add_zone, hb_item, host, join_req, make_env
from tests.crew.test_crew_integration import HEADER, crew_app

CREW = crew_id_for(OWNER, PROJECT)


def _join(session_id: str, **kw: Any) -> dict[str, Any]:
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


async def _events(db: Any, crew_id: str, type_: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT seq, actor, payload, refs FROM crew_events WHERE crew_id = ? AND type = ? ORDER BY seq", (crew_id, type_)
    )
    return [
        {"seq": r["seq"], "actor": json.loads(r["actor"]), "payload": json.loads(r["payload"]), "refs": json.loads(r["refs"])}
        for r in rows
    ]


async def test_sub_agent_lifecycle_over_http(tmp_path) -> None:
    async with crew_app(tmp_path) as (h, db, _log):
        owner = await h.create_user("owner@example.com")
        key, _ = await h.api_key(owner, "admin")
        k = {"X-API-Key": key}
        reg = (
            await h.client.post(
                "/api/v1/crew/hosts/register", json={"host_label": "mbp1", "platform": "darwin", "crewd_version": "1"}, headers=k
            )
        ).json()
        hk = {**k, HOST_TOKEN_HEADER: reg["host_token"]}

        parent = (await h.client.post("/api/v1/crews/join", json=_join("p-1", host_id=reg["host_id"]), headers=hk)).json()
        # an unrelated session of the same account joins before the sub-agent
        other = (
            await h.client.post(
                "/api/v1/crews/join", json=_join("o-1", agent_id="codex", adapter="codex", host_id=reg["host_id"]), headers=hk
            )
        ).json()
        res = await h.client.post(
            "/api/v1/crews/join",
            json=_join("p-1:task:1", host_id=reg["host_id"], parent_session_id=parent["session_id"], sub_agent_id="explore"),
            headers={**hk, HEADER: parent["session_token"]},  # the parent's token proves the link
        )
        assert res.status_code == 201, res.text
        child = res.json()
        crew_id, pid, cid, oid = parent["crew_id"], parent["session_id"], child["session_id"], other["session_id"]
        assert child["session"]["parent_session_id"] == pid and child["session"]["sub_agent_id"] == "explore"
        ps, cs, os_ = ({**k, HEADER: t} for t in (parent["session_token"], child["session_token"], other["session_token"]))

        # heartbeats are per session: one batch carries the parent and its sub-agent, each with its own token
        def item(sid: str, token: str) -> dict[str, Any]:
            return {
                "session_id": sid,
                "token": token,
                "alive": True,
                "activity_age_s": 2,
                "last_action": None,
                "calls_since_checkpoint": 1,
                "limit": None,
                "footprints": [],
                "cursor": 0,
                "githook_state": "ok",
            }

        hb = {"batch_id": "b1", "sessions": [item(pid, parent["session_token"]), item(cid, child["session_token"])]}
        res = await h.client.post("/api/v1/crew/heartbeat", json=hb, headers=hk)
        assert res.status_code == 200, res.text
        assert {s: v["state"] for s, v in res.json()["per_session"].items()} == {pid: "active", cid: "active"}
        # the sub-agent's token is its own: it cannot heartbeat for its parent
        bad = {"batch_id": "b2", "sessions": [item(pid, child["session_token"])]}
        res = await h.client.post("/api/v1/crew/heartbeat", json=bad, headers=hk)
        assert res.json()["per_session"][pid]["error"] == "session_token_invalid"

        # the sub-agent's claim is attributed to it; its event names the accountable parent
        claim = {"resource": "schema:main", "mode": "exclusive", "wait": False, "source": "mcp"}
        res = await h.client.post(f"/api/v1/crews/{crew_id}/claims", json=claim, headers=cs)
        assert res.status_code == 201, res.text
        claim_id = res.json()["claim"]["id"]
        assert res.json()["claim"]["holder_session_id"] == cid
        granted = await _events(db, crew_id, "claim.granted")
        assert granted[-1]["actor"]["id"] == cid and granted[-1]["actor"]["parent_session_id"] == pid
        joined = await _events(db, crew_id, "session.joined")
        assert "parent_session_id" not in joined[0]["actor"]  # an ordinary session's actor is unchanged

        # a task the sub-agent claims and starts is owned by it
        task = (
            await h.client.post(
                f"/api/v1/crews/{crew_id}/tasks",
                json={"title": "Explore the POS", "zone_ids": [], "acceptance": [], "depends_on": []},
                headers=cs,
            )
        ).json()["task"]
        tid = task["id"]
        assert (await h.client.post(f"/api/v1/tasks/{tid}/claim", headers=cs)).status_code == 200
        res = await h.client.post(f"/api/v1/tasks/{tid}/start", json={}, headers=cs)
        assert res.status_code == 200, res.text
        assert res.json()["task"]["owner_session_id"] == cid

        # the snapshot lists the sub-agent right under its parent
        snap = (await h.client.get(f"/api/v1/crews/{crew_id}/snapshot", headers=k)).json()
        assert [s["id"] for s in snap["sessions"]] == [pid, cid, oid]
        assert [s.get("parent_session_id") for s in snap["sessions"]] == [None, pid, None]
        # the gate templates render it too: the parent sees the claim it answers for, others see who holds it
        you = G.render_you_line(snap, pid, "2026-09-26T12:00:00.000Z")
        assert "schema:main (via sub-agent cc-2)" in you
        assert G.render_do_not_touch(snap, oid) == "DO NOT TOUCH: schema:main → cc-2 (sub-agent of cc-1)"

        # an unrelated session is not accountable: it can neither release the claim nor block the task
        res = await h.client.post(f"/api/v1/claims/{claim_id}/release", json={}, headers=os_)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "not_holder", res.text
        res = await h.client.post(f"/api/v1/tasks/{tid}/block", json={"reason": "waiting"}, headers=os_)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "not_task_owner", res.text
        # the parent is: it blocks and unblocks the sub-agent's task, and releases its claim
        res = await h.client.post(f"/api/v1/tasks/{tid}/block", json={"reason": "needs the schema first"}, headers=ps)
        assert res.status_code == 200 and res.json()["task"]["status"] == "blocked", res.text
        res = await h.client.post(f"/api/v1/tasks/{tid}/unblock", json={}, headers=ps)
        assert res.status_code == 200 and res.json()["task"]["status"] == "in_progress", res.text
        assert res.json()["task"]["owner_session_id"] == cid  # still attributed to the sub-agent
        res = await h.client.post(f"/api/v1/claims/{claim_id}/release", json={}, headers=ps)
        assert res.status_code == 200, res.text
        row = await db.fetchone("SELECT state, holder_session_id FROM crew_claims WHERE id = ?", (claim_id,))
        assert row["state"] == "released" and row["holder_session_id"] == cid
        released = await _events(db, crew_id, "claim.released")
        assert released[-1]["actor"]["id"] == pid  # the parent did it, in its own name

        # a second sub-agent can leave on its own; the parent stays live
        second = (
            await h.client.post(
                "/api/v1/crews/join", json=_join("p-1:task:2", parent_session_id=pid, sub_agent_id="plan"), headers=ps
            )
        ).json()
        leave = {"reason": "other", "facts": {}, "summary": None, "baton": False}
        res = await h.client.post(
            f"/api/v1/sessions/{second['session_id']}/leave", json=leave, headers={**k, HEADER: second["session_token"]}
        )
        assert res.status_code == 200 and res.json()["state"] == "ended"
        assert (await db.fetchone("SELECT state FROM crew_sessions WHERE id = ?", (pid,)))["state"] == "active"

        # the sub-agent claims again, then the parent leaves: the sub-agent ends with it
        claim2 = {**claim, "resource": "schema:other"}
        claim2_id = (await h.client.post(f"/api/v1/crews/{crew_id}/claims", json=claim2, headers=cs)).json()["claim"]["id"]
        res = await h.client.post(f"/api/v1/sessions/{pid}/leave", json=leave, headers=ps)
        assert res.status_code == 200 and res.json()["state"] == "ended", res.text
        child_row = await db.fetchone("SELECT state, end_reason FROM crew_sessions WHERE id = ?", (cid,))
        assert child_row["state"] == "ended" and child_row["end_reason"] == SUB_AGENT_END_REASON
        # its unfinished task stalled with its claims reserved for pickup (as its own leave would)
        t = await db.fetchone("SELECT status FROM crew_tasks WHERE id = ?", (tid,))
        assert t["status"] == "stalled"
        c2 = await db.fetchone("SELECT state FROM crew_claims WHERE id = ?", (claim2_id,))
        assert c2["state"] == "reserved"  # unfinished work: kept for whoever picks the task up
        left = [e for e in await _events(db, crew_id, "session.left") if e["refs"].get("session_id") == cid]
        assert left and left[0]["payload"]["reason"] == SUB_AGENT_END_REASON and left[0]["actor"]["id"] == pid
        # the ended sub-agent's token is dead everywhere; the other session is untouched
        assert (await h.client.post(f"/api/v1/crews/{crew_id}/claims", json=claim, headers=cs)).status_code == 401
        assert (await db.fetchone("SELECT state FROM crew_sessions WHERE id = ?", (oid,)))["state"] == "active"

        report = await verify_crew_chain(db.conn, crew_id)
        assert report.ok, report.errors


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path):
    e = await make_env(tmp_path)
    yield e
    await e.db.close()


def _sub(session_id: str, parent_id: str, **kw: Any) -> JoinRequest:
    req = join_req(session_id, **kw)
    req.parent_session_id = parent_id
    req.sub_agent_id = "general-purpose"
    return req


async def test_parent_end_cascades_to_grandchildren_and_skips_lost_and_ended(env) -> None:
    p = (await env.svc.join(user_id=OWNER, req=join_req("p"), host_token=None, session_token=None)).session
    c1 = (await env.svc.join(user_id=OWNER, req=_sub("c1", p["id"]), host_token=None, session_token=None)).session
    c2 = (await env.svc.join(user_id=OWNER, req=_sub("c2", p["id"]), host_token=None, session_token=None)).session
    g = (await env.svc.join(user_id=OWNER, req=_sub("g", c1["id"]), host_token=None, session_token=None)).session
    lost = (await env.svc.join(user_id=OWNER, req=_sub("l", p["id"]), host_token=None, session_token=None)).session
    zone = await add_zone(env, CREW, "pos")
    claim = await add_claim(env, CREW, g, zone_id=zone)
    async with env.db.transaction():
        await env.conn.execute("UPDATE crew_sessions SET state = 'lost', state_reason = 'silent' WHERE id = ?", (lost["id"],))
    await env.svc.leave(c2, reason="other", facts={}, summary=None, baton=False, baton_ref=None)

    assert await is_accountable_for(env.conn, p["id"], g["id"])  # grandparent answers for the grandchild
    assert await is_accountable_for(env.conn, c1["id"], g["id"])
    assert not await is_accountable_for(env.conn, g["id"], p["id"])
    assert not await is_accountable_for(env.conn, c2["id"], g["id"])
    assert not await is_accountable_for(env.conn, p["id"], p["id"])

    res = await env.svc.leave(p, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    assert res["state"] == "ended"
    rows = {r["id"]: r for r in await env.all("SELECT id, state, end_reason FROM crew_sessions")}
    assert rows[c1["id"]]["state"] == "ended" and rows[c1["id"]]["end_reason"] == SUB_AGENT_END_REASON
    assert rows[g["id"]]["state"] == "ended" and rows[g["id"]]["end_reason"] == SUB_AGENT_END_REASON
    assert rows[lost["id"]]["state"] == "lost"  # the reaper keeps a lost lane (its baton may wait)
    assert rows[c2["id"]]["end_reason"] != SUB_AGENT_END_REASON  # it had left on its own before
    held = await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,))
    assert held["state"] == "released"  # clean work: the grandchild's claim is released, not orphaned
    left = [e for e in await env.events(CREW, types=("session.left",)) if e["payload"]["reason"] == SUB_AGENT_END_REASON]
    assert len(left) == 2
    await env.chain_ok(CREW)


async def test_a_sub_agent_with_unfinished_work_is_stalled_when_its_parent_ends(env) -> None:
    p = (await env.svc.join(user_id=OWNER, req=join_req("p"), host_token=None, session_token=None)).session
    c = (await env.svc.join(user_id=OWNER, req=_sub("c", p["id"]), host_token=None, session_token=None)).session
    zone = await add_zone(env, CREW, "pos")
    task = await add_task(env, CREW, 1, owner=c, zones=[zone])
    claim = await add_claim(env, CREW, c, zone_id=zone, task_id=task)
    await env.svc.leave(p, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    assert (await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,)))["status"] == "stalled"
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "reserved"
    report = await env.one("SELECT kind, session_id FROM crew_reports WHERE task_id = ?", (task,))
    assert report["kind"] == "partial" and report["session_id"] == c["id"]
    await env.chain_ok(CREW)


async def test_relay_close_of_a_parent_ends_its_sub_agents(env) -> None:
    joined = await env.svc.join(user_id=OWNER, req=join_req("p", client_kind="mcp"), host_token=None, session_token=None)
    p = joined.session
    c = await env.svc.join(user_id=OWNER, req=_sub("c", p["id"], client_kind="mcp"), host_token=None, session_token=None)
    result = await on_relay_close(
        env.log,
        user_id=OWNER,
        project_id=PROJECT,
        agent_id="claude-code",
        client_session_id="p",
        agent_verified=True,
        handoff_id="mem_h1",
        end_reason="logout",
        facts={},
        sections={},
        facts_source="relay-cli",
        session_token=joined.session_token,
    )
    assert result is not None and result["session_left"] is True
    row = await get_session(env.conn, c.session["id"])
    assert row is not None and row["state"] == "ended" and row["end_reason"] == SUB_AGENT_END_REASON
    left = await env.events(CREW, types=("session.left",))
    assert [e["payload"]["reason"] for e in left][-1] == SUB_AGENT_END_REASON
    assert set(result["seqs"]) >= {e["seq"] for e in left}  # the close reports every seq it caused
    await env.chain_ok(CREW)


async def test_heartbeat_is_per_sub_agent(env) -> None:
    hrow, htok = await host(env)
    p = await env.svc.join(user_id=OWNER, req=join_req("p", host_id=hrow["id"]), host_token=htok, session_token=None)
    c = await env.svc.join(user_id=OWNER, req=_sub("c", p.session["id"], host_id=hrow["id"]), host_token=htok, session_token=None)
    env.clock.advance(400)  # both go quiet without heartbeats; then only the sub-agent reports activity
    res = await env.svc.heartbeat(
        user_id=OWNER,
        host=hrow,
        body={"batch_id": "b", "sessions": [hb_item(c.session, c.session_token, age=1)]},
    )
    assert res["per_session"][c.session["id"]]["state"] == "active"
    child = await get_session(env.conn, c.session["id"])
    parent = await get_session(env.conn, p.session["id"])
    assert child is not None and parent is not None
    assert child["last_heartbeat_at"] != parent["last_heartbeat_at"]  # each lease is its own


# ---------------------------------------------------------------------------
# Views and templates
# ---------------------------------------------------------------------------


def test_nest_sessions_puts_each_sub_agent_under_its_parent() -> None:
    rows = [
        {"id": "cs_a"},
        {"id": "cs_b"},
        {"id": "cs_a2", "parent_session_id": "cs_a"},
        {"id": "cs_orphan", "parent_session_id": "cs_gone"},
        {"id": "cs_a2x", "parent_session_id": "cs_a2"},
        {"id": "cs_a1", "parent_session_id": "cs_a"},
    ]
    assert [r["id"] for r in views.nest_sessions(rows)] == ["cs_a", "cs_a2", "cs_a2x", "cs_a1", "cs_b", "cs_orphan"]
    assert views.nest_sessions([]) == []
    looped = [{"id": "cs_x", "parent_session_id": "cs_y"}, {"id": "cs_y", "parent_session_id": "cs_x"}]
    assert sorted(r["id"] for r in views.nest_sessions(looped)) == ["cs_x", "cs_y"]  # never loses a row


def test_gate_templates_name_the_accountable_parent() -> None:
    snap = {
        "crew": {"project_id": "yaadbooks"},
        "sessions": [
            {"id": "cs_p", "callsign": "cc-1", "state": "active"},
            {"id": "cs_c", "callsign": "cc-2", "state": "active", "parent_session_id": "cs_p"},
            {"id": "cs_o", "callsign": "codex-1", "state": "active"},
        ],
        "zones": [{"id": "zon_pos", "slug": "pos", "title": "POS", "include_globs": ["src/pos/**"], "source": "repo"}],
        "claims": [
            {
                "id": "clm_1",
                "zone_id": "zon_pos",
                "mode": "exclusive",
                "holder_kind": "session",
                "holder_session_id": "cs_c",
                "state": "active",
            }
        ],
        "tasks": [],
    }
    assert "zone pos (via sub-agent cc-2)" in G.render_you_line(snap, "cs_p", "2026-09-26T12:00:00.000Z")
    assert "zone pos (lease ok)" in G.render_you_line(snap, "cs_c", "2026-09-26T12:00:00.000Z")
    assert G.render_do_not_touch(snap, "cs_o") == "DO NOT TOUCH: zone pos → cc-2 (sub-agent of cc-1)"
    assert "via sub-agent" not in G.render_you_line(snap, "cs_o", "2026-09-26T12:00:00.000Z")
