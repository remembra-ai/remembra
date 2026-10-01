"""The desk's spend ledger: hard, fail-closed platform caps in integer micro-dollars.

Every ask reserves its whole budget (``ASK_RESERVE_MICRO``, $0.02) before
the model is called, inside one ``BEGIN IMMEDIATE`` transaction: the
per-account asks of the UTC day, then the platform's day and month dollars
(spent plus still reserved plus this reserve) are checked against the caps and
the hold is written, atomically. In one process asks are serialised by the
connection's lock; across processes by SQLite's write lock. So
``spent + reserved <= cap`` holds at every commit, whatever the concurrency.

When the ask ends, :func:`settle` releases the reserve and records the real
dollars (never more than the reserve: the per-call guard in :mod:`.llm` keeps
an ask under its budget). A hold that is never settled (a crash, a failed
settle) is expired by the next :func:`reserve` after ``RESERVATION_TTL_S`` and
counted at its full reserve. Any error inside :func:`reserve` refuses the ask
(:class:`BudgetUnavailable`); nothing here ever lets an ask through on doubt.

The day and month an ask counts in are fixed when it is reserved and stored on
the hold, so an ask that starts before UTC midnight and ends after it settles
into the day it started in. Spend never touches a user's smart credits.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from remembra.marshal.desk.constants import ASK_RESERVE_MICRO, PRUNE_AFTER_DAYS, PRUNE_BATCH, RESERVATION_TTL_S


class BudgetRefused(Exception):
    """An ask the ledger refused (the subclass says why)."""


class DailyAskLimit(BudgetRefused):
    def __init__(self, limit: int, used: int, resets_at: datetime) -> None:
        super().__init__("daily ask limit")
        self.limit = limit
        self.used = used
        self.resets_at = resets_at


class DailyBudgetSpent(BudgetRefused):
    reason = "daily_budget"

    def __init__(self, resets_at: datetime) -> None:
        super().__init__(self.reason)
        self.resets_at = resets_at


class MonthlyBudgetSpent(BudgetRefused):
    reason = "monthly_budget"

    def __init__(self, resets_at: datetime) -> None:
        super().__init__(self.reason)
        self.resets_at = resets_at


class BudgetUnavailable(BudgetRefused):
    """The ledger could not be read or written: the ask is refused (fail closed)."""

    reason = "budget_unavailable"


@dataclass(frozen=True)
class Reservation:
    id: str
    user_id: str
    day: str  # UTC 'YYYY-MM-DD', fixed at reserve
    month: str  # UTC 'YYYY-MM'
    reserved_micro: int
    user_asks: int  # the account's asks today, this one included


@dataclass(frozen=True)
class Snapshot:
    day_used_micro: int  # spent + reserved today (UTC), the whole platform
    month_used_micro: int
    user_asks: int  # this account's asks today


def to_micro(usd: float) -> int:
    """Dollars as whole micro-dollars, rounded up (a fraction of a micro-dollar is never lost)."""
    return max(0, math.ceil(usd * 1_000_000 - 1e-9))


def day_key(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def month_key(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m")


def next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def next_month_start(now: datetime) -> datetime:
    at = now.astimezone(UTC)
    return datetime(at.year + (at.month == 12), at.month % 12 + 1, 1, tzinfo=UTC)


def iso_z(at: datetime) -> str:
    """ISO 8601 UTC with ``Z`` and no fraction (what the API returns)."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(at: datetime) -> str:
    # A fixed-width UTC stamp, so string order is time order in SQL comparisons.
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def caps(settings: Any) -> tuple[int, int]:
    return to_micro(float(settings.marshal_daily_usd)), to_micro(float(settings.marshal_monthly_usd))


async def _one(conn: Any, sql: str, params: tuple[Any, ...]) -> Any:
    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()
    return row


