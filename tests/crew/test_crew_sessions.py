"""WP-4 session lifecycle on a real crew.db and the real event log.

Covers join (lazy crew, callsigns, member keys, token rules, seats), heartbeat
(server clock, lease renewal, no events), stall (quota baton path), leave
(clean, dirty, orphan, clear), recovery, auto-adopt and baton offers (D6, D33),
human pause/resume/request-checkpoint/release-all, limits and footprints.
"""

from __future__ import annotations

import json
import re

import pytest

from remembra.crew import schemas
from remembra.crew.hosts import hash_token
from remembra.crew.sessions import (
    FOOTPRINT_SINKS,
    SessionError,
    callsign_prefix,
    fence_horizon,
    get_session,
    has_seat,
    member_key_for,
    register_footprint_sink,
)
from remembra.crew.store import crew_id_for
from tests.crew.sessions_support import (
    OWNER,
    PROJECT,
    add_claim,
    add_task,
    add_zone,
    hb_item,
    host,
    join_req,
    make_env,
)

CREW = crew_id_for(OWNER, PROJECT)


@pytest.fixture
async def mk(tmp_path):
    made = []

    async def factory(**kw):
        env = await make_env(tmp_path, name=f"crew{len(made)}.db", **kw)
        made.append(env)
        return env

    yield factory
    for env in made:
        await env.db.close()


async def _join(env, sid, **kw):
    host_token = kw.pop("host_token", None)
    session_token = kw.pop("session_token", None)
    return await env.svc.join(user_id=OWNER, req=join_req(sid, **kw), host_token=host_token, session_token=session_token)


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


async def test_first_join_creates_the_crew_and_a_session_with_hashed_token(mk):
    env = await mk()
    res = await _join(env, "s-a")
    assert res.crew_created and res.crew_id == CREW and not res.rejoined
    assert res.session_token and res.session_token.startswith("rcs_")
    row = await get_session(env.conn, res.session["id"])
    assert row["token_hash"] == hash_token(res.session_token)
    assert res.session_token not in json.dumps(row)
    assert row["callsign"] == "cc-1" and row["state"] == "active"
    assert re.fullmatch(schemas.MEMBER_KEY_PATTERN, row["member_key"])
    assert row["adapter_enforcement"] == "enforced" and row["agent_verified"] == 1
    types = [e["type"] for e in await env.events(CREW)]
    assert types == ["crew.created", "session.joined"]
    members = await env.all("SELECT user_id, role FROM crew_members WHERE crew_id = ?", (CREW,))
    assert members == [{"user_id": OWNER, "role": "owner"}]
    await env.chain_ok(CREW)


