"""
Agent inbox manager — targeted agent-to-agent message delivery.

Implements GitHub issue #9: agents can send directives to named logical
recipients. The recipient picks them up at session start via `get_inbox`
and acknowledges with `ack_inbox` after acting.

Stores inbox rows in SQLite, scoped per owner_user_id so a single tenant
can partition its agents freely without cross-tenant concerns.

Scoping inside a tenant (the same rules as the crew branch, so its merge is clean):

* **project scoping**: every read and ack takes ``project_ids`` (the caller's
  allow-list): ``None`` means unrestricted, a list keeps only rows tagged with
  one of those projects, so rows with no project are invisible to
  project-restricted keys. A row's project is the ``project_id`` column once
  main-DB migration v5 (crew) has added it, and ``metadata.project_id`` (the tag
  the session brief already filters on) before that.
* **agent scoping**: ``recipient`` limits reads and acks to rows addressed to
  that agent (an agent-scoped key or agent-bound connector grant reads only its
  own inbox).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_INBOX_STATUSES: set[str] = {"unread", "read", "done", "blocked", "rejected"}
TERMINAL_STATUSES: set[str] = {"done", "blocked", "rejected"}
SCOPES: frozenset[str] = frozenset({"all", "project", "unscoped"})

# The row's project before migration v5 adds the column: a text
# ``metadata.project_id`` (what v5's backfill copies into the column). Malformed
# metadata or a non-text value reads as "no project", never an error.
_META_PROJECT = (
    "(CASE WHEN json_valid(metadata) THEN"
    " CASE WHEN json_type(metadata, '$.project_id') = 'text' THEN json_extract(metadata, '$.project_id') END END)"
)


def project_filter(project_ids: list[str] | None, project_id: str | None = None, scope: str = "all") -> tuple[str, list[Any]]:
    """SQL appended to a WHERE clause for project scoping (and its parameters).

    ``project_ids``: the caller's allow-list (None = unrestricted; an empty list
    matches nothing). NULL-project rows never match a restriction. ``project_id``
    narrows to one project; ``scope='project'`` keeps only tagged rows,
    ``scope='unscoped'`` only NULL-project rows (none for a restricted caller).
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}, got '{scope}'")
    sql = ""
    params: list[Any] = []
    if project_ids is not None:
        if not project_ids:
            return " AND 0", []
        sql += f" AND project_id IN ({', '.join('?' for _ in project_ids)})"
        params.extend(project_ids)
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    if scope == "project":
        sql += " AND project_id IS NOT NULL"
    elif scope == "unscoped":
        sql += " AND project_id IS NULL"
    return sql, params


def _recipient_sql(recipient: str | None) -> tuple[str, list[Any]]:
    """SQL keeping only rows addressed to ``recipient`` (None = any recipient)."""
    if recipient is None:
        return "", []
    return " AND to_agent = ?", [recipient]


def _new_inbox_id() -> str:
    return f"inbox_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Inbox Manager
# ---------------------------------------------------------------------------


