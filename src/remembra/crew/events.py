"""Crew event log (WP-2, spec §4.1–§4.3).

The event log is the truth of a crew. Every crew mutation writes its state change
and its event in **one** ``BEGIN IMMEDIATE`` transaction on the ``crew.db``
connection; the event takes the next per-crew ``seq`` (gap-free, total order per
crew because SQLite has one writer per file) and extends the per-crew hash chain.

How other work packages use it::

    log: CrewEventLog = request.app.state.crew_events
    async with log.transaction() as tx:
        await tx.conn.execute("UPDATE crew_claims SET state='active' ... ")
        result = await tx.emit(
            crew_id=crew_id,
            type="claim.granted",
            actor=Actor.session(...),          # derived from the credential, never the body
            payload={"claim": claim_view},     # validated against the closed schema
            summary="cc-1 claimed zone pos (exclusive) for T-14",  # ids/slugs/callsigns only
            refs={"zone_id": ..., "claim_id": ..., "session_id": ...},
            idem_key="claim:clm_...:granted:1",  # optional natural key; never starts with "c:"
        )
    # committed: the events were published to the CrewBus (WebSocket fan-out)

``emit_in_tx`` is the low-level form for callers that manage the transaction
themselves (they must publish the returned envelopes after COMMIT).

Client-submittable events (``POST /crews/{id}/events`` and heartbeats) go through
:func:`ingest_client_events`, which enforces the whitelist (§4.2), derives the
actor from the session, stores the idempotency key as ``c:<id>`` and coalesces
``guard.blocked`` per (session, zone) per 5 min and ``activity.burst`` to one
per session per 60 s.

Request-level idempotency (the ``Idempotency-Key`` header) is stored in
``crew_idempotency`` through :func:`idem_lookup` / :func:`idem_store`, which
refuse to store token-bearing responses (§4.3, §11).

Storage contract: the ``crew_events`` table of ``CREW_MIGRATIONS`` v1 (§3.2)
**plus two JSON columns** ``actor`` and ``refs``. The envelope's actor
(callsign, agent id, verified flag) and its refs are hashed into the chain, so
they must be stored verbatim for replay and for the nightly chain verification;
the §3.2 scalar columns (``actor_kind``, ``actor_id``, ``session_id``,
``task_id``, ``zone_id``, ``ref_id``) remain as the indexed projections.
:func:`check_schema` verifies this at startup.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

import aiosqlite
import structlog

from remembra.crew import schemas

if TYPE_CHECKING:
    from remembra.crew.bus import CrewBus

log = structlog.get_logger(__name__)


class CrewDatabase(Protocol):
    """What the event log needs from ``crew.db`` (WP-1 ``crew/db.py``).

    ``conn`` is the crew connection; ``transaction()`` opens ``BEGIN IMMEDIATE`` …
    ``COMMIT`` (``ROLLBACK`` on error) under the crew DB's own lock (D35), and
    ``after_commit()`` runs a callback once the outermost transaction the caller
    owns has committed (dropped on rollback).
    ``remembra.storage.database.Database`` satisfies it structurally.
    """

    @property
    def conn(self) -> aiosqlite.Connection: ...

    def transaction(self) -> AbstractAsyncContextManager[None]: ...

    def after_commit(self, callback: Callable[[], Awaitable[None]]) -> bool: ...


class CrewEventError(Exception):
    """Base class for event-log errors."""


class UnknownCrew(CrewEventError):
    pass


class NotInTransaction(CrewEventError):
    pass


class EventValidationError(CrewEventError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:5]))


class ChainHeadMissing(CrewEventError):
    pass


class IdempotencyConflict(CrewEventError):
    """The same Idempotency-Key was reused with a different request body."""


class TokenBearingResponse(CrewEventError):
    """Refused: token-bearing responses are never stored in the idempotency table."""


# ---------------------------------------------------------------------------
# Time and ids
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_ts(dt: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and ``Z`` (``schemas.TS_PATTERN``)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def new_event_id(now: datetime | None = None) -> str:
    """``evt_`` + 12 hex digits of epoch ms + 12 random hex digits (time-sortable)."""
    ms = int((now or utc_now()).timestamp() * 1000)
    return f"evt_{ms:012x}{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """The principal behind an event. Always derived from the credential, never from a request body."""

    kind: str  # session | human | system
    id: str
    callsign: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    verified: bool = False

    @classmethod
    def system(cls) -> Actor:
        return cls(kind="system", id="server", verified=True)

    @classmethod
    def human(cls, user_id: str) -> Actor:
        """A dashboard (JWT) principal. Callers must have checked ``is_human`` (D27)."""
        return cls(kind="human", id=user_id, user_id=user_id, verified=True)

    @classmethod
    def session(cls, session_id: str, *, callsign: str, agent_id: str, user_id: str, verified: bool) -> Actor:
        return cls(
            kind="session",
            id=session_id,
            callsign=callsign,
            agent_id=agent_id,
            user_id=user_id,
            verified=bool(verified),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "callsign": self.callsign,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "verified": self.verified,
        }


async def load_session_actor(conn: aiosqlite.Connection, crew_id: str, session_id: str) -> Actor | None:
    """The actor for a crew session, read from ``crew_sessions`` (scoped by crew)."""
    async with conn.execute(
        "SELECT id, callsign, agent_id, user_id, agent_verified FROM crew_sessions WHERE crew_id = ? AND id = ?",
        (crew_id, session_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return Actor.session(row[0], callsign=row[1], agent_id=row[2], user_id=row[3], verified=bool(row[4]))


# ---------------------------------------------------------------------------
# Stored rows <-> envelopes
# ---------------------------------------------------------------------------

REF_KEYS: Final = tuple(schemas.REFS.fields)
_REF_ID_ORDER: Final = ("claim_id", "report_id", "collision_id", "message_id", "decision_id", "inbox_item_id", "host_id")
REQUIRED_EVENT_COLUMNS: Final = frozenset(
    {
        "crew_id",
        "seq",
        "id",
        "owner_user_id",
        "project_id",
        "ts",
        "type",
        "v",
        "actor_kind",
        "actor_id",
        "session_id",
        "task_id",
        "zone_id",
        "ref_id",
        "severity",
        "moment",
        "summary",
        "payload",
        "idem_key",
        "origin",
        "prev_hash",
        "hash",
        "actor",
        "refs",
    }
)

EVENT_COLUMNS: Final = (
    "seq, id, crew_id, project_id, ts, type, v, origin, actor, refs, severity, moment, summary, payload, prev_hash, hash"
)
EVENT_SELECT: Final = f"SELECT {EVENT_COLUMNS} FROM crew_events"


@dataclass(frozen=True)
class StoredEvent:
    envelope: dict[str, Any]
    prev_hash: str | None
    hash: str | None


def _json_text(value: Any) -> str:
    return schemas.canonical_json(value).decode("utf-8")


def row_to_stored(row: Sequence[Any]) -> StoredEvent:
    envelope = {
        "seq": int(row[0]),
        "id": row[1],
        "crew_id": row[2],
        "project_id": row[3],
        "ts": row[4],
        "type": row[5],
        "v": int(row[6]),
        "origin": row[7],
        "actor": json.loads(row[8]) if row[8] else {},
        "refs": json.loads(row[9]) if row[9] else {},
        "severity": row[10],
        "moment": bool(row[11]),
        "summary": row[12],
        "payload": json.loads(row[13]),
    }
    return StoredEvent(envelope=envelope, prev_hash=row[14], hash=row[15])


async def check_schema(conn: aiosqlite.Connection) -> None:
    """Fail loudly when ``crew_events`` lacks a column the event log needs."""
    async with conn.execute("PRAGMA table_info(crew_events)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    if not cols:
        raise CrewEventError("crew.db has no crew_events table (CREW_MIGRATIONS v1 not applied)")
    missing = REQUIRED_EVENT_COLUMNS - cols
    if missing:
        raise CrewEventError(f"crew_events is missing columns required by the event log: {sorted(missing)}")


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------

_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]+")


def clean_summary(summary: str) -> str:
    text = _CONTROL_RE.sub(" ", summary).strip()
    return text[: schemas.MAX_SUMMARY_CHARS]


@dataclass(frozen=True)
class EmitResult:
    envelope: dict[str, Any]
    replayed: bool
    hash: str | None

    @property
    def seq(self) -> int:
        return int(self.envelope["seq"])


@dataclass(frozen=True)
class CrewHead:
    crew_id: str
    owner_user_id: str
    project_id: str
    last_seq: int
    last_hash: str | None


async def crew_head(conn: aiosqlite.Connection, crew_id: str) -> CrewHead | None:
    async with conn.execute(
        "SELECT owner_user_id, project_id, last_seq, last_hash FROM crews WHERE id = ?",
        (crew_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return CrewHead(crew_id, row[0], row[1], int(row[2]), row[3])


async def _event_by_idem(conn: aiosqlite.Connection, crew_id: str, idem_key: str) -> StoredEvent | None:
    async with conn.execute(f"{EVENT_SELECT} WHERE crew_id = ? AND idem_key = ?", (crew_id, idem_key)) as cur:
        row = await cur.fetchone()
    return row_to_stored(row) if row is not None else None


async def emit_in_tx(
    conn: aiosqlite.Connection,
    *,
    crew_id: str,
    type: str,
    actor: Actor,
    payload: Mapping[str, Any],
    summary: str,
    severity: str = "info",
    refs: Mapping[str, Any] | None = None,
    origin: str = "server",
    idem_key: str | None = None,
    age_s: int = 0,
    now: datetime | None = None,
    allow_l1: bool = False,
) -> EmitResult:
    """Append one event inside the caller's open ``BEGIN IMMEDIATE`` transaction.

    Assigns ``seq = crews.last_seq + 1``, stamps server time (minus a client-reported
    ``age_s``, never an absolute client time), sets ``moment`` from the fixed rule
    table, validates the whole envelope against the closed contract, extends the
    hash chain and advances ``crews.last_seq``/``last_hash``.

    With ``idem_key``, a repeat returns the original event (``replayed=True``) and
    writes nothing. Server keys must not start with ``c:``; client keys must.
    """
    if not conn.in_transaction:
        raise NotInTransaction("emit_in_tx must run inside the crew.db BEGIN IMMEDIATE transaction")
    if origin not in schemas.EVENT_ORIGINS:
        raise EventValidationError([f"$.origin: {origin!r} is not a valid origin"])
    if idem_key is not None:
        if origin == "server":
            schemas.server_idem_key(idem_key)  # raises on the client prefix
        elif not schemas.is_client_idem_key(idem_key):
            raise EventValidationError(["client idempotency keys must use the 'c:' prefix"])
        existing = await _event_by_idem(conn, crew_id, idem_key)
        if existing is not None:
            return EmitResult(existing.envelope, replayed=True, hash=existing.hash)

    head = await crew_head(conn, crew_id)
    if head is None:
        raise UnknownCrew(crew_id)
    if head.last_seq == 0:
        prev_hash = schemas.GENESIS_HASH
    elif head.last_hash:
        prev_hash = head.last_hash
    else:
        raise ChainHeadMissing(f"crew {crew_id} has last_seq={head.last_seq} but no last_hash")

    stamp = (now or utc_now()) - timedelta(seconds=max(0, int(age_s)))
    clean_refs = {k: v for k, v in (refs or {}).items() if v is not None}
    actor_dict = actor.to_dict()
    payload_dict = dict(payload)
    envelope: dict[str, Any] = {
        "seq": head.last_seq + 1,
        "id": new_event_id(stamp),
        "crew_id": crew_id,
        "project_id": head.project_id,
        "ts": format_ts(stamp),
        "type": type,
        "v": 1,
        "origin": origin,
        "actor": actor_dict,
        "refs": clean_refs,
        "severity": severity,
        "moment": schemas.is_moment(type, payload_dict, actor.kind),
        "summary": clean_summary(summary),
        "payload": payload_dict,
    }
    errors = schemas.validate_envelope(envelope, allow_l1=allow_l1)
    if not envelope["summary"]:
        errors.append("$.summary: empty")
    if errors:
        raise EventValidationError(errors)

    digest = schemas.event_hash(prev_hash, envelope)
    session_col = clean_refs.get("session_id") or (actor.id if actor.kind == "session" else None)
    ref_col = next((clean_refs[k] for k in _REF_ID_ORDER if k in clean_refs), None)
    await conn.execute(
        """
        INSERT INTO crew_events (crew_id, seq, id, owner_user_id, project_id, ts, type, v, actor_kind, actor_id,
            session_id, task_id, zone_id, ref_id, severity, moment, summary, payload, idem_key, origin,
            prev_hash, hash, actor, refs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            crew_id,
            envelope["seq"],
            envelope["id"],
            head.owner_user_id,
            head.project_id,
            envelope["ts"],
            type,
            1,
            actor.kind,
            actor.id,
            session_col,
            clean_refs.get("task_id"),
            clean_refs.get("zone_id"),
            ref_col,
            severity,
            1 if envelope["moment"] else 0,
            envelope["summary"],
            _json_text(payload_dict),
            idem_key,
            origin,
            prev_hash,
            digest,
            _json_text(actor_dict),
            _json_text(clean_refs),
        ),
    )
    cur = await conn.execute(
        "UPDATE crews SET last_seq = ?, last_hash = ?, updated_at = ? WHERE id = ? AND last_seq = ?",
        (envelope["seq"], digest, format_ts(utc_now()), crew_id, head.last_seq),
    )
    if cur.rowcount != 1:
        # Only possible if something wrote crews.last_seq outside BEGIN IMMEDIATE.
        raise CrewEventError(f"crew {crew_id}: last_seq moved during emit")
    return EmitResult(envelope, replayed=False, hash=digest)


