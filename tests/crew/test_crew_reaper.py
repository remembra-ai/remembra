"""WP-4 reaper: presence thresholds on the server clock, boot grace, host-wide silence,
lease expiry and fencing, idle park, reservation expiry and baton notices, lost synthesis
and recovery, idempotent sweeps, and the startup hook. Real crew.db, real event log.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI

from remembra.core.tasks import TaskRegistry
from remembra.crew import startup
from remembra.crew.events import format_ts
from remembra.crew.reaper import CrewReaper, presence_target
from remembra.crew.sessions import get_session
from remembra.crew.settings import default_settings
from remembra.crew.store import crew_id_for
from tests.crew.sessions_support import OWNER, PROJECT, T0, add_claim, add_task, add_zone, hb_item, host, join_req, make_env

CREW = crew_id_for(OWNER, PROJECT)
SETTINGS = default_settings()


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
    return await env.svc.join(user_id=OWNER, req=join_req(sid, **kw), host_token=host_token)


def _ts(seconds_ago: float) -> str:
    return format_ts(T0 - timedelta(seconds=seconds_ago))


BOOT_LONG_AGO = T0 - timedelta(days=1)


@pytest.mark.parametrize(
    ("row", "host_seen_ago", "expected"),
    [
        ({"host_id": "hst_1", "last_heartbeat_at": _ts(10), "last_activity_at": _ts(10)}, 10, ("active", None, None)),
        ({"host_id": "hst_1", "last_heartbeat_at": _ts(10), "last_activity_at": _ts(200)}, 10, ("idle", None, None)),
        (
            {"host_id": "hst_1", "last_heartbeat_at": _ts(200), "last_activity_at": _ts(200)},
            200,
            ("quiet", "host_unreachable", None),
        ),
        ({"host_id": "hst_1", "last_heartbeat_at": _ts(1900), "last_activity_at": _ts(1900)}, 1900, ("lost", None, "host_lost")),
        # host alive, this session dropped out of its heartbeats: quiet, then lost at lease expiry
        (
            {"host_id": "hst_1", "last_heartbeat_at": _ts(200), "last_activity_at": _ts(200)},
            5,
            ("quiet", "host_unreachable", None),
        ),
        ({"host_id": "hst_1", "last_heartbeat_at": _ts(700), "last_activity_at": _ts(700)}, 5, ("lost", None, "lease_expired")),
        # MCP-only: calls are the signal
        ({"host_id": None, "last_seen_at": _ts(60), "last_activity_at": _ts(60)}, None, ("active", None, None)),
        ({"host_id": None, "last_seen_at": _ts(600), "last_activity_at": _ts(600)}, None, ("idle", None, None)),
        ({"host_id": None, "last_seen_at": _ts(1300), "last_activity_at": _ts(1300)}, None, ("quiet", "mcp_silent", None)),
        ({"host_id": None, "last_seen_at": _ts(3700), "last_activity_at": _ts(3700)}, None, ("lost", None, "mcp_silent")),
    ],
)
def test_presence_thresholds_table(row, host_seen_ago, expected):
    host_row = {"last_seen_at": _ts(host_seen_ago)} if host_seen_ago is not None else None
    target = presence_target({"joined_at": _ts(4000), **row}, host_row, SETTINGS, now=T0, boot_at=BOOT_LONG_AGO)
    assert (target.state, target.quiet_reason, target.lost_reason) == expected


def test_presence_measures_from_boot_when_the_server_restarted():
    row = {"host_id": "hst_1", "joined_at": _ts(9000), "last_heartbeat_at": _ts(4000), "last_activity_at": _ts(4000)}
    target = presence_target(row, {"last_seen_at": _ts(4000)}, SETTINGS, now=T0, boot_at=T0 - timedelta(seconds=30))
    assert target.state == "active"  # nothing could heartbeat while the server was down


async def test_boot_grace_does_not_mass_mark_sessions(mk):
    env = await mk()
    h, tok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=tok)
    claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW), lease_s=600)
    env.clock.advance(3600)
    env.svc.boot_at = env.clock.now - timedelta(seconds=20)  # the server just restarted
    report = await CrewReaper(env.svc).sweep()
    assert report.errors == []
    assert (report.hosts_unreachable, report.lost, report.leases_expired, report.fenced) == (0, 0, 0, 0)
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "active"
    assert (await get_session(env.conn, a.session["id"]))["state"] == "active"


async def test_host_wide_silence_is_not_a_stall_until_host_lost(mk):
    env = await mk()
    h, tok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=tok)
    b = await _join(env, "s-b", host_id=h["id"], host_token=tok, checkout="fp-b")
    zone = await add_zone(env, CREW)
    task = await add_task(env, CREW, 1, owner=a.session, zones=[zone])
    claim = await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
    reaper = CrewReaper(env.svc)

    env.clock.advance(200)
    r = await reaper.sweep()
    assert r.hosts_unreachable == 1 and r.state_changes == 2 and r.lost == 0
    unreachable = await env.events(CREW, types=("host.unreachable",))
    assert unreachable[0]["moment"] and sorted(unreachable[0]["payload"]["session_ids"]) == sorted(
        [a.session["id"], b.session["id"]]
    )
    assert (await get_session(env.conn, a.session["id"]))["quiet_reason"] == "host_unreachable"

    env.clock.advance(360)  # 560 s: past the fence horizon (lease - 60 s), before expiry
    r = await reaper.sweep()
    assert r.fenced == 1 and r.leases_expired == 0
    r = await reaper.sweep()
    assert r.fenced == 0  # once per lease
    fenced = await env.events(CREW, types=("claim.fenced",))
    assert len(fenced) == 1 and fenced[0]["payload"]["claim_id"] == claim

    env.clock.advance(60)  # 620 s: lease expired
    r = await reaper.sweep()
    assert r.leases_expired == 1
    c = await env.one("SELECT * FROM crew_claims WHERE id = ?", (claim,))
    assert (c["state"], c["reserve_reason"], c["reserved_for"]) == ("reserved", "offline", a.session["id"])
    t = await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,))
    assert t["status"] == "in_progress"  # not stalled while the host is only unreachable
    assert await env.one("SELECT 1 AS x FROM crew_reports") is None
    assert await env.one("SELECT 1 AS x FROM crew_outbox") is None
    # an offline reservation is never offered to another session
    other = await _join(env, "s-c", checkout="fp-c")
    assert other.batons_offered == [] and other.auto_adopted == []

    env.clock.advance(1200)  # 1820 s > host_lost_after_s
    r = await reaper.sweep()
    assert r.lost == 2
    s = await get_session(env.conn, a.session["id"])
    assert (s["state"], s["state_reason"]) == ("lost", "host_lost")
    c = await env.one("SELECT reserve_reason, reserved_for FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"reserve_reason": "lost", "reserved_for": a.session["id"]}  # now a baton others can be offered
    t = await env.one("SELECT status, current_report_id FROM crew_tasks WHERE id = ?", (task,))
    assert t["status"] == "stalled"
    rep = await env.one("SELECT kind, facts_source, is_current FROM crew_reports WHERE id = ?", (t["current_report_id"],))
    assert rep == {"kind": "stalled", "facts_source": "server-inferred", "is_current": 1}
    handoffs = [json.loads(r["payload"]) for r in await env.all("SELECT payload FROM crew_outbox WHERE kind = 'relay_handoff'")]
    assert {p["end_reason"] for p in handoffs} == {"auto:silent"} and len(handoffs) == 2
    assert await env.one("SELECT 1 AS x FROM crew_inbox_items WHERE kind = 'baton_available' AND ref_id = ?", (task,))
    late = await env.svc.join(user_id=OWNER, req=join_req("s-d", checkout="fp-d"))
    assert [o["claim_id"] for o in late.batons_offered] == [claim]
    # A second sweep changes nothing (idempotent)
    before = await env.last_seq(CREW)
    r = await reaper.sweep()
    assert r.as_dict() | {"errors": []} == type(r)().as_dict()
    assert await env.last_seq(CREW) == before

    # The host comes back: recovery re-takes, restores and supersedes
    env.clock.advance(10)
    res = await env.svc.heartbeat(
        user_id=OWNER,
        host=h,
        body={"batch_id": "back", "sessions": [hb_item(a.session, a.session_token), hb_item(b.session, b.session_token)]},
    )
    assert res["per_session"][a.session["id"]]["state"] == "active"
    assert [e["type"] for e in await env.events(CREW, types=("host.recovered",))] == ["host.recovered"]
    c = await env.one("SELECT state, epoch FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "active", "epoch": 2}
    t = await env.one("SELECT status, current_report_id FROM crew_tasks WHERE id = ?", (task,))
    assert t == {"status": "in_progress", "current_report_id": None}
    rec = [e for e in await env.events(CREW, types=("session.recovered",)) if e["payload"]["from"] == "lost"]
    assert len(rec) == 2
    await env.chain_ok(CREW)


async def test_short_outage_retakes_offline_reservations_without_loss(mk):
    env = await mk()
    h, tok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=tok)
    claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW))
    reaper = CrewReaper(env.svc)
    env.clock.advance(700)
    await reaper.sweep()
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "reserved"
    await env.svc.heartbeat(
        user_id=OWNER, host=h, body={"batch_id": "x", "sessions": [hb_item(a.session, a.session_token, age=600)]}
    )
    c = await env.one("SELECT state, epoch, reserve_reason FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "active", "epoch": 2, "reserve_reason": None}
    s = await get_session(env.conn, a.session["id"])
    assert s["state"] == "idle"  # alive, but no new activity
    assert await env.events(CREW, types=("session.lost", "session.recovered")) == []


async def test_idle_park_and_retake_on_activity(mk):
    env = await mk()
    h, tok = await host(env)
    a = await _join(env, "s-a", host_id=h["id"], host_token=tok)
    claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW))
    reaper = CrewReaper(env.svc)
    # alive every minute but no tool activity for an hour
    for i in range(62):
        env.clock.advance(60)
        await env.svc.heartbeat(
            user_id=OWNER, host=h, body={"batch_id": f"b{i}", "sessions": [hb_item(a.session, a.session_token, age=60 * (i + 1))]}
        )
        await reaper.sweep()
    s = await get_session(env.conn, a.session["id"])
    assert s["state"] == "idle"
    c = await env.one("SELECT state, reserve_reason, reserved_for FROM crew_claims WHERE id = ?", (claim,))
    assert c == {"state": "reserved", "reserve_reason": "idle", "reserved_for": a.session["id"]}
    notice = await env.one(
        "SELECT kind, priority, audience FROM crew_inbox_items WHERE dedupe_key = ?", (f"idle_park:{a.session['id']}",)
    )
    assert notice == {"kind": "idle_park", "priority": 3, "audience": "project"}
    env.clock.advance(30)
    await env.svc.heartbeat(
        user_id=OWNER, host=h, body={"batch_id": "act", "sessions": [hb_item(a.session, a.session_token, age=1)]}
    )
    assert (await env.one("SELECT state, epoch FROM crew_claims WHERE id = ?", (claim,))) == {"state": "active", "epoch": 2}
    assert (await get_session(env.conn, a.session["id"]))["state"] == "active"


async def test_reservation_expiry_and_task_baton_notices(mk):
    env = await mk()
    a = await _join(env, "s-a")
    free_claim = await add_claim(env, CREW, a.session, zone_id=await add_zone(env, CREW, "docs"))
    zone = await add_zone(env, CREW)
    task = await add_task(env, CREW, 7, owner=a.session, zones=[zone])
    task_claim = await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    reaper = CrewReaper(env.svc)
    env.clock.advance(24 * 3600 + 60)
    r = await reaper.sweep()
    assert r.reservations_expired == 1 and r.baton_notices == 1
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (free_claim,)))["state"] == "expired"
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (task_claim,)))["state"] == "reserved"
    r = await reaper.sweep()
    assert r.baton_notices == 0
    env.clock.advance(48 * 3600)
    r = await reaper.sweep()
    assert r.baton_notices == 1
    notices = await env.all(
        "SELECT dedupe_key, priority, title FROM crew_inbox_items WHERE kind = 'baton_waiting' ORDER BY priority DESC"
    )
    assert [n["dedupe_key"] for n in notices] == [f"baton_waiting:{task}:24h", f"baton_waiting:{task}:72h"]
    assert notices[1]["priority"] == 1 and "T-7" in notices[1]["title"]
    # an adopter resolves every waiting notice for the task
    b = await _join(env, "s-b", checkout="fp-a")
    assert b.auto_adopted and b.auto_adopted[0]["task_id"] == task
    open_items = await env.all("SELECT kind FROM crew_inbox_items WHERE ref_id = ? AND state = 'open'", (task,))
    assert open_items == []


async def test_owner_set_task_baton_expiry_is_honoured(mk):
    env = await mk()
    await env.svc.join(user_id=OWNER, req=join_req("s-0"))
    await env.svc.store.patch_settings(CREW, {"task_baton_expiry": 86400}, if_match=1)
    a = await _join(env, "s-a", checkout="fp-z")
    zone = await add_zone(env, CREW)
    task = await add_task(env, CREW, 1, owner=a.session, zones=[zone])
    claim = await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
    await env.svc.stall(a.session, error="billing_error", facts={}, baton_ref=None)
    env.clock.advance(86400 + 30)
    await CrewReaper(env.svc).sweep()
    assert (await env.one("SELECT state FROM crew_claims WHERE id = ?", (claim,)))["state"] == "expired"


async def test_mcp_session_goes_quiet_then_lost_and_stale_lost_sessions_close(mk):
    env = await mk()
    m = await _join(env, "mcp-1", agent="cursor", client_kind="mcp", checkout=None)
    reaper = CrewReaper(env.svc)
    env.clock.advance(21 * 60)
    await reaper.sweep()
    s = await get_session(env.conn, m.session["id"])
    assert (s["state"], s["quiet_reason"]) == ("quiet", "mcp_silent")
    env.clock.advance(40 * 60)
    r = await reaper.sweep()
    assert r.lost == 1
    assert (await get_session(env.conn, m.session["id"]))["state_reason"] == "mcp_silent"
    env.clock.advance(25 * 3600)
    r = await reaper.sweep()
    assert r.lost_closed == 1
    s = await get_session(env.conn, m.session["id"])
    assert (s["state"], s["end_reason"]) == ("ended", "lost_expired")


async def test_lost_session_holding_a_baton_is_kept(mk):
    env = await mk()
    m = await _join(env, "mcp-1", agent="cursor", client_kind="mcp", checkout=None)
    zone = await add_zone(env, CREW)
    task = await add_task(env, CREW, 3, owner=m.session, zones=[zone])
    await add_claim(env, CREW, m.session, zone_id=zone, task_id=task)  # task batons never expire (D5)
    reaper = CrewReaper(env.svc)
    env.clock.advance(3700)
    await reaper.sweep()
    env.clock.advance(25 * 3600)
    r = await reaper.sweep()
    assert r.lost_closed == 0 and (await get_session(env.conn, m.session["id"]))["state"] == "lost"


async def test_reaper_hook_runs_in_the_app_lifespan(tmp_path, monkeypatch):
    env = await make_env(tmp_path)
    try:
        app = FastAPI()
        app.state.tasks = TaskRegistry()
        app.state.crew_db = env.db
        monkeypatch.setattr(startup, "HOOK_MODULES", ("remembra.crew.reaper",))
        # only the bus and the reaper: the db is provided, the outbox needs the main lifespan
        for name in ("crew.tailer", "crew.retention", "crew.db", "crew.outbox"):
            startup._HOOKS.pop(name, None)
        try:
            await startup.start(app)
            assert "crew-reaper" in app.state.tasks.names()
            assert app.state.crew_reaper is not None and app.state.crew_sessions.log is app.state.crew_events
            await startup.stop(app)
            assert app.state.crew_sessions is None and app.state.crew_reaper is None
        finally:
            import remembra.crew.db_hook as db_hook

            startup.add_hook("crew.tailer", order=20, start=startup._start_tailer, stop=startup._stop_tailer)
            startup.add_hook("crew.retention", order=30, start=startup._start_retention, stop=startup._stop_retention)
            db_hook.register_hooks()
            await app.state.tasks.shutdown(timeout=2.0)
    finally:
        await env.db.close()
