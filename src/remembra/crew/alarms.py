"""Needs-you safety alarms (spec §5.8, §9.9, §10.3): the items only a person can act on.

Server-generated, ``audience=project``, sorted above agent-originated items
(``schemas.SAFETY_INBOX_KINDS``). Titles are server templates (callsigns, slugs,
ids, counts): never agent text.

From client events (crewd's ``POST /crews/{id}/events``, called inside the ingest
transaction by :func:`remembra.crew.events.ingest_client_events`):

* ``guard.tamper_blocked`` → ``tamper_blocked`` (the same item the server guard raises);
* ``githook.missing`` with ``state = missing`` → ``githook_missing``;
* ``guard.blocked`` denies → ``false_deny_alarm`` at ≥5 blocks for one session in
  10 min (§10.3 "false-deny storm"; primary action: bypass code or observe mode).
  The server guard path (MCP) counts through :func:`note_guard_block` too.

From the reaper (every 30 s, :func:`sweep`):

* ``stuck_agent``: a hooked session busy (active) without a checkpoint for two
  checkpoint periods past due ("checkpoint missed ×2", §9.11). It sets the
  ``stuck`` health flag with ``session.stuck{signal, stuck}``; the first miss emits
  ``checkpoint.missed``. A checkpoint, or the session going idle, clears it.
  MCP-only lanes are never stuck from silence alone (§10.1): they get the nudge only.
* ``zone_contested``: a zone held exclusively while another session has waited for
  it (a queued claim, or guard denies naming it) for more than 5 min. Resolved
  once nobody has been blocked from it for 5 min and no claim is queued.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

import aiosqlite

from remembra.crew import schemas as S
from remembra.crew.events import Actor, EventTx, format_ts, parse_ts
from remembra.crew.zones import fetchall, fetchone, raise_inbox_item, resolve_inbox_items

FALSE_DENY_WINDOW_S: Final = 600
FALSE_DENY_THRESHOLD: Final = 5
CONTESTED_AFTER_S: Final = 300
CHECKPOINT_MISSED_FACTOR: Final = 2  # checkpoint.missed at 2x the interval since the last checkpoint (§5.5)
STUCK_FACTOR: Final = 4  # "checkpoint missed ×2": stuck at 4x

# guard denies per (crew, session): the server coalesces repeated denies of one zone into a single
# event (5 min), so the storm counter keeps every deny it saw here, with the event log as a floor
# after a restart (one process; a multi-worker deployment counts per worker, still a lower bound).
_blocks: dict[tuple[str, str], deque[float]] = {}
_blocks_lock = threading.Lock()


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _pts(value: Any) -> datetime | None:
    try:
        return parse_ts(str(value)) if value else None
    except ValueError:
        return None


async def on_client_event(
    tx: EventTx, crew_id: str, actor: Actor, etype: str, payload: Mapping[str, Any], *, now: datetime
) -> None:
    """Called for every accepted or coalesced client event, in the ingest transaction."""
    sid = actor.id
    name = actor.callsign or sid
    if etype == "guard.tamper_blocked":
        kind = str(payload.get("kind") or "crew_policy_write")
        await raise_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="tamper_blocked",
            title=f"{name} tried to switch off crew protection ({kind}, {payload.get('surface')})",
            dedupe_key=f"tamper:{sid}:{kind}",
            ref_type="session",
            ref_id=sid,
            priority=1,
            primary_action="review",
            actor=Actor.system(),
        )
    elif etype == "githook.missing" and payload.get("state") == "missing":
        hook = str(payload.get("hook") or "pre-commit")
        await raise_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="githook_missing",
            title=f"commit gate missing for {name} ({hook})",
            dedupe_key=f"githook:{sid}:{hook}",
            ref_type="session",
            ref_id=sid,
            priority=1,
            primary_action="review",
            actor=Actor.system(),
        )
    elif etype == "guard.blocked" and payload.get("decision") == "deny":
        await note_guard_block(tx, crew_id, sid, name, now=now, count=int(payload.get("coalesced") or 1))


async def note_guard_block(tx: EventTx, crew_id: str, session_id: str, callsign: str, *, now: datetime, count: int = 1) -> bool:
    """Count one guard deny for the session; raise ``false_deny_alarm`` at the §10.3 threshold. True when raised."""
    stamp = _ts(now)
    with _blocks_lock:
        window = _blocks.setdefault((crew_id, session_id), deque())
        for _ in range(max(1, min(count, 50))):
            window.append(stamp)
        while window and window[0] < stamp - FALSE_DENY_WINDOW_S:
            window.popleft()
        seen = len(window)
    logged = await _logged_blocks(tx.conn, crew_id, session_id, now)
    total = max(seen, logged)
    if total < FALSE_DENY_THRESHOLD:
        return False
    await raise_inbox_item(
        tx,
        crew_id,
        audience="project",
        kind="false_deny_alarm",
        title=f"{callsign} was blocked {total} times in 10 min: issue a bypass code or switch to observe if these are false",
        dedupe_key=f"false_deny:{session_id}",
        ref_type="session",
        ref_id=session_id,
        priority=1,
        primary_action="bypass",
        actor=Actor.system(),
    )
    return True


async def _logged_blocks(conn: aiosqlite.Connection, crew_id: str, session_id: str, now: datetime) -> int:
    rows = await fetchall(
        conn,
        "SELECT payload FROM crew_events WHERE crew_id = ? AND session_id = ? AND type = 'guard.blocked' AND ts >= ?",
        (crew_id, session_id, format_ts(now - timedelta(seconds=FALSE_DENY_WINDOW_S))),
    )
    total = 0
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        if p.get("decision") == "deny":
            total += int(p.get("coalesced") or 1)
    return total


# ---------------------------------------------------------------------------
# Reaper sweep: stuck agents and contested zones
# ---------------------------------------------------------------------------


async def sweep(log_: Any, now: datetime, settings_for: Any) -> dict[str, int]:
    """One pass (the reaper calls it every 30 s). ``settings_for(crew_id)`` returns the crew settings."""
    out = {"checkpoint_missed": 0, "stuck": 0, "unstuck": 0, "contested": 0, "uncontested": 0}
    await _stuck(log_, now, settings_for, out)
    await _contested(log_, now, out)
    return out


def _stuck_facts(row: Mapping[str, Any], now: datetime, interval: int) -> tuple[float, datetime, bool, bool]:
    due = _pts(row.get("next_checkpoint_due_at")) or ((_pts(row.get("joined_at")) or now) + timedelta(seconds=interval))
    last = due - timedelta(seconds=interval)  # the last checkpoint (or the join)
    overdue = (now - last).total_seconds()
    busy = row["state"] == "active"
    stuck_now = bool(row.get("host_id")) and busy and overdue >= STUCK_FACTOR * interval
    return overdue, last, busy, stuck_now


async def _stuck(log_: Any, now: datetime, settings_for: Any, out: dict[str, int]) -> None:
    candidates: list[tuple[dict[str, Any], int]] = []
    # An unchanged alarm must not enter the writer queue once per session.
    # Keep this read snapshot short, then re-read every mutation below.
    async with log_.db.read_snapshot() as conn:
        rows = await fetchall(conn, "SELECT * FROM crew_sessions WHERE state = 'active' OR stuck = 1")
        for row in rows:
            settings = await settings_for(str(row["crew_id"]))
            interval = int(((settings or {}).get("checkpoint") or {}).get("interval_s") or 600)
            overdue, last, busy, stuck_now = _stuck_facts(row, now, interval)
            missed = busy and overdue >= CHECKPOINT_MISSED_FACTOR * interval and not await _missed_since(conn, row, last)
            if not missed and stuck_now == bool(row.get("stuck")):
                continue  # nothing to change: no transaction
            candidates.append((row, interval))
    for row, interval in candidates:
        async with log_.transaction() as tx:
            fresh = await fetchone(tx.conn, "SELECT * FROM crew_sessions WHERE id = ?", (row["id"],))
            if fresh is None:
                continue
            overdue, last, busy, stuck_now = _stuck_facts(fresh, now, interval)
            hooked = bool(fresh.get("host_id"))
            actor = Actor.system()
            if busy and overdue >= CHECKPOINT_MISSED_FACTOR * interval and not await _missed_since(tx.conn, fresh, last):
                await tx.emit(
                    crew_id=fresh["crew_id"],
                    type="checkpoint.missed",
                    actor=actor,
                    payload={"overdue_s": int(overdue), "nudge": not hooked},
                    summary=f"{fresh['callsign']} has no checkpoint for {int(overdue // 60)} min",
                    refs={"session_id": fresh["id"]},
                    now=now,
                )
                out["checkpoint_missed"] += 1
            if stuck_now and not fresh.get("stuck"):
                await tx.conn.execute("UPDATE crew_sessions SET stuck = 1 WHERE id = ? AND stuck = 0", (fresh["id"],))
                await tx.emit(
                    crew_id=fresh["crew_id"],
                    type="session.stuck",
                    actor=actor,
                    payload={"signal": "checkpoint_missed", "stuck": True},
                    summary=f"{fresh['callsign']} looks stuck: busy with no checkpoint for {int(overdue // 60)} min",
                    refs={"session_id": fresh["id"]},
                    now=now,
                )
                await raise_inbox_item(
                    tx,
                    str(fresh["crew_id"]),
                    audience="project",
                    kind="stuck_agent",
                    title=f"{fresh['callsign']} looks stuck: busy with no checkpoint for {int(overdue // 60)} min",
                    dedupe_key=f"stuck:{fresh['id']}",
                    ref_type="session",
                    ref_id=str(fresh["id"]),
                    priority=1,
                    primary_action="checkpoint",
                    actor=actor,
                )
                out["stuck"] += 1
            elif fresh.get("stuck") and not stuck_now:
                await tx.conn.execute("UPDATE crew_sessions SET stuck = 0 WHERE id = ? AND stuck = 1", (fresh["id"],))
                await tx.emit(
                    crew_id=fresh["crew_id"],
                    type="session.stuck",
                    actor=actor,
                    payload={"signal": "checkpoint" if busy else str(fresh["state"]), "stuck": False},
                    summary=f"{fresh['callsign']} is no longer stuck",
                    refs={"session_id": fresh["id"]},
                    now=now,
                )
                await resolve_inbox_items(
                    tx, str(fresh["crew_id"]), dedupe_keys=[f"stuck:{fresh['id']}"], resolved_by="system", actor=actor
                )
                out["unstuck"] += 1


async def _missed_since(conn: aiosqlite.Connection, session: Mapping[str, Any], since: datetime) -> bool:
    row = await fetchone(
        conn,
        "SELECT 1 FROM crew_events WHERE crew_id = ? AND session_id = ? AND type = 'checkpoint.missed' AND ts >= ? LIMIT 1",
        (session["crew_id"], session["id"], format_ts(since)),
    )
    return row is not None


async def _contested(log_: Any, now: datetime, out: dict[str, int]) -> None:
    conn = log_.db.conn
    since = format_ts(now - timedelta(seconds=2 * CONTESTED_AFTER_S))
    held = await fetchall(
        conn,
        "SELECT c.*, z.slug AS zone_slug FROM crew_claims c JOIN crew_zones z ON z.id = c.zone_id"
        " WHERE c.mode = 'exclusive' AND c.state IN ('active','offered','reserved') AND c.zone_id IS NOT NULL",
    )
    live_keys: set[tuple[str, str]] = set()
    recent: dict[str, list[tuple[datetime | None, Any, dict[str, Any]]]] = {}
    for claim in held:
        crew_id, zone_id, slug = str(claim["crew_id"]), str(claim["zone_id"]), str(claim["zone_slug"])
        holder = claim.get("holder_session_id")
        waited: list[datetime] = []
        for q in await fetchall(
            conn,
            "SELECT created_at, holder_session_id FROM crew_claims WHERE crew_id = ? AND zone_id = ? AND state = 'queued'",
            (crew_id, zone_id),
        ):
            if q.get("holder_session_id") != holder and (t := _pts(q.get("created_at"))) is not None:
                waited.append(t)
        if crew_id not in recent:
            recent[crew_id] = [
                (_pts(b["ts"]), b["session_id"], json.loads(b["payload"] or "{}"))
                for b in await fetchall(
                    conn,
                    "SELECT ts, session_id, payload FROM crew_events WHERE crew_id = ? AND type = 'guard.blocked' AND ts >= ?"
                    " ORDER BY seq",
                    (crew_id, since),
                )
            ]
        blocks = [
            (ts, sid) for ts, sid, p in recent[crew_id] if p.get("zone") == slug and p.get("decision") == "deny" and sid != holder
        ]
        blocked_for = 0.0
        if blocks:
            first, last = blocks[0][0], blocks[-1][0]
            if first and last and (now - last).total_seconds() <= CONTESTED_AFTER_S:
                blocked_for = (last - first).total_seconds()
        queued_for = max(((now - t).total_seconds() for t in waited), default=0.0)
        contested = max(blocked_for, queued_for) > CONTESTED_AFTER_S
        key = f"contested:{zone_id}"
        if contested:
            live_keys.add((crew_id, key))
            waiters = len({s for _, s in blocks} | ({"q"} if waited else set()))
            async with log_.transaction() as tx:
                existing = await fetchone(
                    tx.conn,
                    "SELECT id FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
                    (crew_id, key),
                )
                if existing is None:
                    await raise_inbox_item(
                        tx,
                        crew_id,
                        audience="project",
                        kind="zone_contested",
                        title=f"zone {slug} contested for {int(max(blocked_for, queued_for) // 60)} min ({waiters} waiting)",
                        dedupe_key=key,
                        ref_type="zone",
                        ref_id=zone_id,
                        priority=1,
                        primary_action="hand_baton",
                        actor=Actor.system(),
                    )
                    out["contested"] += 1
    # resolve contested items whose zone is no longer contested
    for item in await fetchall(
        conn,
        "SELECT crew_id, dedupe_key FROM crew_inbox_items WHERE kind = 'zone_contested' AND state IN ('open','seen','claimed')",
    ):
        if (str(item["crew_id"]), str(item["dedupe_key"])) in live_keys:
            continue
        async with log_.transaction() as tx:
            await resolve_inbox_items(
                tx, str(item["crew_id"]), dedupe_keys=[str(item["dedupe_key"])], resolved_by="system", actor=Actor.system()
            )
            out["uncontested"] += 1


assert "stuck_agent" in S.SAFETY_INBOX_KINDS and "false_deny_alarm" in S.SAFETY_INBOX_KINDS