# ---------------------------------------------------------------------------
# Transactions that publish after COMMIT
# ---------------------------------------------------------------------------


class EventTx:
    """One crew.db transaction. Events emitted here are published only after COMMIT."""

    def __init__(self, log_: CrewEventLog) -> None:
        self._log = log_
        self.emitted: list[dict[str, Any]] = []

    @property
    def conn(self) -> aiosqlite.Connection:
        return self._log.db.conn

    async def emit(self, **kwargs: Any) -> EmitResult:
        result = await emit_in_tx(self.conn, **kwargs)
        if not result.replayed:
            self.emitted.append(result.envelope)
        return result


_current_tx: ContextVar[EventTx | None] = ContextVar("remembra_crew_event_tx", default=None)


class CrewEventLog:
    """The crew event log bound to ``crew.db`` and (optionally) the in-process CrewBus."""

    def __init__(self, db: CrewDatabase, bus: CrewBus | None = None) -> None:
        self.db = db
        self.bus = bus

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[EventTx]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; publish emitted events to the bus after COMMIT.

        Nested calls join the outer transaction, whether the outer block is another
        ``log.transaction()`` or a plain ``crew_db.transaction()`` (how services write
        state, ``store.py``): the events are handed to ``crew_db.after_commit`` and publish
        only when the **outermost** transaction commits. On any exception or rollback
        nothing is published (and the seq a rolled-back event took is reused by the next
        real event, which then reaches subscribers).
        """
        outer = _current_tx.get()
        if outer is not None and outer._log is self:
            yield outer
            return
        tx = EventTx(self)
        token = _current_tx.set(tx)
        try:
            async with self.db.transaction():
                if not self.db.after_commit(lambda: self._publish(tx)):
                    raise CrewEventError("crew.db transaction is not owned by this task")  # pragma: no cover
                yield tx
        finally:
            _current_tx.reset(token)

    async def _publish(self, tx: EventTx) -> None:
        if self.bus is not None and tx.emitted:
            await self.bus.publish(tx.emitted)

    async def emit(self, **kwargs: Any) -> EmitResult:
        """Emit a single event in its own transaction (for events with no accompanying state change)."""
        async with self.transaction() as tx:
            return await tx.emit(**kwargs)


# ---------------------------------------------------------------------------
# Reads (replay, polling fallback, chain verification)
# ---------------------------------------------------------------------------


async def fetch_events(
    conn: aiosqlite.Connection,
    crew_id: str,
    *,
    after_seq: int,
    upto_seq: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Envelopes with ``after_seq < seq [<= upto_seq]`` in seq order (at most ``limit``)."""
    if upto_seq is None:
        sql = f"{EVENT_SELECT} WHERE crew_id = ? AND seq > ? ORDER BY seq LIMIT ?"
        params: tuple[Any, ...] = (crew_id, after_seq, limit)
    else:
        sql = f"{EVENT_SELECT} WHERE crew_id = ? AND seq > ? AND seq <= ? ORDER BY seq LIMIT ?"
        params = (crew_id, after_seq, upto_seq, limit)
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [row_to_stored(r).envelope for r in rows]