async def test_callsigns_per_agent_and_mode_changes(mk):
    env = await mk()
    a = await _join(env, "s-a")
    b = await _join(env, "s-b", agent="codex", adapter="codex", verified=False)
    c = await _join(env, "s-c")
    assert [a.session["callsign"], b.session["callsign"], c.session["callsign"]] == ["cc-1", "codex-1", "cc-2"]
    assert b.session["adapter_enforcement"] == "advisory" and b.session["agent_verified"] == 0
    modes = await env.events(CREW, types=("crew.mode_changed",))
    assert [(m["payload"]["from"], m["payload"]["to"]) for m in modes] == [("solo", "multi")]
    assert modes[0]["moment"] is True
    # two leave -> back to solo
    await env.svc.leave(b.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    await env.svc.leave(c.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    modes = await env.events(CREW, types=("crew.mode_changed",))
    assert [(m["payload"]["from"], m["payload"]["to"]) for m in modes] == [("solo", "multi"), ("multi", "solo")]


async def test_callsign_number_is_reused_only_24h_after_end(mk):
    env = await mk()
    a = await _join(env, "s-a")
    await env.svc.leave(a.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    b = await _join(env, "s-b")
    assert b.session["callsign"] == "cc-2"
    env.clock.advance(25 * 3600)
    c = await _join(env, "s-c")
    assert c.session["callsign"] == "cc-1"


def test_callsign_prefix_and_member_key_shapes():
    assert callsign_prefix("claude-code") == "cc" and callsign_prefix("Codex") == "codex"
    assert callsign_prefix("my agent:9") == "my-agent-9" and callsign_prefix("9lives") == "a9lives"
    key = member_key_for(crew_id=CREW, user_id=OWNER, agent_id="team:bot", session_id="x", host_label="mbp1", client_kind="hook")
    assert re.fullmatch(schemas.MEMBER_KEY_PATTERN, key) and key.startswith("team-bot:mbp1:")
    mcp = member_key_for(crew_id=CREW, user_id=OWNER, agent_id="cursor", session_id="x", host_label=None, client_kind="mcp")
    assert mcp.split(":")[1] == "mcp"
    for label in ("MBP.local", "my-host"):  # never a raw hostname
        assert (
            member_key_for(crew_id=CREW, user_id=OWNER, agent_id="c", session_id="x", host_label=label, client_kind="hook").split(
                ":"
            )[1]
            == "nohost"
        )


async def test_rejoin_token_rules(mk):
    env = await mk()
    h, htok = await host(env)
    other, otok = await host(env, "mbp2")
    first = await _join(env, "s-a", host_id=h["id"], host_token=htok)
    token = first.session_token
    # no credential -> 409 session_exists, token never returned
    with pytest.raises(SessionError) as e:
        await _join(env, "s-a")
    assert e.value.status == 409 and e.value.code == "session_exists"
    # another host's token cannot rotate it
    with pytest.raises(SessionError) as e:
        await _join(env, "s-a", host_id=other["id"], host_token=otok)
    assert e.value.code == "session_exists"
    # the current token re-joins without a token in the answer
    again = await _join(env, "s-a", session_token=token, source="compact")
    assert again.rejoined and again.session_token is None and not again.token_rotated
    assert again.session["id"] == first.session["id"]
    # the owning host rotates
    rotated = await _join(env, "s-a", host_id=h["id"], host_token=htok, source="resume")
    assert rotated.token_rotated and rotated.session_token and rotated.session_token != token
    row = await get_session(env.conn, first.session["id"])
    assert row["token_hash"] == hash_token(rotated.session_token) and row["token_version"] == 2
    assert [e["type"] for e in await env.events(CREW, types=("session.token_rotated",))] == ["session.token_rotated"]
    # the old token stops working for heartbeats
    res = await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": "b1", "sessions": [hb_item(row, token)]})
    assert res["per_session"][row["id"]]["error"] == "session_token_invalid"
    # a wrong host token for host binding is refused
    with pytest.raises(SessionError) as e:
        await _join(env, "s-new", host_id=h["id"], host_token="rch_wrong")
    assert e.value.status == 403


async def test_join_over_the_seat_cap_is_observe_only_and_gets_a_seat_later(mk):
    env = await mk(max_live=1)
    a = await _join(env, "s-a")
    b = await _join(env, "s-b", agent="codex")
    assert not a.observe_only and b.observe_only
    assert not await has_seat(env.conn, CREW, b.session["id"], 1)
    await env.svc.leave(a.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    assert await has_seat(env.conn, CREW, b.session["id"], 1)


async def test_resume_of_validation(mk):
    env = await mk()
    a = await _join(env, "s-a")
    with pytest.raises(SessionError) as e:
        await _join(env, "s-b", resume_of=a.session["id"])
    assert e.value.code == "resume_of_live"
    with pytest.raises(SessionError) as e:
        await _join(env, "s-c", resume_of="cs_doesnotexist")
    assert e.value.status == 422
    with pytest.raises(SessionError) as e:
        await _join(env, "s-d", agent="codex", resume_of=a.session["id"])
    assert e.value.code == "resume_of_mismatch"


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


async def test_heartbeat_uses_server_clock_renews_leases_and_writes_no_event(mk):
    env = await mk()
    h, htok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=htok)
    zone = await add_zone(env, CREW)
    claim = await add_claim(env, CREW, a.session, zone_id=zone, lease_s=120)
    seq = await env.last_seq(CREW)
    env.clock.advance(60)
    res = await env.svc.heartbeat(
        user_id=OWNER,
        host=h,
        body={
            "batch_id": "b1",
            "sessions": [
                hb_item(
                    a.session,
                    a.session_token,
                    age=12,
                    last_action={"tool": "Edit", "path_rel": "src/pos/a.ts", "verb": None, "age_s": 12},
                )
            ],
        },
    )
    out = res["per_session"][a.session["id"]]
    assert out["state"] == "active" and out["crew_id"] == CREW
    expected = env.clock.now.replace(microsecond=0).isoformat().replace("+00:00", "")
    assert out["lease_expires_at"].startswith(expected[:13])
    lease = (await env.one("SELECT lease_expires_at FROM crew_claims WHERE id = ?", (claim,)))["lease_expires_at"]
    assert lease == out["lease_expires_at"] and out["fence_at"] == fence_horizon(lease)
    assert out["epoch_by_claim"] == {claim: 1}
    row = await get_session(env.conn, a.session["id"])
    assert row["last_heartbeat_at"] and row["calls_since_checkpoint"] == 3 and row["githook_state"] == "ok"
    assert json.loads(row["last_action"])["path_rel"] == "src/pos/a.ts"
    assert await env.last_seq(CREW) == seq  # heartbeats and lease renewals are never events
    assert res["snapshot_etag"] == {CREW: f'"{seq}"'}


async def test_heartbeat_isolates_bad_sessions_in_the_batch(mk):
    env = await mk()
    h1, t1 = await host(env)
    h2, t2 = await host(env, "mbp2")
    a = await _join(env, "s-a", host_id=h1["id"], host_token=t1)
    b = await _join(env, "s-b", host_id=h2["id"], host_token=t2)
    body = {"batch_id": "b", "sessions": [hb_item(a.session, a.session_token), hb_item(b.session, b.session_token)]}
    res = await env.svc.heartbeat(user_id=OWNER, host=h1, body=body)
    assert "error" not in res["per_session"][a.session["id"]]
    assert res["per_session"][b.session["id"]]["error"] == "session_token_invalid"  # wrong host


async def test_heartbeat_redacts_last_action_and_upserts_footprints_for_sinks(mk):
    env = await mk()
    h, htok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=htok)
    seen = []

    async def sink(tx, row, fps):
        seen.append((row["id"], [f["path"] for f in fps]))

    register_footprint_sink(sink)
    register_footprint_sink(sink)
    try:
        fp = {"path": "src/pos/a.ts", "state": "dirty", "attribution": "certain", "claim_epoch": 1, "last_commit": None}
        bad = {"path": "/etc/passwd", "state": "dirty", "attribution": "certain", "claim_epoch": None, "last_commit": None}
        item = hb_item(
            a.session, a.session_token, last_action={"tool": "Bash", "path_rel": None, "verb": "AKIAIOSFODNN7EXAMPLE", "age_s": 1}
        )
        item["footprints"] = [fp, bad]
        await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": "b", "sessions": [item]})
        await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": "c", "sessions": [item]})
    finally:
        FOOTPRINT_SINKS.remove(sink)
    assert seen == [(a.session["id"], ["src/pos/a.ts", "/etc/passwd"])] * 2
    rows = await env.all("SELECT path, touches, state, worktree_id FROM crew_footprints WHERE session_id = ?", (a.session["id"],))
    assert rows == [{"path": "src/pos/a.ts", "touches": 2, "state": "dirty", "worktree_id": "wt-fp-a"}]
    stored = (await get_session(env.conn, a.session["id"]))["last_action"]
    assert "AKIAIOSFODNN7EXAMPLE" not in stored


async def test_heartbeat_limit_warning_and_detected_exhaustion(mk):
    env = await mk()
    h, htok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=htok)
    warn = hb_item(a.session, a.session_token, limit={"level": "warn", "pct": 0.82, "source": "reported"})
    await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": "1", "sessions": [warn]})
    await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": "2", "sessions": [warn]})
    warnings = await env.events(CREW, types=("session.limit_warning",))
    assert len(warnings) == 1 and warnings[0]["payload"]["pct"] == 0.82
    done = hb_item(a.session, a.session_token, limit={"level": "exhausted", "pct": 1.0, "source": "detected"})
    res = await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": "3", "sessions": [done]})
    assert res["per_session"][a.session["id"]]["state"] == "quota_blocked"
    blocked = await env.events(CREW, types=("session.quota_blocked",))
    assert blocked[0]["payload"]["source"] == "detected" and blocked[0]["payload"]["error"] == "detected_limit"


async def test_touch_renews_leases_of_mcp_sessions(mk):
    env = await mk()
    m = await _join(env, "mcp-1", agent="cursor", client_kind="mcp", checkout=None)
    assert m.session["member_key"].split(":")[1] == "mcp"
    claim = await add_claim(env, CREW, m.session, lease_s=60)
    env.clock.advance(50)
    row = await env.svc.touch(m.session["id"])
    assert row["state"] == "active"
    lease = (await env.one("SELECT lease_expires_at FROM crew_claims WHERE id = ?", (claim,)))["lease_expires_at"]
    assert lease > row["last_seen_at"]
    assert await env.svc.touch("cs_unknown") is None


# ---------------------------------------------------------------------------
# stall and recovery
# ---------------------------------------------------------------------------


async def _holder_with_task(env, sid="s-a", checkout="fp-a", host_pair=None):
    kw = {"checkout": checkout}
    if host_pair:
        kw.update(host_id=host_pair[0]["id"], host_token=host_pair[1])
    j = await _join(env, sid, **kw)
    zone = await add_zone(env, CREW, f"pos{sid[-1]}")
    task = await add_task(env, CREW, 1 if sid == "s-a" else 2, owner=j.session, zones=[zone])
    claim = await add_claim(env, CREW, j.session, zone_id=zone, task_id=task)
    return j, zone, task, claim


async def test_a_stalled_task_without_zones_asks_a_person_to_hand_it_over(mk):
    """A task with no zones reserves no claim, so no later session is offered its baton (only one in the same
    checkout picks it up): the Needs-you item says to hand it to an agent instead of "waiting for pickup"."""
    env = await mk()
    j = await _join(env, "s-a", checkout="fp-a")
    task = await add_task(env, CREW, 1, owner=j.session, zones=[])
    await env.svc.stall(j.session, error="billing_error", facts={}, baton_ref=None)
    item = await env.one(
        "SELECT title, primary_action FROM crew_inbox_items WHERE kind = 'baton_available' AND ref_id = ?", (task,)
    )
    assert item["title"] == "T-1 stopped: hand it to an agent (cc-1 stopped: quota)" and item["primary_action"] == "hand_baton"
    # a task with zones keeps its baton offer, and the item says so
    env2 = await mk()
    a, _zone, task2, _claim = await _holder_with_task(env2)
    await env2.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    item = await env2.one("SELECT title FROM crew_inbox_items WHERE kind = 'baton_available' AND ref_id = ?", (task2,))
    assert item["title"] == "Baton T-1 is waiting for pickup (cc-1 stopped: quota)"


async def test_quota_stall_reserves_stalls_reports_and_queues_the_handoff(mk):
    env = await mk()
    a, zone, task, claim = await _holder_with_task(env)
    facts = {
        "branch": "main",
        "uncommitted_files": ["src/pos/tender.ts"],
        "unpushed_commits": 2,
        "notes": "token=ghp_" + "a" * 36,
    }
    res = await env.svc.stall(a.session, error="billing_error", facts=facts, baton_ref="refs/remembra/baton/T-1/7")
    assert res["state"] == "quota_blocked" and res["claims_reserved"] == [claim] and res["tasks_stalled"] == [task]
    c = await env.one("SELECT * FROM crew_claims WHERE id = ?", (claim,))
    assert (c["state"], c["reserve_reason"], c["reserved_for"], c["baton_ref"]) == (
        "reserved",
        "quota",
        a.session["id"],
        "refs/remembra/baton/T-1/7",
    )
    assert c["reserve_expires_at"] is None  # task-linked batons never silently expire (D5)
    t = await env.one("SELECT * FROM crew_tasks WHERE id = ?", (task,))
    assert (t["status"], t["status_before_stall"]) == ("stalled", "in_progress")
    rep = await env.one("SELECT * FROM crew_reports WHERE id = ?", (t["current_report_id"],))
    assert (rep["kind"], rep["is_current"], rep["facts_source"], rep["baton_ref"]) == (
        "stalled",
        1,
        "relay-cli",
        "refs/remembra/baton/T-1/7",
    )
    assert rep["handoff_id"] == res["handoff_id"]
    ckp = await env.one("SELECT * FROM crew_checkpoints WHERE id = ?", (res["checkpoint_id"],))
    assert ckp["trigger"] == "quota" and "ghp_" + "a" * 36 not in ckp["facts"]
    outbox = await env.one("SELECT * FROM crew_outbox WHERE id = ?", (res["handoff_id"],))
    payload = json.loads(outbox["payload"])
    assert outbox["kind"] == "relay_handoff" and payload["end_reason"] == "stalled:billing_error"
    assert payload["session_id"] == "s-a" and payload["project_id"] == PROJECT
    items = await env.all("SELECT kind, audience, ref_id, state FROM crew_inbox_items WHERE crew_id = ? ORDER BY kind", (CREW,))
    assert items == [
        {"kind": "baton_available", "audience": "project", "ref_id": task, "state": "open"},
        {"kind": "baton_reserved", "audience": "crew", "ref_id": task, "state": "open"},
    ]
    types = [e["type"] for e in await env.events(CREW)]
    for expected in (
        "baton.ref_created",
        "checkpoint.created",
        "handoff.created",
        "claim.reserved",
        "report.submitted",
        "task.stalled",
    ):
        assert expected in types
    moment = (await env.events(CREW, types=("session.quota_blocked",)))[0]
    assert moment["moment"] and moment["payload"]["claims_reserved"] == [claim]
    again = await env.svc.stall(a.session, error="billing_error", facts=facts, baton_ref=None)
    assert again["already"] is True
    await env.chain_ok(CREW)


async def test_non_quota_stop_failure_checkpoints_only(mk):
    env = await mk()
    a, _, task, claim = await _holder_with_task(env)
    res = await env.svc.stall(a.session, error="overloaded", facts={"branch": "main"}, baton_ref=None)
    assert res["state"] == "active" and res["checkpoint_id"]
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "active"
    assert (await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,)))["status"] == "in_progress"


