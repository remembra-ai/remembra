"""The Marshal desk's ledger: hard, fail-closed caps in integer micro-dollars (plan section 5).

Real SQLite (``Database``, migration 11), real concurrency: many reserves in
one ``asyncio.gather`` against one connection, and two connections on one
file. Clocks are frozen where a day or month boundary is the point.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remembra.marshal.desk import budget
from remembra.marshal.desk.budget import (
    BudgetUnavailable,
    DailyAskLimit,
    DailyBudgetSpent,
    MonthlyBudgetSpent,
    Reservation,
    reserve,
    settle,
    snapshot,
)
from remembra.marshal.desk.constants import ASK_RESERVE_MICRO, PRUNE_BATCH, RESERVATION_TTL_S
from remembra.storage.database import Database

NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def _settings(day: float = 5.0, month: float = 100.0, asks: int = 40) -> SimpleNamespace:
    return SimpleNamespace(marshal_daily_usd=day, marshal_monthly_usd=month, marshal_user_daily_asks=asks)


@pytest.fixture()
async def db(tmp_path: Path) -> Any:
    database = Database(str(tmp_path / "ledger.db"))
    await database.connect()
    await database.init_schema()
    yield database
    await database.close()


async def _rows(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor = await db.conn.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def _period(db: Database, period: str) -> dict[str, Any]:
    rows = await _rows(db, "SELECT reserved_micro, spent_micro, asks FROM marshal_budget WHERE period = ?", (period,))
    return rows[0] if rows else {}


async def test_concurrent_reserves_never_pass_the_day_cap(db: Database) -> None:
    settings = _settings(day=5.0)

    async def one(user: str) -> str:
        try:
            await reserve(db, user, NOW, settings)
            return "ok"
        except DailyBudgetSpent:
            return "spent"

    results = await asyncio.gather(*(one(f"u{u}") for _ in range(30) for u in range(10)))
    assert results.count("ok") == 250  # $5 / $0.02, exactly
    assert results.count("spent") == 50
    day = await _period(db, "day:2026-10-12")
    assert day["reserved_micro"] == 250 * ASK_RESERVE_MICRO <= 5_000_000
    assert day["spent_micro"] == 0 and day["asks"] == 250
    per_user = await _rows(db, "SELECT user_id, asks FROM marshal_user_day")
    assert sum(r["asks"] for r in per_user) == 250 and max(r["asks"] for r in per_user) <= 40
    assert (await _rows(db, "SELECT COUNT(*) AS n FROM marshal_reservations WHERE state = 'held'"))[0]["n"] == 250


async def test_two_connections_on_one_file_hold_the_cap(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    first, second = Database(path), Database(path)
    await first.connect()
    await first.init_schema()
    await second.connect()
    try:
        settings = _settings(day=1.0)  # 50 asks

        async def one(database: Database, user: str) -> str:
            try:
                await reserve(database, user, NOW, settings)
                return "ok"
            except DailyBudgetSpent:
                return "spent"
            except BudgetUnavailable:
                return "unavailable"

        calls = [one(first if i % 2 else second, f"u{i % 7}") for i in range(80)]
        results = await asyncio.gather(*calls)
        assert set(results) <= {"ok", "spent", "unavailable"}
        ok = results.count("ok")
        assert ok <= 50
        day = await _period(first, "day:2026-10-12")
        assert day["reserved_micro"] == ok * ASK_RESERVE_MICRO <= 1_000_000
        held = await _rows(second, "SELECT COUNT(*) AS n FROM marshal_reservations WHERE state = 'held'")
        assert held[0]["n"] == ok
    finally:
        await second.close()
        await first.close()


async def test_the_41st_ask_of_the_day_is_refused(db: Database) -> None:
    settings = _settings()
    for _ in range(40):
        await reserve(db, "u1", NOW, settings)
    with pytest.raises(DailyAskLimit) as refused:
        await reserve(db, "u1", NOW, settings)
    assert (refused.value.limit, refused.value.used) == (40, 40)
    assert refused.value.resets_at == datetime(2026, 10, 13, tzinfo=UTC)
    await reserve(db, "u2", NOW, settings)  # another account is unaffected
    assert (await snapshot(db, "u1", NOW)).user_asks == 40


async def test_the_month_cap_binds_when_the_day_cap_does_not(db: Database) -> None:
    settings = _settings(day=5.0, month=0.05)  # two asks a month
    await reserve(db, "u1", NOW, settings)
    await reserve(db, "u2", NOW, settings)
    with pytest.raises(MonthlyBudgetSpent) as refused:
        await reserve(db, "u3", NOW, settings)
    assert refused.value.resets_at == datetime(2026, 11, 1, tzinfo=UTC)
    month = await _period(db, "month:2026-10")
    assert month["reserved_micro"] == 2 * ASK_RESERVE_MICRO


async def test_settle_frees_the_reserve_records_real_dollars_and_runs_once(db: Database) -> None:
    settings = _settings()
    hold = await reserve(db, "u1", NOW, settings)
    assert hold.user_asks == 1 and hold.reserved_micro == ASK_RESERVE_MICRO
    assert await settle(db, hold, 0.000682, counted=True, now=NOW)
    day = await _period(db, "day:2026-10-12")
    assert day == {"reserved_micro": 0, "spent_micro": 682, "asks": 1}
    assert await _period(db, "month:2026-10") == {"reserved_micro": 0, "spent_micro": 682, "asks": 1}
    [row] = await _rows(db, "SELECT state, spent_micro, settled_at FROM marshal_reservations")
    assert row["state"] == "settled" and row["spent_micro"] == 682 and row["settled_at"]
    [user_day] = await _rows(db, "SELECT asks, spent_micro FROM marshal_user_day")
    assert user_day == {"asks": 1, "spent_micro": 682}
    # A second settle of the same hold changes nothing.
    assert not await settle(db, hold, 0.01, counted=True, now=NOW)
    assert await _period(db, "day:2026-10-12") == day

    # counted=False (no model call completed) gives the ask back.
    other = await reserve(db, "u1", NOW, settings)
    assert other.user_asks == 2
    assert await settle(db, other, 0.0, counted=False, now=NOW)
    assert await _period(db, "day:2026-10-12") == {"reserved_micro": 0, "spent_micro": 682, "asks": 1}
    assert (await snapshot(db, "u1", NOW)).user_asks == 1
    states = [r["state"] for r in await _rows(db, "SELECT state FROM marshal_reservations ORDER BY created_at, id")]
    assert sorted(states) == ["released", "settled"]


async def test_a_stale_hold_expires_at_its_full_reserve(db: Database) -> None:
    settings = _settings()
    crashed = await reserve(db, "u1", NOW, settings)
    later = NOW + timedelta(seconds=RESERVATION_TTL_S + 1)
    await reserve(db, "u2", later, settings)
    rows = {r["id"]: r for r in await _rows(db, "SELECT id, state, spent_micro FROM marshal_reservations")}
    assert rows[crashed.id]["state"] == "expired" and rows[crashed.id]["spent_micro"] == ASK_RESERVE_MICRO
    day = await _period(db, "day:2026-10-12")
    assert day["spent_micro"] == ASK_RESERVE_MICRO and day["reserved_micro"] == ASK_RESERVE_MICRO  # u2's hold
    # The crashed ask's late settle changes nothing: its dollars are counted once.
    assert not await settle(db, crashed, 0.001, counted=True, now=later)
    assert await _period(db, "day:2026-10-12") == day
    # The snapshot already showed it at full reserve (reserved + spent do not change on expiry).
    assert (await snapshot(db, "u1", later)).day_used_micro == 2 * ASK_RESERVE_MICRO


async def test_a_hold_inside_its_ttl_is_not_expired(db: Database) -> None:
    settings = _settings()
    live = await reserve(db, "u1", NOW, settings)
    await reserve(db, "u2", NOW + timedelta(seconds=RESERVATION_TTL_S - 1), settings)
    rows = {r["id"]: r["state"] for r in await _rows(db, "SELECT id, state FROM marshal_reservations")}
    assert rows[live.id] == "held"


async def test_utc_day_rollover_is_fixed_at_reserve(db: Database) -> None:
    settings = _settings(day=0.02)  # one ask a day
    just_before = datetime(2026, 10, 12, 23, 59, 59, 900000, tzinfo=UTC)
    hold = await reserve(db, "u1", just_before, settings)
    assert (hold.day, hold.month) == ("2026-10-12", "2026-10")
    with pytest.raises(DailyBudgetSpent) as refused:
        await reserve(db, "u2", just_before, settings)
    assert refused.value.resets_at == datetime(2026, 10, 13, tzinfo=UTC)
    after = datetime(2026, 10, 13, 0, 0, 1, tzinfo=UTC)
    assert await settle(db, hold, 0.001, counted=True, now=after)
    assert await _period(db, "day:2026-10-12") == {"reserved_micro": 0, "spent_micro": 1000, "asks": 1}
    fresh = await reserve(db, "u2", after, settings)  # a new day: a fresh cap
    assert fresh.day == "2026-10-13"
    assert await _period(db, "day:2026-10-13") == {"reserved_micro": ASK_RESERVE_MICRO, "spent_micro": 0, "asks": 1}
    [row] = await _rows(db, "SELECT spent_micro FROM marshal_user_day WHERE user_id = 'u1' AND day = '2026-10-12'")
    assert row["spent_micro"] == 1000


async def test_utc_month_rollover_is_fixed_at_reserve(db: Database) -> None:
    settings = _settings(month=0.02)  # one ask a month
    just_before = datetime(2026, 10, 31, 23, 59, 59, 900000, tzinfo=UTC)
    hold = await reserve(db, "u1", just_before, settings)
    with pytest.raises(MonthlyBudgetSpent) as refused:
        await reserve(db, "u2", just_before, settings)
    assert refused.value.resets_at == datetime(2026, 11, 1, tzinfo=UTC)
    after = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)
    await settle(db, hold, 0.002, counted=True, now=after)
    assert (await _period(db, "month:2026-10"))["spent_micro"] == 2000
    fresh = await reserve(db, "u2", after, settings)
    assert (fresh.day, fresh.month) == ("2026-11-01", "2026-11")
    assert budget.next_month_start(datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)


async def test_a_reserve_whose_sql_fails_is_refused_and_changes_nothing(db: Database) -> None:
    await db.conn.execute("DROP TABLE marshal_user_day")
    await db.conn.commit()
    with pytest.raises(BudgetUnavailable):
        await reserve(db, "u1", NOW, _settings())
    assert await _rows(db, "SELECT * FROM marshal_budget") == []
    assert await _rows(db, "SELECT * FROM marshal_reservations") == []


async def test_a_settle_that_fails_leaves_the_hold_for_expiry(db: Database) -> None:
    hold = await reserve(db, "u1", NOW, _settings())
    await db.conn.execute("DROP TABLE marshal_user_day")
    await db.conn.commit()
    with pytest.raises(Exception):  # noqa: B017 - any database error
        await settle(db, hold, 0.001, counted=True, now=NOW)
    [row] = await _rows(db, "SELECT state FROM marshal_reservations")
    assert row["state"] == "held"
    assert await _period(db, "day:2026-10-12") == {"reserved_micro": ASK_RESERVE_MICRO, "spent_micro": 0, "asks": 1}


async def test_pruning_is_bounded_per_call(db: Database) -> None:
    old = NOW - timedelta(days=40)
    stamp = old.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for i in range(PRUNE_BATCH + 200):
        await db.conn.execute(
            "INSERT INTO marshal_reservations (id, user_id, day, month, reserved_micro, spent_micro, state, created_at)"
            " VALUES (?, 'old', '2026-09-02', '2026-09', 20000, 100, 'settled', ?)",
            (uuid.uuid4().hex, stamp),
        )
        await db.conn.execute(
            "INSERT INTO marshal_user_day (user_id, day, asks, spent_micro) VALUES (?, '2026-09-02', 1, 0)", (f"u{i}",)
        )
    await db.conn.commit()

    async def count(sql: str) -> int:
        return int((await _rows(db, sql))[0]["n"])

    await reserve(db, "now", NOW, _settings())
    assert await count("SELECT COUNT(*) AS n FROM marshal_reservations WHERE user_id = 'old'") == 200
    assert await count("SELECT COUNT(*) AS n FROM marshal_user_day WHERE day = '2026-09-02'") == 200
    await reserve(db, "now", NOW, _settings())
    assert await count("SELECT COUNT(*) AS n FROM marshal_reservations WHERE user_id = 'old'") == 0
    assert await count("SELECT COUNT(*) AS n FROM marshal_user_day WHERE day = '2026-09-02'") == 0
    assert await count("SELECT COUNT(*) AS n FROM marshal_reservations WHERE user_id = 'now'") == 2


def test_money_is_whole_micro_dollars_rounded_up() -> None:
    assert budget.to_micro(0.000682) == 682
    assert budget.to_micro(0.0000001) == 1
    assert budget.to_micro(0.0) == 0
    assert budget.to_micro(5.0) == 5_000_000
    assert budget.caps(_settings()) == (5_000_000, 100_000_000)


def test_reservation_is_frozen() -> None:
    hold = Reservation("id", "u", "2026-10-12", "2026-10", ASK_RESERVE_MICRO, 1)
    with pytest.raises(AttributeError):
        hold.user_asks = 2  # type: ignore[misc]
