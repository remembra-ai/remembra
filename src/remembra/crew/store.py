"""Repository layer over ``crew.db`` (spec §3.2, §4.3).

Shared row access that every crew service builds on: ids and server time,
crews and membership, settings with optimistic concurrency, generic entity
lookup (what ``load_crew_entity`` resolves through), per-crew numbering
(``T-n``, ``D-n``), the crew idempotency table and the cross-DB outbox queue.

Service logic (claims, sessions, tasks, the event log) lives in the owning
modules; they call these methods inside their own
``async with crew_db.transaction():`` blocks, which the methods here join.

Access control is NOT done here. Callers resolve the crew through
``crew/access.py`` first; every method below is scoped by ``crew_id``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas
from remembra.crew.db import CrewDatabase
from remembra.crew.settings import apply_settings_patch, changed_keys, default_settings, dumps_settings, load_settings

# ---------------------------------------------------------------------------
# Ids and time
# ---------------------------------------------------------------------------

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
OUTBOX_ID_PREFIX: Final = "obx"
OUTBOX_STATES: Final = ("pending", "done", "failed")
MAX_OUTBOX_PAYLOAD_BYTES: Final = 64 * 1024
IDEMPOTENCY_TTL: Final = timedelta(hours=72)
# Keys that carry a credential. A response holding one is never cached (§4.3, §11).
TOKEN_FIELDS: Final = frozenset({"session_token", "host_token", "token", "bypass_code", "code"})

# kind -> table for entity lookups. Every table except crew_hosts carries crew_id.
ENTITY_TABLES: Final[dict[str, str]] = {
    "crew": "crews",
    "session": "crew_sessions",
    "host": "crew_hosts",
    "zone": "crew_zones",
    "claim": "crew_claims",
    "task": "crew_tasks",
    "report": "crew_reports",
    "checkpoint": "crew_checkpoints",
    "collision": "crew_collisions",
    "message": "crew_messages",
    "decision": "crew_decisions",
    "inbox_item": "crew_inbox_items",
    "offer": "crew_baton_offers",
    "zone_change": "crew_zone_changes",
    "bypass_code": "crew_bypass_codes",
    "baton": "crew_batons",
    "proposal": "crew_proposals",
}
# Events are addressed by (crew_id, seq), never fetched by id, so they have no entry.
assert set(ENTITY_TABLES) == set(schemas.ID_PREFIXES) - {"event"}, "every id kind needs a table"

# Tables with a per-crew display number (T-n, D-n, proposal n).
NUMBERED_TABLES: Final = frozenset({"crew_tasks", "crew_decisions", "crew_proposals"})


def now_iso(at: datetime | None = None) -> str:
    """Server-stamped ISO-8601 UTC time with millisecond precision and a ``Z`` suffix."""
    moment = (at or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_iso(value: str) -> datetime:
    """Parse a time written by :func:`now_iso` (or any ISO-8601 string) as aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


_ulid_lock = threading.Lock()
_ulid_last: list[int] = [0, 0]  # [milliseconds, random part] of the last id issued


def _ulid() -> str:
    """26-char Crockford base32 ULID: 48-bit milliseconds + 80 random bits.

    Monotonic within the process: ids issued in the same millisecond increment
    the random part, so ids sort in creation order.
    """
    with _ulid_lock:
        ms = int(time.time() * 1000)
        last_ms, last_rand = _ulid_last
        if ms <= last_ms and last_rand < (1 << 80) - 1:
            ms, rand = last_ms, last_rand + 1
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _ulid_last[0], _ulid_last[1] = ms, rand
    value = (ms << 80) | rand
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def new_id(kind: str) -> str:
    """A new id for ``kind`` (a key of ``schemas.ID_PREFIXES``), e.g. ``clm_01J…``."""
    return f"{schemas.ID_PREFIXES[kind]}_{_ulid()}"


def crew_id_for(owner_user_id: str, project_id: str) -> str:
    """Deterministic crew id (D1): ``crw_`` + sha256(owner:project)[:16]. Guessable by design; the ACL protects it."""
    digest = hashlib.sha256(f"{owner_user_id}:{project_id}".encode()).hexdigest()
    return f"{schemas.ID_PREFIXES['crew']}_{digest[:16]}"


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# Sub-agents of sub-agents are allowed; the chain is walked at most this deep (a cycle cannot form:
# a parent must exist before its child joins, but the walk is bounded anyway).
MAX_SUB_AGENT_DEPTH: Final = 8