def events_etag(last_seq: int) -> str:
    """ETag of ``GET /crews/{id}/events``: the crew's ``last_seq`` (304 when it matches)."""
    return f'"{int(last_seq)}"'


def etag_matches(if_none_match: str | None, last_seq: int) -> bool:
    if not if_none_match:
        return False
    tags = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
    return "*" in tags or events_etag(last_seq) in tags


@dataclass(frozen=True)
class EventsPage:
    events: list[dict[str, Any]]
    last_seq: int
    etag: str
    has_more: bool
    not_modified: bool


async def events_page(
    conn: aiosqlite.Connection,
    crew_id: str,
    *,
    since_seq: int,
    limit: int = 200,
    if_none_match: str | None = None,
) -> EventsPage:
    """Polling fallback (``GET /crews/{id}/events``, §4.4). The route has already applied ``load_crew``."""
    limit = max(1, min(int(limit), 200))
    head = await crew_head(conn, crew_id)
    if head is None:
        raise UnknownCrew(crew_id)
    etag = events_etag(head.last_seq)
    if etag_matches(if_none_match, head.last_seq):
        return EventsPage([], head.last_seq, etag, has_more=False, not_modified=True)
    events = await fetch_events(conn, crew_id, after_seq=max(0, int(since_seq)), upto_seq=head.last_seq, limit=limit)
    has_more = bool(events) and events[-1]["seq"] < head.last_seq
    return EventsPage(events, head.last_seq, etag, has_more=has_more, not_modified=False)


