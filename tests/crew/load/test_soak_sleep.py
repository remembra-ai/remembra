"""Sleep soak, scripted (spec §13.4 "24 h soak on macOS with lid close/open"; WP-15).

A simulated working day on the real crew stack (``crew.db``, event log, :class:`CrewSessions`, the
:class:`CrewReaper` sweeping every 30 s and the real-time :class:`NotificationDispatcher`), with a
controllable server clock: one laptop (one host) with an agent holding POS for T-1 heartbeats every
60 s while awake and goes silent while the lid is closed. Sleeps of 5, 19, 25 and 29.5 minutes and
one night of 8 hours.

Asserted, per §13.4 and §10.1:

* a sleep under 30 min never makes the session ``lost`` (only quiet, ``host_unreachable``), never
  stalls the task and never sends a real-time notification (none under 20 min, D-rule §9.11);
* on wake the host recovers, the session is active again and POS is held by the same session;
* the night: ``lost`` (``host_lost``) after 30 min, the Needs-you baton and one real-time handoff
  alert, then on wake ``session.recovered`` re-takes POS and restores T-1 (nothing adopted it).

``tests/crew/load/soak_macos.py`` is the real 24 h run (lid actually closed) for the Mac.
"""

from __future__ import annotations

import json

from remembra.crew.events import format_ts
from remembra.crew.notify import KIND_CREW_NOTIFY, NotificationDispatcher
from remembra.crew.reaper import CrewReaper
from remembra.crew.sessions import get_session
from remembra.crew.store import crew_id_for
from tests.crew.sessions_support import OWNER, PROJECT, add_claim, add_task, add_zone, hb_item, host, join_req, make_env
from tests.crew.wp7_support import set_realtime

CREW = crew_id_for(OWNER, PROJECT)
MIN = 60

DAY = [
    ("work", 120 * MIN),
    ("sleep", 5 * MIN),
    ("work", 30 * MIN),
    ("sleep", 19 * MIN),
    ("work", 60 * MIN),
    ("sleep", 25 * MIN),
    ("work", 10 * MIN),
    ("sleep", int(29.5 * MIN)),
    ("work", 40 * MIN),
    ("sleep", 8 * 60 * MIN),
    ("work", 30 * MIN),
]