async def test_recovery_after_quota_needs_new_activity_and_restores_everything(mk):
    env = await mk()
    hp = await host(env)
    a, zone, task, claim = await _holder_with_task(env, host_pair=hp)
    env.clock.advance(30)
    await env.svc.stall(a.session, error="billing_error", facts={"uncommitted_files": ["x"]}, baton_ref=None)
    env.clock.advance(60)
    # activity from before the stall does not clear it (D14)
    res = await env.svc.heartbeat(
        user_id=OWNER, host=hp[0], body={"batch_id": "1", "sessions": [hb_item(a.session, a.session_token, age=120)]}
    )
    assert res["per_session"][a.session["id"]]["state"] == "quota_blocked"
    res = await env.svc.heartbeat(
        user_id=OWNER, host=hp[0], body={"batch_id": "2", "sessions": [hb_item(a.session, a.session_token, age=2)]}
    )
    assert res["per_session"][a.session["id"]]["state"] == "active"
    c = await env.one("SELECT * FROM crew_claims WHERE id = ?", (claim,))
    assert (c["state"], c["epoch"], c["reserve_reason"]) == ("active", 2, None)
    t = await env.one("SELECT * FROM crew_tasks WHERE id = ?", (task,))
    assert (t["status"], t["current_report_id"]) == ("in_progress", None)
    rep = await env.one("SELECT is_current, superseded_reason FROM crew_reports WHERE task_id = ?", (task,))
    assert rep == {"is_current": 0, "superseded_reason": "recovered"}
    states = {r["state"] for r in await env.all("SELECT state FROM crew_inbox_items WHERE crew_id = ?", (CREW,))}
    assert states == {"resolved"}
    rec = (await env.events(CREW, types=("session.recovered",)))[0]
    assert rec["moment"] and rec["payload"]["claims_retaken"] == [claim] and rec["payload"]["tasks_restored"] == [task]
    s = await get_session(env.conn, a.session["id"])
    assert s["limit_level"] is None
    await env.chain_ok(CREW)