@dataclass
class ChainReport:
    crew_id: str
    checked: int = 0
    pruned_gaps: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def _pruned_ranges(conn: aiosqlite.Connection, crew_id: str) -> dict[int, tuple[int, str, str]]:
    """``first_seq → (last_seq, prev_hash, last_hash)`` of the runs retention deleted."""
    async with conn.execute(
        "SELECT first_seq, last_seq, prev_hash, last_hash FROM crew_pruned_ranges WHERE crew_id = ?", (crew_id,)
    ) as cur:
        return {int(r[0]): (int(r[1]), str(r[2]), str(r[3])) for r in await cur.fetchall()}


async def _digest_moments_present(conn: aiosqlite.Connection, crew_id: str, report: ChainReport) -> None:
    """Moments are never deleted (§4.5): every moment a digest listed must still be stored, unchanged in type."""
    async with conn.execute("SELECT day, moments FROM crew_digests WHERE crew_id = ? ORDER BY day", (crew_id,)) as cur:
        digests = [(str(r[0]), r[1]) for r in await cur.fetchall()]
    for day, raw in digests:
        try:
            listed = json.loads(raw or "[]")
        except ValueError:
            report.errors.append(f"digest {day}: moments list is not JSON")
            continue
        for item in listed if isinstance(listed, list) else ():
            seq = item.get("seq") if isinstance(item, dict) else None
            if not isinstance(seq, int):
                continue
            async with conn.execute("SELECT type FROM crew_events WHERE crew_id = ? AND seq = ?", (crew_id, seq)) as cur:
                row = await cur.fetchone()
            if row is None:
                report.errors.append(f"seq {seq}: moment listed in the {day} digest is missing")
            elif row[0] != item.get("type"):
                report.errors.append(f"seq {seq}: moment type changed since the {day} digest")


