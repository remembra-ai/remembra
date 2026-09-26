"""Crew side of a relay close-out (WP-8, spec §6 "Existing relay": ``POST /session/close``).

A relay close (``POST /session/close``, the ``close_session`` MCP tool, or
``remembra-relay close`` from the SessionEnd hook) stores ONE handoff in the
main database. When the project has a crew, :func:`on_relay_close` then
records the crew side in one ``crew.db`` transaction:

* ``handoff.created`` (idempotent per handoff id, so a repeated identical close
  emits nothing new);
* if the caller has a live crew session (keyed ``(crew, user, agent, client
  session id)``, §2), the session **leaves**:

  - each exclusive claim it holds is **reserved** (``ended_dirty``) when the
    close reports uncommitted files, the claim's task is not done, or the end
    reason is ``clear`` (§8.2 SessionEnd step 2); otherwise it is **released**.
    Shared/watch claims and claims still waiting (requested, queued) are
    released. Task-linked reservations never expire (D5); others expire after
    ``reserve_ttl_s``;
  - tasks it owns that are unfinished (claimed, in_progress, blocked) move to
    ``stalled`` with ``status_before_stall`` (§5.4);
  - a stalled task that reached ``in_progress`` without a current report gets
    a ``partial`` report built from the handoff sections, so the "always a
    report" invariant holds (§5.6);
  - ``session.left`` with the released and reserved claim ids, and
    ``crew.mode_changed`` when the crew drops from multi to solo.

Nothing here creates a crew or a session. A close by an agent that never
joined only records ``handoff.created`` (actor ``system``, attributed to the
agent). Every event goes through the WP-2 event log (validated, hash-chained,
published after COMMIT).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog

from remembra.crew import schemas, views
from remembra.crew.events import Actor, CrewEventLog, EventTx
from remembra.crew.settings import load_settings
from remembra.crew.store import dumps, new_id, now_iso

log = structlog.get_logger(__name__)

UNFINISHED_TASK_STATUSES: Final = ("claimed", "in_progress", "blocked")
FINISHED_TASK_STATUSES: Final = ("done", "cancelled")
WAITING_CLAIM_STATES: Final = ("requested", "queued")
HELD_CLAIM_STATES: Final = ("active", "offered")
_AGENT_RE: Final = re.compile(schemas.AGENT_ID_PATTERN)
_REASON_RE: Final = re.compile(r"[^A-Za-z0-9_.:-]+")


def crew_facts_source(relay_facts_source: Any) -> str:
    """Map a relay handoff's ``facts_source`` to the crew vocabulary (``schemas.FACTS_SOURCES``)."""
    value = str(relay_facts_source or "")
    if value == "relay-cli" or value.startswith("relay-cli:"):
        return "relay-cli"
    if value in ("server-inferred", "server-verified"):
        return value
    return "agent-declared"


def reason_slug(end_reason: str | None) -> str:
    """A short machine reason for ``session.left`` / ``crew_sessions.end_reason`` (free text never reaches a summary)."""
    text = _REASON_RE.sub("_", (end_reason or "").strip()).strip("_")[:64]
    return text or "closed"


def _report_sections(sections: Mapping[str, Any]) -> dict[str, list[str]]:
    def items(key: str) -> list[str]:
        value = sections.get(key)
        if isinstance(value, str):
            value = [value]
        return [str(v)[:500] for v in (value or []) if isinstance(v, str) and v.strip()][:50]

    return {
        "done": items("done"),
        "not_done": items("not_done"),
        "failing": items("failing"),
        "next": items("next"),
        "follow_ups": [],
    }


