"""Crew zones (WP-5): zone rows, zones.yml ingest with policy diff, pending changes, freeze,
suggestions, the repo tree and the no-zone bootstrap.

Spec anchors: D8, D9, D10, D28, D37, §2, §3.2 (``crew_zones``, ``crew_zone_overlaps``,
``crew_zone_files``, ``crew_zone_changes``, ``crew_repo_trees``), §5.9 (freeze), §6 (zone
routes), §8.1 (zones.yml handling), §11 (policy protection).

Every mutation runs in one ``BEGIN IMMEDIATE`` transaction on ``crew.db`` with its events
(:meth:`remembra.crew.events.CrewEventLog.transaction`). Access control is the route's job
(``crew/access.py``); functions here get an already-authorised :class:`Principal`.

This module also holds the small helpers the other WP-5 modules share: :class:`Principal`,
:class:`CrewOpError` (mapped to the ``CrewError`` body by the routes), :class:`CrewOps`
(event log + audit sink + plan limits), inbox items raised by the server, and the zone view.

Interface notes for other work packages:

* WP-8 (snapshot): :func:`zone_view`, :func:`active_commons`, :func:`active_ignore`,
  :func:`bootstrap_active` and :func:`list_pending_change_ids` build the zone part of the
  snapshot; :func:`ensure_policy_zone` makes sure the built-in ``crew-policy`` zone exists.
* WP-4 (join): call :func:`maybe_bootstrap` in the join transaction when the crew goes multi.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import aiosqlite
import structlog

from remembra.crew import policy as P
from remembra.crew import schemas as S
from remembra.crew.events import Actor, CrewEventLog, EventTx
from remembra.crew.gatecore import ZoneIndex, normalize_rel
from remembra.crew.limits import CrewLimits, enforce_zone_capacity
from remembra.crew.settings import load_settings
from remembra.crew.store import CrewStore, NotFound, dumps, loads, new_id, now_iso, parse_iso

log = structlog.get_logger(__name__)

LIVE_CLAIM_STATES: Final = ("active", "offered", "reserved")
PENDING_SUPERSEDED_BY: Final = "system:superseded"
MAX_REASON: Final = 280


# ---------------------------------------------------------------------------
# Shared primitives (used by claims, bypass, collisions and the routes)
# ---------------------------------------------------------------------------


class CrewOpError(Exception):
    """A refused crew operation. Routes turn it into ``HTTPException(status, CrewError body)``."""

    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        *,
        blockers: Sequence[Mapping[str, Any]] | None = None,
        **extra: Any,
    ) -> None:
        self.status = status
        self.error = error
        self.message = message
        self.blockers = list(blockers or [])
        self.extra = {k: v for k, v in extra.items() if v is not None}
        super().__init__(f"{error}: {message}")

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error, "message": self.message}
        if self.blockers:
            out["blockers"] = self.blockers
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class Principal:
    """Who is acting. Sessions come from a session token, humans from a dashboard login (D27)."""

    kind: str  # session | human | system
    user_id: str
    session: Mapping[str, Any] | None = None
    api_key_id: str | None = None

    @classmethod
    def system(cls) -> Principal:
        return cls("system", "system")

    @classmethod
    def human(cls, user_id: str, *, api_key_id: str | None = "jwt_auth") -> Principal:
        return cls("human", user_id, None, api_key_id)

    @classmethod
    def for_session(cls, session: Mapping[str, Any], *, api_key_id: str | None = None) -> Principal:
        return cls("session", str(session["user_id"]), session, api_key_id)

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    @property
    def session_id(self) -> str | None:
        return str(self.session["id"]) if self.session else None

    @property
    def name(self) -> str:
        """Display handle for templates: a callsign, ``human`` or ``system``."""
        if self.session is not None:
            return str(self.session.get("callsign") or "session")
        return "human" if self.is_human else "system"

    def actor(self) -> Actor:
        if self.session is not None:
            return Actor.session(
                str(self.session["id"]),
                callsign=str(self.session["callsign"]),
                agent_id=str(self.session["agent_id"]),
                user_id=str(self.session["user_id"]),
                verified=bool(self.session.get("agent_verified")),
            )
        if self.is_human:
            return Actor.human(self.user_id)
        return Actor.system()


AuditSink = Callable[[str, str, str | None, Mapping[str, Any]], Awaitable[None]]


@dataclass
class CrewOps:
    """What WP-5 services need: the event log (crew.db + bus), an audit sink and the plan limits."""

    log: CrewEventLog
    audit_sink: AuditSink | None = None
    limits: CrewLimits | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def db(self) -> Any:
        return self.log.db

    @property
    def store(self) -> CrewStore:
        return CrewStore(self.log.db)  # type: ignore[arg-type]

    def audit(self, user_id: str, action: str, resource_id: str | None, details: Mapping[str, Any]) -> None:
        """Queue an ``audit_log`` row (main DB) for after the crew transaction commits (never inside it)."""
        sink = self.audit_sink
        if sink is None:
            return
        payload = dict(details)

        async def write() -> None:
            try:
                await sink(user_id, action, resource_id, payload)
            except Exception as e:  # the crew event log already holds the record
                log.error("crew_audit_write_failed", action=action, error_type=type(e).__name__)

        if not self.db.after_commit(write):
            raise RuntimeError("CrewOps.audit must be called inside a crew transaction")


def main_db_audit_sink(main_db: Any) -> AuditSink:
    """An :data:`AuditSink` writing to the main database ``audit_log`` (retained, never deleted)."""
    from remembra.security.audit import AuditLogger

    async def sink(user_id: str, action: str, resource_id: str | None, details: Mapping[str, Any]) -> None:
        await main_db.log_audit_event(
            audit_id=AuditLogger.generate_audit_id(),
            user_id=user_id,
            action=action,
            api_key_id=details.get("api_key_id"),
            resource_id=resource_id,
            success=True,
            error_message=json.dumps(dict(details), sort_keys=True, default=str)[:2000],
        )

    return sink


def utcnow() -> datetime:
    return datetime.now(UTC)


def clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(str(text).split())
    return cleaned[:limit]


async def fetchone(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    cursor = await conn.execute(sql, tuple(params))
    row = await cursor.fetchone()
    if row is None:
        return None
    names = [d[0] for d in cursor.description]
    return dict(zip(names, tuple(row), strict=True))


async def fetchall(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor = await conn.execute(sql, tuple(params))
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, tuple(r), strict=True)) for r in await cursor.fetchall()]


async def crew_row(conn: aiosqlite.Connection, crew_id: str) -> dict[str, Any]:
    row = await fetchone(conn, "SELECT * FROM crews WHERE id = ?", (crew_id,))
    if row is None:
        raise NotFound(crew_id)
    return row


async def crew_settings(conn: aiosqlite.Connection, crew_id: str) -> dict[str, Any]:
    return load_settings((await crew_row(conn, crew_id))["settings"])


# -- inbox items raised by the server (§5.8) ---------------------------------------


def inbox_item_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "audience": row["audience"],
        "recipient": row.get("recipient"),
        "kind": row["kind"],
        "origin": row["origin"],
        "ref_type": row.get("ref_type"),
        "ref_id": row.get("ref_id"),
        "priority": int(row.get("priority") or 2),
        "title": row["title"],
        "primary_action": row.get("primary_action"),
        "state": row["state"],
        "claimed_by": row.get("claimed_by"),
        "coalesced_count": int(row.get("coalesced_count") or 1),
    }


async def raise_inbox_item(
    tx: EventTx,
    crew_id: str,
    *,
    audience: str,
    kind: str,
    title: str,
    dedupe_key: str,
    recipient: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    priority: int = 2,
    primary_action: str | None = None,
    actor: Actor | None = None,
) -> dict[str, Any]:
    """Open (or coalesce into) a server-generated inbox item and emit ``inbox.item_created`` once.

    ``dedupe_key`` is unique among live items of the crew (``uq_inbox_open``): a second raise
    while one is open bumps ``coalesced_count`` instead of creating a duplicate. WP-7 owns the
    inbox service; this writes the same rows through the same index.
    """
    assert audience in S.INBOX_AUDIENCES and kind in S.INBOX_KINDS, (audience, kind)
    now = now_iso()
    existing = await fetchone(
        tx.conn,
        "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
        (crew_id, dedupe_key),
    )
    if existing is not None:
        await tx.conn.execute(
            "UPDATE crew_inbox_items SET coalesced_count = coalesced_count + 1, updated_at = ? WHERE id = ?",
            (now, existing["id"]),
        )
        existing["coalesced_count"] = int(existing["coalesced_count"]) + 1
        return existing
    item_id = new_id("inbox_item")
    title = clip(title, 200) or kind
    await tx.conn.execute(
        """INSERT INTO crew_inbox_items (id, crew_id, audience, recipient, kind, origin, ref_type, ref_id, priority, title,
               primary_action, state, dedupe_key, coalesced_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'server', ?, ?, ?, ?, ?, 'open', ?, 1, ?, ?)""",
        (item_id, crew_id, audience, recipient, kind, ref_type, ref_id, priority, title, primary_action, dedupe_key, now, now),
    )
    row = await fetchone(tx.conn, "SELECT * FROM crew_inbox_items WHERE id = ?", (item_id,))
    assert row is not None
    res = await tx.emit(
        crew_id=crew_id,
        type="inbox.item_created",
        actor=actor or Actor.system(),
        payload={"item": inbox_item_view(row)},
        summary=f"inbox item {kind} for {audience}",
        refs={"inbox_item_id": item_id},
    )
    await tx.conn.execute("UPDATE crew_inbox_items SET created_seq = ? WHERE id = ?", (res.seq, item_id))
    return row


async def resolve_inbox_items(
    tx: EventTx,
    crew_id: str,
    *,
    ref_ids: Iterable[str] = (),
    dedupe_keys: Iterable[str] = (),
    kinds: Iterable[str] | None = None,
    resolved_by: str = "system",
    actor: Actor | None = None,
) -> list[str]:
    """Resolve live items by ``ref_id`` or ``dedupe_key`` (optionally limited to ``kinds``)."""
    refs, keys = list(ref_ids), list(dedupe_keys)
    if not refs and not keys:
        return []
    clauses, params = [], [crew_id]
    if refs:
        clauses.append(f"ref_id IN ({','.join('?' for _ in refs)})")
        params.extend(refs)
    if keys:
        clauses.append(f"dedupe_key IN ({','.join('?' for _ in keys)})")
        params.extend(keys)
    sql = f"SELECT * FROM crew_inbox_items WHERE crew_id = ? AND state IN ('open','seen','claimed') AND ({' OR '.join(clauses)})"
    kind_list = list(kinds) if kinds is not None else None
    if kind_list is not None:
        sql += f" AND kind IN ({','.join('?' for _ in kind_list)})"
        params.extend(kind_list)
    rows = await fetchall(tx.conn, sql, params)
    now = now_iso()
    out: list[str] = []
    for row in rows:
        await tx.conn.execute(
            "UPDATE crew_inbox_items SET state = 'resolved', resolved_by = ?, updated_at = ? WHERE id = ?",
            (resolved_by, now, row["id"]),
        )
        row["state"] = "resolved"
        res = await tx.emit(
            crew_id=crew_id,
            type="inbox.item_resolved",
            actor=actor or Actor.system(),
            payload={"item": inbox_item_view(row)},
            summary=f"inbox item {row['kind']} resolved",
            refs={"inbox_item_id": row["id"]},
        )
        await tx.conn.execute("UPDATE crew_inbox_items SET resolved_seq = ? WHERE id = ?", (res.seq, row["id"]))
        out.append(str(row["id"]))
    return out


# ---------------------------------------------------------------------------
# Zone rows and views
# ---------------------------------------------------------------------------


def _arr(value: Any) -> list[Any]:
    parsed = loads(value, []) if isinstance(value, str) else value
    return list(parsed or [])


def zone_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """``ZoneView`` (schemas) of a ``crew_zones`` row."""
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": str(row["title"])[:120],
        "parent_id": row.get("parent_id"),
        "is_leaf": bool(row.get("is_leaf", 1)),
        "builtin": bool(row.get("builtin", 0)),
        "include_globs": _arr(row.get("include_globs")),
        "exclude_globs": _arr(row.get("exclude_globs")),
        "services": _arr(row.get("services")),
        "command_patterns": _arr(row.get("command_patterns")),
        "mcp_tools": [{"tool": r.get("tool"), "service": r.get("service")} for r in _arr(row.get("mcp_tools"))],
        "mode": row["mode"],
        "auto_claim": bool(row.get("auto_claim", 1)),
        "protected": bool(row.get("protected", 0)),
        "reserve_for": row.get("reserve_for"),
        "fail_closed": bool(row.get("fail_closed", 0)),
        "frozen_by": row.get("frozen_by"),
        "frozen_note": row.get("frozen_note"),
        "frozen_until": row.get("frozen_until"),
        "source": row["source"],
        "version": int(row.get("version") or 1),
    }


def zone_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """The dashboard view: ``ZoneView`` plus description, colour, file estimate and archive time."""
    return {
        **zone_view(row),
        "description": row.get("description"),
        "color": row.get("color"),
        "files_estimate": row.get("files_estimate"),
        "archived_at": row.get("archived_at"),
    }


def is_frozen(row: Mapping[str, Any], now: datetime | None = None) -> bool:
    if not row.get("frozen_by"):
        return False
    until = row.get("frozen_until")
    return until is None or parse_iso(str(until)) > (now or utcnow())


async def load_zone_rows(conn: aiosqlite.Connection, crew_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM crew_zones WHERE crew_id = ?"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    return await fetchall(conn, sql + " ORDER BY builtin DESC, slug", (crew_id,))


async def zone_index(conn: aiosqlite.Connection, crew_id: str) -> ZoneIndex:
    """gatecore's :class:`ZoneIndex` over the crew's live zones, commons and ignore."""
    rows = await load_zone_rows(conn, crew_id)
    return ZoneIndex([zone_view(r) for r in rows], await active_commons(conn, crew_id), await active_ignore(conn, crew_id))


async def zone_file(conn: aiosqlite.Connection, crew_id: str) -> dict[str, Any] | None:
    return await fetchone(conn, "SELECT * FROM crew_zone_files WHERE crew_id = ?", (crew_id,))


def _file_policy(file_row: Mapping[str, Any] | None) -> P.Policy | None:
    if file_row is None:
        return None
    try:
        return P.Policy.from_dict(json.loads(file_row["compiled"]))
    except (ValueError, KeyError, TypeError):
        return None


async def active_commons(conn: aiosqlite.Connection, crew_id: str) -> list[dict[str, str]]:
    """Commons in force: declared in zones.yml (applied part) plus the built-in lockfile/migration defaults."""
    return P.effective_commons(_file_policy(await zone_file(conn, crew_id)))


async def active_ignore(conn: aiosqlite.Connection, crew_id: str) -> list[str]:
    pol = _file_policy(await zone_file(conn, crew_id))
    return list(pol.ignore) if pol else []


async def live_claim_zone_ids(conn: aiosqlite.Connection, crew_id: str) -> set[str]:
    rows = await fetchall(
        conn,
        "SELECT DISTINCT zone_id FROM crew_claims WHERE crew_id = ? AND zone_id IS NOT NULL AND state IN "
        "('active','offered','reserved')",
        (crew_id,),
    )
    return {str(r["zone_id"]) for r in rows}


async def bootstrap_active(conn: aiosqlite.Connection, crew_id: str) -> bool:
    """True while no-zone bootstrap zones are the crew's only zones (the brief says "temporary zones")."""
    rows = await load_zone_rows(conn, crew_id)
    user = [r for r in rows if not r["builtin"]]
    return bool(user) and all(r["source"] == "suggested" for r in user)