async def verify_crew_chain(conn: aiosqlite.Connection, crew_id: str, *, batch: int = 1000) -> ChainReport:
    """Nightly verify (§4.1): every stored event's hash, every link, every gap and the crew head.

    Retention removes non-moment events and records each deleted run in
    ``crew_pruned_ranges`` with the chain links on both sides. A gap in the
    stored seqs (including one at the tail, below ``crews.last_seq``) is
    accepted only when it matches a recorded range exactly and the range links
    to the events around it; any other gap is a deleted event. Each event's own
    hash is checked against its stored ``prev_hash``, every range that matches
    no gap is reported, and every moment a digest listed must still exist.

    Limit: the chain is a plain SHA-256 chain (the §4.1 contract), so someone
    who can rewrite the whole file can also recompute every hash and range;
    this detects partial edits and deletions, not a full re-forge.
    """
    report = ChainReport(crew_id)
    # The head first: events committed after it are the next run's business. Reading it after the scan
    # raced with live writers (the nightly job runs while agents work) and reported their fresh tail as
    # "missing" (found by the WP-15 load run).
    head = await crew_head(conn, crew_id)
    upto = head.last_seq if head is not None else None
    ranges = await _pruned_ranges(conn, crew_id)
    used: set[int] = set()
    expected = 1  # the next seq the chain needs
    link = schemas.GENESIS_HASH  # the hash that seq must name as prev_hash

    def bridge(first: int, upto: int) -> None:
        """Seqs ``first..upto`` are absent: they must be one recorded pruned range that links to ``link``."""
        nonlocal link
        rng = ranges.get(first)
        if rng is None or rng[0] != upto:
            report.errors.append(f"seq {first}..{upto}: events missing and not recorded as pruned")
            return
        used.add(first)
        report.pruned_gaps += 1
        if rng[1] != link:
            report.errors.append(f"pruned seq {first}..{upto}: does not link to seq {first - 1}")
        link = rng[2]

    cursor = 0
    while True:
        bound, params = ("", (crew_id, cursor, batch)) if upto is None else (" AND seq <= ?", (crew_id, cursor, upto, batch))
        async with conn.execute(
            f"{EVENT_SELECT} WHERE crew_id = ? AND seq > ?{bound} ORDER BY seq LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            break
        for row in rows:
            ev = row_to_stored(row)
            seq = int(ev.envelope["seq"])
            report.checked += 1
            stored_prev = ev.prev_hash or ""
            if seq > expected:
                bridge(expected, seq - 1)
            if stored_prev != link:
                report.errors.append(
                    "seq 1: prev_hash is not the genesis hash"
                    if seq == 1
                    else f"seq {seq}: prev_hash does not link to seq {seq - 1}"
                )
            if schemas.event_hash(stored_prev, ev.envelope) != ev.hash:
                report.errors.append(f"seq {seq}: hash mismatch")
            link = ev.hash or ""
            expected = seq + 1
            cursor = seq
    if head is not None:
        # an event stored beyond the head is forged: a real one moves crews.last_seq in its own transaction,
        # so read the highest stored seq first and the head again after it
        async with conn.execute("SELECT MAX(seq) FROM crew_events WHERE crew_id = ?", (crew_id,)) as cur:
            top = (await cur.fetchone() or (None,))[0]
        now_head = await crew_head(conn, crew_id)
        if top is not None and now_head is not None and int(top) > now_head.last_seq:
            report.errors.append(f"event seq {int(top)} beyond crews.last_seq {now_head.last_seq}")
        if expected - 1 > head.last_seq:
            report.errors.append(f"event seq {expected - 1} beyond crews.last_seq {head.last_seq}")
        else:
            if head.last_seq >= expected:
                bridge(expected, head.last_seq)  # a missing tail must be a recorded pruned range too
            if head.last_seq > 0 and head.last_hash != link:
                report.errors.append("crews.last_hash does not match the last event")
    for first in sorted(set(ranges) - used):
        report.errors.append(f"pruned seq {first}..{ranges[first][0]}: recorded range matches no gap")
    await _digest_moments_present(conn, crew_id, report)
    return report


# ---------------------------------------------------------------------------
# Client-submitted events (§4.2 whitelist)
# ---------------------------------------------------------------------------

CLIENT_SEVERITY: Final[Mapping[str, str]] = {
    "activity.burst": "info",
    "activity.commit": "info",
    "activity.push": "info",
    "activity.deploy": "notice",
    "activity.test_verdict_changed": "info",
    "guard.blocked": "notice",
    "guard.tamper_blocked": "high",
    "gate.error": "low",
    "gate.deadline": "low",
    "githook.missing": "medium",
}
GUARD_BLOCK_COALESCE_S: Final = 300
BURST_MIN_INTERVAL_S: Final = 60
_SAFE_WORD_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}")


