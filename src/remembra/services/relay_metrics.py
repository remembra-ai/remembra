"""Relay activation metrics for the platform operator (R-18, R-21).

Computed from first-party rows only: ``users`` (signups), relay handoffs in
``memories`` (server-written continuity records, see
:data:`~remembra.storage.database.RELAY_RECORD_SQL`), and ``relay_pickups``
(a handoff served to a different agent in a session brief). No content is
read; nothing leaves the server.

* ``funnel``: signups, users with a handoff, users with a cross-agent pickup,
  activated users (a pickup by another agent within 7 days of the handoff),
  and the median hours from signup to the first handoff and to the first pickup.
* ``weekly``: per UTC week (Monday start), pickups, the users picking up,
  repeat users (who had already picked up in an earlier week), and the share
  of that week's handoffs by health grade.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from remembra.relay.handoff import HEALTH_LABELS
from remembra.storage.database import RELAY_RECORD_SQL

ACTIVATION_WINDOW_SECONDS = 7 * 86400
NOT_GRADED = "not_graded"


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _week_start(dt: datetime) -> datetime:
    day = dt.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _median_hours(deltas: list[float]) -> float | None:
    return round(statistics.median(deltas) / 3600.0, 2) if deltas else None


async def relay_metrics(db: Any, weeks: int = 8, since: datetime | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Funnel and weekly series. ``since`` limits the funnel to users who signed up at or after it."""
    weeks = max(1, min(int(weeks), 52))
    now = (now or datetime.now(UTC)).astimezone(UTC)
    since_utc = since.astimezone(UTC) if since and since.tzinfo else (since.replace(tzinfo=UTC) if since else None)

    cursor = await db.conn.execute("SELECT id, created_at FROM users")
    signups: dict[str, datetime] = {}
    for user_id, created in await cursor.fetchall():
        at = _ts(created)
        if at is None or (since_utc is not None and at < since_utc):
            continue
        signups[str(user_id)] = at

    cursor = await db.conn.execute(
        "SELECT user_id, MIN(julianday(created_at)) FROM memories "
        f"WHERE memory_type = 'handoff' AND {RELAY_RECORD_SQL} GROUP BY user_id"  # noqa: S608 - fixed SQL fragment
    )
    first_handoff = {str(u): _from_julian(jd) for u, jd in await cursor.fetchall() if jd is not None}

    cursor = await db.conn.execute(
        """
        SELECT user_id, MIN(julianday(picked_up_at)),
               MAX(CASE WHEN handoff_agent IS NOT NULL AND reader_agent != handoff_agent
                         AND gap_seconds IS NOT NULL AND gap_seconds <= ? THEN 1 ELSE 0 END)
        FROM relay_pickups GROUP BY user_id
        """,
        (ACTIVATION_WINDOW_SECONDS,),
    )
    first_pickup: dict[str, datetime] = {}
    activated: set[str] = set()
    for user_id, jd, active in await cursor.fetchall():
        if jd is not None:
            first_pickup[str(user_id)] = _from_julian(jd)
        if active:
            activated.add(str(user_id))

    cohort = set(signups)

    def _deltas(firsts: dict[str, datetime]) -> list[float]:
        return [(firsts[u] - signups[u]).total_seconds() for u in cohort & set(firsts) if firsts[u] >= signups[u]]

    funnel = {
        "signups": len(cohort),
        "users_with_handoff": len(cohort & set(first_handoff)),
        "users_with_cross_agent_pickup": len(cohort & set(first_pickup)),
        "activated_users": len(cohort & activated),
        "median_hours_signup_to_first_handoff": _median_hours(_deltas(first_handoff)),
        "median_hours_signup_to_first_pickup": _median_hours(_deltas(first_pickup)),
        "since": since_utc.isoformat() if since_utc else None,
        "activation_window_days": ACTIVATION_WINDOW_SECONDS // 86400,
    }

    first_week = _week_start(now) - timedelta(weeks=weeks - 1)
    series: dict[datetime, dict[str, Any]] = {}
    for i in range(weeks):
        start = first_week + timedelta(weeks=i)
        series[start] = {
            "week_start": start.date().isoformat(),
            "pickups": 0,
            "users": set(),
            "repeat_users": set(),
            "handoffs": 0,
            "health": {status: 0 for status in (*HEALTH_LABELS, NOT_GRADED)},
        }

    cursor = await db.conn.execute(
        "SELECT user_id, picked_up_at FROM relay_pickups WHERE julianday(picked_up_at) >= julianday(?)",
        (first_week.isoformat(),),
    )
    for user_id, at_raw in await cursor.fetchall():
        at = _ts(at_raw)
        if at is None or (bucket := series.get(_week_start(at))) is None:
            continue
        bucket["pickups"] += 1
        bucket["users"].add(str(user_id))
        first = first_pickup.get(str(user_id))
        if first is not None and first < _week_start(at):
            bucket["repeat_users"].add(str(user_id))

    health_expr = "CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.relay.health.status') END"
    cursor = await db.conn.execute(
        f"""
        SELECT created_at, {health_expr} FROM memories
        WHERE memory_type = 'handoff' AND superseded_by IS NULL AND {RELAY_RECORD_SQL}
          AND julianday(created_at) >= julianday(?)
        """,  # noqa: S608 - fixed SQL fragments
        (first_week.replace(tzinfo=None).isoformat(),),
    )
    for created, status in await cursor.fetchall():
        at = _ts(created)
        if at is None or (bucket := series.get(_week_start(at))) is None:
            continue
        bucket["handoffs"] += 1
        key = status if status in HEALTH_LABELS else NOT_GRADED
        bucket["health"][key] += 1

    weekly = []
    for bucket in series.values():
        total = bucket["handoffs"]
        weekly.append(
            {
                "week_start": bucket["week_start"],
                "pickups": bucket["pickups"],
                "users_picking_up": len(bucket["users"]),
                "repeat_users": len(bucket["repeat_users"]),
                "handoffs": total,
                "health": bucket["health"],
                "health_share": {k: (round(v / total, 3) if total else 0.0) for k, v in bucket["health"].items()},
            }
        )
    return {"generated_at": now.isoformat(), "funnel": funnel, "weekly": weekly}


_UNIX_EPOCH_JULIAN = 2440587.5


def _from_julian(jd: float) -> datetime:
    return datetime.fromtimestamp(round((float(jd) - _UNIX_EPOCH_JULIAN) * 86400.0, 3), tz=UTC)