# ---------------------------------------------------------------------------
# batons: offers and auto-adopt (D6, D33)
# ---------------------------------------------------------------------------


async def test_other_checkout_is_offered_same_checkout_auto_adopts(mk):
    env = await mk()
    a, zone, task, claim = await _holder_with_task(env, checkout="fp-a")
    await env.svc.stall(
        a.session, error="billing_error", facts={"uncommitted_files": ["t.ts"]}, baton_ref="refs/remembra/baton/T-1/1"
    )
    b = await _join(env, "s-b", checkout="fp-b")
    assert b.auto_adopted == []
    [offer] = b.batons_offered
    assert offer["claim_id"] == claim and offer["task"] == "T-1" and offer["adopt_command"] == "remembra-crew adopt T-1"
    assert offer["from_callsign"] == "cc-1" and offer["baton_ref"] == "refs/remembra/baton/T-1/1"
    rows = await env.all("SELECT via, to_session FROM crew_baton_offers WHERE claim_id = ?", (claim,))
    assert rows == [{"via": "brief", "to_session": b.session["id"]}]
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "reserved"
    # the same session re-joining is not offered twice (one offer row, one event)
    await _join(env, "s-b", checkout="fp-b", session_token=b.session_token)
    assert len(await env.events(CREW, types=("claim.offered_in_brief",))) == 1

    c = await _join(env, "s-c", checkout="fp-a")
    [adopted] = c.auto_adopted
    assert adopted["kind"] == "same_checkout" and adopted["claim_ids"] == [claim] and adopted["task"] == "T-1"
    assert adopted["restore_command"] == "remembra-crew adopt T-1"
    cl = await env.one("SELECT * FROM crew_claims WHERE id = ?", (claim,))
    assert (cl["state"], cl["holder_session_id"], cl["epoch"], cl["source"]) == ("active", c.session["id"], 2, "adopt")
    t = await env.one("SELECT status, owner_session_id FROM crew_tasks WHERE id = ?", (task,))
    assert t == {"status": "claimed", "owner_session_id": c.session["id"]}
    baton = await env.one("SELECT * FROM crew_batons WHERE task_id = ?", (task,))
    assert (baton["kind"], baton["from_session"], baton["to_session"]) == ("same_checkout", a.session["id"], c.session["id"])
    passed = (await env.events(CREW, types=("baton.passed",)))[0]
    assert passed["moment"] and passed["payload"]["zones"] == [zone] and baton["seq"] == passed["seq"]
    adopted_ev = (await env.events(CREW, types=("claim.adopted",)))[0]
    assert adopted_ev["payload"]["cross_checkout"] is False and adopted_ev["moment"] is False
    assert {r["state"] for r in await env.all("SELECT state FROM crew_inbox_items WHERE ref_id = ?", (task,))} == {"resolved"}
    assert env.audits == []  # same-checkout adopts are not cross-checkout
    # the stalled holder coming back cannot take the adopted claim or task back
    env.clock.advance(5)
    await env.svc.touch(a.session["id"])
    cl = await env.one("SELECT holder_session_id, epoch FROM crew_claims WHERE id = ?", (claim,))
    assert cl == {"holder_session_id": c.session["id"], "epoch": 2}
    assert (await env.one("SELECT owner_session_id FROM crew_tasks WHERE id = ?", (task,)))["owner_session_id"] == c.session["id"]
    await env.chain_ok(CREW)


