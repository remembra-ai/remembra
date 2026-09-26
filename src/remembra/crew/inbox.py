"""Crew inboxes: Needs-you, Crew and the per-session queue (spec §5.8, D20).

All three live in ``crew_inbox_items`` (``crew.db``), told apart by ``audience``:

* ``project`` (**Needs you**): things only a person can decide. Server-generated
  safety items (breach, baton, stuck, tamper, …) always sort above
  agent-originated items. Agent-originated items are **coalesced by
  (session, kind)** and capped at :data:`AGENT_NEEDS_YOU_PER_SESSION_PER_HOUR`
  per session and :data:`AGENT_NEEDS_YOU_PER_USER_PER_HOUR` per user per hour
  (§5.7, §11.2 "agent-originated Needs-you").
* ``crew``: work any member may pick up; ``open → claimed`` (first taker wins)
  ``→ resolved``.
* ``session``: one live session's queue (mentions, handover offers, overrides,
  collisions). Delivered at the next hook or MCP call and acknowledged by
  ``crew_sessions.delivered_seq`` (:meth:`CrewInbox.pending_for_session`,
  :meth:`CrewInbox.ack_session_queue`).

The agent inbox (``agent_inbox``, main DB) is a separate store handled by
``inbox/manager.py``; crew code reaches it only through the outbox (D35).

Every live item has a ``dedupe_key``; the partial unique index
``uq_inbox_open`` makes a second raise of the same key coalesce into the live
item (``coalesced_count + 1``) instead of adding a row. Creating, claiming and
resolving emit ``inbox.item_created`` / ``inbox.item_claimed`` /
``inbox.item_resolved`` (a dismissal is an ``item_resolved`` whose item state is
``dismissed``). Titles are server templates (ids, slugs, callsigns, counts),
never agent text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas
from remembra.crew.events import Actor, CrewEventLog, EventTx
from remembra.crew.store import new_id, now_iso

AGENT_NEEDS_YOU_PER_SESSION_PER_HOUR: Final = 6
AGENT_NEEDS_YOU_PER_USER_PER_HOUR: Final = 30
_HOUR: Final = timedelta(hours=1)

SAFETY_KINDS: Final = frozenset(schemas.SAFETY_INBOX_KINDS)
LIVE_STATES: Final = schemas.LIVE_INBOX_STATES
READ_STREAM_RE: Final = re.compile(r"[a-z][a-z0-9_.:-]{0,31}")
MAX_TITLE: Final = 200

Title = str | Callable[[int], str]


class InboxError(Exception):
    """Base class; ``status``/``error`` map to the CrewError body."""

    status = 409
    error = "conflict"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ItemStateConflict(InboxError):
    error = "inbox_state"


class ItemAlreadyClaimed(InboxError):
    error = "already_claimed"


class InvalidCursor(InboxError):
    status = 422
    error = "invalid_cursor"


class CrossCrewReference(InboxError):
    status = 422
    error = "cross_crew_reference"


class NotAllowed(InboxError):
    status = 403
    error = "forbidden"


class ValidationFailed(InboxError):
    status = 422
    error = "validation_error"


@dataclass(frozen=True)
class Author:
    """Who writes: a crew session (from its session token) or a human (dashboard JWT). Never from a request body."""

    kind: str  # "agent" | "human"
    user_id: str
    session_id: str | None = None
    agent_id: str | None = None
    callsign: str | None = None
    verified: bool = False
    parent_session_id: str | None = None  # a sub-agent's accountable parent session

    @classmethod
    def human(cls, user_id: str) -> Author:
        return cls(kind="human", user_id=user_id, verified=True)

    @classmethod
    def session(cls, row: Mapping[str, Any]) -> Author:
        return cls(
            kind="agent",
            user_id=str(row["user_id"]),
            session_id=str(row["id"]),
            agent_id=str(row["agent_id"]),
            callsign=str(row["callsign"]),
            verified=bool(row["agent_verified"]),
            parent_session_id=row.get("parent_session_id"),
        )

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    @property
    def principal(self) -> str:
        """Stable id of the writer: the crew session id, or the human's user id."""
        return self.session_id or self.user_id

    @property
    def label(self) -> str:
        """Short name for server templates: the callsign, or ``human``."""
        return self.callsign or ("human" if self.is_human else "agent")

    @property
    def provenance(self) -> str:
        """ "agent codex (key-verified)" / "agent codex (self-declared)" / "human" (§5.8 labels)."""
        if self.is_human:
            return "human"
        return f"agent {self.agent_id} ({'key-verified' if self.verified else 'self-declared'})"

    def actor(self) -> Actor:
        if self.is_human:
            return Actor.human(self.user_id)
        assert self.session_id and self.callsign and self.agent_id
        return Actor.session(
            self.session_id,
            callsign=self.callsign,
            agent_id=self.agent_id,
            user_id=self.user_id,
            verified=self.verified,
            parent_session_id=self.parent_session_id,
        )

    def agent_origin(self) -> AgentOrigin:
        return AgentOrigin(user_id=self.user_id, principal=self.principal)