async def _expire_stale(conn: Any, now: datetime) -> int:
    """Holds older than the TTL were left by asks that never settled: count them at their full reserve."""
    cutoff = _stamp(now - timedelta(seconds=RESERVATION_TTL_S))
    cursor = await conn.execute(
        "SELECT id, user_id, day, month, reserved_micro FROM marshal_reservations WHERE state = 'held' AND created_at < ?",
        (cutoff,),
    )
    stale = [tuple(row) for row in await cursor.fetchall()]
    stamp = _stamp(now)
    for hold_id, user_id, day, month, reserved in stale:
        await conn.execute(
            "UPDATE marshal_reservations SET state = 'expired', spent_micro = reserved_micro, settled_at = ?"
            " WHERE id = ? AND state = 'held'",
            (stamp, hold_id),
        )
        await conn.execute(
            "UPDATE marshal_budget SET reserved_micro = MAX(reserved_micro - ?, 0), spent_micro = spent_micro + ?,"
            " updated_at = ? WHERE period IN (?, ?)",
            (reserved, reserved, stamp, f"day:{day}", f"month:{month}"),
        )
        await conn.execute(
            "UPDATE marshal_user_day SET spent_micro = spent_micro + ? WHERE user_id = ? AND day = ?",
            (reserved, user_id, day),
        )
    return len(stale)


async def _prune(conn: Any, now: datetime) -> None:
    """Finished holds and per-account days older than 35 days go, at most PRUNE_BATCH of each per call."""
    cutoff = now - timedelta(days=PRUNE_AFTER_DAYS)
    await conn.execute(
        "DELETE FROM marshal_reservations WHERE rowid IN (SELECT rowid FROM marshal_reservations"
        " WHERE state != 'held' AND created_at < ? LIMIT ?)",
        (_stamp(cutoff), PRUNE_BATCH),
    )
    await conn.execute(
        "DELETE FROM marshal_user_day WHERE rowid IN (SELECT rowid FROM marshal_user_day WHERE day < ? LIMIT ?)",
        (day_key(cutoff), PRUNE_BATCH),
    )


async def reserve(db: Any, user_id: str, now: datetime, settings: Any) -> Reservation:
    """Hold one ask's budget, or raise :class:`BudgetRefused` (never lets an ask through on an error)."""
    day, month = day_key(now), month_key(now)
    day_cap, month_cap = caps(settings)
    ask_limit = int(settings.marshal_user_daily_asks)
    stamp = _stamp(now)
    refusal: BudgetRefused | None = None
    reservation: Reservation | None = None
    try:
        async with db.transaction():
            conn = db.conn
            await _expire_stale(conn, now)
            for period in (f"day:{day}", f"month:{month}"):
                await conn.execute(
                    "INSERT OR IGNORE INTO marshal_budget (period, reserved_micro, spent_micro, asks, updated_at)"
                    " VALUES (?, 0, 0, 0, ?)",
                    (period, stamp),
                )
            await conn.execute(
                "INSERT OR IGNORE INTO marshal_user_day (user_id, day, asks, spent_micro) VALUES (?, ?, 0, 0)", (user_id, day)
            )
            used_sql = "SELECT reserved_micro + spent_micro FROM marshal_budget WHERE period = ?"
            day_row = await _one(conn, used_sql, (f"day:{day}",))
            month_row = await _one(conn, used_sql, (f"month:{month}",))
            user_row = await _one(conn, "SELECT asks FROM marshal_user_day WHERE user_id = ? AND day = ?", (user_id, day))
            day_used, month_used, user_asks = int(day_row[0]), int(month_row[0]), int(user_row[0])
            if user_asks >= ask_limit:
                refusal = DailyAskLimit(ask_limit, user_asks, next_utc_midnight(now))
            elif day_used + ASK_RESERVE_MICRO > day_cap:
                refusal = DailyBudgetSpent(next_utc_midnight(now))
            elif month_used + ASK_RESERVE_MICRO > month_cap:
                refusal = MonthlyBudgetSpent(next_month_start(now))
            else:
                hold_id = uuid.uuid4().hex
                await conn.execute(
                    "UPDATE marshal_budget SET reserved_micro = reserved_micro + ?, asks = asks + 1, updated_at = ?"
                    " WHERE period IN (?, ?)",
                    (ASK_RESERVE_MICRO, stamp, f"day:{day}", f"month:{month}"),
                )
                await conn.execute(
                    "UPDATE marshal_user_day SET asks = asks + 1 WHERE user_id = ? AND day = ?",
                    (user_id, day),
                )
                await conn.execute(
                    "INSERT INTO marshal_reservations (id, user_id, day, month, reserved_micro, spent_micro, state, created_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL, 'held', ?)",
                    (hold_id, user_id, day, month, ASK_RESERVE_MICRO, stamp),
                )
                reservation = Reservation(hold_id, user_id, day, month, ASK_RESERVE_MICRO, user_asks + 1)
            await _prune(conn, now)
    except Exception as e:
        raise BudgetUnavailable(type(e).__name__) from e
    if refusal is not None:
        raise refusal
    assert reservation is not None
    return reservation