async def test_auto_adopt_off_offers_instead(mk):
    env = await mk()
    a, _, _, claim = await _holder_with_task(env, checkout="fp-a")
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    await env.svc.store.patch_settings(CREW, {"auto_adopt": "off"}, if_match=1)
    c = await _join(env, "s-c", checkout="fp-a")
    assert c.auto_adopted == [] and [o["claim_id"] for o in c.batons_offered] == [claim]


async def test_zone_reserve_for_needs_a_key_verified_agent(mk):
    env = await mk()
    a = await _join(env, "s-a")
    zone = await add_zone(env, CREW, "billing", reserve_for="codex")
    claim = await add_claim(env, CREW, a.session, zone_id=zone)
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    fake = await _join(env, "s-fake", agent="codex", verified=False, checkout="fp-z")
    real = await _join(env, "s-real", agent="codex", verified=True, checkout="fp-y")
    assert fake.batons_offered == []
    assert [o["claim_id"] for o in real.batons_offered] == [claim]


async def test_observe_only_session_gets_no_baton(mk):
    env = await mk(max_live=1)
    a, _, _, claim = await _holder_with_task(env, checkout="fp-a")
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    # a (quota_blocked) gave its seat up; b takes the only seat (and is offered the baton)
    b = await _join(env, "s-b", checkout="fp-b")
    assert not b.observe_only and len(b.batons_offered) == 1
    c = await _join(env, "s-c", checkout="fp-a")  # same checkout as a, but no seat left
    assert c.observe_only and c.auto_adopted == [] and c.batons_offered == []


async def test_resume_of_adopts_what_was_reserved_for_the_old_session(mk):
    env = await mk()
    a, zone, task, claim = await _holder_with_task(env, checkout="fp-a")
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    r = await _join(env, "s-a2", checkout="fp-other", resume_of=a.session["id"])
    [adopted] = r.auto_adopted
    assert adopted["kind"] == "reserved_for" and adopted["claim_ids"] == [claim]
    old = await get_session(env.conn, a.session["id"])
    assert (old["state"], old["end_reason"]) == ("ended", "resumed")
    cross = (await env.events(CREW, types=("claim.adopted",)))[0]
    assert cross["payload"]["cross_checkout"] is True and cross["moment"] is True
    assert env.audits and env.audits[0]["action"] == "crew.adopt_cross_checkout"