# kind -> table for same-crew validation of client-supplied ids (§6).
_REF_TABLES: Final[Mapping[str, str]] = {
    "session": "crew_sessions",
    "zone": "crew_zones",
    "claim": "crew_claims",
    "task": "crew_tasks",
    "message": "crew_messages",
    "decision": "crew_decisions",
    "collision": "crew_collisions",
    "report": "crew_reports",
    "checkpoint": "crew_checkpoints",
    "inbox_item": "crew_inbox_items",
    "baton": "crew_batons",
}
_PREFIX_KIND: Final[Mapping[str, str]] = {schemas.ID_PREFIXES[k]: k for k in _REF_TABLES}


def ref_kind(ref_id: str) -> str | None:
    """The entity kind of a client-supplied id by its prefix (``tsk_…`` → ``task``), or None."""
    prefix = ref_id.split("_", 1)[0] if isinstance(ref_id, str) and "_" in ref_id else ""
    kind = _PREFIX_KIND.get(prefix)
    return kind if kind is not None and schemas.is_id(kind, ref_id) else None


async def require_same_crew(conn: Any, crew_id: str, kind: str, ref_id: Any) -> dict[str, Any]:
    """The row of ``ref_id`` when it belongs to ``crew_id``; :class:`CrossCrewReference` (422) otherwise.

    Unknown ids and ids of another crew get the same answer (no existence oracle).
    """
    table = _REF_TABLES.get(kind)
    if table is None:
        raise ValueError(f"unknown reference kind {kind!r}")
    row = None
    if schemas.is_id(kind, ref_id):
        async with conn.execute(f"SELECT * FROM {table} WHERE id = ?", (ref_id,)) as cur:  # noqa: S608 - fixed map
            found = await cur.fetchone()
        row = dict(found) if found is not None else None
    if row is None or row.get("crew_id") != crew_id:
        raise CrossCrewReference(f"The referenced {kind} is not part of this crew.")
    return row


def _clip_title(title: str) -> str:
    flat = " ".join(title.replace("\n", " ").split())
    return flat if len(flat) <= MAX_TITLE else flat[: MAX_TITLE - 1] + "…"


def item_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """``InboxItemView`` (schemas) of a ``crew_inbox_items`` row."""
    return {
        "id": row["id"],
        "audience": row["audience"],
        "recipient": row["recipient"],
        "kind": row["kind"],
        "origin": row["origin"],
        "ref_type": row["ref_type"],
        "ref_id": row["ref_id"],
        "priority": int(row["priority"]),
        "title": row["title"],
        "primary_action": row["primary_action"],
        "state": row["state"],
        "claimed_by": row["claimed_by"],
        "coalesced_count": int(row["coalesced_count"]),
    }


