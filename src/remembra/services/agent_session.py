"""Agent session services — session brief, status upsert, timeline, type policy.

Built for the multi-agent workflow where several AI clients (Claude Code,
Claude Desktop, Codex, Gemini, Clawdbot) share one memory. It gives them:

* ``apply_memory_type_policy`` — hygiene rules for the agent memory types:
  ``checkpoint`` gets a default TTL and is stored atomically, ``handoff`` is
  stored as ONE unit (never fact-split or merged), ``status`` must go through
  the upsert path so the previous value is superseded instead of piling up.
* ``AgentSessionService.brief`` — what an agent needs at session start: the
  latest handoff snapshot, its unread inbox, current status values, and the
  most recent memories BY TIME (not semantic similarity).
* ``AgentSessionService.upsert_status`` — store-by-key: the new value
  supersedes the prior value for the same (user, project, key).
* ``AgentSessionService.timeline`` — chronological listing with a real
  server-side ``created_at`` range filter and optional entity filter.

All reads are SQLite queries scoped to the authenticated ``user_id``;
superseded and expired memories are excluded unless explicitly requested.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.core.time import utcnow
from remembra.models.memory import StoreRequest
from remembra.relay.handoff import assess_text

log = structlog.get_logger(__name__)

STATUS_KEY_FIELD = "status_key"
STATUS_VALUE_FIELD = "status_value"
MAX_STATUS_KEY_LEN = 128
INBOX_PREVIEW_CHARS = 200

# Serialises status upserts in this process so two concurrent writes for the
# same key can't each supersede the other (leaving no current value).
_status_lock = asyncio.Lock()


class MemoryTypePolicyError(ValueError):
    """Raised when a store request violates the policy for its memory_type."""


def apply_memory_type_policy(request: StoreRequest, checkpoint_default_ttl: str) -> None:
    """Apply hygiene rules for agent memory types to a store request, in place.

    - ``checkpoint``: default TTL when the caller set neither ``ttl`` nor
      ``expires_at``; stored atomically so it is never consolidated into (and
      never lends its TTL to) a permanent memory.
    - ``handoff``: stored atomically as one unit — a session snapshot must be
      readable verbatim by the next agent, not split into facts.
    - ``status``: rejected here; use the status upsert endpoint.
    """
    memory_type = request.memory_type
    if memory_type == "checkpoint":
        if not request.ttl and not request.expires_at:
            request.ttl = checkpoint_default_ttl
        request.skip_extraction = True
    elif memory_type == "handoff":
        request.skip_extraction = True
    elif memory_type == "status":
        raise MemoryTypePolicyError(
            "memory_type='status' must be written with POST /api/v1/session/status "
            "(store_status) so the previous value for the key is superseded."
        )


def normalize_status_key(key: str) -> str:
    """Validate and normalize a status key (trimmed, lower-cased, 1-128 chars)."""
    cleaned = " ".join((key or "").split()).lower()
    if not cleaned:
        raise ValueError("status key must not be empty")
    if len(cleaned) > MAX_STATUS_KEY_LEN:
        raise ValueError(f"status key must be at most {MAX_STATUS_KEY_LEN} characters")
    return cleaned


def _inbox_project_filter(project_ids: list[str] | None) -> tuple[str, list[str]]:
    """SQL (appended to a WHERE) keeping inbox rows tagged with one of ``project_ids``.

    None means no restriction. An empty list matches nothing (fail closed).
    Rows without ``metadata.project_id`` never match a restriction.
    """
    if project_ids is None:
        return "", []
    if not project_ids:
        return " AND 0", []
    marks = ", ".join("?" for _ in project_ids)
    # Malformed legacy metadata reads as untagged instead of failing the query.
    return (
        f" AND (CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.project_id') END) IN ({marks})",
        list(project_ids),
    )


def _to_naive_utc_iso(value: datetime) -> str:
    """Normalize a datetime to the naive-UTC ISO format memories are stored in."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat()


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def serialize_memory_row(row: dict[str, Any]) -> dict[str, Any]:
    """Lean, agent-facing view of a memory row with provenance surfaced."""
    metadata = _parse_metadata(row.get("metadata"))
    return {
        "id": row["id"],
        "project_id": row.get("project_id"),
        "content": row.get("content") or "",
        "memory_type": row.get("memory_type") or metadata.get("memory_type"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "superseded_by": row.get("superseded_by"),
        "source_id": metadata.get("source_id"),
        "agent_id": metadata.get("agent_id"),
        "metadata": metadata,
        # Provenance columns: which write path stored the row, and the sanitizer's verdict.
        "source": row.get("source"),
        "trust_score": row.get("trust_score"),
    }


_ROW_COLUMNS = (
    "id, user_id, project_id, content, metadata, created_at, expires_at, memory_type, superseded_by, source, trust_score"
)

# When a handoff's session ended, as a julianday: the close time the relay
# recorded (only on rows the relay wrote, and never after the row was stored),
# else the row's stored time (free-form handoffs have no close time).
HANDOFF_ENDED_JD = (
    "COALESCE(CASE WHEN source = 'agent_generated' AND json_valid(metadata) "
    "THEN MIN(julianday(json_extract(metadata, '$.relay.closed_at')), julianday(created_at)) END, "
    "julianday(created_at))"
)


class AgentSessionService:
    """Session brief, status upsert, and timeline over the metadata store.

    Args:
        db: The application ``Database`` (uses its aiosqlite ``conn``).
        memory_service: ``MemoryService`` used for writes, so status values go
            through the normal embed + index path and stay recallable.
    """

    def __init__(self, db: Any, memory_service: Any | None = None) -> None:
        self.db = db
        self.memory_service = memory_service

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _active_clause(self, user_id: str, include_superseded: bool = False) -> tuple[str, list[Any]]:
        clause = "user_id = ? AND (expires_at IS NULL OR expires_at > ?)"
        params: list[Any] = [user_id, utcnow().isoformat()]
        if not include_superseded:
            clause += " AND superseded_by IS NULL"
        return clause, params

    async def timeline(
        self,
        user_id: str,
        project_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        entity: str | None = None,
        memory_types: list[str] | None = None,
        exclude_types: tuple[str, ...] = ("source",),
        include_superseded: bool = False,
        limit: int = 20,
        offset: int = 0,
        newest_first: bool = False,
        agent_id: str | None = None,
        before: tuple[datetime, str | None] | None = None,
    ) -> dict[str, Any]:
        """Chronological memories with server-side created_at range filtering.

        ``start`` is inclusive, ``end`` exclusive. ``entity`` matches an
        entity's canonical name or alias exactly (case-insensitive) — never a
        substring, so "A" or "bot" can't pull in unrelated memories.
        ``agent_id`` keeps only memories whose metadata names that agent.
        ``before`` is a keyset cursor ``(created_at, id)``: only memories that
        sort strictly after it in newest-first order (older, or the same time
        with a smaller id) are returned, so a page does not shift when newer
        rows arrive. A ``None`` id keeps rows strictly older than the time.
        Returns ``{"memories": [...], "total": N}`` where ``total`` counts all
        matches (after the cursor, when given) ignoring limit/offset.
        """
        where, params = self._active_clause(user_id, include_superseded)
        if project_id:
            where += " AND project_id = ?"
            params.append(project_id)
        if start is not None:
            where += " AND julianday(created_at) >= julianday(?)"
            params.append(_to_naive_utc_iso(start))
        if end is not None:
            where += " AND julianday(created_at) < julianday(?)"
            params.append(_to_naive_utc_iso(end))
        if memory_types:
            where += f" AND memory_type IN ({','.join('?' for _ in memory_types)})"
            params.extend(memory_types)
        if agent_id:
            where += " AND json_valid(metadata) AND json_extract(metadata, '$.agent_id') = ?"
            params.append(agent_id)
        if before is not None:
            before_at = _to_naive_utc_iso(before[0])
            if before[1]:
                where += " AND (julianday(created_at) < julianday(?) OR (julianday(created_at) = julianday(?) AND id < ?))"
                params.extend([before_at, before_at, before[1]])
            else:
                where += " AND julianday(created_at) < julianday(?)"
                params.append(before_at)
        if exclude_types:
            where += f" AND (memory_type IS NULL OR memory_type NOT IN ({','.join('?' for _ in exclude_types)}))"
            params.extend(exclude_types)
        if entity:
            where += """
              AND id IN (
                SELECT me.memory_id FROM memory_entities me
                JOIN entities e ON e.id = me.entity_id
                WHERE e.user_id = ?
                  AND (
                    lower(e.canonical_name) = lower(?)
                    OR (
                      e.aliases IS NOT NULL AND json_valid(e.aliases)
                      AND EXISTS (SELECT 1 FROM json_each(e.aliases) WHERE lower(json_each.value) = lower(?))
                    )
                  )
              )
            """
            params.extend([user_id, entity.strip(), entity.strip()])

        cursor = await self.db.conn.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params)
        count_row = await cursor.fetchone()
        total = int(count_row[0]) if count_row else 0

        # julianday() keeps sub-second precision; datetime() truncates to whole
        # seconds, which made same-second writes (status v1 -> v2) sort randomly.
        order = "DESC" if newest_first else "ASC"
        cursor = await self.db.conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM memories WHERE {where} "
            f"ORDER BY julianday(created_at) {order}, id {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"memories": [serialize_memory_row(dict(r)) for r in rows], "total": total}

    async def latest_handoff(self, user_id: str, project_id: str | None) -> dict[str, Any] | None:
        """The current handoff whose session ended last.

        A relay handoff records when its session ended (``relay.closed_at``),
        which for one sent late from a client's offline queue is well before the
        server received it. Ordering by that time keeps a late delivery from
        replacing a newer handoff as the "Last session". Handoffs without it
        (free-form ones) use their stored time.
        """
        where, params = self._active_clause(user_id)
        where += " AND memory_type = 'handoff'"
        if project_id:
            where += " AND project_id = ?"
            params.append(project_id)
        cursor = await self.db.conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM memories WHERE {where} "
            f"ORDER BY {HANDOFF_ENDED_JD} DESC, julianday(created_at) DESC, id DESC LIMIT 1",
            params,
        )
        row = await cursor.fetchone()
        return serialize_memory_row(dict(row)) if row else None

    async def _current_status_rows(self, user_id: str, project_id: str, key: str | None = None) -> list[dict[str, Any]]:
        where, params = self._active_clause(user_id)
        where += " AND project_id = ? AND memory_type = 'status'"
        params.append(project_id)
        if key is not None:
            where += " AND json_valid(metadata) AND json_extract(metadata, '$.status_key') = ?"
            params.append(key)
        cursor = await self.db.conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM memories WHERE {where} ORDER BY julianday(created_at) DESC, id DESC",
            params,
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_status(self, user_id: str, project_id: str) -> list[dict[str, Any]]:
        """Current (non-superseded, non-expired) status values for a project."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in await self._current_status_rows(user_id, project_id):
            metadata = _parse_metadata(row.get("metadata"))
            key = metadata.get(STATUS_KEY_FIELD)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "key": key,
                    "value": metadata.get(STATUS_VALUE_FIELD, row.get("content")),
                    "memory_id": row["id"],
                    "updated_at": row.get("created_at"),
                    "expires_at": row.get("expires_at"),
                    "agent_id": metadata.get("agent_id"),
                    "trust_score": row.get("trust_score"),
                }
            )
        items.sort(key=lambda item: item["key"])
        return items

    # ------------------------------------------------------------------
    # Status upsert
    # ------------------------------------------------------------------

    async def upsert_status(
        self,
        user_id: str,
        project_id: str,
        key: str,
        value: str,
        metadata: dict[str, Any] | None = None,
        ttl: str | None = None,
        source: str = "user_input",
        trust_score: float = 1.0,
        checksum: str | None = None,
        before_write: Callable[[], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        """Set the current value for ``key``; supersede any previous value.

        The prior memory is kept (marked superseded, excluded from recall) so
        the history of the status stays queryable. Writing the value that is
        already current is a no-op (``changed: False``) — no new memory.

        ``before_write`` runs only when a new value will be stored, after the
        unchanged check and under the same lock (the plan gate: an unchanged
        re-send is never charged). If it raises, nothing is written.
        """
        if self.memory_service is None:
            raise RuntimeError("upsert_status requires a memory service")
        key = normalize_status_key(key)
        value = (value or "").strip()
        if not value:
            raise ValueError("status value must not be empty")

        async with _status_lock:
            current = await self._current_status_rows(user_id, project_id, key)
            if len(current) == 1:
                current_meta = _parse_metadata(current[0].get("metadata"))
                if current_meta.get(STATUS_VALUE_FIELD) == value:
                    return {
                        "key": key,
                        "value": value,
                        "memory_id": current[0]["id"],
                        "changed": False,
                        "superseded": [],
                    }

            if before_write is not None:
                await before_write()

            store_metadata = dict(metadata or {})
            store_metadata[STATUS_KEY_FIELD] = key
            store_metadata[STATUS_VALUE_FIELD] = value
            request = StoreRequest(
                content=f"{key}: {value}",
                user_id=user_id,
                project_id=project_id,
                memory_type="status",
                metadata=store_metadata,
                ttl=ttl,
                skip_extraction=True,
            )
            stored = await self.memory_service.store(
                request,
                source=source,
                trust_score=trust_score,
                checksum=checksum,
                skip_extraction=True,
            )
            new_id = stored.id

            superseded: list[str] = []
            for row in await self._current_status_rows(user_id, project_id, key):
                if row["id"] == new_id:
                    continue
                await self.db.mark_memory_superseded(row["id"], new_id)
                superseded.append(row["id"])

        log.info("status_upserted", key=key, project_id=project_id, memory_id=new_id, superseded=len(superseded))
        return {"key": key, "value": value, "memory_id": new_id, "changed": True, "superseded": superseded}

    # ------------------------------------------------------------------
    # Inbox (read-only views for the brief)
    # ------------------------------------------------------------------

    async def _inbox_summary(
        self, user_id: str, agent_id: str, limit: int, project_ids: list[str] | None = None
    ) -> dict[str, Any]:
        now_iso = datetime.now(UTC).isoformat()
        project_sql, project_args = _inbox_project_filter(project_ids)
        try:
            cursor = await self.db.conn.execute(
                f"""
                SELECT COUNT(*) FROM agent_inbox
                WHERE owner_user_id = ? AND to_agent = ? AND status = 'unread'
                  AND (expires_at IS NULL OR expires_at > ?){project_sql}
                """,  # noqa: S608 - placeholders only; values are bound
                (user_id, agent_id, now_iso, *project_args),
            )
            count_row = await cursor.fetchone()
            cursor = await self.db.conn.execute("PRAGMA table_info(agent_inbox)")
            trust_col = "trust_score" if "trust_score" in {r[1] for r in await cursor.fetchall()} else "NULL"
            cursor = await self.db.conn.execute(
                f"""
                SELECT inbox_id, from_agent, subject, body, created_at, {trust_col} AS trust_score FROM agent_inbox
                WHERE owner_user_id = ? AND to_agent = ? AND status = 'unread'
                  AND (expires_at IS NULL OR expires_at > ?){project_sql}
                ORDER BY julianday(created_at) DESC, inbox_id DESC
                LIMIT ?
                """,  # noqa: S608 - placeholders only; values are bound
                (user_id, agent_id, now_iso, *project_args, limit),
            )
            rows = await cursor.fetchall()
        except Exception as e:  # agent_inbox table absent (inbox disabled)
            log.warning("session_brief_inbox_unavailable", error=str(e))
            return {"available": False, "agent_id": agent_id, "unread_count": 0, "items": []}

        items = []
        for r in rows:
            body = r["body"] or ""
            trust = r["trust_score"]
            if trust is None:
                # Written before messages were scored (or on a table without the
                # column): score the whole message now, not just the preview.
                trust = assess_text(r["from_agent"], r["subject"], body).trust
            items.append(
                {
                    "inbox_id": r["inbox_id"],
                    "from_agent": r["from_agent"],
                    "subject": r["subject"],
                    "created_at": r["created_at"],
                    "body_preview": body[:INBOX_PREVIEW_CHARS] + ("..." if len(body) > INBOX_PREVIEW_CHARS else ""),
                    "trust_score": trust,
                }
            )
        return {
            "available": True,
            "agent_id": agent_id,
            "unread_count": int(count_row[0]) if count_row else 0,
            "items": items,
        }

    async def known_agents(self, user_id: str, project_ids: list[str] | None = None) -> list[str]:
        """Agent ids that have sent or received inbox messages for this user
        (only in messages tagged with one of ``project_ids`` when given)."""
        project_sql, project_args = _inbox_project_filter(project_ids)
        try:
            cursor = await self.db.conn.execute(
                f"""
                SELECT to_agent AS agent FROM agent_inbox WHERE owner_user_id = ?{project_sql}
                UNION
                SELECT from_agent AS agent FROM agent_inbox WHERE owner_user_id = ?{project_sql}
                ORDER BY agent LIMIT 100
                """,  # noqa: S608 - placeholders only; values are bound
                (user_id, *project_args, user_id, *project_args),
            )
            return [r[0] for r in await cursor.fetchall() if r[0]]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Brief
    # ------------------------------------------------------------------

    async def brief(
        self,
        user_id: str,
        project_id: str | None,
        agent_id: str | None,
        recent_n: int = 10,
        inbox_limit: int = 10,
        inbox_project_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Everything an agent needs at session start, in one call.

        ``inbox_project_ids`` limits the inbox and ``known_agents`` to messages
        tagged (``metadata.project_id``) with one of those projects. Callers
        restricted to projects (project-scoped keys, connector grants) must
        pass their allowed projects; untagged messages are then excluded
        because nothing says which project they belong to.
        """
        warnings: list[str] = []
        agent = (agent_id or "").strip() or None

        handoff = await self.latest_handoff(user_id, project_id)
        status_items = await self.list_status(user_id, project_id) if project_id else []
        if not project_id:
            warnings.append("No project_id given: recent memories span all projects and status items are omitted.")

        recent = (
            await self.timeline(
                user_id=user_id,
                project_id=project_id,
                exclude_types=("source", "handoff", "status"),
                limit=recent_n,
                newest_first=True,
            )
        )["memories"]

        if agent:
            inbox: dict[str, Any] | None = await self._inbox_summary(user_id, agent, inbox_limit, inbox_project_ids)
        else:
            inbox = None
            warnings.append("No agent_id: inbox not checked. Set REMEMBRA_AGENT_ID for this client so other agents can reach it.")

        known = await self.known_agents(user_id, inbox_project_ids)
        if agent and known and agent not in known:
            warnings.append(f"agent_id '{agent}' has never sent or received an inbox message; check for id typos.")

        return {
            "project_id": project_id,
            "agent_id": agent,
            "generated_at": datetime.now(UTC).isoformat(),
            "handoff": handoff,
            "inbox": inbox,
            "status_items": status_items,
            "recent": recent,
            "known_agents": known,
            "warnings": warnings,
        }