async def test_cap_limits_auto_adopt(mk):
    env = await mk()
    a = await _join(env, "s-a", checkout="fp-a")
    claims = [await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW, f"z{i}")) for i in range(4)]
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    c = await _join(env, "s-c", checkout="fp-a")
    assert len(c.auto_adopted) == 3  # max_exclusive_claims_per_session
    assert [o["claim_id"] for o in c.batons_offered] == [claims[3]]


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------


async def test_clean_leave_releases_everything(mk):
    env = await mk()
    a = await _join(env, "s-a")
    claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW))
    res = await env.svc.leave(a.session, reason="logout", facts={"branch": "main"}, summary=None, baton=False, baton_ref=None)
    assert res["state"] == "ended" and res["claims_released"] == [claim] and res["claims_reserved"] == []
    assert (await env.one("SELECT state, end_reason FROM crew_claims WHERE id = ?", (claim,))) == {
        "state": "released",
        "end_reason": "session_ended",
    }
    left = (await env.events(CREW, types=("session.left",)))[0]
    assert left["payload"] == {"reason": "logout", "claims_released": [claim], "claims_reserved": []}
    assert (await env.svc.leave(a.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None))["already"]
    assert await env.one("SELECT 1 AS x FROM crew_outbox") is None


async def test_dirty_or_unfinished_leave_reserves_with_partial_report(mk):
    env = await mk()
    a, zone, task, claim = await _holder_with_task(env)
    res = await env.svc.leave(
        a.session,
        reason="prompt_input_exit",
        facts={"uncommitted_files": ["a", "b"]},
        summary=None,
        baton=False,
        baton_ref="refs/remembra/baton/T-1/3",
    )
    assert res["claims_reserved"] == [claim] and res["tasks_stalled"] == [task]
    c = await env.one("SELECT state, reserve_reason, baton_ref FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "reserved", "reserve_reason": "ended_dirty", "baton_ref": "refs/remembra/baton/T-1/3"}
    rep = await env.one("SELECT kind, verdict, is_current FROM crew_reports WHERE task_id = ?", (task,))
    assert rep == {"kind": "partial", "verdict": "partial", "is_current": 1}
    assert await env.one("SELECT 1 AS x FROM crew_outbox") is None  # crewd ran the relay close itself
    ref = (await env.events(CREW, types=("baton.ref_created",)))[0]
    assert ref["payload"]["dirty_files"] == 2


async def test_clear_leave_reserves_and_next_session_in_checkout_adopts(mk):
    env = await mk()
    a = await _join(env, "s-a", checkout="fp-a")
    claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW))
    await env.svc.leave(a.session, reason="clear", facts={}, summary=None, baton=False, baton_ref=None)
    assert (await env.one("SELECT reserve_reason FROM crew_claims WHERE id = ?", (claim,)))["reserve_reason"] == "ended_dirty"
    b = await _join(env, "s-b", checkout="fp-a")
    assert b.auto_adopted and b.auto_adopted[0]["claim_ids"] == [claim]


async def test_process_exited_is_lost_at_once_without_a_server_handoff(mk):
    env = await mk()
    a, zone, task, claim = await _holder_with_task(env)
    res = await env.svc.leave(
        a.session, reason="process_exited", facts={}, summary=None, baton=False, baton_ref="refs/remembra/baton/T-1/9"
    )
    assert res["state"] == "lost"
    s = await get_session(env.conn, a.session["id"])
    assert (s["state"], s["state_reason"]) == ("lost", "process_exited")
    c = await env.one("SELECT state, reserve_reason, baton_ref FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "reserved", "reserve_reason": "lost", "baton_ref": "refs/remembra/baton/T-1/9"}
    rep = await env.one("SELECT kind, facts_source, baton_ref FROM crew_reports WHERE task_id = ?", (task,))
    assert rep == {"kind": "stalled", "facts_source": "server-inferred", "baton_ref": "refs/remembra/baton/T-1/9"}
    assert await env.one("SELECT 1 AS x FROM crew_outbox") is None
    lost = (await env.events(CREW, types=("session.lost",)))[0]
    assert lost["moment"] and lost["payload"]["reason"] == "process_exited"


async def test_rejoin_of_an_ended_session_takes_back_its_own_batons(mk):
    env = await mk()
    a, zone, task, claim = await _holder_with_task(env)
    await env.svc.leave(
        a.session, reason="prompt_input_exit", facts={"uncommitted_files": ["x"]}, summary=None, baton=False, baton_ref=None
    )
    back = await _join(env, "s-a", session_token=a.session_token, source="resume")
    assert back.rejoined and back.session["state"] == "active"
    assert (await env.one("SELECT state, epoch FROM crew_claims WHERE id = ?", (claim,))) == {"state": "active", "epoch": 2}
    assert (await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,)))["status"] == "in_progress"
    await env.chain_ok(CREW)


# ---------------------------------------------------------------------------
# human actions
# ---------------------------------------------------------------------------