def _word(value: Any, fallback: str) -> str:
    """Interpolate only short identifier-like words into a summary; anything else becomes ``fallback``."""
    text = str(value or "").lower()
    return text if _SAFE_WORD_RE.fullmatch(text) else fallback


def client_summary(event_type: str, payload: Mapping[str, Any], callsign: str) -> str:
    """Server templates for client events: ids, slugs, callsigns, enums and counts only."""
    who = callsign or "session"
    if event_type == "activity.burst":
        tests = payload.get("tests") or {}
        files = len(payload.get("files_touched") or [])
        return f"{who} touched {files} files ({tests.get('pass', 0)} tests passed, {tests.get('fail', 0)} failed)"
    if event_type == "activity.commit":
        return f"{who} committed {str(payload.get('sha', ''))[:7]} ({len(payload.get('files') or [])} files)"
    if event_type == "activity.push":
        where = " to the default branch" if payload.get("default_branch") else ""
        return f"{who} pushed {int(payload.get('count', 0))} commits{where}"
    if event_type == "activity.deploy":
        return f"{who} deploy {_word(payload.get('target'), 'target')} {payload.get('status')}"
    if event_type == "activity.test_verdict_changed":
        counts = f"{payload.get('passed', 0)} passed, {payload.get('failed', 0)} failed"
        return f"{who} tests {payload.get('from')} -> {payload.get('to')} ({counts})"
    if event_type == "guard.blocked":
        zone = payload.get("zone")
        where = f" in zone {zone}" if zone else ""
        return f"{who} {payload.get('decision')} by guard rule {payload.get('rule')}{where} ({payload.get('surface')})"
    if event_type == "guard.tamper_blocked":
        return f"{who} tamper attempt blocked: {payload.get('kind')} ({payload.get('surface')})"
    if event_type == "gate.error":
        return f"{who} gate error at {_word(payload.get('stage'), 'stage')}"
    if event_type == "gate.deadline":
        return f"{who} gate deadline at {_word(payload.get('stage'), 'stage')} ({int(payload.get('elapsed_ms', 0))} ms)"
    if event_type == "githook.missing":
        return f"{who} git hook {payload.get('hook')} {payload.get('state')}"
    return f"{who} {event_type}"