def item_api(row: Mapping[str, Any]) -> dict[str, Any]:
    """The REST shape: the view plus timestamps, seqs and the safety flag used for sorting."""
    out = item_view(row)
    out.update(
        crew_id=row["crew_id"],
        safety=is_safety(row),
        created_seq=row["created_seq"],
        resolved_seq=row["resolved_seq"],
        resolved_by=row["resolved_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    return out


def is_safety(row: Mapping[str, Any]) -> bool:
    return row["origin"] == "server" and row["kind"] in SAFETY_KINDS


def sort_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Server safety items first, then other server/human items, then agent-originated; then priority, newest first."""
    newest_first = sorted(rows, key=lambda r: (str(r["updated_at"]), str(r["id"])), reverse=True)
    return sorted(newest_first, key=lambda r: (0 if is_safety(r) else (2 if r["origin"] == "agent" else 1), int(r["priority"])))


def agent_dedupe_key(user_id: str, principal: str, kind: str) -> str:
    """Dedupe key of an agent-originated Needs-you item: one live item per (session, kind)."""
    return f"agent:{user_id}:{principal}:{kind}"


@dataclass(frozen=True)
class AgentOrigin:
    """Who raised an agent-originated item (for coalescing and caps)."""

    user_id: str
    principal: str  # crew session id


@dataclass(frozen=True)
class RaiseResult:
    item: dict[str, Any] | None  # the live item (None when capped)
    created: bool
    coalesced: bool
    capped: bool
    seq: int | None = None


class CrewInbox:
    """Service over ``crew_inbox_items`` bound to the crew event log."""

    def __init__(self, log: CrewEventLog) -> None:
        self.log = log

    @property
    def db(self) -> Any:
        return self.log.db

    # -- raising items ----------------------------------------------------------------------

    async def raise_item(
        self,
        *,
        crew_id: str,
        audience: str,
        kind: str,
        title: Title,
        actor: Actor,
        dedupe_key: str | None = None,
        origin: str = "server",
        recipient: str | None = None,
        ref_type: str | None = None,
        ref_id: str | None = None,
        priority: int = 2,
        primary_action: str | None = None,
        agent: AgentOrigin | None = None,
        now: datetime | None = None,
    ) -> RaiseResult:
        """Create a live item, or coalesce into the live item with the same ``dedupe_key``.

        ``title`` may be a callable taking the (new) coalesced count, so a
        coalesced item reads "cc-2 asked 4 questions". Agent-originated
        Needs-you items (``origin='agent'``, ``audience='project'``) need
        ``agent`` and are coalesced by (session, kind) and capped per session and
        per user per hour; a capped raise creates nothing (``capped=True``).
        Joins the caller's crew transaction when there is one.
        """
        if audience not in schemas.INBOX_AUDIENCES:
            raise ValueError(f"audience must be one of {schemas.INBOX_AUDIENCES}")
        if kind not in schemas.INBOX_KINDS:
            raise ValueError(f"unknown inbox kind {kind!r}")
        if origin not in schemas.INBOX_ORIGINS:
            raise ValueError(f"origin must be one of {schemas.INBOX_ORIGINS}")
        if not 0 <= int(priority) <= 4:
            raise ValueError("priority must be 0..4")
        if audience == "session" and not recipient:
            raise ValueError("session items need a recipient session id")
        agent_needs_you = origin == "agent" and audience == "project"
        if agent_needs_you:
            if agent is None:
                raise ValueError("agent-originated Needs-you items need the raising session")
            dedupe_key = agent_dedupe_key(agent.user_id, agent.principal, kind)
        if not dedupe_key:
            raise ValueError("dedupe_key is required")
        stamp = now or datetime.now(UTC)

        async with self.log.transaction() as tx:
            live = await self._live_by_key(tx, crew_id, dedupe_key)
            if live is not None:
                count = int(live["coalesced_count"]) + 1
                new_title = _clip_title(title(count) if callable(title) else title)
                await tx.conn.execute(
                    "UPDATE crew_inbox_items SET coalesced_count = ?, title = ?, updated_at = ?,"
                    " ref_type = COALESCE(?, ref_type), ref_id = COALESCE(?, ref_id) WHERE id = ?",
                    (count, new_title, now_iso(stamp), ref_type, ref_id, live["id"]),
                )
                row = await self._get(tx, live["id"])
                assert row is not None
                return RaiseResult(item_api(row), created=False, coalesced=True, capped=False)
            if agent_needs_you:
                assert agent is not None
                if await self._agent_cap_reached(tx, agent, stamp):
                    return RaiseResult(None, created=False, coalesced=False, capped=True)
            item_id = new_id("inbox_item")
            ts = now_iso(stamp)
            await tx.conn.execute(
                """
                INSERT INTO crew_inbox_items (id, crew_id, audience, recipient, kind, origin, ref_type, ref_id, priority,
                    title, primary_action, state, dedupe_key, coalesced_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 1, ?, ?)
                """,
                (
                    item_id,
                    crew_id,
                    audience,
                    recipient,
                    kind,
                    origin,
                    ref_type,
                    ref_id,
                    int(priority),
                    _clip_title(title(1) if callable(title) else title),
                    primary_action,
                    dedupe_key,
                    ts,
                    ts,
                ),
            )
            row = await self._get(tx, item_id)
            assert row is not None
            result = await tx.emit(
                crew_id=crew_id,
                type="inbox.item_created",
                actor=actor,
                payload={"item": item_view(row)},
                summary=f"inbox {audience} item {kind} ({item_id})",
                refs=_refs(row),
            )
            await tx.conn.execute("UPDATE crew_inbox_items SET created_seq = ? WHERE id = ?", (result.seq, item_id))
            row = await self._get(tx, item_id)
            assert row is not None
            return RaiseResult(item_api(row), created=True, coalesced=False, capped=False, seq=result.seq)

    async def _agent_cap_reached(self, tx: EventTx, agent: AgentOrigin, now: datetime) -> bool:
        since = now_iso(now - _HOUR)
        session_prefix = f"agent:{agent.user_id}:{agent.principal}:"
        user_prefix = f"agent:{agent.user_id}:"
        async with tx.conn.execute(
            """
            SELECT SUM(substr(dedupe_key, 1, ?) = ?), SUM(substr(dedupe_key, 1, ?) = ?)
              FROM crew_inbox_items
             WHERE origin = 'agent' AND audience = 'project' AND created_at >= ?
            """,
            (len(session_prefix), session_prefix, len(user_prefix), user_prefix, since),
        ) as cur:
            row = await cur.fetchone()
        per_session = int(row[0] or 0) if row else 0
        per_user = int(row[1] or 0) if row else 0
        return per_session >= AGENT_NEEDS_YOU_PER_SESSION_PER_HOUR or per_user >= AGENT_NEEDS_YOU_PER_USER_PER_HOUR

    # -- transitions ----------------------------------------------------------------------------

    async def mark_seen(self, item_id: str) -> dict[str, Any]:
        """``open → seen`` (no event; the item stays live). Already seen/claimed is a no-op."""
        async with self.db.transaction():
            row = await self._get_plain(item_id)
            if row is None:
                raise ItemStateConflict("inbox item not found")
            if row["state"] == "open":
                await self.db.conn.execute(
                    "UPDATE crew_inbox_items SET state = 'seen', updated_at = ? WHERE id = ? AND state = 'open'",
                    (now_iso(), item_id),
                )
            elif row["state"] not in LIVE_STATES:
                raise ItemStateConflict(f"inbox item is {row['state']}")
            row = await self._get_plain(item_id)
        assert row is not None
        return item_api(row)

    async def claim(self, item_id: str, *, claimer: str, actor: Actor) -> tuple[dict[str, Any], int | None]:
        """First taker wins: ``open|seen → claimed``. Returns ``(item, seq)``; a repeat by the same claimer is a no-op."""
        async with self.log.transaction() as tx:
            row = await self._get(tx, item_id)
            if row is None:
                raise ItemStateConflict("inbox item not found")
            if row["state"] == "claimed":
                if row["claimed_by"] == claimer:
                    return item_api(row), None
                raise ItemAlreadyClaimed(f"already claimed by {row['claimed_by']}")
            if row["state"] not in ("open", "seen"):
                raise ItemStateConflict(f"inbox item is {row['state']}")
            cur = await tx.conn.execute(
                "UPDATE crew_inbox_items SET state = 'claimed', claimed_by = ?, updated_at = ?"
                " WHERE id = ? AND state IN ('open', 'seen')",
                (claimer, now_iso(), item_id),
            )
            if (cur.rowcount or 0) != 1:  # pragma: no cover - serialised by BEGIN IMMEDIATE
                raise ItemAlreadyClaimed("already claimed")
            row = await self._get(tx, item_id)
            assert row is not None
            result = await tx.emit(
                crew_id=row["crew_id"],
                type="inbox.item_claimed",
                actor=actor,
                payload={"item": item_view(row)},
                summary=f"inbox item {item_id} claimed",
                refs=_refs(row),
            )
            return item_api(row), result.seq

    async def resolve(self, item_id: str, *, by: str, actor: Actor, dismiss: bool = False) -> tuple[dict[str, Any], int | None]:
        """``live → resolved`` (or ``dismissed``). Resolving a resolved item again is a no-op."""
        target = "dismissed" if dismiss else "resolved"
        async with self.log.transaction() as tx:
            row = await self._get(tx, item_id)
            if row is None:
                raise ItemStateConflict("inbox item not found")
            if row["state"] == target:
                return item_api(row), None
            if row["state"] not in LIVE_STATES:
                raise ItemStateConflict(f"inbox item is {row['state']}")
            seq = await self._close(tx, row, target, by, actor)
            row = await self._get(tx, item_id)
            assert row is not None
            return item_api(row), seq

    async def resolve_by_ref(
        self,
        crew_id: str,
        *,
        ref_type: str,
        ref_id: str,
        actor: Actor,
        by: str,
        kinds: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve every live item pointing at ``(ref_type, ref_id)`` (e.g. a confirmed decision)."""
        sql = (
            "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND ref_type = ? AND ref_id = ?"
            " AND state IN ('open','seen','claimed')"
        )
        params: list[Any] = [crew_id, ref_type, ref_id]
        kind_list = list(kinds or ())
        if kind_list:
            sql += f" AND kind IN ({', '.join('?' for _ in kind_list)})"
            params.extend(kind_list)
        out: list[dict[str, Any]] = []
        async with self.log.transaction() as tx:
            async with tx.conn.execute(sql, params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            for row in rows:
                await self._close(tx, row, "resolved", by, actor)
                updated = await self._get(tx, row["id"])
                assert updated is not None
                out.append(item_api(updated))
        return out

    async def _close(self, tx: EventTx, row: Mapping[str, Any], state: str, by: str, actor: Actor) -> int:
        await tx.conn.execute(
            "UPDATE crew_inbox_items SET state = ?, resolved_by = ?, updated_at = ? WHERE id = ?",
            (state, by, now_iso(), row["id"]),
        )
        updated = await self._get(tx, row["id"])
        assert updated is not None
        result = await tx.emit(
            crew_id=row["crew_id"],
            type="inbox.item_resolved",
            actor=actor,
            payload={"item": item_view(updated)},
            summary=f"inbox item {row['id']} {state}",
            refs=_refs(updated),
        )
        await tx.conn.execute("UPDATE crew_inbox_items SET resolved_seq = ? WHERE id = ?", (result.seq, row["id"]))
        return result.seq

    # -- reads -----------------------------------------------------------------------------------

    async def get(self, item_id: str) -> dict[str, Any] | None:
        row = await self._get_plain(item_id)
        return item_api(row) if row else None

    async def list_items(
        self,
        crew_id: str,
        *,
        audience: str,
        recipient: str | None = None,
        include_closed: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Items of one audience (live ones unless ``include_closed``), safety first (§5.8)."""
        if audience not in schemas.INBOX_AUDIENCES:
            raise ValueError(f"audience must be one of {schemas.INBOX_AUDIENCES}")
        sql = "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND audience = ?"
        params: list[Any] = [crew_id, audience]
        if recipient is not None:
            sql += " AND recipient = ?"
            params.append(recipient)
        if not include_closed:
            sql += " AND state IN ('open','seen','claimed')"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = await self.db.fetchall(sql, params)
        return [item_api(r) for r in sort_items(rows)]

    async def counts(self, crew_id: str) -> dict[str, int]:
        """Live item counts per visible audience (the snapshot's ``inbox_counts``)."""
        rows = await self.db.fetchall(
            "SELECT audience, COUNT(*) AS n FROM crew_inbox_items WHERE crew_id = ? AND state IN ('open','seen','claimed')"
            " AND audience IN ('project','crew') GROUP BY audience",
            (crew_id,),
        )
        out = {"project": 0, "crew": 0}
        out.update({r["audience"]: int(r["n"]) for r in rows})
        return out

    async def overview(self, crews: Iterable[Mapping[str, Any]], *, limit: int = 200) -> dict[str, Any]:
        """ "My inbox": live Needs-you items across ``crews`` (already filtered by the caller's access)."""
        by_id = {str(c["id"]): c for c in crews}
        if not by_id:
            return {"items": [], "total": 0, "crews": []}
        marks = ", ".join("?" for _ in by_id)
        rows = await self.db.fetchall(
            f"SELECT * FROM crew_inbox_items WHERE crew_id IN ({marks}) AND audience = 'project'"  # noqa: S608
            " AND state IN ('open','seen','claimed')",
            list(by_id),
        )
        rows = sort_items(rows)
        per_crew: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for r in rows:
            per_crew[r["crew_id"]] = per_crew.get(r["crew_id"], 0) + 1
            if len(items) < limit:
                api = item_api(r)
                api["project_id"] = by_id[r["crew_id"]]["project_id"]
                items.append(api)
        crews_out = [
            {"crew_id": cid, "project_id": c["project_id"], "needs_you": per_crew.get(cid, 0)} for cid, c in by_id.items()
        ]
        return {"items": items, "total": len(rows), "crews": crews_out}

    # -- session queue (§5.8: injection, acknowledged by delivered_seq) --------------------------

    async def pending_for_session(self, crew_id: str, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Open queue items of a session created after its ``delivered_seq``, oldest first."""
        rows = await self.db.fetchall(
            """
            SELECT i.* FROM crew_inbox_items i
              JOIN crew_sessions s ON s.id = i.recipient AND s.crew_id = i.crew_id
             WHERE i.crew_id = ? AND i.audience = 'session' AND i.recipient = ? AND i.state = 'open'
               AND COALESCE(i.created_seq, 0) > s.delivered_seq
             ORDER BY i.created_seq LIMIT ?
            """,
            (crew_id, session_id, max(1, min(int(limit), 200))),
        )
        return [item_api(r) for r in rows]

    async def ack_session_queue(self, crew_id: str, session_id: str, upto_seq: int) -> int:
        """Record delivery up to ``upto_seq`` (monotonic) and mark those queue items ``seen``; returns delivered_seq."""
        async with self.db.transaction():
            head = await self.db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
            if head is None:
                raise InvalidCursor("unknown crew")
            upto = max(0, min(int(upto_seq), int(head["last_seq"])))
            await self.db.conn.execute(
                "UPDATE crew_sessions SET delivered_seq = MAX(delivered_seq, ?) WHERE id = ? AND crew_id = ?",
                (upto, session_id, crew_id),
            )
            await self.db.conn.execute(
                "UPDATE crew_inbox_items SET state = 'seen', updated_at = ?"
                " WHERE crew_id = ? AND audience = 'session' AND recipient = ? AND state = 'open' AND created_seq <= ?",
                (now_iso(), crew_id, session_id, upto),
            )
            row = await self.db.fetchone(
                "SELECT delivered_seq FROM crew_sessions WHERE id = ? AND crew_id = ?", (session_id, crew_id)
            )
        if row is None:
            raise InvalidCursor("unknown session")
        return int(row["delivered_seq"])

    # -- read cursors ------------------------------------------------------------------------------

    async def advance_cursor(self, crew_id: str, principal: str, stream: str, seq: int) -> int:
        """Move a read cursor forward (never back); returns the stored value. 422 on a bad stream or a seq past the head."""
        if not isinstance(stream, str) or not READ_STREAM_RE.fullmatch(stream):
            raise InvalidCursor("stream must be a short lowercase name")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise InvalidCursor("seq must be a non-negative integer")
        async with self.db.transaction():
            head = await self.db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
            if head is None:
                raise InvalidCursor("unknown crew")
            if seq > int(head["last_seq"]):
                raise InvalidCursor(f"seq {seq} is past the crew's last seq {head['last_seq']}")
            await self.db.conn.execute(
                """
                INSERT INTO crew_read_cursors (crew_id, principal, stream, last_seq, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(crew_id, principal, stream)
                DO UPDATE SET last_seq = MAX(last_seq, excluded.last_seq), updated_at = excluded.updated_at
                """,
                (crew_id, principal, stream, seq, now_iso()),
            )
            row = await self.db.fetchone(
                "SELECT last_seq FROM crew_read_cursors WHERE crew_id = ? AND principal = ? AND stream = ?",
                (crew_id, principal, stream),
            )
        assert row is not None
        return int(row["last_seq"])

    async def cursor(self, crew_id: str, principal: str, stream: str) -> int:
        row = await self.db.fetchone(
            "SELECT last_seq FROM crew_read_cursors WHERE crew_id = ? AND principal = ? AND stream = ?",
            (crew_id, principal, stream),
        )
        return int(row["last_seq"]) if row else 0

    # -- helpers -----------------------------------------------------------------------------------

    async def _live_by_key(self, tx: EventTx, crew_id: str, dedupe_key: str) -> dict[str, Any] | None:
        async with tx.conn.execute(
            "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
            (crew_id, dedupe_key),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _get(self, tx: EventTx, item_id: str) -> dict[str, Any] | None:
        async with tx.conn.execute("SELECT * FROM crew_inbox_items WHERE id = ?", (item_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _get_plain(self, item_id: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self.db.fetchone("SELECT * FROM crew_inbox_items WHERE id = ?", (item_id,))
        return result


def _refs(row: Mapping[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {"inbox_item_id": row["id"]}
    if row["audience"] == "session" and row["recipient"]:
        refs["session_id"] = row["recipient"]
    return refs