async def test_pause_resume_checkpoint_request_and_release_all(mk):
    env = await mk()
    hp = await host(env)
    a = await _join(env, "s-a", host_id=hp[0]["id"], host_token=hp[1])
    c1 = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW))
    cursor = await env.last_seq(CREW)
    await env.svc.pause(a.session, human_user_id=OWNER, reason="checking POS myself")
    s = await get_session(env.conn, a.session["id"])
    assert s["state"] == "paused"
    ev = await env.events(CREW, types=("session.paused", "human.override"), after=cursor)
    assert [e["type"] for e in ev] == ["session.paused", "human.override"] and all(e["moment"] for e in ev)
    assert ev[1]["payload"] == {
        "action": "pause",
        "reason": "checking POS myself",
        "target_kind": "session",
        "target_id": a.session["id"],
    }
    hb = await env.svc.heartbeat(
        user_id=OWNER, host=hp[0], body={"batch_id": "1", "sessions": [hb_item(a.session, a.session_token, cursor=cursor)]}
    )
    out = hb["per_session"][a.session["id"]]
    assert out["state"] == "paused" and "PAUSED" in out["inject_text"] and len(out["inject_text"]) <= 300
    assert (await env.svc.pause(a.session, human_user_id=OWNER, reason="again"))["already"]
    res = await env.svc.resume(a.session, human_user_id=OWNER, reason="done")
    assert res["state"] == "active"
    with pytest.raises(SessionError):
        await env.svc.resume(a.session, human_user_id=OWNER, reason="twice")
    cursor2 = await env.last_seq(CREW)
    await env.svc.request_checkpoint(a.session, human_user_id=OWNER, reason="now please")
    hb = await env.svc.heartbeat(
        user_id=OWNER, host=hp[0], body={"batch_id": "2", "sessions": [hb_item(a.session, a.session_token, cursor=cursor2)]}
    )
    assert "checkpoint" in hb["per_session"][a.session["id"]]["inject_text"]
    hb = await env.svc.heartbeat(
        user_id=OWNER,
        host=hp[0],
        body={"batch_id": "3", "sessions": [hb_item(a.session, a.session_token, cursor=await env.last_seq(CREW))]},
    )
    assert hb["per_session"][a.session["id"]]["inject_text"] is None  # delivered once (cursor)
    rel = await env.svc.release_all(a.session, human_user_id=OWNER, reason="reassigning")
    assert rel["claims_released"] == [c1]
    assert (await env.one("SELECT state, end_reason FROM crew_claims WHERE id = ?", (c1,))) == {
        "state": "released",
        "end_reason": "human_release_all",
    }
    assert [a_["action"] for a_ in env.audits] == ["crew.pause", "crew.resume", "crew.request_checkpoint", "crew.release_all"]
    await env.chain_ok(CREW)


async def test_list_sessions_filters_and_reports_seats(mk):
    env = await mk(max_live=1)
    a = await _join(env, "s-a")
    b = await _join(env, "s-b", agent="codex")
    await env.svc.leave(a.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    c = await _join(env, "s-c", agent="gemini")
    live = await env.svc.list_sessions(CREW, "live")
    assert {s["id"]: s["observe_only"] for s in live} == {b.session["id"]: False, c.session["id"]: True}
    ended = await env.svc.list_sessions(CREW, "ended")
    assert [s["id"] for s in ended] == [a.session["id"]]
    for s in live:
        assert not schemas.validate({k: v for k, v in s.items() if k in schemas.SESSION_VIEW.fields}, schemas.SESSION_VIEW)
    with pytest.raises(SessionError):
        await env.svc.list_sessions(CREW, "bogus")


# ---------------------------------------------------------------------------
# concurrency and the report invariant under random lost / recover sequences
# ---------------------------------------------------------------------------


async def test_concurrent_joins_get_unique_callsigns_and_a_gap_free_log(mk):
    import asyncio

    env = await mk()
    results = await asyncio.gather(*(_join(env, f"s-{i}", checkout=f"fp-{i}") for i in range(12)))
    callsigns = sorted(r.session["callsign"] for r in results)
    assert callsigns == sorted(f"cc-{i}" for i in range(1, 13))
    assert len({r.crew_id for r in results}) == 1
    seqs = [e["seq"] for e in await env.events(CREW)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len([e for e in await env.events(CREW) if e["type"] == "crew.created"]) == 1
    await env.chain_ok(CREW)


async def _assert_report_invariant(env):
    """§5.6: stalled/done/cancelled tasks that reached in_progress have exactly one current report;
    claimed/in_progress/review have at most one; current_report_id always names the current one."""
    for t in await env.all("SELECT * FROM crew_tasks"):
        current = await env.all("SELECT id FROM crew_reports WHERE task_id = ? AND is_current = 1", (t["id"],))
        if t["status"] in ("stalled", "done", "cancelled"):
            assert len(current) == 1, t
        else:
            assert len(current) <= 1, t
        assert t["current_report_id"] == (current[0]["id"] if current else None)


async def test_random_lost_recover_sequences_keep_the_report_invariant(mk):
    import random

    from remembra.crew.reaper import CrewReaper

    seen: set[tuple[str, str]] = set()
    for seed in range(8):
        rng = random.Random(seed)
        env = await mk()
        h, htok = await host(env, f"h{seed}")
        a = await _join(env, "s-a", host_id=h["id"], host_token=htok)
        zone = await add_zone(env, CREW)
        task = await add_task(env, CREW, 1, owner=a.session, zones=[zone])
        claim = await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
        reaper = CrewReaper(env.svc)
        for step in range(12):
            action = rng.choice(["silence", "stall", "heartbeat", "orphan_rejoin", "sweep"])
            if action == "silence":
                env.clock.advance(rng.choice([200, 700, 1900]))
                await reaper.sweep()
            elif action == "stall":
                await env.svc.stall(
                    a.session,
                    error=rng.choice(["billing_error", "rate_limit"]),
                    facts={"uncommitted_files": ["x"]},
                    baton_ref=None,
                )
            elif action == "heartbeat":
                env.clock.advance(5)
                item = hb_item(a.session, a.session_token, age=1)
                await env.svc.heartbeat(user_id=OWNER, host=h, body={"batch_id": f"{seed}-{step}", "sessions": [item]})
            elif action == "orphan_rejoin":
                s = await get_session(env.conn, a.session["id"])
                if s["state"] not in ("lost", "ended"):
                    await env.svc.leave(a.session, reason="process_exited", facts={}, summary=None, baton=False, baton_ref=None)
                env.clock.advance(5)
                await env.svc.heartbeat(
                    user_id=OWNER,
                    host=h,
                    body={"batch_id": f"{seed}-{step}-r", "sessions": [hb_item(a.session, a.session_token, age=1)]},
                )
            else:
                await reaper.sweep()
            await _assert_report_invariant(env)
            s = await get_session(env.conn, a.session["id"])
            c = await env.one("SELECT state, epoch FROM crew_claims WHERE id = ?", (claim,))
            t = await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,))
            seen.add((s["state"], t["status"]))
            if s["state"] == "active":
                # back and nobody adopted: the claim is held again and the task is live again
                assert c["state"] == "active" and t["status"] == "in_progress", (seed, step, action, s["state"], c, t)
            if s["state"] in ("lost", "quota_blocked"):
                assert c["state"] in ("reserved", "active") and t["status"] in ("stalled", "in_progress"), (seed, step, action)
            if s["state"] in ("active", "idle"):
                open_batons = await env.all(
                    "SELECT 1 FROM crew_inbox_items WHERE ref_id = ? AND kind LIKE 'baton_%' AND state = 'open'", (task,)
                )
                if c["state"] == "active":
                    assert open_batons == [], (seed, step, action)
        await env.chain_ok(CREW)
    # the sequences really went through stall, loss and recovery
    assert {("quota_blocked", "stalled"), ("lost", "stalled"), ("active", "in_progress")} <= seen, seen


