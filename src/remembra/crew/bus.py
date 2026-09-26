"""CrewBus: in-process fan-out of committed crew events, plus the optional DB tailer (§4.4).

* ``CrewEventLog`` publishes each committed transaction's events here.
* Delivery is **per crew, strictly by seq, deduplicated by (crew_id, seq)**. If
  events arrive out of order (two transactions commit in order but their
  post-commit publishes interleave) the bus back-fills the missing seqs from the
  database before delivering, so listeners never see a hole or a duplicate.
* The DB tailer (``crew_events WHERE rowid > ?`` every 500 ms) exists for
  multi-worker deployments. It is **disabled when it runs in the same process as
  the publisher** (production runs one uvicorn worker); when both run, the same
  dedupe makes the double delivery harmless.

Listeners are synchronous and must not block (the WebSocket layer only enqueues).

This module also holds the small read model the WebSocket layer needs for crew
subscriptions (who may read a crew, summary counts, presence session rows).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import aiosqlite
import structlog

from remembra.crew import schemas
from remembra.crew.events import EVENT_COLUMNS, CrewDatabase, fetch_events, format_ts, row_to_stored, utc_now

log = structlog.get_logger(__name__)

Envelope = Mapping[str, Any]
Listener = Callable[[Envelope], None]
# (crew_id, after_seq, before_seq) -> envelopes with after_seq < seq < before_seq, in order
Loader = Callable[[str, int, int], Awaitable[list[dict[str, Any]]]]

MAX_BACKFILL: Final = 500


class CrewBus:
    def __init__(self, loader: Loader | None = None, *, max_backfill: int = MAX_BACKFILL) -> None:
        self._loader = loader
        self._max_backfill = max_backfill
        self._listeners: list[Listener] = []
        self._last: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.published = 0
        self.duplicates = 0
        self.backfilled = 0

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def last_seq(self, crew_id: str) -> int | None:
        return self._last.get(crew_id)

    def _deliver(self, envelope: Envelope) -> None:
        self.published += 1
        for listener in list(self._listeners):
            try:
                listener(envelope)
            except Exception as e:  # a broken listener must not stop fan-out
                log.error("crew_bus_listener_failed", error_type=type(e).__name__, error=str(e))

    async def publish(self, envelopes: Iterable[Envelope]) -> int:
        """Deliver committed events. Returns how many were delivered (duplicates excluded)."""
        by_crew: dict[str, list[Envelope]] = defaultdict(list)
        for env in envelopes:
            by_crew[str(env["crew_id"])].append(env)
        delivered = 0
        for crew_id, items in by_crew.items():
            items.sort(key=lambda e: int(e["seq"]))
            async with self._locks[crew_id]:
                for env in items:
                    delivered += await self._publish_one(crew_id, env)
        return delivered

    async def _publish_one(self, crew_id: str, env: Envelope) -> int:
        seq = int(env["seq"])
        last = self._last.get(crew_id)
        if last is not None and seq <= last:
            self.duplicates += 1
            return 0
        count = 0
        if last is not None and seq > last + 1 and self._loader is not None and seq - last - 1 <= self._max_backfill:
            try:
                missing = await self._loader(crew_id, last, seq)
            except Exception as e:
                log.error("crew_bus_backfill_failed", crew_id=crew_id, error_type=type(e).__name__)
                missing = []
            for m in missing:
                mseq = int(m["seq"])
                if last < mseq < seq:
                    self._deliver(m)
                    self.backfilled += 1
                    count += 1
                    last = mseq
        self._deliver(env)
        self._last[crew_id] = seq
        return count + 1


def db_loader(db: CrewDatabase) -> Loader:
    async def load(crew_id: str, after_seq: int, before_seq: int) -> list[dict[str, Any]]:
        return await fetch_events(db.conn, crew_id, after_seq=after_seq, upto_seq=before_seq - 1, limit=MAX_BACKFILL)

    return load


class CrewEventTailer:
    """Polls ``crew_events`` by rowid and publishes new rows to the bus (multi-process delivery)."""

    def __init__(self, db: CrewDatabase, bus: CrewBus, *, interval_s: float = 0.5, batch: int = 500) -> None:
        self._db = db
        self._bus = bus
        self.interval_s = interval_s
        self._batch = batch
        self.last_rowid: int | None = None

    async def prime(self) -> None:
        """Start from the current end of the log (the tailer never replays history)."""
        async with self._db.conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM crew_events") as cur:
            row = await cur.fetchone()
        self.last_rowid = int(row[0]) if row else 0

    async def poll_once(self) -> int:
        if self.last_rowid is None:
            await self.prime()
        async with self._db.conn.execute(
            f"SELECT rowid, {EVENT_COLUMNS} FROM crew_events WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (self.last_rowid, self._batch),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return 0
        envelopes = []
        for row in rows:
            envelopes.append(row_to_stored(tuple(row)[1:]).envelope)
            self.last_rowid = int(row[0])
        return await self._bus.publish(envelopes)

    async def run(self) -> None:
        if self.last_rowid is None:
            await self.prime()
        while True:
            try:
                await self.poll_once()
            except Exception as e:
                log.error("crew_tailer_poll_failed", error_type=type(e).__name__, error=str(e))
            await asyncio.sleep(self.interval_s)


# ---------------------------------------------------------------------------
# Read model for WebSocket subscribers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrewRef:
    crew_id: str
    project_id: str
    owner_user_id: str


def _project_allowed(project_ids: Sequence[str] | None, project_id: str) -> bool:
    return not project_ids or project_id in project_ids


async def readable_crews(conn: aiosqlite.Connection, user_id: str, project_ids: Sequence[str] | None) -> list[CrewRef]:
    async with conn.execute(
        """
        SELECT c.id, c.project_id, c.owner_user_id FROM crews c WHERE c.owner_user_id = ?
        UNION
        SELECT c.id, c.project_id, c.owner_user_id FROM crews c JOIN crew_members m ON m.crew_id = c.id
        WHERE m.user_id = ?
        ORDER BY 1
        """,
        (user_id, user_id),
    ) as cur:
        rows = await cur.fetchall()
    return [CrewRef(r[0], r[1], r[2]) for r in rows if _project_allowed(project_ids, r[1])]


SUMMARY_MOMENT_WINDOW: Final = timedelta(hours=24)


async def summary_items(
    conn: aiosqlite.Connection,
    crews: Sequence[CrewRef],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """``crew.summary`` rows: counts only (live sessions, moments in the last 24 h, Needs-you items)."""
    since = format_ts((now or utc_now()) - SUMMARY_MOMENT_WINDOW)
    live_states = schemas.LIVE_PRESENCE_STATES
    live_marks = ",".join("?" * len(live_states))
    inbox_marks = ",".join("?" * len(schemas.LIVE_INBOX_STATES))
    out: list[dict[str, Any]] = []
    for crew in crews:
        async with conn.execute(
            f"SELECT COUNT(*) FROM crew_sessions WHERE crew_id = ? AND state IN ({live_marks})",
            (crew.crew_id, *live_states),
        ) as cur:
            live = int((await cur.fetchone() or (0,))[0])
        async with conn.execute(
            "SELECT COUNT(*) FROM crew_events WHERE crew_id = ? AND moment = 1 AND ts >= ?",
            (crew.crew_id, since),
        ) as cur:
            moments = int((await cur.fetchone() or (0,))[0])
        async with conn.execute(
            f"SELECT COUNT(*) FROM crew_inbox_items WHERE crew_id = ? AND audience = 'project' AND state IN ({inbox_marks})",
            (crew.crew_id, *schemas.LIVE_INBOX_STATES),
        ) as cur:
            needs_you = int((await cur.fetchone() or (0,))[0])
        out.append(
            {
                "crew_id": crew.crew_id,
                "project_id": crew.project_id,
                "mode": "multi" if live >= 2 else "solo",
                "live": live,
                "moments": moments,
                "needs_you": needs_you,
            }
        )
    return out


@dataclass(frozen=True)
class PresenceSession:
    session_id: str
    user_id: str
    state: str
    stuck: bool


async def presence_sessions(
    conn: aiosqlite.Connection,
    crew_id: str,
    session_ids: Sequence[str],
) -> dict[str, PresenceSession]:
    if not session_ids:
        return {}
    marks = ",".join("?" * len(session_ids))
    async with conn.execute(
        f"SELECT id, user_id, state, stuck FROM crew_sessions WHERE crew_id = ? AND id IN ({marks})",
        (crew_id, *session_ids),
    ) as cur:
        rows = await cur.fetchall()
    return {r[0]: PresenceSession(r[0], r[1], r[2], bool(r[3])) for r in rows}