async def on_relay_close(
    events: CrewEventLog,
    *,
    user_id: str,
    project_id: str,
    agent_id: str,
    client_session_id: str,
    agent_verified: bool,
    handoff_id: str,
    end_reason: str | None,
    facts: Mapping[str, Any],
    sections: Mapping[str, Any],
    facts_source: str,
    leave: bool = True,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Record the crew side of a relay close. Returns a summary, or None when the project has no crew.

    ``leave=False`` records only ``handoff.created`` (for server-written handoffs of a
    session that has not left, e.g. a stall handoff the crew outbox applies).
    """
    db = events.db
    crew = await _fetchone(db, "SELECT * FROM crews WHERE owner_user_id = ? AND project_id = ?", (user_id, project_id))
    if crew is None:
        return None
    crew_id = crew["id"]
    now = now or datetime.now(UTC)
    stamp = now_iso(now)
    source = crew_facts_source(facts_source)
    result: dict[str, Any] = {
        "crew_id": crew_id,
        "crew_session_id": None,
        "seqs": [],
        "claims_released": [],
        "claims_reserved": [],
        "tasks_stalled": [],
        "reports_created": [],
        "session_left": False,
    }
    async with events.transaction() as tx:
        session = await _fetchone(
            db,
            """SELECT * FROM crew_sessions WHERE crew_id = ? AND user_id = ? AND agent_id = ? AND session_id = ?
               ORDER BY CASE WHEN state = 'ended' THEN 1 ELSE 0 END, joined_at DESC LIMIT 1""",
            (crew_id, user_id, agent_id, client_session_id),
        )
        if session is not None:
            actor = Actor.session(
                session["id"],
                callsign=session["callsign"],
                agent_id=session["agent_id"],
                user_id=session["user_id"],
                verified=bool(session["agent_verified"]),
            )
            who = session["callsign"]
            result["crew_session_id"] = session["id"]
        else:
            actor = Actor(
                kind="system",
                id="relay",
                agent_id=agent_id if _AGENT_RE.fullmatch(agent_id) else None,
                user_id=user_id,
                verified=bool(agent_verified),
            )
            who = actor.agent_id or "an agent"
        task_id = session.get("current_task_id") if session else None
        if not schemas.is_id("task", task_id):
            task_id = None
        emitted = await tx.emit(
            crew_id=crew_id,
            type="handoff.created",
            actor=actor,
            payload={
                "handoff_id": str(handoff_id)[:80],
                "end_reason": (end_reason or "closed")[:80],
                "facts_source": source,
                "task_id": task_id,
            },
            summary=f"{who} closed its session; handoff recorded",
            refs={"session_id": session["id"] if session else None, "task_id": task_id},
            idem_key=f"handoff:{handoff_id}",
            now=now,
        )
        if not emitted.replayed:
            result["seqs"].append(emitted.seq)
        if leave and session is not None and session["state"] != "ended":
            await _leave(tx, crew, session, actor, end_reason, facts, sections, source, handoff_id, now, stamp, result)
    log.info(
        "crew_relay_close",
        crew_id=crew_id,
        crew_session_id=result["crew_session_id"],
        released=len(result["claims_released"]),
        reserved=len(result["claims_reserved"]),
        stalled=len(result["tasks_stalled"]),
    )
    return result


async def _leave(
    tx: EventTx,
    crew: Mapping[str, Any],
    session: Mapping[str, Any],
    actor: Actor,
    end_reason: str | None,
    facts: Mapping[str, Any],
    sections: Mapping[str, Any],
    facts_source: str,
    handoff_id: str,
    now: datetime,
    stamp: str,
    result: dict[str, Any],
) -> None:
    conn = tx.conn
    crew_id = crew["id"]
    sid = session["id"]
    callsign = session["callsign"]
    settings = load_settings(crew.get("settings"))
    reason = reason_slug(end_reason)
    live_before = await _live_count(conn, crew_id)

    tasks = {
        t["id"]: t
        for t in await _fetchall(
            conn,
            "SELECT * FROM crew_tasks WHERE crew_id = ? AND (owner_session_id = ? OR id IN "
            "(SELECT task_id FROM crew_claims WHERE crew_id = ? AND holder_session_id = ? AND task_id IS NOT NULL))",
            (crew_id, sid, crew_id, sid),
        )
    }
    dirty = bool(facts.get("uncommitted_files")) or reason == "clear"

    # 1) claims
    claims = await _fetchall(
        conn,
        f"SELECT * FROM crew_claims WHERE crew_id = ? AND holder_session_id = ?"
        f" AND state IN ({_marks((*WAITING_CLAIM_STATES, *HELD_CLAIM_STATES))}) ORDER BY created_at, id",
        (crew_id, sid, *WAITING_CLAIM_STATES, *HELD_CLAIM_STATES),
    )
    reserve_ttl = timedelta(seconds=int(settings.get("reserve_ttl_s", 86400)))
    for claim in claims:
        task = tasks.get(claim.get("task_id") or "")
        unfinished_task = task is not None and task["status"] not in FINISHED_TASK_STATUSES
        reserve = claim["state"] in HELD_CLAIM_STATES and claim["mode"] == "exclusive" and (dirty or unfinished_task)
        if reserve:
            expires = None if claim.get("task_id") else now_iso(now + reserve_ttl)
            await conn.execute(
                """UPDATE crew_claims SET state = 'reserved', reserve_reason = 'ended_dirty', reserved_for = NULL,
                       reserve_expires_at = ?, offered_to = NULL, offer_expires_at = NULL, queue_pos = NULL,
                       version = version + 1, updated_at = ?
                   WHERE id = ? AND crew_id = ?""",
                (expires, stamp, claim["id"], crew_id),
            )
            row = await _fetchone(conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
            assert row is not None
            ev = await tx.emit(
                crew_id=crew_id,
                type="claim.reserved",
                actor=actor,
                payload={"claim": views.claim_view(row, now), "reason": "ended_dirty"},
                summary=f"claim {claim['id']} reserved: {callsign} left with unfinished work",
                refs={
                    "claim_id": claim["id"],
                    "zone_id": claim.get("zone_id"),
                    "task_id": claim.get("task_id"),
                    "session_id": sid,
                },
                now=now,
            )
            result["claims_reserved"].append(claim["id"])
        else:
            await conn.execute(
                """UPDATE crew_claims SET state = 'released', ended_at = ?, end_reason = 'session_ended',
                       offered_to = NULL, offer_expires_at = NULL, queue_pos = NULL, version = version + 1, updated_at = ?
                   WHERE id = ? AND crew_id = ?""",
                (stamp, stamp, claim["id"], crew_id),
            )
            row = await _fetchone(conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
            assert row is not None
            ev = await tx.emit(
                crew_id=crew_id,
                type="claim.released",
                actor=actor,
                payload={"claim": views.claim_view(row, now), "baton": False},
                summary=f"claim {claim['id']} released: {callsign} left",
                refs={
                    "claim_id": claim["id"],
                    "zone_id": claim.get("zone_id"),
                    "task_id": claim.get("task_id"),
                    "session_id": sid,
                },
                now=now,
            )
            result["claims_released"].append(claim["id"])
        result["seqs"].append(ev.seq)

    # 2) tasks the session owns that are not finished → stalled (+ report invariant)
    for task in tasks.values():
        if task.get("owner_session_id") != sid or task["status"] not in UNFINISHED_TASK_STATUSES:
            continue
        await conn.execute(
            """UPDATE crew_tasks SET status = 'stalled', status_before_stall = ?, stalled_at = ?,
                   version = version + 1, updated_at = ? WHERE id = ? AND crew_id = ?""",
            (task["status"], stamp, stamp, task["id"], crew_id),
        )
        report_id = None
        if task.get("started_at") or task["status"] in ("in_progress", "blocked"):
            report_id = await _ensure_report(conn, crew_id, task, sid, sections, facts_source, handoff_id, stamp)
        row = await _fetchone(conn, "SELECT * FROM crew_tasks WHERE id = ?", (task["id"],))
        assert row is not None
        deps = [
            r["depends_on_id"]
            for r in await _fetchall(
                conn,
                "SELECT depends_on_id FROM crew_task_deps WHERE crew_id = ? AND task_id = ? ORDER BY depends_on_id",
                (crew_id, task["id"]),
            )
        ]
        task_view = views.task_view(row, deps)
        ev = await tx.emit(
            crew_id=crew_id,
            type="task.stalled",
            actor=actor,
            payload={"task": task_view, "reason": "ended_unfinished"},
            summary=f"{views.task_ref(row['number'])} stalled: owner {callsign} left",
            refs={"task_id": task["id"], "session_id": sid},
            now=now,
        )
        result["seqs"].append(ev.seq)
        result["tasks_stalled"].append(task["id"])
        if report_id is not None:
            report = await _fetchone(conn, "SELECT * FROM crew_reports WHERE id = ?", (report_id,))
            assert report is not None
            ev = await tx.emit(
                crew_id=crew_id,
                type="report.submitted",
                actor=actor,
                payload={"report": views.report_view(report)},
                summary=f"partial report for {views.task_ref(row['number'])} from {callsign}'s close",
                refs={"task_id": task["id"], "report_id": report_id, "session_id": sid},
                now=now,
            )
            result["seqs"].append(ev.seq)
            result["reports_created"].append(report_id)

    # 3) the session leaves
    await conn.execute(
        "UPDATE crew_sessions SET state = 'ended', ended_at = ?, end_reason = ?, last_seen_at = ? WHERE id = ? AND crew_id = ?",
        (stamp, reason, stamp, sid, crew_id),
    )
    ev = await tx.emit(
        crew_id=crew_id,
        type="session.left",
        actor=actor,
        payload={
            "reason": reason,
            "claims_released": result["claims_released"][:50],
            "claims_reserved": result["claims_reserved"][:50],
        },
        summary=f"{callsign} left the crew",
        refs={"session_id": sid},
        idem_key=f"session.left:{sid}",
        now=now,
    )
    if not ev.replayed:
        result["seqs"].append(ev.seq)
    result["session_left"] = True

    live_after = await _live_count(conn, crew_id)
    if live_before >= 2 and live_after <= 1:
        ev = await tx.emit(
            crew_id=crew_id,
            type="crew.mode_changed",
            actor=actor,
            payload={"from": "multi", "to": "solo", "live_sessions": live_after},
            summary=f"crew is solo again ({live_after} live)",
            now=now,
        )
        result["seqs"].append(ev.seq)


async def _ensure_report(
    conn: Any,
    crew_id: str,
    task: Mapping[str, Any],
    session_id: str,
    sections: Mapping[str, Any],
    facts_source: str,
    handoff_id: str,
    stamp: str,
) -> str | None:
    """A ``partial`` report from the handoff when the task has no current report (§5.6 invariant). Returns its id."""
    current = await _fetchone(
        conn, "SELECT id FROM crew_reports WHERE crew_id = ? AND task_id = ? AND is_current = 1", (crew_id, task["id"])
    )
    if current is not None:
        return None
    body = _report_sections(sections)
    facts_hash = hashlib.sha256(schemas.canonical_json({"handoff_id": handoff_id, "sections": body})).hexdigest()
    existing = await _fetchone(
        conn,
        "SELECT id FROM crew_reports WHERE task_id = ? AND session_id = ? AND facts_hash = ?",
        (task["id"], session_id, facts_hash),
    )
    if existing is not None:  # same close replayed after a later report superseded it: never duplicate
        return None
    report_id = new_id("report")
    await conn.execute(
        """INSERT INTO crew_reports (id, crew_id, task_id, session_id, kind, verdict, criteria, sections,
               facts_source, facts_hash, review_state, is_current, handoff_id, created_at)
           VALUES (?, ?, ?, ?, 'partial', 'partial', '[]', ?, ?, ?, NULL, 1, ?, ?)""",
        (report_id, crew_id, task["id"], session_id, dumps(body), facts_source, facts_hash, str(handoff_id)[:80], stamp),
    )
    await conn.execute(
        "UPDATE crew_tasks SET current_report_id = ? WHERE id = ? AND crew_id = ?", (report_id, task["id"], crew_id)
    )
    return report_id


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


async def _live_count(conn: Any, crew_id: str) -> int:
    states = schemas.LIVE_PRESENCE_STATES
    row = await _fetchone(
        conn, f"SELECT COUNT(*) AS n FROM crew_sessions WHERE crew_id = ? AND state IN ({_marks(states)})", (crew_id, *states)
    )
    return int(row["n"]) if row else 0


async def _fetchone(db_or_conn: Any, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
    conn = getattr(db_or_conn, "conn", db_or_conn)
    cursor = await conn.execute(sql, tuple(params))
    try:
        row = await cursor.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cursor.description]
        return dict(zip(names, tuple(row), strict=True))
    finally:
        await cursor.close()


async def _fetchall(conn: Any, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    cursor = await conn.execute(sql, tuple(params))
    try:
        rows = await cursor.fetchall()
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, tuple(r), strict=True)) for r in rows]
    finally:
        await cursor.close()