async def list_pending_change_ids(conn: aiosqlite.Connection, crew_id: str) -> list[str]:
    rows = await fetchall(
        conn, "SELECT id FROM crew_zone_changes WHERE crew_id = ? AND state = 'pending' ORDER BY created_at", (crew_id,)
    )
    return [str(r["id"]) for r in rows]


# ---------------------------------------------------------------------------
# The built-in crew-policy zone (D28)
# ---------------------------------------------------------------------------


async def ensure_policy_zone(tx: EventTx, crew_id: str) -> dict[str, Any]:
    """Create the always-on protected ``crew-policy`` zone if missing (idempotent; emits ``zone.created`` once)."""
    row = await fetchone(tx.conn, "SELECT * FROM crew_zones WHERE crew_id = ? AND slug = ?", (crew_id, P.BUILTIN_ZONE_SLUG))
    if row is not None:
        if row["archived_at"] is not None or not row["protected"] or not row["builtin"]:
            # Nothing may weaken the built-in zone; restore it if the row was tampered with.
            await tx.conn.execute(
                "UPDATE crew_zones SET archived_at = NULL, protected = 1, builtin = 1, fail_closed = 1, auto_claim = 0,"
                " include_globs = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (dumps(list(P.BUILTIN_ZONE_GLOBS)), now_iso(), row["id"]),
            )
            row = await fetchone(tx.conn, "SELECT * FROM crew_zones WHERE id = ?", (row["id"],))
            assert row is not None
        return row
    z = P.builtin_zone()
    zone_id = new_id("zone")
    now = now_iso()
    await tx.conn.execute(
        """INSERT INTO crew_zones (id, crew_id, slug, title, description, parent_id, is_leaf, builtin, include_globs,
               exclude_globs, services, command_patterns, mcp_tools, mode, auto_claim, protected, reserve_for, fail_closed,
               source, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, NULL, NULL, 1, 1, ?, '[]', '[]', '[]', '[]', 'exclusive', 0, 1, NULL, 1, 'builtin',
               'system', ?, ?)""",
        (zone_id, crew_id, z.slug, z.title, dumps(list(z.include)), now, now),
    )
    row = await fetchone(tx.conn, "SELECT * FROM crew_zones WHERE id = ?", (zone_id,))
    assert row is not None
    await tx.emit(
        crew_id=crew_id,
        type="zone.created",
        actor=Actor.system(),
        payload={"zone": zone_view(row)},
        summary=f"built-in zone {z.slug} created",
        refs={"zone_id": zone_id},
    )
    return row