@dataclass(frozen=True)
class ClientEventResult:
    id: str | None
    status: str  # accepted | duplicate | coalesced | rejected
    seq: int | None = None
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "status": self.status, "seq": self.seq}
        if self.errors:
            out["errors"] = list(self.errors)
        return out


async def _coalesce_target(
    conn: aiosqlite.Connection,
    crew_id: str,
    session_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    now: datetime,
) -> int | None:
    """Seq of an event this one coalesces into, or None (guard.blocked 5 min per zone; burst 60 s)."""
    if event_type == "guard.blocked":
        window = GUARD_BLOCK_COALESCE_S
    elif event_type == "activity.burst":
        window = BURST_MIN_INTERVAL_S
    else:
        return None
    since = format_ts(now - timedelta(seconds=window))
    async with conn.execute(
        "SELECT seq, payload FROM crew_events WHERE session_id = ? AND crew_id = ? AND type = ? AND ts >= ? "
        "ORDER BY seq DESC LIMIT 50",
        (session_id, crew_id, event_type, since),
    ) as cur:
        rows = await cur.fetchall()
    for seq, raw in rows:
        if event_type == "activity.burst":
            return int(seq)
        if json.loads(raw).get("zone") == payload.get("zone"):
            return int(seq)
    return None


async def ingest_client_events(
    log_: CrewEventLog,
    *,
    crew_id: str,
    actor: Actor,
    items: Sequence[Any],
    now: datetime | None = None,
) -> list[ClientEventResult]:
    """Store client-submitted events (``POST /crews/{id}/events`` or the heartbeat).

    ``actor`` must be the session derived from the session token (the route resolves
    the token; this function re-checks that the session belongs to ``crew_id``).
    Only whitelisted types are accepted; the body never supplies actor, session or
    time. Each item gets a result; one bad item does not reject the batch.
    """
    if actor.kind != "session":
        raise EventValidationError(["client events require a session principal"])
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise EventValidationError(["$.events: must be a list"])
    if len(items) > schemas.MAX_EVENTS_PER_POST:
        raise EventValidationError([f"$.events: at most {schemas.MAX_EVENTS_PER_POST} per call"])
    stamp = now or utc_now()
    results: list[ClientEventResult] = []
    seen: dict[str, int | None] = {}
    async with log_.transaction() as tx:
        async with tx.conn.execute(
            "SELECT 1 FROM crew_sessions WHERE crew_id = ? AND id = ?",
            (crew_id, actor.id),
        ) as cur:
            if await cur.fetchone() is None:
                raise EventValidationError(["session does not belong to this crew"])
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) and isinstance(item.get("id"), str) else None
            errors = schemas.validate_client_event(item)
            if errors:
                results.append(ClientEventResult(item_id, "rejected", errors=tuple(errors[:10])))
                continue
            assert item_id is not None  # validated above
            if item_id in seen:
                results.append(ClientEventResult(item_id, "duplicate", seq=seen[item_id]))
                continue
            key = schemas.client_idem_key(item_id)
            prior = await _event_by_idem(tx.conn, crew_id, key)
            if prior is not None:
                seen[item_id] = prior.envelope["seq"]
                results.append(ClientEventResult(item_id, "duplicate", seq=prior.envelope["seq"]))
                continue
            etype = item["type"]
            payload = item["payload"]
            target = await _coalesce_target(tx.conn, crew_id, actor.id, etype, payload, stamp)
            if target is not None:
                seen[item_id] = target
                results.append(ClientEventResult(item_id, "coalesced", seq=target))
                continue
            try:
                res = await tx.emit(
                    crew_id=crew_id,
                    type=etype,
                    actor=actor,
                    payload=payload,
                    summary=client_summary(etype, payload, actor.callsign or ""),
                    severity=CLIENT_SEVERITY.get(etype, "info"),
                    refs={"session_id": actor.id},
                    origin="client",
                    idem_key=key,
                    age_s=int(item.get("age_s") or 0),
                    now=stamp,
                )
            except EventValidationError as e:
                results.append(ClientEventResult(item_id, "rejected", errors=tuple(e.errors[:10])))
                continue
            seen[item_id] = res.seq
            results.append(ClientEventResult(item_id, "accepted", seq=res.seq))
    return results