async def test_joined_event_reports_observe_only(mk):
    env = await mk(max_live=1)
    await _join(env, "s-a")
    await _join(env, "s-b", agent="codex")
    flags = [e["payload"]["observe_only"] for e in await env.events(CREW, types=("session.joined",))]
    assert flags == [False, True]


async def test_clean_leave_releases_its_parked_reservations(mk):
    env = await mk()
    a = await _join(env, "s-a")
    claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW))
    await env.conn.execute(
        "UPDATE crew_claims SET state = 'reserved', reserve_reason = 'idle', reserved_for = holder_session_id WHERE id = ?",
        (claim,),
    )
    await env.conn.commit()
    res = await env.svc.leave(a.session, reason="logout", facts={}, summary=None, baton=False, baton_ref=None)
    assert res["claims_released"] == [claim]


async def test_stall_supersedes_an_earlier_current_report(mk):
    env = await mk()
    a, _, task, _ = await _holder_with_task(env)
    await env.conn.execute(
        """INSERT INTO crew_reports (id, crew_id, task_id, session_id, kind, verdict, facts_source, facts_hash,
               is_current, created_at)
           VALUES ('rpt_earlier', ?, ?, ?, 'partial', 'partial', 'agent-declared', 'h0', 1, '2026-09-25T19:00:00.000Z')""",
        (CREW, task, a.session["id"]),
    )
    await env.conn.execute("UPDATE crew_tasks SET current_report_id = 'rpt_earlier' WHERE id = ?", (task,))
    await env.conn.commit()
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    old = await env.one("SELECT is_current, superseded_reason FROM crew_reports WHERE id = 'rpt_earlier'")
    assert old == {"is_current": 0, "superseded_reason": "replaced_by_stalled"}
    await _assert_report_invariant(env)
    assert [e["payload"]["report"]["id"] for e in await env.events(CREW, types=("report.superseded",))] == ["rpt_earlier"]


async def test_inbox_items_coalesce_by_dedupe_key(mk):
    from remembra.crew.events import Actor

    env = await mk()
    await _join(env, "s-a")
    ids = []
    for _ in range(3):
        async with env.log.transaction() as tx:
            ids.append(
                await env.svc._inbox_upsert(
                    tx,
                    CREW,
                    audience="project",
                    recipient=None,
                    kind="stuck_agent",
                    title="cc-1 looks stuck",
                    ref_type="session",
                    ref_id=None,
                    priority=1,
                    primary_action=None,
                    dedupe_key="stuck:cc-1",
                    actor=Actor.system(),
                    origin="server",
                    now=env.clock(),
                )
            )
    assert len(set(ids)) == 1
    row = await env.one("SELECT coalesced_count, created_seq FROM crew_inbox_items WHERE id = ?", (ids[0],))
    assert row["coalesced_count"] == 3 and row["created_seq"]
    assert len(await env.events(CREW, types=("inbox.item_created",))) == 1