# ---------------------------------------------------------------------------
# Structure: parents, leaves and the overlap table
# ---------------------------------------------------------------------------


def ancestors_of(zone_id: str, by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    seen = {zone_id}
    cur = by_id.get(zone_id)
    while cur is not None and cur.get("parent_id") and cur["parent_id"] not in seen:
        pid = str(cur["parent_id"])
        seen.add(pid)
        out.append(pid)
        cur = by_id.get(pid)
    return out


async def _restructure(tx: EventTx, crew_id: str) -> set[str]:
    """Recompute ``is_leaf`` and ``crew_zone_overlaps`` for live zones. Returns ids whose ``is_leaf`` changed."""
    rows = await load_zone_rows(tx.conn, crew_id)
    live_ids = {str(r["id"]) for r in rows}
    parents = {str(r["parent_id"]) for r in rows if r.get("parent_id") and str(r["parent_id"]) in live_ids}
    changed: set[str] = set()
    now = now_iso()
    for r in rows:
        leaf = 0 if str(r["id"]) in parents else 1
        if int(r["is_leaf"]) != leaf:
            await tx.conn.execute(
                "UPDATE crew_zones SET is_leaf = ?, version = version + 1, updated_at = ? WHERE id = ?", (leaf, now, r["id"])
            )
            changed.add(str(r["id"]))
    await tx.conn.execute("DELETE FROM crew_zone_overlaps WHERE crew_id = ?", (crew_id,))
    by_id = {str(r["id"]): r for r in rows}
    user = [r for r in rows if not r["builtin"]]
    pairs: list[tuple[str, str, str]] = []
    for i, a in enumerate(user):
        ga = _arr(a["include_globs"])
        anc_a = set(ancestors_of(str(a["id"]), by_id))
        for b in user[i + 1 :]:
            if str(b["id"]) in anc_a or str(a["id"]) in set(ancestors_of(str(b["id"]), by_id)):
                continue  # parent/child coverage is handled through the hierarchy, not the overlap table
            if P.any_overlap(ga, _arr(b["include_globs"])):
                pairs.append((crew_id, str(a["id"]), str(b["id"])))
                pairs.append((crew_id, str(b["id"]), str(a["id"])))
    if pairs:
        await tx.conn.executemany("INSERT OR IGNORE INTO crew_zone_overlaps (crew_id, zone_a, zone_b) VALUES (?, ?, ?)", pairs)
    return changed


async def related_zone_ids(conn: aiosqlite.Connection, crew_id: str, zone_id: str) -> set[str]:
    """The zone, its ancestors, its descendants and every zone whose globs overlap it (claim conflicts, §5.1)."""
    rows = await load_zone_rows(conn, crew_id)
    by_id = {str(r["id"]): r for r in rows}
    out = {zone_id, *ancestors_of(zone_id, by_id)}
    for zid in by_id:
        if zone_id in ancestors_of(zid, by_id):
            out.add(zid)
    for r in await fetchall(conn, "SELECT zone_b FROM crew_zone_overlaps WHERE crew_id = ? AND zone_a = ?", (crew_id, zone_id)):
        out.add(str(r["zone_b"]))
    return out


# ---------------------------------------------------------------------------
# Applying a policy to crew_zones
# ---------------------------------------------------------------------------

_ZONE_FIELDS: Final = (
    "title",
    "description",
    "include_globs",
    "exclude_globs",
    "services",
    "command_patterns",
    "mcp_tools",
    "mode",
    "auto_claim",
    "protected",
    "reserve_for",
    "fail_closed",
    "color",
)


def _zone_columns(z: P.ZoneDef) -> dict[str, Any]:
    return {
        "title": z.title,
        "description": z.description,
        "include_globs": dumps(list(z.include)),
        "exclude_globs": dumps(list(z.exclude)),
        "services": dumps(list(z.services)),
        "command_patterns": dumps(list(z.commands)),
        "mcp_tools": dumps([{"tool": t, "service": s} for t, s in z.mcp_tools]),
        "mode": z.mode,
        "auto_claim": 1 if z.auto_claim else 0,
        "protected": 1 if z.protected else 0,
        "reserve_for": z.reserve_for,
        "fail_closed": 1 if z.fail_closed else 0,
        "color": z.color,
    }


def _norm_col(key: str, value: Any) -> Any:
    if key in ("include_globs", "exclude_globs", "services", "command_patterns", "mcp_tools"):
        return dumps(_arr(value))
    if key in ("auto_claim", "protected", "fail_closed"):
        return int(bool(value))
    return value


async def _insert_zone(tx: EventTx, crew_id: str, z: P.ZoneDef, *, source: str, created_by: str) -> str:
    zone_id = new_id("zone")
    cols = _zone_columns(z)
    now = now_iso()
    await tx.conn.execute(
        """INSERT INTO crew_zones (id, crew_id, slug, title, description, parent_id, is_leaf, builtin, include_globs,
               exclude_globs, services, command_patterns, mcp_tools, mode, auto_claim, protected, reserve_for, fail_closed,
               source, color, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, NULL, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            zone_id,
            crew_id,
            z.slug,
            cols["title"],
            cols["description"],
            cols["include_globs"],
            cols["exclude_globs"],
            cols["services"],
            cols["command_patterns"],
            cols["mcp_tools"],
            cols["mode"],
            cols["auto_claim"],
            cols["protected"],
            cols["reserve_for"],
            cols["fail_closed"],
            source,
            cols["color"],
            created_by,
            now,
            now,
        ),
    )
    return zone_id


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    released_claims: list[str] = field(default_factory=list)

    @property
    def zone_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.created, *self.updated, *self.archived]))


async def _archive_zone_rows(
    ops: CrewOps, tx: EventTx, crew_id: str, zone_ids: Sequence[str], actor: Actor, reason: str
) -> list[str]:
    """Archive zones and release their live claims (claims on an archived zone protect nothing)."""
    from remembra.crew import claims as C

    released: list[str] = []
    now = now_iso()
    for zid in zone_ids:
        await tx.conn.execute(
            "UPDATE crew_zones SET archived_at = ?, version = version + 1, updated_at = ? WHERE id = ? AND archived_at IS NULL",
            (now, now, zid),
        )
        await tx.conn.execute(
            "UPDATE crew_zones SET parent_id = NULL, version = version + 1, updated_at = ? WHERE crew_id = ? AND parent_id = ?",
            (now, crew_id, zid),
        )
        released.extend(await C.end_zone_claims(ops, tx, crew_id, zid, actor=actor, reason=reason))
    return released


async def apply_policy(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    policy: P.Policy,
    *,
    actor: Actor,
    source: str = "repo",
    manage_slugs: Iterable[str] | None = None,
) -> SyncResult:
    """Make the crew's zones match ``policy`` (inside the caller's transaction) and emit zone events.

    Zones of ``source`` that the policy no longer declares are archived (their live claims end).
    A declared slug that exists as a dashboard, API or suggested zone is taken over. With a repo
    policy that declares zones, suggested bootstrap zones without a live claim are retired.
    """
    await ensure_policy_zone(tx, crew_id)
    rows = {str(r["slug"]): r for r in await load_zone_rows(tx.conn, crew_id, include_archived=True)}
    res = SyncResult()
    now = now_iso()
    touched: set[str] = set()
    for z in policy.zones:
        row = rows.get(z.slug)
        if row is not None and row["builtin"]:
            continue
        if row is None:
            zid = await _insert_zone(tx, crew_id, z, source=source, created_by=actor.id)
            res.created.append(zid)
            continue
        cols = _zone_columns(z)
        diffs = {k: v for k, v in cols.items() if _norm_col(k, row.get(k)) != _norm_col(k, v)}
        if row["archived_at"] is not None or row["source"] != source or diffs:
            sets = ", ".join(f"{k} = ?" for k in diffs)
            params = [*diffs.values()]
            sql = "UPDATE crew_zones SET " + (sets + ", " if sets else "")
            sql += "source = ?, archived_at = NULL, version = version + 1, updated_at = ? WHERE id = ?"
            await tx.conn.execute(sql, (*params, source, now, row["id"]))
            (res.created if row["archived_at"] is not None else res.updated).append(str(row["id"]))
        touched.add(str(row["id"]))
    # parents (after every declared zone has an id)
    fresh = {str(r["slug"]): r for r in await load_zone_rows(tx.conn, crew_id, include_archived=True)}
    for z in policy.zones:
        row = fresh.get(z.slug)
        if row is None or row["builtin"]:
            continue
        want = str(fresh[z.parent]["id"]) if z.parent and z.parent in fresh else None
        if row.get("parent_id") != want:
            await tx.conn.execute(
                "UPDATE crew_zones SET parent_id = ?, version = version + 1, updated_at = ? WHERE id = ?", (want, now, row["id"])
            )
            if str(row["id"]) not in res.created and str(row["id"]) not in res.updated:
                res.updated.append(str(row["id"]))
    declared = set(policy.slugs)
    managed = set(manage_slugs) if manage_slugs is not None else None
    retire: list[str] = []
    live = await live_claim_zone_ids(tx.conn, crew_id)
    for slug, row in fresh.items():
        if row["builtin"] or row["archived_at"] is not None or slug in declared:
            continue
        own = row["source"] == source and (managed is None or slug in managed)
        stale_suggestion = source == "repo" and bool(policy.zones) and row["source"] == "suggested" and str(row["id"]) not in live
        if own or stale_suggestion:
            retire.append(str(row["id"]))
    res.released_claims = await _archive_zone_rows(ops, tx, crew_id, retire, actor, "zone_archived")
    res.archived.extend(retire)
    leaf_changed = await _restructure(tx, crew_id)
    for zid in leaf_changed:
        if zid not in res.created and zid not in res.updated and zid not in res.archived:
            res.updated.append(zid)
    await _emit_zone_events(tx, crew_id, res, actor)
    return res


async def _emit_zone_events(tx: EventTx, crew_id: str, res: SyncResult, actor: Actor) -> None:
    for kind, ids in (("created", res.created), ("updated", res.updated), ("archived", res.archived)):
        for zid in ids:
            row = await fetchone(tx.conn, "SELECT * FROM crew_zones WHERE id = ?", (zid,))
            if row is None:
                continue
            await tx.emit(
                crew_id=crew_id,
                type=f"zone.{kind}",
                actor=actor,
                payload={"zone": zone_view(row)},
                summary=f"zone {row['slug']} {kind}",
                refs={"zone_id": zid},
            )


# ---------------------------------------------------------------------------
# zones.yml ingest (D9, §8.1)
# ---------------------------------------------------------------------------


async def _diff_base(conn: aiosqlite.Connection, crew_id: str, new: P.Policy) -> P.Policy:
    """The policy a zones.yml upload is compared with: repo zones (and any zone it takes over) as they are now."""
    rows = await load_zone_rows(conn, crew_id)
    by_id = {str(r["id"]): r for r in rows}
    new_slugs = set(new.slugs)
    zones = [
        P.zone_from_row(r, parent_slug=str(by_id[r["parent_id"]]["slug"]) if r.get("parent_id") in by_id else None)
        for r in rows
        if not r["builtin"] and (r["source"] == "repo" or r["slug"] in new_slugs)
    ]
    current = _file_policy(await zone_file(conn, crew_id))
    return P.Policy(
        zones=tuple(sorted(zones, key=lambda z: z.slug)),
        commons=current.commons if current else (),
        ignore=current.ignore if current else (),
        enforcement=current.enforcement if current else None,
    )


async def _live_slugs(conn: aiosqlite.Connection, crew_id: str) -> set[str]:
    ids = await live_claim_zone_ids(conn, crew_id)
    if not ids:
        return set()
    rows = await fetchall(conn, f"SELECT slug FROM crew_zones WHERE id IN ({','.join('?' for _ in ids)})", list(ids))
    return {str(r["slug"]) for r in rows}


async def _check_zone_cap(
    ops: CrewOps, conn: aiosqlite.Connection, crew_id: str, adding_slugs: Iterable[str], *, replaces_repo: bool = False
) -> None:
    """Refuse only growth past the plan's zone cap. With ``replaces_repo`` (a zones.yml), repo and suggested zones
    the file no longer declares are about to be retired, so they do not count."""
    if ops.limits is None:
        return
    rows = await load_zone_rows(conn, crew_id)
    live = {r["slug"] for r in rows if not r["builtin"]}
    wanted = set(adding_slugs)
    kept = {r["slug"] for r in rows if not r["builtin"] and not (replaces_repo and r["source"] in ("repo", "suggested"))}
    future = kept | wanted if replaces_repo else live | wanted
    adding = len(future) - len(live)
    try:
        enforce_zone_capacity(len(live), adding, ops.limits)
    except Exception as e:  # HTTPException from limits → our error type
        detail = getattr(e, "detail", {}) or {}
        raise CrewOpError(409, "zone_cap", str(detail.get("message") or "zone limit reached")) from e


async def _store_zone_file(
    tx: EventTx, crew_id: str, applied: P.Policy, *, sha: str, branch: str | None, uploaded_by: str
) -> None:
    now = now_iso()
    await tx.conn.execute(
        """INSERT INTO crew_zone_files (crew_id, yaml_sha, branch, compiled, commons, ignore, enforcement,
            uploaded_by, uploaded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(crew_id) DO UPDATE SET yaml_sha = excluded.yaml_sha, branch = excluded.branch,
               compiled = excluded.compiled, commons = excluded.commons, ignore = excluded.ignore,
               enforcement = excluded.enforcement, uploaded_by = excluded.uploaded_by, uploaded_at = excluded.uploaded_at""",
        (
            crew_id,
            sha,
            branch,
            P.policy_json(applied),
            dumps([{"glob": g, "kind": k} for g, k in applied.commons]),
            dumps(list(applied.ignore)),
            applied.enforcement,
            uploaded_by,
            now,
        ),
    )
    await tx.conn.execute("UPDATE crews SET active_zones_sha = ?, updated_at = ? WHERE id = ?", (sha, now, crew_id))


async def _apply_enforcement(ops: CrewOps, tx: EventTx, crew_id: str, enforcement: str | None, actor: Actor) -> None:
    if enforcement is None:
        return
    settings, version = await ops.store.get_settings(crew_id)
    if settings["enforcement"] == enforcement:
        return
    _, new_version, changed = await ops.store.patch_settings(crew_id, {"enforcement": enforcement}, if_match=version)
    await tx.emit(
        crew_id=crew_id,
        type="crew.settings_changed",
        actor=actor,
        payload={"settings_version": new_version, "changed_keys": changed, "enforcement": enforcement},
        summary=f"crew enforcement set to {enforcement} by zones policy",
    )


async def _sync_and_record(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    policy: P.Policy,
    diff: P.PolicyDiff,
    *,
    sha: str,
    branch: str | None,
    actor: Actor,
    uploaded_by: str,
) -> SyncResult:
    res = await apply_policy(ops, tx, crew_id, policy, actor=actor, source="repo")
    await _store_zone_file(tx, crew_id, policy, sha=sha, branch=branch, uploaded_by=uploaded_by)
    await _apply_enforcement(ops, tx, crew_id, policy.enforcement, actor)
    await tx.emit(
        crew_id=crew_id,
        type="zone.synced",
        actor=actor,
        payload={
            "sha": sha[:64],
            "branch": branch,
            "diff_summary": diff.summary(),
            "policy_changed": diff.changed,
            "zone_ids": res.zone_ids[:200],
        },
        summary=f"zones synced ({sha[:12]}): {diff.summary(160)}",
    )
    return res


async def _supersede_pending(tx: EventTx, crew_id: str, actor: Actor) -> list[str]:
    rows = await fetchall(tx.conn, "SELECT id FROM crew_zone_changes WHERE crew_id = ? AND state = 'pending'", (crew_id,))
    now = now_iso()
    for r in rows:
        await tx.conn.execute(
            "UPDATE crew_zone_changes SET state = 'rejected', decided_by = ?, decided_at = ? WHERE id = ?",
            (PENDING_SUPERSEDED_BY, now, r["id"]),
        )
        await tx.emit(
            crew_id=crew_id,
            type="zone.change_decided",
            actor=Actor.system(),
            payload={"change_id": r["id"], "decision": "rejected"},
            summary=f"zone change {r['id']} superseded by a newer upload",
        )
    await resolve_inbox_items(tx, crew_id, ref_ids=[str(r["id"]) for r in rows], kinds=["zone_change_pending"])
    return [str(r["id"]) for r in rows]


async def upload_zones_file(
    ops: CrewOps, crew_id: str, principal: Principal, *, yaml_text: str, sha: str, branch: str | None
) -> dict[str, Any]:
    """``PUT /crews/{id}/zones/file`` (§8.1): diff against the active policy; apply or hold loosening for a human.

    Returns ``{"result": "applied" | "pending" | "unchanged", "change_id"?, "diff", "seq"}``.
    Non-loosening changes apply at once (``zone.synced``). With any loosening item the
    non-loosening part applies and the rest lands in ``crew_zone_changes`` as ``pending`` with a
    Needs-you Approve/Reject card; the old values stay in force until a human approves (D9).
    A newer upload supersedes an older pending change.
    """
    try:
        new = P.parse_zones_yaml(yaml_text)
    except P.PolicyError as e:
        raise CrewOpError(422, "invalid_zones_file", "zones.yml is invalid.", problems=e.errors[:20]) from e
    if not sha or len(sha) > 64:
        raise CrewOpError(422, "invalid_zones_file", "sha is required (at most 64 characters).")
    actor = principal.actor()
    async with ops.log.transaction() as tx:
        await ensure_policy_zone(tx, crew_id)
        current_file = await zone_file(tx.conn, crew_id)
        pending_ids = await list_pending_change_ids(tx.conn, crew_id)
        if current_file is not None and current_file["yaml_sha"] == sha:
            return {"result": "unchanged", "change_id": pending_ids[-1] if pending_ids else None, "diff": None}
        settings = await crew_settings(tx.conn, crew_id)
        old = await _diff_base(tx.conn, crew_id, new)
        live = await _live_slugs(tx.conn, crew_id)
        diff = P.diff_policy(old, new, live_claim_slugs=live, current_enforcement=settings["enforcement"])
        if not principal.is_human:
            # an agent upload may not add or tighten a zone into a crew-wide lock on its own (zone squat)
            diff = P.hold_agent_tightening(old, new, diff)
        interim = P.interim_policy(old, new, diff, current_enforcement=settings["enforcement"]) if diff.loosening else new
        # the cap applies to what is applied now (the interim policy while approval is pending)
        await _check_zone_cap(ops, tx.conn, crew_id, interim.slugs, replaces_repo=True)
        await _supersede_pending(tx, crew_id, actor)
        uploader = principal.session_id or principal.user_id
        if not diff.loosening:
            await _sync_and_record(ops, tx, crew_id, new, diff, sha=sha, branch=branch, actor=actor, uploaded_by=uploader)
            ops.audit(principal.user_id, "crew.zones_synced", crew_id, {"sha": sha, "summary": diff.summary(200)})
            return {"result": "applied", "change_id": None, "diff": diff.to_dict()}
        idiff = P.diff_policy(old, interim, live_claim_slugs=live, current_enforcement=settings["enforcement"])
        await _sync_and_record(ops, tx, crew_id, interim, idiff, sha=sha, branch=branch, actor=actor, uploaded_by=uploader)
        change_id = new_id("zone_change")
        record = {**diff.to_dict(), "policy": new.to_dict(), "branch": branch}
        await tx.conn.execute(
            """INSERT INTO crew_zone_changes (id, crew_id, yaml_sha, uploaded_by_session, uploaded_by_user, diff, loosening,
                   state, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', ?)""",
            (change_id, crew_id, sha, principal.session_id, principal.user_id, dumps(record), now_iso()),
        )
        await tx.emit(
            crew_id=crew_id,
            type="zone.change_pending",
            actor=actor,
            payload={"change_id": change_id, "sha": sha[:64], "loosening": True, "diff_summary": diff.summary()},
            summary=f"zone policy change {change_id} from {principal.name} waits for a human (loosening)",
        )
        await raise_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="zone_change_pending",
            title=f"Zone policy change needs approval: {diff.summary(150)}",
            dedupe_key=f"zone_change:{change_id}",
            ref_type="zone_change",
            ref_id=change_id,
            priority=1,
            primary_action="approve",
            actor=actor,
        )
        ops.audit(principal.user_id, "crew.zones_change_pending", change_id, {"sha": sha, "summary": diff.summary(200)})
        return {"result": "pending", "change_id": change_id, "diff": diff.to_dict()}


def change_view(row: Mapping[str, Any]) -> dict[str, Any]:
    record = loads(row["diff"], {}) or {}
    return {
        "id": row["id"],
        "yaml_sha": row["yaml_sha"],
        "state": row["state"],
        "loosening": bool(row["loosening"]),
        "summary": record.get("summary"),
        "items": record.get("items", []),
        "uploaded_by_session": row.get("uploaded_by_session"),
        "uploaded_by_user": row.get("uploaded_by_user"),
        "decided_by": row.get("decided_by"),
        "decided_at": row.get("decided_at"),
        "created_at": row["created_at"],
    }


async def list_zone_changes(conn: aiosqlite.Connection, crew_id: str, state: str | None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM crew_zone_changes WHERE crew_id = ?"
    params: list[Any] = [crew_id]
    if state:
        sql += " AND state = ?"
        params.append(state)
    return [change_view(r) for r in await fetchall(conn, sql + " ORDER BY created_at DESC LIMIT 200", params)]


async def decide_zone_change(ops: CrewOps, change: Mapping[str, Any], human: Principal, *, approve: bool) -> dict[str, Any]:
    """Approve (apply the full uploaded policy) or reject a pending change. Human principal only (route-enforced)."""
    crew_id = str(change["crew_id"])
    actor = human.actor()
    async with ops.log.transaction() as tx:
        row = await fetchone(tx.conn, "SELECT * FROM crew_zone_changes WHERE id = ?", (change["id"],))
        assert row is not None
        if row["state"] != "pending":
            raise CrewOpError(409, "change_not_pending", f"This zone change is already {row['state']}.")
        record = loads(row["diff"], {}) or {}
        if approve:
            new = P.Policy.from_dict(record.get("policy") or {})
            settings = await crew_settings(tx.conn, crew_id)
            old = await _diff_base(tx.conn, crew_id, new)
            diff = P.diff_policy(
                old, new, live_claim_slugs=await _live_slugs(tx.conn, crew_id), current_enforcement=settings["enforcement"]
            )
            await _check_zone_cap(ops, tx.conn, crew_id, new.slugs, replaces_repo=True)
            await _sync_and_record(
                ops,
                tx,
                crew_id,
                new,
                diff,
                sha=str(row["yaml_sha"]),
                branch=record.get("branch"),
                actor=actor,
                uploaded_by=human.user_id,
            )
        now = now_iso()
        state = "applied" if approve else "rejected"
        await tx.conn.execute(
            "UPDATE crew_zone_changes SET state = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (state, human.user_id, now, row["id"]),
        )
        await tx.emit(
            crew_id=crew_id,
            type="zone.change_decided",
            actor=actor,
            payload={"change_id": row["id"], "decision": "approved" if approve else "rejected"},
            summary=f"zone change {row['id']} {'approved' if approve else 'rejected'} by a human",
        )
        await resolve_inbox_items(
            tx, crew_id, ref_ids=[str(row["id"])], kinds=["zone_change_pending"], resolved_by=human.user_id, actor=actor
        )
        ops.audit(
            human.user_id,
            f"crew.zone_change_{'approved' if approve else 'rejected'}",
            str(row["id"]),
            {"api_key_id": human.api_key_id},
        )
        fresh = await fetchone(tx.conn, "SELECT * FROM crew_zone_changes WHERE id = ?", (row["id"],))
        assert fresh is not None
        return change_view(fresh)


# ---------------------------------------------------------------------------
# Server zones: create, patch, archive (dashboard / API)
# ---------------------------------------------------------------------------

ZONE_BODY_KEYS: Final = frozenset(
    {
        "slug",
        "title",
        "description",
        "parent",
        "include",
        "exclude",
        "mode",
        "auto_claim",
        "protected",
        "reserve_for",
        "fail_closed",
        "services",
        "commands",
        "mcp_tools",
        "color",
    }
)


def _validate_body(slug: str, body: Mapping[str, Any]) -> P.ZoneDef:
    unknown = set(body) - ZONE_BODY_KEYS
    if unknown:
        raise CrewOpError(422, "invalid_zone", f"Unknown field(s): {', '.join(sorted(unknown))}.")
    errors: list[str] = []
    zone = P.validate_zone_body(slug, {k: v for k, v in body.items() if k != "slug"}, "$", errors)
    if zone is not None and not zone.include:
        errors.append("$.include: at least one glob")
    if errors or zone is None:
        raise CrewOpError(422, "invalid_zone", "The zone is invalid.", problems=errors[:20])
    return zone


async def _parent_id(
    conn: aiosqlite.Connection, crew_id: str, parent_slug: str | None, *, child_id: str | None = None
) -> str | None:
    if parent_slug is None:
        return None
    row = await fetchone(
        conn, "SELECT * FROM crew_zones WHERE crew_id = ? AND slug = ? AND archived_at IS NULL", (crew_id, parent_slug)
    )
    if row is None or row["builtin"]:
        raise CrewOpError(422, "invalid_zone", f"Unknown parent zone {parent_slug!r}.")
    if child_id is not None:
        rows = await load_zone_rows(conn, crew_id)
        by_id = {str(r["id"]): r for r in rows}
        if str(row["id"]) == child_id or child_id in ancestors_of(str(row["id"]), by_id):
            raise CrewOpError(422, "invalid_zone", "A zone cannot be nested under itself.")
    return str(row["id"])


async def get_zone(conn: aiosqlite.Connection, zone_id: str) -> dict[str, Any]:
    row = await fetchone(conn, "SELECT * FROM crew_zones WHERE id = ?", (zone_id,))
    if row is None:
        raise NotFound(zone_id)
    return row


async def _agent_zone_check(
    conn: aiosqlite.Connection, crew_id: str, zone: P.ZoneDef, prev: P.ZoneDef | None, *, zone_id: str | None = None
) -> None:
    """An agent's server zone must not lock the crew out: 403 ``human_only`` for a zone squat.

    Agents cannot, on their own, make a zone ``protected``, ``reserve_for`` or ``fail_closed``,
    give it globs that cover the repo root, or lay it over another zone's paths (a zone over
    existing zones turns them into its children and denies their unclaimed paths). Removing any
    of these is human-only too (archive, loosening), so an agent squat could never be undone by
    the agents it locked out. A human does these from the dashboard.
    """
    why = P.agent_zone_concern(zone, prev)
    if why is not None:
        raise CrewOpError(403, "human_only", f"An agent cannot set {why} on a zone; a human does that from the dashboard.")
    added = [g for g in zone.include if prev is None or g not in prev.include]
    if not added:
        return
    for other in await load_zone_rows(conn, crew_id):
        if (zone_id is not None and str(other["id"]) == zone_id) or other["slug"] == zone.slug:
            continue
        if P.any_overlap(added, [str(g) for g in _arr(other.get("include_globs"))]):
            raise CrewOpError(
                403,
                "human_only",
                f"The zone would overlap zone {other['slug']}; a human lays zones over other zones from the dashboard.",
            )


async def create_zone(ops: CrewOps, crew_id: str, principal: Principal, body: Mapping[str, Any]) -> dict[str, Any]:
    """``POST /crews/{id}/zones``: a server zone (``source`` dashboard for humans, api for keys)."""
    slug = body.get("slug")
    if not isinstance(slug, str):
        raise CrewOpError(422, "invalid_zone", "slug is required.")
    zone = _validate_body(slug, body)
    actor = principal.actor()
    source = "dashboard" if principal.is_human else "api"
    async with ops.log.transaction() as tx:
        await ensure_policy_zone(tx, crew_id)
        existing = await fetchone(tx.conn, "SELECT * FROM crew_zones WHERE crew_id = ? AND slug = ?", (crew_id, slug))
        if existing is not None and existing["archived_at"] is None:
            raise CrewOpError(409, "zone_exists", f"Zone {slug} already exists.")
        if not principal.is_human:
            await _agent_zone_check(tx.conn, crew_id, zone, None)
        await _check_zone_cap(ops, tx.conn, crew_id, [slug])
        parent_id = await _parent_id(tx.conn, crew_id, zone.parent)
        now = now_iso()
        if existing is None:
            zid = await _insert_zone(tx, crew_id, zone, source=source, created_by=principal.session_id or principal.user_id)
        else:
            zid = str(existing["id"])
            cols = _zone_columns(zone)
            sets = ", ".join(f"{k} = ?" for k in cols)
            await tx.conn.execute(
                f"UPDATE crew_zones SET {sets}, source = ?, archived_at = NULL, frozen_by = NULL, frozen_note = NULL,"
                " frozen_until = NULL, version = version + 1, updated_at = ? WHERE id = ?",
                (*cols.values(), source, now, zid),
            )
        await tx.conn.execute("UPDATE crew_zones SET parent_id = ? WHERE id = ?", (parent_id, zid))
        res = SyncResult(created=[zid])
        for other in await _restructure(tx, crew_id):
            if other != zid:
                res.updated.append(other)
        await _emit_zone_events(tx, crew_id, res, actor)
        if principal.is_human:
            ops.audit(principal.user_id, "crew.zone_created", zid, {"slug": slug, "api_key_id": principal.api_key_id})
        return zone_detail(await get_zone(tx.conn, zid))


def _merged_def(row: Mapping[str, Any], parent_slug: str | None, patch: Mapping[str, Any]) -> P.ZoneDef:
    current = P.zone_from_row(row, parent_slug=parent_slug)
    base = current.to_dict()
    base.pop("slug")
    merged: dict[str, Any] = {k: v for k, v in base.items() if v is not None}
    merged["mcp_tools"] = [{"tool": t, **({"service": s} if s else {})} for t, s in current.mcp_tools]
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return _validate_body(str(row["slug"]), merged)


async def _repo_policy(conn: aiosqlite.Connection, crew_id: str) -> P.Policy:
    rows = await load_zone_rows(conn, crew_id)
    by_id = {str(r["id"]): r for r in rows}
    zones = [
        P.zone_from_row(r, parent_slug=str(by_id[r["parent_id"]]["slug"]) if r.get("parent_id") in by_id else None)
        for r in rows
        if not r["builtin"] and r["source"] == "repo"
    ]
    current = _file_policy(await zone_file(conn, crew_id))
    return P.Policy(
        tuple(sorted(zones, key=lambda z: z.slug)),
        current.commons if current else (),
        current.ignore if current else (),
        current.enforcement if current else None,
    )


async def patch_zone(
    ops: CrewOps, zone: Mapping[str, Any], principal: Principal, patch: Mapping[str, Any], *, if_match: int
) -> dict[str, Any]:
    """``PATCH /zones/{zid}`` (If-Match). Repo zones return an export patch; loosening edits need a human."""
    if "slug" in patch:
        raise CrewOpError(422, "invalid_zone", "A zone slug cannot be changed; create a new zone instead.")
    crew_id = str(zone["crew_id"])
    actor = principal.actor()
    async with ops.log.transaction() as tx:
        row = await get_zone(tx.conn, str(zone["id"]))
        if row["builtin"]:
            raise CrewOpError(423, "crew_policy", "The built-in crew-policy zone cannot be changed.")
        if row["archived_at"] is not None:
            raise CrewOpError(409, "zone_archived", "This zone is archived.")
        if int(row["version"]) != if_match:
            raise CrewOpError(412, "version_mismatch", "The zone changed; reload it.", current_version=int(row["version"]))
        parent = await fetchone(tx.conn, "SELECT slug FROM crew_zones WHERE id = ?", (row.get("parent_id"),))
        before = P.zone_from_row(row, parent_slug=parent["slug"] if parent else None)
        after = _merged_def(row, before.parent, patch)
        if row["source"] == "repo":
            repo = await _repo_policy(tx.conn, crew_id)
            edited = P.Policy(
                tuple(after if z.slug == after.slug else z for z in repo.zones), repo.commons, repo.ignore, repo.enforcement
            )
            return {"applied": False, "export_patch": P.export_patch(repo, edited), "yaml": P.export_yaml(edited)}
        live = str(row["id"]) in await live_claim_zone_ids(tx.conn, crew_id)
        items = P.diff_zone(before, after, live_claim=live)
        if any(i.loosening for i in items) and not principal.is_human:
            raise CrewOpError(403, "human_only", "Loosening a zone needs a dashboard login.")
        if items and not principal.is_human:
            await _agent_zone_check(tx.conn, crew_id, after, before, zone_id=str(row["id"]))
        if not items:
            return {"applied": True, "zone": zone_detail(row)}
        parent_id = await _parent_id(tx.conn, crew_id, after.parent, child_id=str(row["id"]))
        cols = _zone_columns(after)
        sets = ", ".join(f"{k} = ?" for k in cols)
        await tx.conn.execute(
            f"UPDATE crew_zones SET {sets}, parent_id = ?, version = version + 1, updated_at = ? WHERE id = ?",
            (*cols.values(), parent_id, now_iso(), row["id"]),
        )
        res = SyncResult(updated=[str(row["id"])])
        res.updated.extend(z for z in await _restructure(tx, crew_id) if z != row["id"])
        await _emit_zone_events(tx, crew_id, res, actor)
        if principal.is_human:
            ops.audit(
                principal.user_id, "crew.zone_updated", str(row["id"]), {"fields": sorted({i.field or i.op for i in items})}
            )
        return {"applied": True, "zone": zone_detail(await get_zone(tx.conn, str(row["id"])))}


async def archive_zone(ops: CrewOps, zone: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
    """``DELETE /zones/{zid}``: archive a server zone (loosening: human only). Repo zones return an export patch."""
    crew_id = str(zone["crew_id"])
    actor = principal.actor()
    async with ops.log.transaction() as tx:
        row = await get_zone(tx.conn, str(zone["id"]))
        if row["builtin"]:
            raise CrewOpError(423, "crew_policy", "The built-in crew-policy zone cannot be removed.")
        if row["archived_at"] is not None:
            return {"applied": True, "zone": zone_detail(row), "released_claims": []}
        if row["source"] == "repo":
            repo = await _repo_policy(tx.conn, crew_id)
            kept = tuple(
                z if z.parent != row["slug"] else P.ZoneDef(**{**z.__dict__, "parent": None})
                for z in repo.zones
                if z.slug != row["slug"]
            )
            edited = P.Policy(kept, repo.commons, repo.ignore, repo.enforcement)
            return {"applied": False, "export_patch": P.export_patch(repo, edited), "yaml": P.export_yaml(edited)}
        if not principal.is_human:
            raise CrewOpError(403, "human_only", "Removing a zone loosens protection and needs a dashboard login.")
        released = await _archive_zone_rows(ops, tx, crew_id, [str(row["id"])], actor, "zone_archived")
        res = SyncResult(archived=[str(row["id"])])
        res.updated.extend(await _restructure(tx, crew_id))
        await _emit_zone_events(tx, crew_id, res, actor)
        ops.audit(
            principal.user_id, "crew.zone_archived", str(row["id"]), {"slug": row["slug"], "api_key_id": principal.api_key_id}
        )
        return {"applied": True, "zone": zone_detail(await get_zone(tx.conn, str(row["id"]))), "released_claims": released}


# ---------------------------------------------------------------------------
# Freeze / unfreeze (human only, §5.9)
# ---------------------------------------------------------------------------


async def freeze_zone(
    ops: CrewOps, zone: Mapping[str, Any], human: Principal, *, reason: str, until: str | None
) -> dict[str, Any]:
    """Freeze a zone ("Mani is editing POS himself"): guard row 3 denies every agent; a human exclusive claim records it."""
    from remembra.crew import claims as C

    crew_id = str(zone["crew_id"])
    actor = human.actor()
    note = clip(reason, MAX_REASON) or "frozen"
    if until is not None and parse_iso(until) <= utcnow():
        raise CrewOpError(422, "invalid_until", "until must be in the future.")
    async with ops.log.transaction() as tx:
        row = await get_zone(tx.conn, str(zone["id"]))
        if row["builtin"]:
            raise CrewOpError(423, "crew_policy", "The built-in crew-policy zone is always protected.")
        if row["archived_at"] is not None:
            raise CrewOpError(409, "zone_archived", "This zone is archived.")
        await tx.conn.execute(
            "UPDATE crew_zones SET frozen_by = ?, frozen_note = ?, frozen_until = ?, version = version + 1, "
            "updated_at = ? WHERE id = ?",
            (human.user_id, note, until, now_iso(), row["id"]),
        )
        claim = await C.hold_zone_for_human(ops, tx, crew_id, str(row["id"]), human)
        fresh = await get_zone(tx.conn, str(row["id"]))
        await tx.emit(
            crew_id=crew_id,
            type="zone.frozen",
            actor=actor,
            payload={"zone": zone_view(fresh), "reason": note},
            summary=f"zone {fresh['slug']} frozen by a human",
            refs={"zone_id": fresh["id"]},
        )
        await tx.emit(
            crew_id=crew_id,
            type="human.override",
            actor=actor,
            payload={"action": "freeze", "reason": note, "target_kind": "zone", "target_id": fresh["id"]},
            summary=f"human froze zone {fresh['slug']}",
            refs={"zone_id": fresh["id"]},
        )
        await C.notify_zone_holders(ops, tx, crew_id, str(fresh["id"]), f"Zone {fresh['slug']} was frozen by a human", actor)
        ops.audit(
            human.user_id, "crew.zone_frozen", str(fresh["id"]), {"reason": note, "until": until, "api_key_id": human.api_key_id}
        )
        return {"zone": zone_detail(fresh), "claim": C.claim_view(claim) if claim else None}


async def unfreeze_zone(ops: CrewOps, zone: Mapping[str, Any], principal: Principal, *, reason: str) -> dict[str, Any]:
    """Lift a freeze (a human, or the system when ``frozen_until`` passed); ends the human claim and wakes the queue."""
    from remembra.crew import claims as C

    crew_id = str(zone["crew_id"])
    actor = principal.actor()
    note = clip(reason, MAX_REASON) or "unfrozen"
    async with ops.log.transaction() as tx:
        row = await get_zone(tx.conn, str(zone["id"]))
        if not row.get("frozen_by"):
            return {"zone": zone_detail(row)}
        await tx.conn.execute(
            "UPDATE crew_zones SET frozen_by = NULL, frozen_note = NULL, frozen_until = NULL, version = "
            "version + 1, updated_at = ?"
            " WHERE id = ?",
            (now_iso(), row["id"]),
        )
        await C.release_human_holds(ops, tx, crew_id, str(row["id"]), actor=actor)
        fresh = await get_zone(tx.conn, str(row["id"]))
        await tx.emit(
            crew_id=crew_id,
            type="zone.unfrozen",
            actor=actor,
            payload={"zone": zone_view(fresh), "reason": note},
            summary=f"zone {fresh['slug']} unfrozen",
            refs={"zone_id": fresh["id"]},
        )
        if principal.is_human:
            await tx.emit(
                crew_id=crew_id,
                type="human.override",
                actor=actor,
                payload={"action": "unfreeze", "reason": note, "target_kind": "zone", "target_id": fresh["id"]},
                summary=f"human unfroze zone {fresh['slug']}",
                refs={"zone_id": fresh["id"]},
            )
            ops.audit(
                principal.user_id, "crew.zone_unfrozen", str(fresh["id"]), {"reason": note, "api_key_id": principal.api_key_id}
            )
        await C.promote_queue(ops, tx, crew_id)
        return {"zone": zone_detail(fresh)}


# ---------------------------------------------------------------------------
# Tree, suggestions and the no-zone bootstrap (D37)
# ---------------------------------------------------------------------------


async def put_tree(ops: CrewOps, crew_id: str, principal: Principal, tree: Any) -> dict[str, Any]:
    """``PUT /crews/{id}/tree``: store the folder-only tree snapshot, then run the no-zone bootstrap check."""
    errors = S.validate_tree(tree)
    if errors:
        raise CrewOpError(422, "invalid_tree", "The tree snapshot is invalid.", problems=errors[:20])
    count = 0

    def walk(node: Mapping[str, Any]) -> None:
        nonlocal count
        count += 1
        for child in node.get("children") or ():
            walk(child)

    walk(tree)
    async with ops.log.transaction() as tx:
        await tx.conn.execute(
            """INSERT INTO crew_repo_trees (crew_id, tree, node_count, captured_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(crew_id) DO UPDATE SET tree = excluded.tree, node_count = excluded.node_count,
                   captured_at = excluded.captured_at""",
            (crew_id, dumps(tree), count, now_iso()),
        )
        applied = await maybe_bootstrap(ops, tx, crew_id, actor=principal.actor())
        return {"node_count": count, "bootstrap_zone_ids": applied}


async def stored_tree(conn: aiosqlite.Connection, crew_id: str) -> dict[str, Any] | None:
    row = await fetchone(conn, "SELECT tree FROM crew_repo_trees WHERE crew_id = ?", (crew_id,))
    return loads(row["tree"]) if row else None


async def suggest(
    conn: aiosqlite.Connection, crew_id: str, *, tree: Any = None, churn: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """``POST /crews/{id}/zones/suggest``: deterministic suggestions from ``tree`` (or the stored one). No writes."""
    source = tree if tree is not None else await stored_tree(conn, crew_id)
    if source is None:
        return {"zones": [], "yaml": P.export_yaml(P.Policy()), "reason": "no_tree"}
    try:
        zones = P.suggest_zones(source, churn)
    except P.PolicyError as e:
        raise CrewOpError(422, "invalid_tree", "The tree snapshot is invalid.", problems=e.errors[:20]) from e
    return {"zones": [z.to_dict() for z in zones], "yaml": P.export_yaml(P.Policy(tuple(zones)))}


async def live_session_count(conn: aiosqlite.Connection, crew_id: str) -> int:
    marks = ",".join("?" for _ in S.LIVE_PRESENCE_STATES)
    row = await fetchone(
        conn,
        f"SELECT COUNT(*) AS n FROM crew_sessions WHERE crew_id = ? AND state IN ({marks})",
        (crew_id, *S.LIVE_PRESENCE_STATES),
    )
    return int(row["n"]) if row else 0


async def maybe_bootstrap(ops: CrewOps, tx: EventTx, crew_id: str, *, actor: Actor | None = None) -> list[str]:
    """No-zone bootstrap (D37): with ≥2 live sessions and no zones at all, apply suggested zones in enforce mode.

    Call it inside the transaction that makes the crew multi (WP-4 join) and after a tree
    upload. Idempotent: returns ``[]`` when zones exist, the setting is off, fewer than two
    sessions are live or no tree snapshot is stored. Suggested zones are ``source='suggested'``
    and removable in one step by a human (DELETE each, or a repo zones.yml that replaces them).
    """
    settings = await crew_settings(tx.conn, crew_id)
    if settings.get("no_zone_bootstrap") != "suggested_enforce":
        return []
    rows = await load_zone_rows(tx.conn, crew_id, include_archived=True)
    if any(not r["builtin"] for r in rows):
        return []  # zones exist (or existed: an owner who removed every zone chose that)
    if await live_session_count(tx.conn, crew_id) < 2:
        return []
    tree = await stored_tree(tx.conn, crew_id)
    if tree is None:
        return []
    try:
        zones = P.suggest_zones(tree)
    except P.PolicyError:
        return []
    if ops.limits is not None:
        zones = zones[: max(0, ops.limits.max_zones)]
    if not zones:
        return []
    who = actor or Actor.system()
    await ensure_policy_zone(tx, crew_id)
    res = SyncResult()
    for z in zones:
        res.created.append(await _insert_zone(tx, crew_id, z, source="suggested", created_by="system"))
    await _restructure(tx, crew_id)
    await _emit_zone_events(tx, crew_id, res, who)
    await tx.emit(
        crew_id=crew_id,
        type="zone.suggested_applied",
        actor=who,
        payload={"zone_ids": res.created[:200], "undo_available": True},
        summary=f"{len(res.created)} temporary zones applied from folders",
    )
    return res.created


# ---------------------------------------------------------------------------
# Export and match
# ---------------------------------------------------------------------------


async def export(conn: aiosqlite.Connection, crew_id: str) -> dict[str, Any]:
    """``GET /crews/{id}/zones/export``: every live zone (any source) as zones.yml text."""
    rows = await load_zone_rows(conn, crew_id)
    by_id = {str(r["id"]): r for r in rows}
    zones = [
        P.zone_from_row(r, parent_slug=str(by_id[r["parent_id"]]["slug"]) if r.get("parent_id") in by_id else None)
        for r in rows
        if not r["builtin"]
    ]
    current = _file_policy(await zone_file(conn, crew_id))
    pol = P.Policy(
        tuple(sorted(zones, key=lambda z: z.slug)),
        current.commons if current else (),
        current.ignore if current else (),
        current.enforcement if current else None,
    )
    return {"yaml": P.export_yaml(pol), "zones": len(zones)}


async def match(
    conn: aiosqlite.Connection,
    crew_id: str,
    *,
    paths: Sequence[str],
    command_tokens: Sequence[str] | None = None,
    mcp_tool: str | None = None,
) -> dict[str, Any]:
    """``POST /crews/{id}/match``: which zones, commons, ignore and services apply (read-only)."""
    idx = await zone_index(conn, crew_id)
    out_paths: list[dict[str, Any]] = []
    for raw in paths:
        rel = normalize_rel(str(raw))
        if not S.is_path_rel(rel) or rel == ".":
            out_paths.append({"path": raw, "error": "not a repo-relative path"})
            continue
        zones = idx.match(rel, False)
        leaves = [z for z in zones if z.get("is_leaf", True)]
        policy = any(_glob_hit(str(g), rel) for z in idx.builtin for g in z.get("include_globs") or ())
        commons = idx.commons_entry(rel, False)
        out_paths.append(
            {
                "path": rel,
                "zones": sorted(str(z["slug"]) for z in zones),
                "leaf_zone": str(leaves[0]["slug"]) if leaves else None,
                "commons": dict(commons) if commons else None,
                "ignored": idx.ignored(rel, False),
                "crew_policy": policy,
            }
        )
    command: dict[str, Any] | None = None
    if command_tokens:
        zone_ids = idx.trie.match(list(command_tokens))
        hit = [idx.by_id[z] for z in sorted(zone_ids) if z in idx.by_id]
        command = {
            "zones": [str(z["slug"]) for z in hit],
            "services": sorted({str(s) for z in hit for s in z.get("services") or ()}),
        }
    mcp: dict[str, Any] | None = None
    if mcp_tool:
        cls = S.classify_mcp_tool(mcp_tool, {}, idx.mcp_rules)
        mcp = {"kind": cls.kind, "services": list(cls.services), "zone": cls.zone_slug}
    return {"paths": out_paths, "command": command, "mcp": mcp}


def _glob_hit(glob: str, rel: str) -> bool:
    from remembra.crew.gatecore import glob_match

    return glob_match(glob, rel)