async def test_a_simulated_day_of_lid_close_and_open(tmp_path):
    env = await make_env(tmp_path)
    try:
        h, tok = await host(env)
        a = await env.svc.join(user_id=OWNER, req=join_req("s-a", host_id=h["id"]), host_token=tok)
        sid = a.session["id"]
        zone = await add_zone(env, CREW)
        task = await add_task(env, CREW, 1, owner=a.session, zones=[zone])
        claim = await add_claim(env, CREW, a.session, zone_id=zone, task_id=task)
        await set_realtime(env.db, CREW, ["email", "webhook"])
        async with env.db.transaction():
            await env.conn.execute(
                "INSERT INTO crew_notify_targets (id, user_id, kind, target, secret_hash, verified_at, created_at)"
                " VALUES ('ntt_soak', ?, 'webhook', 'https://hooks.example.com/x', 'h', ?, ?)",
                (OWNER, format_ts(env.clock()), format_ts(env.clock())),
            )
        reaper = CrewReaper(env.svc)
        dispatcher = NotificationDispatcher(env.db)

        async def notifications() -> int:
            await dispatcher.run_once(now=env.clock())
            row = await env.one("SELECT COUNT(*) AS n FROM crew_outbox WHERE kind = ?", (KIND_CREW_NOTIFY,))
            return int(row["n"]) if row else 0

        async def heartbeat(batch: str) -> dict:
            return await env.svc.heartbeat(
                user_id=OWNER, host=h, body={"batch_id": batch, "sessions": [hb_item(a.session, a.session_token, age=5)]}
            )

        async def advance(seconds: int, awake: bool) -> None:
            for step in range(0, seconds, 30):
                env.clock.advance(30)
                if awake and step % 60 == 0:
                    await heartbeat(f"hb-{format_ts(env.clock())}")
                report = await reaper.sweep()
                assert report.errors == [], report.errors
                await dispatcher.run_once(now=env.clock())  # the dispatcher runs all the time (bus wake-ups, polling)

        seen = 0
        for i, (phase, seconds) in enumerate(DAY):
            if phase == "work":
                await advance(seconds, awake=True)
                continue
            before = await env.last_seq(CREW)
            sent_before = await notifications()
            await advance(seconds, awake=False)
            during = await env.events(CREW, after=before)
            await heartbeat(f"wake-{i}")  # lid open: crewd's first heartbeat
            await reaper.sweep()
            woke = await env.events(CREW, after=before)
            types = [e["type"] for e in woke]
            s = await get_session(env.conn, sid)
            c = await env.one("SELECT state, holder_session_id, epoch FROM crew_claims WHERE id = ?", (claim,))
            t = await env.one("SELECT status FROM crew_tasks WHERE id = ?", (task,))
            sent = await notifications() - sent_before
            assert s["state"] == "active", (seconds, s["state"], types)
            assert c["state"] == "active" and c["holder_session_id"] == sid, (seconds, c, types)
            assert t["status"] == "in_progress", (seconds, t, types)
            if seconds < 30 * MIN:
                assert "session.lost" not in [e["type"] for e in during], (seconds, types)
                assert "task.stalled" not in types and "report.submitted" not in types, (seconds, types)
                if seconds > 3 * MIN:
                    assert "host.unreachable" in types and "host.recovered" in types, (seconds, types)
                assert sent == 0, (seconds, sent, types)  # no false alerts for a closed lid
            else:  # the night
                lost = [e for e in during if e["type"] == "session.lost"]
                assert lost and lost[0]["payload"]["reason"] == "host_lost", types
                assert "task.stalled" in types and "inbox.item_created" in types, types
                recovered = [e for e in woke if e["type"] == "session.recovered"]
                assert recovered and recovered[-1]["payload"]["from"] == "lost", types
                assert task in recovered[-1]["payload"]["tasks_restored"] and claim in recovered[-1]["payload"]["claims_retaken"]
                assert sent == 1, sent  # one handoff alert for the long loss, sent at the loss
                row = await env.one("SELECT payload FROM crew_outbox WHERE kind = ? ORDER BY rowid DESC", (KIND_CREW_NOTIFY,))
                assert row is not None, row
                items = json.loads(row["payload"])["items"]
                assert [(i["event_type"], i["kind"]) for i in items] == [("session.lost", "handoff")], items
            seen += 1
        assert seen == 5
        await env.chain_ok(CREW)
    finally:
        await env.db.close()


def test_the_mac_soak_judges_each_sleep_by_the_same_rules():
    """``soak_macos.judge`` (the real 24 h run's verdict) applies the rules asserted above."""
    from tests.crew.load.soak_macos import judge

    held = {"state": "active", "claims": [("zn_pos", "active")]}
    assert judge(10 * MIN, [{"type": "host.unreachable"}, {"type": "host.recovered"}], held, 0)["ok"]
    assert not judge(10 * MIN, [], held, 1)["ok"]  # an alert for a 10-minute lid close is false
    assert not judge(25 * MIN, [{"type": "session.lost"}], held, 0)["ok"]  # lost before 30 min
    assert not judge(25 * MIN, [], {"state": "active", "claims": [("zn_pos", "reserved")]}, 0)["ok"]
    assert judge(25 * MIN, [], held, 1)["ok"]  # 20-30 min: an alert is allowed, a loss is not
    night = [{"type": "session.lost"}, {"type": "session.recovered"}]
    assert judge(8 * 3600, night, held, 1)["ok"]
    assert not judge(8 * 3600, [{"type": "session.lost"}], held, 1)["ok"]  # never recovered
