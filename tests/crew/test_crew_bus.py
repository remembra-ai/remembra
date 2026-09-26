"""WP-2 CrewBus and DB tailer: per-crew seq order, (crew_id, seq) dedupe, cross-connection tailing."""

from __future__ import annotations

import pytest

from remembra.api.v1.websocket import _load_crew_ref
from remembra.auth.middleware import AuthenticatedUser
from remembra.crew.bus import CrewBus, CrewEventTailer, readable_crews, summary_items, db_loader
from remembra.crew.events import Actor, CrewEventLog, format_ts, utc_now
from remembra.storage.database import Database
from tests.crew.crewdb import CREW_A, CREW_B, open_crew_db, seed_crew, seed_member, seed_session, state_changed


@pytest.fixture
async def crewdb(tmp_path):
    db = await open_crew_db(tmp_path)
    await seed_crew(db, CREW_A)
    await seed_crew(db, CREW_B, owner="owner-2", project="other")
    try:
        yield db
    finally:
        await db.close()


def _kw(crew_id=CREW_A, **over):
    base = dict(
        crew_id=crew_id,
        type="session.state_changed",
        actor=Actor.system(),
        payload=state_changed(),
        summary="state changed",
    )
    base.update(over)
    return base


async def test_dedupe_and_out_of_order_backfill(crewdb):
    log = CrewEventLog(crewdb)  # no bus: we publish by hand
    envs = [(await log.emit(**_kw())).envelope for _ in range(4)]
    bus = CrewBus(loader=db_loader(crewdb))
    seen = []
    bus.subscribe(lambda e: seen.append(e["seq"]))
    assert await bus.publish([envs[0]]) == 1
    # seq 4 arrives before 2 and 3 (post-commit publishes interleaved): 2 and 3 are back-filled from the DB
    assert await bus.publish([envs[3]]) == 3
    assert await bus.publish([envs[1], envs[2], envs[3]]) == 0  # all duplicates
    assert seen == [1, 2, 3, 4]
    assert bus.backfilled == 2 and bus.duplicates == 3 and bus.last_seq(CREW_A) == 4


async def test_listener_failure_is_isolated_and_unsubscribe(crewdb):
    bus = CrewBus()
    good = []

    def bad(_env):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    unsubscribe = bus.subscribe(good.append)
    log = CrewEventLog(crewdb, bus)
    await log.emit(**_kw())
    assert len(good) == 1
    unsubscribe()
    await log.emit(**_kw())
    assert len(good) == 1


async def test_tailer_delivers_events_written_by_another_connection_once(crewdb, tmp_path):
    # "Process" A writes with its own connection and no in-process bus.
    writer = CrewEventLog(crewdb)
    await writer.emit(**_kw())  # before the tailer starts: never replayed
    # "Process" B: its own connection to the same crew.db, a bus, and the tailer.
    reader_db = Database(str(tmp_path / "crew.db"))
    await reader_db.connect()
    try:
        bus = CrewBus(loader=db_loader(reader_db))
        seen = []
        bus.subscribe(lambda e: seen.append((e["crew_id"], e["seq"])))
        tailer = CrewEventTailer(reader_db, bus, interval_s=0.01, batch=2)
        await tailer.prime()
        await writer.emit(**_kw())
        await writer.emit(**_kw(crew_id=CREW_B))
        await writer.emit(**_kw())
        assert await tailer.poll_once() == 2  # batch of 2 rows
        assert await tailer.poll_once() == 1
        assert await tailer.poll_once() == 0
        assert seen == [(CREW_A, 2), (CREW_B, 1), (CREW_A, 3)]
        # Publisher and tailer both active on one bus: every event still delivered exactly once.
        both = CrewEventLog(reader_db, bus)
        await both.emit(**_kw())
        assert await tailer.poll_once() == 0
        assert seen[-1] == (CREW_A, 4) and len(seen) == 4
    finally:
        await reader_db.close()


def _user(user_id: str, *, projects: list[str] | None = None, role: str = "editor") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=user_id, api_key_id="key_1", rate_limit_tier="standard", role=role, project_ids=projects)


async def test_membership_read_model(crewdb):
    await seed_member(crewdb, CREW_B, "owner-1", "member")
    await seed_member(crewdb, CREW_A, "watcher", "viewer")
    # The WebSocket resolves crews through WP-14's load_crew (same ACL as REST).
    assert (await _load_crew_ref(crewdb.conn, CREW_A, _user("owner-1"), "crew:read")).project_id == "yaadbooks"
    assert (await _load_crew_ref(crewdb.conn, CREW_B, _user("owner-1"), "crew:read")).crew_id == CREW_B
    assert await _load_crew_ref(crewdb.conn, CREW_A, _user("stranger"), "crew:read") is None
    assert await _load_crew_ref(crewdb.conn, CREW_A, _user("owner-1", projects=["other"]), "crew:read") is None  # restricted key
    assert await _load_crew_ref(crewdb.conn, "crw_missing00000000", _user("owner-1"), "crew:read") is None
    # A viewer-role member can read but can never push presence (crew:write).
    assert await _load_crew_ref(crewdb.conn, CREW_A, _user("watcher"), "crew:read") is not None
    assert await _load_crew_ref(crewdb.conn, CREW_A, _user("watcher"), "crew:write") is None
    # A viewer API key has no crew:write either, even as the crew owner.
    assert await _load_crew_ref(crewdb.conn, CREW_A, _user("owner-1", role="viewer"), "crew:write") is None
    assert [c.crew_id for c in await readable_crews(crewdb.conn, "owner-1", None)] == [CREW_A, CREW_B]
    assert [c.crew_id for c in await readable_crews(crewdb.conn, "owner-1", ["other"])] == [CREW_B]
    assert await readable_crews(crewdb.conn, "stranger", None) == []


async def test_summary_counts(crewdb):
    await seed_session(crewdb, CREW_A, "cs_one", state="active")
    await seed_session(crewdb, CREW_A, "cs_two", callsign="cc-2", state="idle")
    await seed_session(crewdb, CREW_A, "cs_old", callsign="cc-3", state="ended")
    now = format_ts(utc_now())
    async with crewdb.transaction():
        for i, (aud, state) in enumerate([("project", "open"), ("project", "resolved"), ("crew", "open")]):
            await crewdb.conn.execute(
                "INSERT INTO crew_inbox_items (id, crew_id, audience, kind, title, state, dedupe_key, created_at, updated_at) "
                "VALUES (?, ?, ?, 'baton_available', 't', ?, ?, ?, ?)",
                (f"inb_{i}", CREW_A, aud, state, f"k{i}", now, now),
            )
    log = CrewEventLog(crewdb)
    await log.emit(**_kw(type="crew.mode_changed", payload={"from": "solo", "to": "multi", "live_sessions": 2}))
    await log.emit(**_kw())
    refs = await readable_crews(crewdb.conn, "owner-1", None)
    items = await summary_items(crewdb.conn, refs)
    assert items == [
        {"crew_id": CREW_A, "project_id": "yaadbooks", "mode": "multi", "live": 2, "moments": 1, "needs_you": 1},
    ]