class InboxManager:
    """Manages the agent_inbox table — sends, reads, acks.

    Args:
        db: The application's Database instance (exposes a `.conn` attribute
            that is an open aiosqlite connection).
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._has_v5: bool | None = None

    async def init_schema(self) -> None:
        """Create agent_inbox table and indexes if not present."""
        await self._db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_inbox (
                inbox_id    TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                from_agent  TEXT NOT NULL,
                to_agent    TEXT NOT NULL,
                subject     TEXT NOT NULL,
                body        TEXT NOT NULL,
                metadata    TEXT NOT NULL DEFAULT '{}',
                status      TEXT NOT NULL DEFAULT 'unread',
                created_at  TEXT NOT NULL,
                ack_at      TEXT,
                ack_note    TEXT,
                ack_result  TEXT,
                expires_at  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_agent_inbox_to
                ON agent_inbox(owner_user_id, to_agent, status, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_agent_inbox_from
                ON agent_inbox(owner_user_id, from_agent, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_agent_inbox_expires
                ON agent_inbox(expires_at);
            """
        )
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Send
    # -----------------------------------------------------------------------

    async def send(
        self,
        owner_user_id: str,
        from_agent: str,
        to_agent: str,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a new inbox row addressed to `to_agent`.

        ``project_id`` tags the row: it is written to ``metadata.project_id``
        (replacing any value there) and, once migration v5 has run, to the
        ``project_id`` column. Without it, the metadata is stored as given.

        Returns the created inbox row.
        """
        from_agent = (from_agent or "").strip()
        to_agent = (to_agent or "").strip()
        subject = (subject or "").strip()
        body = body or ""
        project_id = (project_id or "").strip() or None

        if not from_agent:
            raise ValueError("from_agent must not be empty")
        if not to_agent:
            raise ValueError("to_agent must not be empty")
        if not subject:
            raise ValueError("subject must not be empty")
        if not body.strip():
            raise ValueError("body must not be empty")

        inbox_id = _new_inbox_id()
        now = datetime.now(UTC).isoformat()
        meta = dict(metadata or {})
        if project_id:
            meta["project_id"] = project_id
        meta_json = json.dumps(meta)
        expires_iso = expires_at.isoformat() if expires_at else None

        base = (inbox_id, owner_user_id, from_agent, to_agent, subject, body, meta_json, now, expires_iso)
        if await self._scoped_columns():
            await self._db.conn.execute(
                """
                INSERT INTO agent_inbox (
                    inbox_id, owner_user_id, from_agent, to_agent, subject, body,
                    metadata, status, created_at, expires_at, project_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?)
                """,
                (*base, project_id),
            )
        else:  # pre-v5 table: the project lives in metadata.project_id only
            await self._db.conn.execute(
                """
                INSERT INTO agent_inbox (
                    inbox_id, owner_user_id, from_agent, to_agent, subject, body,
                    metadata, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?)
                """,
                base,
            )
        await self._db.conn.commit()

        logger.info(
            "inbox_sent owner=%s from=%s to=%s id=%s",
            owner_user_id,
            from_agent,
            to_agent,
            inbox_id,
        )

        return {
            "inbox_id": inbox_id,
            "owner_user_id": owner_user_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "subject": subject,
            "body": body,
            "metadata": meta,
            "status": "unread",
            "created_at": now,
            "expires_at": expires_iso,
            "ack_at": None,
            "ack_note": None,
            "ack_result": None,
            "project_id": project_id,
        }

    # -----------------------------------------------------------------------
    # Read
    # -----------------------------------------------------------------------

    async def _scoped_columns(self) -> bool:
        """True when ``agent_inbox`` has the ``project_id`` column (main-DB migration v5, crew).

        Before v5 the row's project is ``metadata.project_id``; project
        restrictions then filter on that instead of the column.
        """
        if self._has_v5 is None:
            cursor = await self._db.conn.execute("PRAGMA table_info(agent_inbox)")
            cols = {r[1] for r in await cursor.fetchall()}
            self._has_v5 = "project_id" in cols
        return self._has_v5

    async def _scope_sql(
        self, project_ids: list[str] | None, project_id: str | None = None, scope: str = "all"
    ) -> tuple[str, list[Any]]:
        sql, params = project_filter(project_ids, project_id, scope)
        if not await self._scoped_columns():
            sql = sql.replace("project_id", _META_PROJECT)
        return sql, params

    async def get_for_agent(
        self,
        owner_user_id: str,
        agent_id: str,
        status: str = "unread",
        limit: int = 20,
        *,
        project_ids: list[str] | None = None,
        project_id: str | None = None,
        scope: str = "all",
    ) -> list[dict[str, Any]]:
        """Return inbox rows addressed to `agent_id`.

        Args:
            owner_user_id: The API-key owner's user_id (tenant scope).
            agent_id: The logical recipient name.
            status: "unread" (default) or "all".
            limit: Max rows to return.
            project_ids: The caller's project allow-list (None = unrestricted).
            project_id / scope: optional narrowing (see :func:`project_filter`).

        Skips rows past their `expires_at`.
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id must not be empty")

        if status not in {"unread", "all"}:
            raise ValueError(f"status must be 'unread' or 'all', got '{status}'")

        limit = max(1, min(int(limit), 200))
        now_iso = datetime.now(UTC).isoformat()
        scope_sql, scope_params = await self._scope_sql(project_ids, project_id, scope)
        status_sql = " AND status = 'unread'" if status == "unread" else ""
        query = f"""
            SELECT * FROM agent_inbox
            WHERE owner_user_id = ?
              AND to_agent = ?{status_sql}
              AND (expires_at IS NULL OR expires_at > ?){scope_sql}
            ORDER BY datetime(created_at) DESC, inbox_id DESC
            LIMIT ?
        """  # noqa: S608 - fixed fragments; values are bound
        cursor = await self._db.conn.execute(query, (owner_user_id, agent_id, now_iso, *scope_params, limit))
        rows = await cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    async def get_one(
        self,
        owner_user_id: str,
        inbox_id: str,
        *,
        project_ids: list[str] | None = None,
        recipient: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a single inbox row scoped to the caller's tenant (and project
        allow-list and, with ``recipient``, rows addressed to that agent)."""
        scope_sql, scope_params = await self._scope_sql(project_ids)
        to_sql, to_params = _recipient_sql(recipient)
        cursor = await self._db.conn.execute(
            f"SELECT * FROM agent_inbox WHERE inbox_id = ? AND owner_user_id = ?{scope_sql}{to_sql}",  # noqa: S608
            (inbox_id, owner_user_id, *scope_params, *to_params),
        )
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def list_messages(
        self,
        owner_user_id: str,
        status: str = "open",
        agent_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        project_ids: list[str] | None = None,
        project_id: str | None = None,
        scope: str = "all",
        recipient: str | None = None,
    ) -> dict[str, Any]:
        """Messages across every agent of this owner (the dashboard view).

        Args:
            owner_user_id: Tenant scope.
            status: "unread", "open" (unread or read: not yet done/blocked/
                rejected) or "all".
            agent_id: Only messages to or from this agent.
            limit: Max rows (1-200). offset: rows to skip.
            project_ids / project_id / scope: project scoping (see :func:`project_filter`).
            recipient: Only messages addressed to this agent (agent-scoped callers).

        Returns ``{"items": [...], "total": N}``, newest first; expired rows
        are skipped.
        """
        if status not in {"unread", "open", "all"}:
            raise ValueError(f"status must be 'unread', 'open' or 'all', got '{status}'")
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        where = "owner_user_id = ? AND (expires_at IS NULL OR expires_at > ?)"
        params: list[Any] = [owner_user_id, datetime.now(UTC).isoformat()]
        if status == "unread":
            where += " AND status = 'unread'"
        elif status == "open":
            where += " AND status IN ('unread', 'read')"
        agent = (agent_id or "").strip()
        if agent:
            where += " AND (to_agent = ? OR from_agent = ?)"
            params.extend([agent, agent])
        scope_sql, scope_params = await self._scope_sql(project_ids, project_id, scope)
        to_sql, to_params = _recipient_sql(recipient)
        where += scope_sql + to_sql
        params.extend([*scope_params, *to_params])

        cursor = await self._db.conn.execute(f"SELECT COUNT(*) FROM agent_inbox WHERE {where}", params)
        count_row = await cursor.fetchone()
        cursor = await self._db.conn.execute(
            f"SELECT * FROM agent_inbox WHERE {where} ORDER BY julianday(created_at) DESC, inbox_id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [_row_to_dict(r) for r in rows], "total": int(count_row[0]) if count_row else 0}

    async def summary(
        self,
        owner_user_id: str,
        *,
        project_ids: list[str] | None = None,
        project_id: str | None = None,
        scope: str = "all",
        recipient: str | None = None,
    ) -> dict[str, Any]:
        """Per-agent counts: messages waiting for each agent and sent by it.

        Returns ``{"unread_total", "open_total", "agents": [{agent_id, unread,
        open, received, sent, last_at}]}``; agents sorted by unread, then by
        most recent activity. Expired rows, rows outside the caller's projects
        and (with ``recipient``) rows not addressed to that agent are skipped.
        """
        now_iso = datetime.now(UTC).isoformat()
        scope_sql, scope_params = await self._scope_sql(project_ids, project_id, scope)
        to_sql, to_params = _recipient_sql(recipient)
        scope_sql += to_sql
        scope_params = [*scope_params, *to_params]
        cursor = await self._db.conn.execute(
            f"""
            SELECT agent,
                   SUM(is_to * (status = 'unread')) AS unread,
                   SUM(is_to * (status IN ('unread', 'read'))) AS open,
                   SUM(is_to) AS received,
                   SUM(1 - is_to) AS sent,
                   MAX(julianday(created_at)) AS last_jd,
                   MAX(created_at) AS last_at
            FROM (
                SELECT to_agent AS agent, 1 AS is_to, status, created_at FROM agent_inbox
                WHERE owner_user_id = ? AND (expires_at IS NULL OR expires_at > ?){scope_sql}
                UNION ALL
                SELECT from_agent AS agent, 0 AS is_to, status, created_at FROM agent_inbox
                WHERE owner_user_id = ? AND (expires_at IS NULL OR expires_at > ?){scope_sql}
            )
            GROUP BY agent
            ORDER BY unread DESC, last_jd DESC, agent
            """,  # noqa: S608 - fixed fragments; values are bound
            (owner_user_id, now_iso, *scope_params, owner_user_id, now_iso, *scope_params),
        )
        agents = [
            {
                "agent_id": r["agent"],
                "unread": int(r["unread"] or 0),
                "open": int(r["open"] or 0),
                "received": int(r["received"] or 0),
                "sent": int(r["sent"] or 0),
                "last_at": r["last_at"],
            }
            for r in await cursor.fetchall()
            if r["agent"]
        ]
        return {
            "unread_total": sum(a["unread"] for a in agents),
            "open_total": sum(a["open"] for a in agents),
            "agents": agents,
        }

    # -----------------------------------------------------------------------
    # Ack
    # -----------------------------------------------------------------------

    async def ack(
        self,
        owner_user_id: str,
        inbox_id: str,
        result: str | None = None,
        note: str | None = None,
        *,
        project_ids: list[str] | None = None,
        recipient: str | None = None,
    ) -> dict[str, Any]:
        """Mark an inbox row as acknowledged.

        Args:
            owner_user_id: Tenant scope.
            inbox_id: The inbox row to ack.
            result: Optional terminal status: "done", "blocked", or "rejected".
                    If omitted, status becomes "read".
            note: Optional free-text note from the receiving agent.
            project_ids: The caller's project allow-list; a row outside it is
                "not found".
            recipient: When set, only a row addressed to this agent can be
                acked; any other row is "not found".

        Returns the updated row. Raises ValueError on bad inputs or if the
        row does not exist / is not owned by caller.
        """
        if result is not None and result not in TERMINAL_STATUSES:
            raise ValueError(f"result must be one of {sorted(TERMINAL_STATUSES)} or omitted, got '{result}'")

        existing = await self.get_one(owner_user_id, inbox_id, project_ids=project_ids, recipient=recipient)
        if existing is None:
            raise ValueError(f"Inbox item '{inbox_id}' not found")

        new_status = result if result else "read"
        now_iso = datetime.now(UTC).isoformat()

        await self._db.conn.execute(
            """
            UPDATE agent_inbox
               SET status = ?, ack_at = ?, ack_note = ?, ack_result = ?
             WHERE inbox_id = ? AND owner_user_id = ?
            """,
            (new_status, now_iso, note, result, inbox_id, owner_user_id),
        )
        await self._db.conn.commit()

        logger.info(
            "inbox_acked owner=%s id=%s status=%s",
            owner_user_id,
            inbox_id,
            new_status,
        )

        # Re-read to get the authoritative row
        updated = await self.get_one(owner_user_id, inbox_id)
        assert updated is not None  # just updated it
        return updated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an aiosqlite Row to a plain dict, parsing JSON metadata."""
    d = dict(row)
    raw_meta = d.get("metadata") or "{}"
    try:
        d["metadata"] = json.loads(raw_meta)
    except (TypeError, json.JSONDecodeError):
        d["metadata"] = {}
    return d