async def is_accountable_for(conn: Any, session_id: str | None, subject_session_id: str | None) -> bool:
    """Is crew session ``session_id`` an ancestor of ``subject_session_id`` (it started that sub-agent, or one above it)?

    Owner decision (gap analysis open question 1): a sub-agent is its own session linked by
    ``parent_session_id``; what it holds is attributed to it, and its parent stays accountable,
    so the parent may release its claims and act on its tasks.
    """
    if not session_id or not subject_session_id or session_id == subject_session_id:
        return False
    current = subject_session_id
    for _ in range(MAX_SUB_AGENT_DEPTH):
        async with conn.execute("SELECT parent_session_id FROM crew_sessions WHERE id = ?", (current,)) as cur:
            row = await cur.fetchone()
        parent = row[0] if row else None
        if not parent:
            return False
        if parent == session_id:
            return True
        current = parent
    return False


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _contains_token(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k in TOKEN_FIELDS or _contains_token(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_token(v) for v in value)
    return False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrewStoreError(Exception):
    """Base class for repository errors."""


class NotFound(CrewStoreError):
    """The row does not exist (routes map this to 404)."""


class PreconditionFailed(CrewStoreError):
    """``If-Match`` version mismatch (routes map this to 412)."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"version mismatch; current version is {current_version}")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CrewStore:
    """Row access on ``crew.db``. Methods that write join the caller's transaction or open their own."""

    def __init__(self, db: CrewDatabase) -> None:
        self.db = db

    # -- crews ---------------------------------------------------------------

    async def get_crew(self, crew_id: str) -> dict[str, Any] | None:
        return await self.db.fetchone("SELECT * FROM crews WHERE id = ?", (crew_id,))

    async def get_crew_by_project(self, owner_user_id: str, project_id: str) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM crews WHERE owner_user_id = ? AND project_id = ?", (owner_user_id, project_id)
        )

    async def ensure_crew(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        name: str | None = None,
        team_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create the crew for ``(owner, project)`` if missing; return ``(row, created)``.

        Idempotent (the id is deterministic). A new crew gets the default
        settings and the owner as an ``owner`` member, in one transaction.
        Only ``join`` by a key allowed on the project may call this (§2): resolve
        is read-only.
        """
        if not owner_user_id or not project_id:
            raise ValueError("owner_user_id and project_id are required")
        crew_id = crew_id_for(owner_user_id, project_id)
        now = now_iso()
        async with self.db.transaction():
            cursor = await self.db.conn.execute(
                """
                INSERT OR IGNORE INTO crews (id, owner_user_id, project_id, team_id, name, settings, settings_version,
                                             last_seq, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (crew_id, owner_user_id, project_id, team_id, name, dumps_settings(default_settings()), now, now),
            )
            created = (cursor.rowcount or 0) > 0
            if created:
                await self.db.conn.execute(
                    "INSERT OR IGNORE INTO crew_members (crew_id, user_id, role, added_by, added_at)"
                    " VALUES (?, ?, 'owner', ?, ?)",
                    (crew_id, owner_user_id, owner_user_id, now),
                )
            row = await self.get_crew(crew_id)
        if row is None:  # pragma: no cover - the row was inserted or already existed
            raise NotFound(crew_id)
        if row["owner_user_id"] != owner_user_id or row["project_id"] != project_id:
            raise CrewStoreError(f"crew id collision for {crew_id}")
        return row, created

    async def list_crews_for_user(self, user_id: str, project_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Crews the user owns or is a member of, optionally limited to ``project_ids``.

        ``project_ids=None`` means no restriction; an empty list matches nothing
        (a restricted key with an empty allow-list sees no crew, §4.4).
        """
        sql = """
            SELECT c.*, COALESCE(m.role, CASE WHEN c.owner_user_id = ? THEN 'owner' END) AS role
              FROM crews c
              LEFT JOIN crew_members m ON m.crew_id = c.id AND m.user_id = ?
             WHERE (c.owner_user_id = ? OR m.user_id IS NOT NULL)
        """
        params: list[Any] = [user_id, user_id, user_id]
        if project_ids is not None:
            if not project_ids:
                return []
            sql += f" AND c.project_id IN ({', '.join('?' for _ in project_ids)})"
            params.extend(project_ids)
        sql += " ORDER BY c.created_at, c.project_id"
        return await self.db.fetchall(sql, params)

    # -- membership ------------------------------------------------------------

    async def get_member_role(self, crew_id: str, user_id: str) -> str | None:
        row = await self.db.fetchone("SELECT role FROM crew_members WHERE crew_id = ? AND user_id = ?", (crew_id, user_id))
        return str(row["role"]) if row else None

    async def list_members(self, crew_id: str) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM crew_members WHERE crew_id = ? ORDER BY added_at, user_id", (crew_id,))

    async def set_member(self, crew_id: str, user_id: str, role: str, added_by: str) -> dict[str, Any]:
        """Add a member or change their role. The last owner cannot be demoted."""
        if role not in schemas.CREW_ROLES:
            raise ValueError(f"role must be one of {', '.join(schemas.CREW_ROLES)}")
        async with self.db.transaction():
            if await self.get_crew(crew_id) is None:
                raise NotFound(crew_id)
            current = await self.get_member_role(crew_id, user_id)
            if current == "owner" and role != "owner" and await self._owner_count(crew_id) <= 1:
                raise CrewStoreError("a crew must keep at least one owner")
            await self.db.conn.execute(
                """
                INSERT INTO crew_members (crew_id, user_id, role, added_by, added_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(crew_id, user_id) DO UPDATE SET role = excluded.role
                """,
                (crew_id, user_id, role, added_by, now_iso()),
            )
            row = await self.db.fetchone("SELECT * FROM crew_members WHERE crew_id = ? AND user_id = ?", (crew_id, user_id))
        assert row is not None
        return row

    async def remove_member(self, crew_id: str, user_id: str) -> bool:
        """Remove a member. The last owner cannot be removed. Returns False if they were not a member."""
        async with self.db.transaction():
            current = await self.get_member_role(crew_id, user_id)
            if current is None:
                return False
            if current == "owner" and await self._owner_count(crew_id) <= 1:
                raise CrewStoreError("a crew must keep at least one owner")
            await self.db.conn.execute("DELETE FROM crew_members WHERE crew_id = ? AND user_id = ?", (crew_id, user_id))
        return True

    async def _owner_count(self, crew_id: str) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM crew_members WHERE crew_id = ? AND role = 'owner'", (crew_id,))
        return int(row["n"]) if row else 0

    # -- settings --------------------------------------------------------------

    async def get_settings(self, crew_id: str) -> tuple[dict[str, Any], int]:
        """``(settings, settings_version)``; stored documents are completed with newer defaults."""
        row = await self.db.fetchone("SELECT settings, settings_version FROM crews WHERE id = ?", (crew_id,))
        if row is None:
            raise NotFound(crew_id)
        return load_settings(row["settings"]), int(row["settings_version"])

    async def patch_settings(
        self, crew_id: str, patch: dict[str, Any], *, if_match: int
    ) -> tuple[dict[str, Any], int, list[str]]:
        """Apply a settings patch under ``If-Match``; return ``(settings, new_version, changed_keys)``.

        Raises :class:`PreconditionFailed` when ``if_match`` is not the current
        ``settings_version``, :class:`~remembra.crew.settings.SettingsError` on an
        invalid patch. A patch that changes nothing does not bump the version.
        Human-only (D27) is enforced by the route, not here.
        """
        async with self.db.transaction():
            current, version = await self.get_settings(crew_id)
            if if_match != version:
                raise PreconditionFailed(version)
            updated = apply_settings_patch(current, patch)
            changed = changed_keys(current, updated)
            if not changed:
                return updated, version, []
            cursor = await self.db.conn.execute(
                """
                UPDATE crews SET settings = ?, settings_version = settings_version + 1, updated_at = ?
                 WHERE id = ? AND settings_version = ?
                """,
                (dumps_settings(updated), now_iso(), crew_id, version),
            )
            if (cursor.rowcount or 0) != 1:  # pragma: no cover - serialised by BEGIN IMMEDIATE
                raise PreconditionFailed(version)
        return updated, version + 1, changed

    # -- generic entity lookup -----------------------------------------------------

    async def get_entity(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        """Fetch one row by id for ``kind`` (``schemas.ID_PREFIXES`` keys).

        Returns None for an unknown id or an id whose prefix does not match the
        kind, so a claim id passed as a task id never resolves.
        """
        table = ENTITY_TABLES.get(kind)
        if table is None:
            raise ValueError(f"unknown entity kind {kind!r}")
        if not schemas.is_id(kind, entity_id):
            return None
        return await self.db.fetchone(f"SELECT * FROM {table} WHERE id = ?", (entity_id,))

    async def get_scoped(self, kind: str, crew_id: str, entity_id: str) -> dict[str, Any] | None:
        """Like :meth:`get_entity` but only when the row belongs to ``crew_id`` (same-crew validation, §6)."""
        if kind in ("crew", "host"):
            raise ValueError(f"{kind} rows are not crew-scoped")
        row = await self.get_entity(kind, entity_id)
        return row if row is not None and row.get("crew_id") == crew_id else None

    async def next_number(self, crew_id: str, table: str) -> int:
        """Next display number (T-n, D-n) for a crew. Call inside the transaction that inserts the row."""
        if table not in NUMBERED_TABLES:
            raise ValueError(f"{table} has no per-crew number")
        if not self.db.in_transaction:
            raise RuntimeError("next_number must run inside crew_db.transaction()")
        row = await self.db.fetchone(f"SELECT COALESCE(MAX(number), 0) + 1 AS n FROM {table} WHERE crew_id = ?", (crew_id,))
        return int(row["n"]) if row else 1

    # -- idempotency (§4.3) ------------------------------------------------------------

    @staticmethod
    def _idem_key(principal: str, key: str) -> str:
        return hashlib.sha256(f"{principal}\x1f{key}".encode()).hexdigest()

    async def idempotency_get(self, principal: str, key: str) -> Any | None:
        """The stored response for ``(principal, key)`` within 72 h, else None."""
        row = await self.db.fetchone(
            "SELECT response_json, created_at FROM crew_idempotency WHERE key = ? AND principal = ?",
            (self._idem_key(principal, key), principal),
        )
        if row is None:
            return None
        if parse_iso(row["created_at"]) < datetime.now(UTC) - IDEMPOTENCY_TTL:
            return None
        return loads(row["response_json"])

    async def idempotency_put(self, principal: str, key: str, response: Any) -> bool:
        """Remember a response. Never stores a token-bearing response (raises). Returns False if already stored."""
        if not principal or not key:
            raise ValueError("principal and key are required")
        if _contains_token(response):
            raise ValueError("token-bearing responses are never stored in the idempotency table")
        cursor = await self.db.conn.execute(
            "INSERT OR IGNORE INTO crew_idempotency (key, principal, response_json, created_at) VALUES (?, ?, ?, ?)",
            (self._idem_key(principal, key), principal, dumps(response), now_iso()),
        )
        if not self.db.in_transaction:
            await self.db.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def purge_idempotency(self, *, older_than: timedelta = IDEMPOTENCY_TTL, batch: int = 500) -> int:
        """Delete idempotency rows older than ``older_than``, ≤ ``batch`` rows per transaction."""
        cutoff = now_iso(datetime.now(UTC) - older_than)
        total = 0
        while True:
            async with self.db.transaction():
                cursor = await self.db.conn.execute(
                    "DELETE FROM crew_idempotency WHERE key IN (SELECT key FROM crew_idempotency WHERE created_at < ? LIMIT ?)",
                    (cutoff, batch),
                )
                n = max(cursor.rowcount or 0, 0)
            total += n
            if n < batch:
                return total

    # -- outbox (D35) -----------------------------------------------------------------------

    async def enqueue_outbox(self, crew_id: str, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None) -> str:
        """Queue a cross-DB effect; return its id.

        Call it inside the crew transaction that produced the effect so the two
        commit together. With ``dedupe_key`` the id is deterministic and a second
        enqueue of the same effect is a no-op returning the same id.
        """
        if not isinstance(payload, dict):
            raise ValueError("outbox payload must be an object")
        body = dumps(payload)
        if len(body.encode()) > MAX_OUTBOX_PAYLOAD_BYTES:
            raise ValueError(f"outbox payload exceeds {MAX_OUTBOX_PAYLOAD_BYTES} bytes")
        if dedupe_key is not None:
            digest = hashlib.sha256(f"{crew_id}\x1f{kind}\x1f{dedupe_key}".encode()).hexdigest()[:26]
            outbox_id = f"{OUTBOX_ID_PREFIX}_{digest}"
        else:
            outbox_id = f"{OUTBOX_ID_PREFIX}_{_ulid()}"
        now = now_iso()
        await self.db.conn.execute(
            """
            INSERT OR IGNORE INTO crew_outbox (id, crew_id, kind, payload, state, attempts, created_at,
                                               next_attempt_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (outbox_id, crew_id, kind, body, now, now, now),
        )
        if not self.db.in_transaction:
            await self.db.conn.commit()
        return outbox_id

    async def get_outbox(self, outbox_id: str) -> dict[str, Any] | None:
        return await self.db.fetchone("SELECT * FROM crew_outbox WHERE id = ?", (outbox_id,))

    async def due_outbox(self, *, limit: int = 20, now: str | None = None) -> list[dict[str, Any]]:
        """Pending items whose next attempt is due, oldest first."""
        return await self.db.fetchall(
            """
            SELECT * FROM crew_outbox
             WHERE state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
             ORDER BY created_at, rowid LIMIT ?
            """,
            (now or now_iso(), limit),
        )

    async def mark_outbox_done(self, outbox_id: str, result_id: str | None) -> bool:
        return await self._outbox_update(
            "UPDATE crew_outbox SET state = 'done', result_id = ?, attempts = attempts + 1, last_error = NULL,"
            " next_attempt_at = NULL, updated_at = ? WHERE id = ? AND state = 'pending'",
            (result_id, now_iso(), outbox_id),
        )

    async def mark_outbox_retry(self, outbox_id: str, error: str, next_attempt_at: str) -> bool:
        return await self._outbox_update(
            "UPDATE crew_outbox SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?, updated_at = ?"
            " WHERE id = ? AND state = 'pending'",
            (error[:500], next_attempt_at, now_iso(), outbox_id),
        )

    async def mark_outbox_failed(self, outbox_id: str, error: str) -> bool:
        return await self._outbox_update(
            "UPDATE crew_outbox SET state = 'failed', attempts = attempts + 1, last_error = ?, next_attempt_at = NULL,"
            " updated_at = ? WHERE id = ? AND state = 'pending'",
            (error[:500], now_iso(), outbox_id),
        )

    async def defer_outbox(self, outbox_id: str, reason: str, next_attempt_at: str) -> bool:
        """Push a pending item to a later time without counting an attempt (a plan cap, not a failure)."""
        return await self._outbox_update(
            "UPDATE crew_outbox SET last_error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ? AND state = 'pending'",
            (reason[:500], next_attempt_at, now_iso(), outbox_id),
        )

    async def requeue_outbox(self, outbox_id: str) -> bool:
        """Put a ``failed`` item back in the queue (operator action after fixing the cause)."""
        return await self._outbox_update(
            "UPDATE crew_outbox SET state = 'pending', next_attempt_at = ?, updated_at = ? WHERE id = ? AND state = 'failed'",
            (now_iso(), now_iso(), outbox_id),
        )

    async def outbox_counts(self, crew_id: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM crew_outbox"
        params: tuple[Any, ...] = ()
        if crew_id is not None:
            sql += " WHERE crew_id = ?"
            params = (crew_id,)
        rows = await self.db.fetchall(sql + " GROUP BY state", params)
        counts = dict.fromkeys(OUTBOX_STATES, 0)
        counts.update({r["state"]: int(r["n"]) for r in rows})
        return counts

    async def purge_outbox_done(self, *, older_than: timedelta, batch: int = 500) -> int:
        """Delete ``done`` items older than ``older_than`` (failed items are kept for inspection)."""
        cutoff = now_iso(datetime.now(UTC) - older_than)
        total = 0
        while True:
            async with self.db.transaction():
                cursor = await self.db.conn.execute(
                    "DELETE FROM crew_outbox WHERE id IN "
                    "(SELECT id FROM crew_outbox WHERE state = 'done' AND updated_at < ? LIMIT ?)",
                    (cutoff, batch),
                )
                n = max(cursor.rowcount or 0, 0)
            total += n
            if n < batch:
                return total

    async def _outbox_update(self, sql: str, params: tuple[Any, ...]) -> bool:
        async with self.db.transaction():
            cursor = await self.db.conn.execute(sql, params)
        return (cursor.rowcount or 0) == 1
