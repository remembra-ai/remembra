"""Crew core read model (WP-8): crews, snapshot, members, per-agent page, batons, brief and trail data.

Spec anchors: §4.4 (snapshot), §6 (``/crews`` routes, per-agent page), §9.10
(agent page, L0 minimal), §6 "Crew block in the brief", §6 "Existing relay"
(trail additions).

Everything here reads ``crew.db`` only. Access control is not done here: the
routes resolve the crew through ``crew/access.py`` first and every method is
scoped by ``crew_id``. Nothing here creates a crew (resolve is read-only, §2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas, views
from remembra.crew.db import CrewDatabase
from remembra.crew.settings import load_settings
from remembra.crew.store import now_iso

# Snapshot caps (schemas.SNAPSHOT list sizes).
SNAPSHOT_LIMITS: Final[Mapping[str, int]] = {
    "sessions": 200,
    "claims": 1000,
    "zones": 500,
    "commons": 200,
    "ignore": 200,
    "tasks": 1000,
    "collisions": 500,
    "decisions": 500,
    "offers": 200,
    "footprints": 2000,
    "pending_zone_changes": 50,
}
# Ended sessions stay in the snapshot this long: collision matching looks at
# footprints of sessions ended no more than 24 h ago (§5.3).
RECENT_ENDED: Final = timedelta(hours=24)
OPEN_TASK_STATUSES: Final = tuple(s for s in schemas.TASK_STATUSES if s not in ("done", "cancelled"))
UNFINISHED_TASK_STATUSES: Final = ("claimed", "in_progress", "blocked")
AGENT_PAGE_SESSIONS: Final = 20
AGENT_PAGE_CHECKPOINTS: Final = 20
AGENT_PAGE_BATONS: Final = 20
MAX_BATONS_PAGE: Final = 100
LIST_LIVE_SESSIONS: Final = 20


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def parse_ts(value: Any) -> datetime | None:
    """Parse any stored ISO time (crew ``…Z`` or main-DB naive/offset) as aware UTC; None if unparseable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def enforcement_of(crew_row: Mapping[str, Any]) -> str:
    return str(load_settings(crew_row.get("settings")).get("enforcement", "enforce"))


def snapshot_etag(body: Mapping[str, Any]) -> str:
    """Strong ETag over everything in the snapshot except the server clock and the ETag itself."""
    material = {k: v for k, v in body.items() if k not in ("server_time", "etag")}
    return '"' + hashlib.sha256(schemas.canonical_json(material)).hexdigest()[:32] + '"'


# ---------------------------------------------------------------------------
# Facts helpers (checkpoint facts are free-form ≤16 KB JSON, §5.5)
# ---------------------------------------------------------------------------