async def settle(db: Any, reservation: Reservation, usd: float, *, counted: bool, now: datetime | None = None) -> bool:
    """Release the hold and record the real dollars, into the day and month stored on the hold.

    ``counted`` is False when no model request reached the provider: the ask is given back
    (the account's count and the platform's ask counts go down by one). A
    hold that was already expired or settled changes nothing (its dollars are
    counted once). Returns True when the hold was settled now.
    """
    at = now or datetime.now(UTC)
    stamp = _stamp(at)
    # An ask can't cost more than its reserve (the per-call guard): only float noise could say so.
    spent = min(to_micro(usd), reservation.reserved_micro)
    give_back = 0 if counted else 1
    async with db.transaction():
        conn = db.conn
        row = await _one(conn, "SELECT state FROM marshal_reservations WHERE id = ?", (reservation.id,))
        if row is None or row[0] != "held":
            return False
        await conn.execute(
            "UPDATE marshal_reservations SET state = ?, spent_micro = ?, settled_at = ? WHERE id = ? AND state = 'held'",
            ("settled" if counted else "released", spent, stamp, reservation.id),
        )
        await conn.execute(
            "UPDATE marshal_budget SET reserved_micro = MAX(reserved_micro - ?, 0), spent_micro = spent_micro + ?,"
            " asks = MAX(asks - ?, 0), updated_at = ? WHERE period IN (?, ?)",
            (reservation.reserved_micro, spent, give_back, stamp, f"day:{reservation.day}", f"month:{reservation.month}"),
        )
        await conn.execute(
            "UPDATE marshal_user_day SET spent_micro = spent_micro + ?, asks = MAX(asks - ?, 0) WHERE user_id = ? AND day = ?",
            (spent, give_back, reservation.user_id, reservation.day),
        )
    return True


async def snapshot(db: Any, user_id: str, now: datetime) -> Snapshot:
    """Today's and this month's platform use and this account's asks today (read-only).

    Stale holds need no expiry here: expiring one moves its dollars from
    reserved to spent, so the sums shown are already what they will become.
    """
    day, month = day_key(now), month_key(now)
    conn = db.conn
    rows = await conn.execute(
        "SELECT period, reserved_micro + spent_micro FROM marshal_budget WHERE period IN (?, ?)", (f"day:{day}", f"month:{month}")
    )
    used = {str(r[0]): int(r[1]) for r in await rows.fetchall()}
    user_row = await _one(conn, "SELECT asks FROM marshal_user_day WHERE user_id = ? AND day = ?", (user_id, day))
    return Snapshot(
        day_used_micro=used.get(f"day:{day}", 0),
        month_used_micro=used.get(f"month:{month}", 0),
        user_asks=int(user_row[0]) if user_row else 0,
    )