# ---------------------------------------------------------------------------
# Request idempotency (Idempotency-Key header, crew_idempotency, 72 h)
# ---------------------------------------------------------------------------

IDEMPOTENCY_TTL: Final = timedelta(hours=72)
_TOKEN_KEY_RE: Final = re.compile(r"(^|_)(token|secret|password|code)$", re.IGNORECASE)


def _stored_idem_key(principal: str, route: str, key: str) -> str:
    return hashlib.sha256(f"{principal}\x00{route}\x00{key}".encode()).hexdigest()


def _body_hash(body: Any) -> str:
    return hashlib.sha256(schemas.canonical_json(body)).hexdigest()


def contains_token(value: Any) -> bool:
    """True if a response carries a credential (session/host token, bypass code, secret, API key)."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str) and _TOKEN_KEY_RE.search(k) and v not in (None, ""):
                return True
            if contains_token(v):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_token(v) for v in value)
    return isinstance(value, str) and value.startswith("rem_")


async def idem_lookup(
    conn: aiosqlite.Connection,
    *,
    principal: str,
    route: str,
    key: str,
    body: Any,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The stored response for this (principal, route, key), or None.

    Raises :class:`IdempotencyConflict` when the key was used with a different body.
    Entries older than 72 h are ignored (the retention job deletes them).
    """
    async with conn.execute(
        "SELECT response_json, created_at FROM crew_idempotency WHERE key = ?",
        (_stored_idem_key(principal, route, key),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    if parse_ts(row[1]) < (now or utc_now()) - IDEMPOTENCY_TTL:
        return None
    stored = json.loads(row[0])
    if stored.get("body_sha") != _body_hash(body):
        raise IdempotencyConflict("Idempotency-Key reused with a different request body")
    response: dict[str, Any] = stored["response"]
    return response


async def idem_store(
    conn: aiosqlite.Connection,
    *,
    principal: str,
    route: str,
    key: str,
    body: Any,
    response: Mapping[str, Any],
    now: datetime | None = None,
) -> None:
    """Record a response for replay. Refuses token-bearing responses (never cached, §4.3)."""
    if contains_token(response):
        raise TokenBearingResponse("token-bearing responses are never stored")
    record = {"body_sha": _body_hash(body), "response": dict(response)}
    await conn.execute(
        "INSERT INTO crew_idempotency (key, principal, response_json, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET response_json = excluded.response_json, created_at = excluded.created_at",
        (
            _stored_idem_key(principal, route, key),
            principal,
            _json_text(record),
            format_ts(now or utc_now()),
        ),
    )