def _count(facts: Mapping[str, Any], count_keys: Iterable[str], list_keys: Iterable[str]) -> int | None:
    for key in count_keys:
        value = facts.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    for key in list_keys:
        value = facts.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def facts_counts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Counts the brief shows from checkpoint/report facts: dirty files, unpushed commits, failing tests.

    Reads the relay facts vocabulary (``uncommitted_files``, ``unpushed_commits``,
    ``tests[{cmd, passed}]``) and the crew checkpoint vocabulary (``dirty_count``,
    ``dirty_files``, ``unpushed_count``, ``tests[{command|fingerprint, verdict|passed|failed}]``).
    """
    failing: list[str] = []
    for test in facts.get("tests") or []:
        if not isinstance(test, dict):
            continue
        failed = (
            test.get("passed") is False
            or test.get("verdict") == "fail"
            or (isinstance(test.get("failed"), int) and not isinstance(test.get("failed"), bool) and test["failed"] > 0)
        )
        if failed:
            name = test.get("command") or test.get("cmd") or test.get("fingerprint") or "test"
            failing.append(str(name))
    return {
        "dirty": _count(facts, ("dirty_count",), ("dirty_files", "uncommitted_files")),
        "unpushed": _count(facts, ("unpushed_count", "unpushed_commits"), ()),
        "failing": list(dict.fromkeys(failing)),
    }


def _section_text(sections: Mapping[str, Any], key: str) -> str | None:
    value = sections.get(key)
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list):
        items = [str(v) for v in value if isinstance(v, str) and v.strip()]
        return "; ".join(items) if items else None
    return None


class CrewCore:
    """Read side of the crew core API over one ``crew.db``."""

    def __init__(self, db: CrewDatabase) -> None:
        self.db = db

    # -- crews -------------------------------------------------------------------

    async def live_count(self, crew_id: str) -> int:
        states = schemas.LIVE_PRESENCE_STATES
        row = await self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM crew_sessions WHERE crew_id = ? AND state IN ({_marks(states)})",
            (crew_id, *states),
        )
        return int(row["n"]) if row else 0

    async def crew_row(self, crew_id: str) -> dict[str, Any] | None:
        return await self.db.fetchone("SELECT * FROM crews WHERE id = ?", (crew_id,))

    async def crew_view(self, crew_row: Mapping[str, Any]) -> dict[str, Any]:
        live = await self.live_count(crew_row["id"])
        return views.crew_view(crew_row, live_sessions=live, enforcement=enforcement_of(crew_row))

    async def crews_for_project(self, user_id: str, project_id: str) -> list[dict[str, Any]]:
        """Crews of ``project_id`` the user can read: their own first, then crews they are a member of."""
        return await self.db.fetchall(
            """
            SELECT c.*, CASE WHEN c.owner_user_id = ? THEN 'owner' ELSE m.role END AS role
              FROM crews c
              LEFT JOIN crew_members m ON m.crew_id = c.id AND m.user_id = ?
             WHERE c.project_id = ? AND (c.owner_user_id = ? OR m.user_id IS NOT NULL)
             ORDER BY CASE WHEN c.owner_user_id = ? THEN 0 ELSE 1 END, c.created_at, c.id
            """,
            (user_id, user_id, project_id, user_id, user_id),
        )

    async def list_summaries(self, crews: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
        """``GET /crews`` rows: crew view, role, counts, live lanes and task progress per phase (Site Board, §9.2)."""
        now = now or datetime.now(UTC)
        since = now_iso(now - timedelta(hours=24))
        live_states = schemas.LIVE_PRESENCE_STATES
        inbox_states = schemas.LIVE_INBOX_STATES
        out: list[dict[str, Any]] = []
        for crew in crews:
            crew_id = crew["id"]
            sessions = await self.db.fetchall(
                f"""SELECT * FROM crew_sessions WHERE crew_id = ? AND state IN ({_marks(live_states)})
                    ORDER BY joined_at, id LIMIT ?""",
                (crew_id, *live_states, LIST_LIVE_SESSIONS + 1),
            )
            live = await self.live_count(crew_id)
            inbox = await self.db.fetchall(
                f"""SELECT audience, COUNT(*) AS n FROM crew_inbox_items
                     WHERE crew_id = ? AND state IN ({_marks(inbox_states)}) GROUP BY audience""",
                (crew_id, *inbox_states),
            )
            counts = {r["audience"]: int(r["n"]) for r in inbox}
            moments = await self.db.fetchone(
                "SELECT COUNT(*) AS n FROM crew_events WHERE crew_id = ? AND moment = 1 AND ts >= ?", (crew_id, since)
            )
            last = await self.db.fetchone("SELECT ts FROM crew_events WHERE crew_id = ? ORDER BY seq DESC LIMIT 1", (crew_id,))
            tasks = await self.db.fetchall(
                "SELECT phase, status, COUNT(*) AS n FROM crew_tasks WHERE crew_id = ? GROUP BY phase, status", (crew_id,)
            )
            by_status: dict[str, int] = {}
            phases: dict[str, dict[str, int]] = {}
            for row in tasks:
                n = int(row["n"])
                by_status[row["status"]] = by_status.get(row["status"], 0) + n
                if row["status"] == "cancelled":
                    continue
                phase = phases.setdefault(row["phase"] or "", {"total": 0, "done": 0})
                phase["total"] += n
                if row["status"] == "done":
                    phase["done"] += n
            out.append(
                {
                    "crew": views.crew_view(crew, live_sessions=live, enforcement=enforcement_of(crew)),
                    "role": crew.get("role") or "owner",
                    "live": live,
                    "needs_you": counts.get("project", 0),
                    "crew_inbox": counts.get("crew", 0),
                    "moments_24h": int(moments["n"]) if moments else 0,
                    "last_event_at": last["ts"] if last else None,
                    "tasks_by_status": by_status,
                    "phases": [{"phase": k or None, **v} for k, v in sorted(phases.items())],
                    "live_sessions": [views.session_view(s) for s in sessions[:LIST_LIVE_SESSIONS]],
                    "live_sessions_truncated": len(sessions) > LIST_LIVE_SESSIONS,
                }
            )
        return out

    # -- snapshot (§4.4) -----------------------------------------------------------

    async def snapshot(self, crew_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """``GET /crews/{id}/snapshot``: one consistent read (a single crew.db transaction) at ``as_of_seq``."""
        now = now or datetime.now(UTC)
        async with self.db.transaction():
            crew = await self.crew_row(crew_id)
            if crew is None:
                raise LookupError(crew_id)
            body = await self._snapshot_body(crew, now)
        body["etag"] = snapshot_etag(body)
        return body

    async def _snapshot_body(self, crew: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        crew_id = crew["id"]
        lim = SNAPSHOT_LIMITS
        live_states = schemas.LIVE_PRESENCE_STATES
        live_claim_states = schemas.LIVE_CLAIM_STATES
        recent = now_iso(now - RECENT_ENDED)

        claims = await self.db.fetchall(
            f"SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ({_marks(live_claim_states)})"
            " ORDER BY created_at, id LIMIT ?",
            (crew_id, *live_claim_states, lim["claims"]),
        )
        holder_ids = sorted({c["holder_session_id"] for c in claims if c.get("holder_session_id")})
        sessions = await self.db.fetchall(
            f"""
            SELECT * FROM crew_sessions
             WHERE crew_id = ?
               AND (state IN ({_marks(live_states)})
                    OR (ended_at IS NOT NULL AND ended_at >= ?)
                    OR state = 'lost'
                    OR id IN ({_marks(holder_ids) or "NULL"}))
             ORDER BY CASE WHEN state IN ({_marks(live_states)}) THEN 0 ELSE 1 END, joined_at, id
             LIMIT ?
            """,
            (crew_id, *live_states, recent, *holder_ids, *live_states, lim["sessions"]),
        )
        live = sum(1 for s in sessions if s["state"] in live_states)
        if len(sessions) >= lim["sessions"]:
            live = await self.live_count(crew_id)
        session_ids = [s["id"] for s in sessions]

        zones = await self.db.fetchall(
            "SELECT * FROM crew_zones WHERE crew_id = ? AND archived_at IS NULL ORDER BY slug LIMIT ?",
            (crew_id, lim["zones"]),
        )
        zone_file = await self.db.fetchone("SELECT commons, ignore FROM crew_zone_files WHERE crew_id = ?", (crew_id,))
        commons = views.commons_entries(zone_file["commons"]) if zone_file else []
        ignore = [g for g in _loads(zone_file["ignore"], []) if isinstance(g, str)] if zone_file else []

        tasks = await self.db.fetchall(
            f"SELECT * FROM crew_tasks WHERE crew_id = ? AND status IN ({_marks(OPEN_TASK_STATUSES)}) ORDER BY number LIMIT ?",
            (crew_id, *OPEN_TASK_STATUSES, lim["tasks"]),
        )
        deps = await self.task_deps(crew_id, [t["id"] for t in tasks])

        collisions = await self.db.fetchall(
            f"SELECT * FROM crew_collisions WHERE crew_id = ? AND state IN ({_marks(schemas.LIVE_COLLISION_STATES)})"
            " ORDER BY created_at, id LIMIT ?",
            (crew_id, *schemas.LIVE_COLLISION_STATES, lim["collisions"]),
        )
        decisions = await self.db.fetchall(
            f"SELECT * FROM crew_decisions WHERE crew_id = ? AND state IN ({_marks(schemas.LIVE_DECISION_STATES)})"
            " ORDER BY number LIMIT ?",
            (crew_id, *schemas.LIVE_DECISION_STATES, lim["decisions"]),
        )
        offers = await self.db.fetchall(
            """
            SELECT o.* FROM crew_baton_offers o JOIN crew_claims c ON c.id = o.claim_id AND c.crew_id = o.crew_id
             WHERE o.crew_id = ? AND o.used_at IS NULL AND c.state = 'reserved'
             ORDER BY o.created_at, o.id LIMIT ?
            """,
            (crew_id, lim["offers"]),
        )
        footprints = (
            await self.db.fetchall(
                f"""SELECT * FROM crew_footprints WHERE crew_id = ? AND state IN ('dirty', 'committed')
                     AND session_id IN ({_marks(session_ids)}) ORDER BY last_at DESC, path LIMIT ?""",
                (crew_id, *session_ids, lim["footprints"]),
            )
            if session_ids
            else []
        )
        inbox = await self.db.fetchall(
            f"""SELECT audience, COUNT(*) AS n FROM crew_inbox_items
                 WHERE crew_id = ? AND audience IN ('project', 'crew') AND state IN ({_marks(schemas.LIVE_INBOX_STATES)})
                 GROUP BY audience""",
            (crew_id, *schemas.LIVE_INBOX_STATES),
        )
        counts = {r["audience"]: int(r["n"]) for r in inbox}
        pending = await self.db.fetchall(
            "SELECT id FROM crew_zone_changes WHERE crew_id = ? AND state = 'pending' ORDER BY created_at, id LIMIT ?",
            (crew_id, lim["pending_zone_changes"]),
        )
        return {
            "crew": views.crew_view(crew, live_sessions=live, enforcement=enforcement_of(crew)),
            "server_time": now_iso(now),
            "as_of_seq": int(crew["last_seq"] or 0),
            "etag": "",
            "sessions": [views.session_view(s) for s in views.nest_sessions(sessions)],
            "claims": [views.claim_view(c, now) for c in claims],
            "zones": [views.zone_view(z) for z in zones],
            "commons": commons[: lim["commons"]],
            "ignore": ignore[: lim["ignore"]],
            "tasks": [views.task_view(t, deps.get(t["id"], [])) for t in tasks],
            "collisions": [views.collision_view(c) for c in collisions],
            "decisions": [views.decision_view(d) for d in decisions],
            "offers": [views.offer_view(o) for o in offers],
            "footprints": [views.footprint_view(f) for f in footprints],
            "inbox_counts": {"project": counts.get("project", 0), "crew": counts.get("crew", 0)},
            "pending_zone_changes": [p["id"] for p in pending],
        }

    async def task_deps(self, crew_id: str, task_ids: Sequence[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for start in range(0, len(task_ids), 500):
            chunk = list(task_ids[start : start + 500])
            rows = await self.db.fetchall(
                f"SELECT task_id, depends_on_id FROM crew_task_deps WHERE crew_id = ? AND task_id IN ({_marks(chunk)})"
                " ORDER BY task_id, depends_on_id",
                (crew_id, *chunk),
            )
            for r in rows:
                out.setdefault(r["task_id"], []).append(r["depends_on_id"])
        return out

    # -- per-agent page (§9.10, L0 minimal) ---------------------------------------------

    async def agent_sessions(
        self, crew_id: str, agent_id: str, *, limit: int, since: str | None = None, until: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM crew_sessions WHERE crew_id = ? AND agent_id = ?"
        params: list[Any] = [crew_id, agent_id]
        if since is not None:
            sql += " AND COALESCE(ended_at, ?) >= ?"
            params.extend(["9999", since])
        if until is not None:
            sql += " AND joined_at < ?"
            params.append(until)
        sql += " ORDER BY joined_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return await self.db.fetchall(sql, params)

    async def agent_page(self, crew_id: str, agent_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Current sessions, last 20 sessions, checkpoints, batons in and out (with the brief text), claims, tasks."""
        now = now or datetime.now(UTC)
        sessions = await self.agent_sessions(crew_id, agent_id, limit=AGENT_PAGE_SESSIONS)
        live_rows = await self.db.fetchall(
            "SELECT * FROM crew_sessions WHERE crew_id = ? AND agent_id = ?"
            f" AND state IN ({_marks(schemas.LIVE_PRESENCE_STATES)}) ORDER BY joined_at DESC, id DESC",
            (crew_id, agent_id, *schemas.LIVE_PRESENCE_STATES),
        )
        if not sessions and not live_rows:
            return None
        all_ids = await self.db.fetchall("SELECT id FROM crew_sessions WHERE crew_id = ? AND agent_id = ?", (crew_id, agent_id))
        ids = [r["id"] for r in all_ids]
        checkpoints = await self._rows_for_sessions(
            "SELECT * FROM crew_checkpoints WHERE crew_id = ? AND session_id IN ({m}) ORDER BY created_at DESC, id DESC LIMIT ?",
            crew_id,
            ids,
            AGENT_PAGE_CHECKPOINTS,
        )
        batons_in = await self._rows_for_sessions(
            "SELECT * FROM crew_batons WHERE crew_id = ? AND to_session IN ({m}) ORDER BY created_at DESC, id DESC LIMIT ?",
            crew_id,
            ids,
            AGENT_PAGE_BATONS,
        )
        batons_out = await self._rows_for_sessions(
            "SELECT * FROM crew_batons WHERE crew_id = ? AND from_session IN ({m}) ORDER BY created_at DESC, id DESC LIMIT ?",
            crew_id,
            ids,
            AGENT_PAGE_BATONS,
        )
        claims = await self._rows_for_sessions(
            f"SELECT * FROM crew_claims WHERE crew_id = ? AND holder_session_id IN ({{m}})"
            f" AND state IN ({_marks(schemas.LIVE_CLAIM_STATES)}) ORDER BY created_at, id LIMIT ?",
            crew_id,
            ids,
            200,
            extra=schemas.LIVE_CLAIM_STATES,
        )
        tasks = await self._rows_for_sessions(
            f"SELECT * FROM crew_tasks WHERE crew_id = ? AND owner_session_id IN ({{m}})"
            f" AND status IN ({_marks(OPEN_TASK_STATUSES)}) ORDER BY number LIMIT ?",
            crew_id,
            ids,
            200,
            extra=OPEN_TASK_STATUSES,
        )
        deps = await self.task_deps(crew_id, [t["id"] for t in tasks])
        callsigns = await self.callsigns(
            crew_id, {b.get("from_session") for b in batons_in} | {b["to_session"] for b in batons_out}
        )
        current = [views.session_view(s) for s in live_rows]
        return {
            "crew_id": crew_id,
            "agent_id": agent_id,
            "verified": any(bool(s.get("agent_verified")) for s in (*live_rows, *sessions)),
            "current_sessions": current,
            "enforcement": [
                {
                    "session_id": s["id"],
                    "callsign": s["callsign"],
                    "adapter": s.get("adapter"),
                    "before_write": s.get("adapter_enforcement") or "advisory",
                    "commit_gate": s.get("githook_state") or "unknown",
                    "push_gate": s.get("githook_state") or "unknown",
                }
                for s in live_rows
            ],
            "sessions": [views.session_view(s) for s in views.nest_sessions(sessions)],
            "checkpoints": [{**views.checkpoint_view(c), "created_at": c["created_at"]} for c in checkpoints],
            "batons_in": [
                {**views.baton_row_view(b), "from_callsign": callsigns.get(b.get("from_session") or "")} for b in batons_in
            ],
            "batons_out": [{**views.baton_row_view(b), "to_callsign": callsigns.get(b["to_session"])} for b in batons_out],
            "claims": [views.claim_view(c, now) for c in claims],
            "tasks": [views.task_view(t, deps.get(t["id"], [])) for t in tasks],
        }

    async def agent_timeline(
        self, crew_id: str, agent_id: str, *, since: str | None = None, until: str | None = None
    ) -> dict[str, Any] | None:
        """L0 timeline: the last 20 sessions in ``[since, until)``, each with its checkpoints and batons."""
        sessions = await self.agent_sessions(crew_id, agent_id, limit=AGENT_PAGE_SESSIONS, since=since, until=until)
        if not sessions:
            exists = await self.db.fetchone(
                "SELECT 1 AS x FROM crew_sessions WHERE crew_id = ? AND agent_id = ? LIMIT 1", (crew_id, agent_id)
            )
            if exists is None:
                return None
        ids = [s["id"] for s in sessions]
        checkpoints = await self._rows_for_sessions(
            "SELECT * FROM crew_checkpoints WHERE crew_id = ? AND session_id IN ({m}) ORDER BY created_at, id LIMIT ?",
            crew_id,
            ids,
            2000,
        )
        batons = await self._rows_for_sessions(
            "SELECT * FROM crew_batons WHERE crew_id = ? AND (to_session IN ({m}) OR from_session IN ({m}))"
            " ORDER BY created_at, id LIMIT ?",
            crew_id,
            ids,
            500,
            repeat=2,
        )
        entries = []
        for s in sessions:
            sid = s["id"]
            entries.append(
                {
                    "session": views.session_view(s),
                    "checkpoints": [
                        {**views.checkpoint_view(c), "created_at": c["created_at"]} for c in checkpoints if c["session_id"] == sid
                    ],
                    "batons_in": [views.baton_row_view(b) for b in batons if b["to_session"] == sid],
                    "batons_out": [views.baton_row_view(b) for b in batons if b.get("from_session") == sid],
                }
            )
        return {"crew_id": crew_id, "agent_id": agent_id, "from": since, "to": until, "sessions": entries}

    async def _rows_for_sessions(
        self,
        sql: str,
        crew_id: str,
        session_ids: Sequence[str],
        limit: int,
        *,
        extra: Sequence[Any] = (),
        repeat: int = 1,
    ) -> list[dict[str, Any]]:
        if not session_ids:
            return []
        ids = list(session_ids)[:900]  # SQLite variable limit; an agent with more sessions shows the newest
        query = sql.format(m=_marks(ids))
        params: list[Any] = [crew_id]
        for _ in range(repeat):
            params.extend(ids)
        params.extend(extra)
        params.append(limit)
        return await self.db.fetchall(query, params)

    async def callsigns(self, crew_id: str, session_ids: Iterable[str | None]) -> dict[str, str]:
        ids = sorted({s for s in session_ids if s})
        if not ids:
            return {}
        rows = await self.db.fetchall(
            f"SELECT id, callsign FROM crew_sessions WHERE crew_id = ? AND id IN ({_marks(ids)})", (crew_id, *ids)
        )
        return {r["id"]: r["callsign"] for r in rows}

    # -- batons ----------------------------------------------------------------------

    async def batons(self, crew_id: str, *, task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_BATONS_PAGE))
        sql = "SELECT * FROM crew_batons WHERE crew_id = ?"
        params: list[Any] = [crew_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetchall(sql, params)
        names = await self.callsigns(crew_id, [r.get("from_session") for r in rows] + [r["to_session"] for r in rows])
        return [
            {
                **views.baton_row_view(r),
                "from_callsign": names.get(r.get("from_session") or ""),
                "to_callsign": names.get(r["to_session"]),
            }
            for r in rows
        ]

    # -- brief crew block data (§6 "Crew block in the brief") -----------------------------

    async def find_session(self, crew_id: str, user_id: str, agent_id: str, client_session_id: str) -> dict[str, Any] | None:
        """The crew session of a relay caller: keyed ``(crew, user, agent, client session id)`` (§2)."""
        return await self.db.fetchone(
            """SELECT * FROM crew_sessions WHERE crew_id = ? AND user_id = ? AND agent_id = ? AND session_id = ?
               ORDER BY CASE WHEN state = 'ended' THEN 1 ELSE 0 END, joined_at DESC LIMIT 1""",
            (crew_id, user_id, agent_id, client_session_id),
        )

    async def brief_data(
        self, crew_id: str, *, viewer_session_id: str | None = None, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Structured input for :func:`remembra.relay.handoff.render_crew_block` (read-only).

        ``viewer_session_id`` is the reading session (``cs_…``): baton offers
        recorded for it are shown as ``YOUR BATON`` with the adopt command (D33);
        other sessions only see that the zone is reserved. Its own claims are
        not listed under DO NOT TOUCH.
        """
        now = now or datetime.now(UTC)
        crew = await self.crew_row(crew_id)
        if crew is None:
            return None
        live = await self.live_count(crew_id)
        zones = {
            z["id"]: z
            for z in await self.db.fetchall("SELECT * FROM crew_zones WHERE crew_id = ? AND archived_at IS NULL", (crew_id,))
        }
        claims = await self.db.fetchall(
            "SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ('active', 'offered', 'reserved') ORDER BY created_at, id",
            (crew_id,),
        )
        holder_ids = sorted({c["holder_session_id"] for c in claims if c.get("holder_session_id")})
        sessions = {
            s["id"]: s
            for s in await self.db.fetchall(
                f"SELECT * FROM crew_sessions WHERE crew_id = ? AND id IN ({_marks(holder_ids) or 'NULL'})",
                (crew_id, *holder_ids),
            )
        }
        task_ids = sorted({c["task_id"] for c in claims if c.get("task_id")})
        tasks = {
            t["id"]: t
            for t in (
                await self.db.fetchall(
                    f"SELECT * FROM crew_tasks WHERE crew_id = ? AND id IN ({_marks(task_ids)})", (crew_id, *task_ids)
                )
                if task_ids
                else []
            )
        }
        offers: list[dict[str, Any]] = []
        if viewer_session_id:
            offers = await self.db.fetchall(
                "SELECT * FROM crew_baton_offers WHERE crew_id = ? AND to_session = ? AND used_at IS NULL",
                (crew_id, viewer_session_id),
            )
        offered_claims = {o["claim_id"] for o in offers}

        batons: dict[str, dict[str, Any]] = {}
        do_not_touch: list[dict[str, Any]] = []
        for claim in claims:
            holder = sessions.get(claim.get("holder_session_id") or "")
            task = tasks.get(claim.get("task_id") or "")
            zone = zones.get(claim.get("zone_id") or "")
            if claim["state"] == "reserved" and claim["id"] in offered_claims:
                key = claim.get("task_id") or claim["id"]
                if key not in batons:
                    batons[key] = await self._baton_entry(crew_id, claim, holder, task, zone)
                if zone is not None:
                    batons[key]["zones"].append(zone["slug"])
                continue
            if holder is not None and holder["id"] == viewer_session_id:
                continue  # the reader's own claims
            if claim["mode"] != "exclusive":
                continue
            do_not_touch.append(
                {
                    "zone": zone["slug"] if zone else None,
                    "resource": claim.get("resource"),
                    "path_glob": claim.get("path_glob") if not zone else None,
                    "state": claim["state"],
                    "holder_kind": claim["holder_kind"],
                    "holder": holder["callsign"] if holder else None,
                    "holder_state": holder["state"] if holder else None,
                    "holder_active_age_s": _age_s(holder.get("last_activity_at") if holder else None, now),
                    "reserve_reason": claim.get("reserve_reason"),
                    "task": views.task_ref(task["number"]) if task else None,
                    "task_title": task["title"] if task else None,
                }
            )
        frozen = [
            {"zone": z["slug"], "note": z.get("frozen_note")}
            for z in zones.values()
            if z.get("frozen_by") and not any(d.get("zone") == z["slug"] for d in do_not_touch)
        ]
        for_you: dict[str, int] = {}
        if viewer_session_id:
            rows = await self.db.fetchall(
                f"""SELECT kind, COUNT(*) AS n FROM crew_inbox_items WHERE crew_id = ? AND audience = 'session'
                     AND recipient = ? AND state IN ({_marks(schemas.LIVE_INBOX_STATES)}) GROUP BY kind ORDER BY kind""",
                (crew_id, viewer_session_id, *schemas.LIVE_INBOX_STATES),
            )
            for_you = {r["kind"]: int(r["n"]) for r in rows}
        sources = {z["source"] for z in zones.values() if not z.get("builtin")}
        decisions = await self.db.fetchall(
            "SELECT number, title FROM crew_decisions WHERE crew_id = ? AND state = 'in_force' ORDER BY number DESC LIMIT 5",
            (crew_id,),
        )
        return {
            "crew_id": crew_id,
            "project_id": crew["project_id"],
            "mode": views.crew_mode(live),
            "live": live,
            "as_of": now_iso(now),
            "viewer_session_id": viewer_session_id,
            "batons": list(batons.values()),
            "do_not_touch": do_not_touch,
            "frozen": frozen,
            "for_you": for_you,
            "temporary_zones": "suggested" in sources and "repo" not in sources,
            "decisions": [{"ref": f"D-{d['number']}", "title": d.get("title") or ""} for d in reversed(decisions)],
        }

    async def _baton_entry(
        self,
        crew_id: str,
        claim: Mapping[str, Any],
        holder: Mapping[str, Any] | None,
        task: Mapping[str, Any] | None,
        zone: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        if holder is not None:
            ckp = await self.db.fetchone(
                "SELECT facts FROM crew_checkpoints WHERE crew_id = ? AND session_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (crew_id, holder["id"]),
            )
            facts = _loads(ckp["facts"], {}) if ckp else {}
        report: dict[str, Any] | None = None
        if task is not None:
            report = await self.db.fetchone(
                "SELECT * FROM crew_reports WHERE crew_id = ? AND task_id = ? AND is_current = 1", (crew_id, task["id"])
            )
        sections = _loads(report["sections"], {}) if report else {}
        counts = facts_counts(facts)
        if not counts["failing"] and report:
            counts["failing"] = facts_counts({"tests": _loads(report.get("tests"), [])})["failing"]
        stopped = None
        if holder is not None:
            stopped = holder.get("ended_at") or holder.get("last_activity_at") or holder.get("last_seen_at")
        return {
            "task": views.task_ref(task["number"]) if task else None,
            "task_title": task["title"] if task else None,
            "claim_id": claim["id"],
            "zones": [],
            "from": holder["callsign"] if holder else None,
            "from_state": holder["state"] if holder else None,
            "stopped_at": stopped,
            "error": holder.get("state_reason") if holder else None,
            "source": holder.get("limit_source") if holder else None,
            "reserve_reason": claim.get("reserve_reason"),
            "baton_ref": claim.get("baton_ref") or (report.get("baton_ref") if report else None),
            "dirty": counts["dirty"],
            "unpushed": counts["unpushed"],
            "failing": counts["failing"],
            "next": _section_text(sections, "next"),
        }

    # -- trail additions (§6 "Existing relay") ----------------------------------------------

    async def trail_items(
        self,
        crew_ids: Sequence[str],
        *,
        agent_id: str | None = None,
        before: tuple[datetime, str | None] | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Crew checkpoints (not yet promoted to memory), reports and batons as trail entries, newest first.

        Returns ``(items, total)`` where ``items`` holds at most ``limit`` entries
        per source (the caller merges and pages) and ``total`` counts every
        matching entry after the cursor.
        """
        if not crew_ids:
            return [], 0
        crews = {
            c["id"]: c
            for c in await self.db.fetchall(f"SELECT id, project_id FROM crews WHERE id IN ({_marks(crew_ids)})", tuple(crew_ids))
        }
        items: list[dict[str, Any]] = []
        total = 0
        select = "SELECT x.*, s.agent_id AS s_agent, s.callsign AS s_callsign"
        sources = (
            (
                "crew_checkpoint",
                f"{select} FROM crew_checkpoints x LEFT JOIN crew_sessions s ON s.id = x.session_id"
                " WHERE x.crew_id IN ({crews}) AND x.memory_id IS NULL",
            ),
            (
                "crew_report",
                f"{select} FROM crew_reports x LEFT JOIN crew_sessions s ON s.id = x.session_id WHERE x.crew_id IN ({{crews}})",
            ),
            (
                "crew_baton",
                f"{select} FROM crew_batons x LEFT JOIN crew_sessions s ON s.id = x.to_session WHERE x.crew_id IN ({{crews}})",
            ),
        )
        for kind, sql in sources:
            where = sql.format(crews=_marks(crew_ids))
            params: list[Any] = list(crew_ids)
            if agent_id:
                where += " AND s.agent_id = ?"
                params.append(agent_id)
            if before is not None:
                at = now_iso(before[0].astimezone(UTC) if before[0].tzinfo else before[0].replace(tzinfo=UTC))
                if before[1]:
                    where += " AND (x.created_at < ? OR (x.created_at = ? AND x.id < ?))"
                    params.extend([at, at, before[1]])
                else:
                    where += " AND x.created_at < ?"
                    params.append(at)
            count = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM ({where})", params)
            total += int(count["n"]) if count else 0
            rows = await self.db.fetchall(where + " ORDER BY x.created_at DESC, x.id DESC LIMIT ?", [*params, limit])
            if not rows:
                continue
            tasks = await self._task_titles([r.get("task_id") for r in rows])
            others = await self._callsigns_any([r.get("from_session") for r in rows]) if kind == "crew_baton" else {}
            for r in rows:
                items.append(_trail_item(kind, r, crews.get(r["crew_id"], {}), tasks, others))
        return items, total

    async def _task_titles(self, task_ids: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        ids = sorted({t for t in task_ids if t})
        if not ids:
            return {}
        rows = await self.db.fetchall(f"SELECT id, number, title FROM crew_tasks WHERE id IN ({_marks(ids)})", tuple(ids))
        return {r["id"]: r for r in rows}

    async def _callsigns_any(self, session_ids: Iterable[str | None]) -> dict[str, str]:
        ids = sorted({s for s in session_ids if s})
        if not ids:
            return {}
        rows = await self.db.fetchall(f"SELECT id, callsign FROM crew_sessions WHERE id IN ({_marks(ids)})", tuple(ids))
        return {r["id"]: r["callsign"] for r in rows}


def _age_s(value: Any, now: datetime) -> int | None:
    at = parse_ts(value)
    if at is None:
        return None
    return max(0, int((now - at).total_seconds()))


def _trail_item(
    kind: str,
    row: Mapping[str, Any],
    crew: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
    others: Mapping[str, str],
) -> dict[str, Any]:
    task = tasks.get(row.get("task_id") or "")
    ref = views.task_ref(task["number"]) if task else None
    who = row.get("s_callsign") or "a session"
    crew_meta: dict[str, Any] = {
        "crew_id": row["crew_id"],
        "crew_session_id": row.get("session_id") or row.get("to_session"),
        "callsign": row.get("s_callsign"),
        "task_id": row.get("task_id"),
        "task_ref": ref,
        "task_title": task["title"] if task else None,
    }
    failing = 0
    open_items = 0
    if kind == "crew_checkpoint":
        facts = _loads(row.get("facts"), {})
        counts = facts_counts(facts)
        failing = len(counts["failing"])
        headline = f"checkpoint ({row['trigger']}) by {who}" + (f" on {ref}" if ref else "")
        crew_meta.update({"checkpoint": views.checkpoint_view(row)})
        content = row.get("headline") or ""
    elif kind == "crew_report":
        view = views.report_view(row)
        sections = _loads(row.get("sections"), {})
        failing = len(sections.get("failing") or []) if isinstance(sections.get("failing"), list) else 0
        open_items = len(sections.get("not_done") or []) if isinstance(sections.get("not_done"), list) else 0
        headline = (
            f"{row['kind']} report for {ref or 'a task'} by {who}"
            + (f": {view['verdict']}" if view["verdict"] else "")
            + (" (current)" if view["is_current"] else " (superseded)")
        )
        crew_meta.update({"report": view, "sections": sections})
        content = "\n".join(
            f"{name}: " + "; ".join(str(v) for v in value)
            for name, value in sections.items()
            if isinstance(value, list) and value
        )
    else:
        frm = others.get(row.get("from_session") or "")
        headline = f"baton {row['kind']}: {ref or 'claims'} " + (f"{frm} → {who}" if frm else f"to {who}")
        crew_meta.update({"baton": views.baton_row_view(row), "from_callsign": frm})
        content = row.get("brief_text") or ""
    return {
        "id": row["id"],
        "project_id": crew.get("project_id"),
        "memory_type": kind,
        "source": "crew",
        "agent_id": row.get("s_agent"),
        # never the client session id: it keys the relay close of that session (§11.2); crew.crew_session_id is enough
        "session_id": None,
        "created_at": row["created_at"],
        "branch": None,
        "head_commit": None,
        "headline": headline,
        "failing": failing,
        "open": open_items,
        "detail": {"structured": False, "content": str(content)[:4000]},
        "crew": crew_meta,
    }
