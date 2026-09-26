"""Crew footprints and collisions (WP-5, spec §5.3, D16, D19, D31).

A **footprint** is a file a session modified (``dirty → committed → landed``), with the claim
epoch it was written under and its attribution: ``certain`` when the session's own file tool
reported it, ``probable`` when it came from a git delta that crewd already filtered for other
local sessions' footprints and tool-call windows (§5.3). ``certain`` is never downgraded.

A **collision** is overlapping work, one row per ``(kind, subject, session_a, session_b)`` while
it is live. ``session_a`` is the writer, ``session_b`` the other party (another writer, or the
holder of the zone). Kinds and severities come from ``schemas.COLLISION_SEVERITY``:

* ``same_worktree_file`` (critical): the same file dirty in two live sessions of one worktree;
* ``foreign_checkout_write`` (critical): a write into another live session's worktree;
* ``exclusive_breach`` (high): a write into a zone another principal holds exclusively or has
  reserved. A ``probable`` breach goes to the dashboard and the holder but never triggers a
  Stop block or any agent-facing corrective text (the collision row carries the attribution);
* ``stale_epoch_write`` (high): written under an older claim epoch than the zone's current
  holder, or after the writer's own lease horizon (fenced);
* ``same_file`` (medium), ``same_zone_shared`` (low), ``merge_conflict_risk`` (medium, from
  the pre-push check) and ``unattributed_change`` (notice: a commit with no crew trailer that
  touches a held zone, probably Mani).

Delivery: ``collision.detected`` (a moment for high and critical), a crew inbox item, a session
queue item for both parties, and a Needs-you item for high and critical kinds. Collisions
resolve automatically when the overlap disappears (footprint landed or gone, claim ended),
when an agent resolves them, or when a human dismisses them.

Interface for WP-4 / WP-6 / WP-9: call :func:`record_footprints` in the heartbeat, checkpoint
or close transaction (the heartbeat does it through :func:`heartbeat_sink`, registered with
``crew.sessions.register_footprint_sink``); :func:`set_footprint_state` when commits land; and
:func:`record_unattributed_change` / :func:`record_merge_conflict_risk` from the git gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

import aiosqlite

from remembra.crew import schemas as S
from remembra.crew.claims import FENCE_MARGIN_S, is_fenced
from remembra.crew.events import Actor, EventTx
from remembra.crew.gatecore import normalize_rel
from remembra.crew.store import dumps, loads, new_id, now_iso
from remembra.crew.zones import (
    CrewOpError,
    CrewOps,
    Principal,
    clip,
    fetchall,
    fetchone,
    raise_inbox_item,
    resolve_inbox_items,
    utcnow,
    zone_index,
)

MATCH_ENDED_WITHIN: Final = timedelta(hours=24)
ESCALATED: Final = frozenset({"high", "critical"})
MAX_FOOTPRINTS_PER_CALL: Final = 500
MAX_WRITE_AGE_S: Final = 7 * 24 * 3600
SYMMETRIC: Final = frozenset({"same_file", "same_worktree_file"})


def collision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """``CollisionView`` (schemas)."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "severity": row["severity"],
        "subject": str(row["subject"])[:1024],
        "zone_id": row.get("zone_id"),
        "session_a": row.get("session_a"),
        "session_b": row.get("session_b"),
        "claim_id": row.get("claim_id"),
        "attribution": row.get("attribution"),
        "state": row["state"],
        "escalated": row["severity"] in ESCALATED,
        "resolution": row.get("resolution"),
    }


async def list_collisions(conn: aiosqlite.Connection, crew_id: str, state: str | None = None) -> list[dict[str, Any]]:
    if state is not None and state not in S.COLLISION_STATES and state != "live":
        raise CrewOpError(422, "invalid_state", f"state must be live or one of {', '.join(S.COLLISION_STATES)}")
    if state is None or state == "live":
        rows = await fetchall(
            conn,
            "SELECT * FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged') ORDER BY created_at",
            (crew_id,),
        )
    else:
        rows = await fetchall(
            conn,
            "SELECT * FROM crew_collisions WHERE crew_id = ? AND state = ? ORDER BY created_at DESC LIMIT 500",
            (crew_id, state),
        )
    return [collision_view(r) for r in rows]


async def _callsign(conn: aiosqlite.Connection, session_id: str | None) -> str:
    if not session_id:
        return "a human"
    row = await fetchone(conn, "SELECT callsign FROM crew_sessions WHERE id = ?", (session_id,))
    return str(row["callsign"]) if row else "another session"


async def open_collision(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    *,
    kind: str,
    subject: str,
    session_a: str | None,
    session_b: str | None,
    attribution: str | None,
    zone_id: str | None = None,
    claim_id: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    actor: Actor | None = None,
) -> dict[str, Any] | None:
    """Open a collision unless the same one is already live. Returns the new row (None when deduplicated)."""
    severity = S.COLLISION_SEVERITY[kind]
    pairs = [(session_a, session_b)]
    if kind in SYMMETRIC:
        pairs.append((session_b, session_a))  # A-vs-B on one file is the same overlap as B-vs-A
    existing = None
    for sa, sb in pairs:
        existing = existing or await fetchone(
            tx.conn,
            """SELECT id FROM crew_collisions WHERE crew_id = ? AND kind = ? AND subject = ? AND session_a IS ? AND session_b IS ?
                 AND state IN ('open','acknowledged')""",
            (crew_id, kind, subject, sa, sb),
        )
    if existing is not None:
        return None
    col_id = new_id("collision")
    await tx.conn.execute(
        """INSERT INTO crew_collisions (id, crew_id, kind, severity, subject, zone_id, session_a, session_b,
            claim_id, attribution,
               evidence, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (
            col_id,
            crew_id,
            kind,
            severity,
            subject,
            zone_id,
            session_a,
            session_b,
            claim_id,
            attribution,
            dumps(dict(evidence or {})),
            now_iso(),
        ),
    )
    row = await fetchone(tx.conn, "SELECT * FROM crew_collisions WHERE id = ?", (col_id,))
    assert row is not None
    who_a, who_b = await _callsign(tx.conn, session_a), await _callsign(tx.conn, session_b)
    res = await tx.emit(
        crew_id=crew_id,
        type="collision.detected",
        actor=actor or Actor.system(),
        payload={"collision": collision_view(row)},
        summary=f"collision {kind}: {who_a} and {who_b}",
        severity=severity,
        refs={"collision_id": col_id, "zone_id": zone_id, "claim_id": claim_id, "session_id": session_a},
    )
    await tx.conn.execute("UPDATE crew_collisions SET detected_seq = ? WHERE id = ?", (res.seq, col_id))
    title = f"Collision {kind} between {who_a} and {who_b}"
    await raise_inbox_item(
        tx,
        crew_id,
        audience="crew",
        kind="collision_open",
        title=title,
        dedupe_key=f"collision:{col_id}",
        ref_type="collision",
        ref_id=col_id,
    )
    for sid in {s for s in (session_a, session_b) if s}:
        await raise_inbox_item(
            tx,
            crew_id,
            audience="session",
            recipient=sid,
            kind="collision_notice",
            title=title,
            dedupe_key=f"collision:{col_id}:{sid}",
            ref_type="collision",
            ref_id=col_id,
        )
    if severity in ESCALATED:
        await raise_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="collision_escalated",
            title=title,
            dedupe_key=f"collision-escalated:{col_id}",
            ref_type="collision",
            ref_id=col_id,
            priority=1,
            primary_action="review",
        )
    return row


async def _close(
    tx: EventTx, crew_id: str, row: Mapping[str, Any], state: str, resolution: str, by: str, actor: Actor
) -> dict[str, Any]:
    await tx.conn.execute(
        "UPDATE crew_collisions SET state = ?, resolution = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
        (state, resolution, by, now_iso(), row["id"]),
    )
    fresh = await fetchone(tx.conn, "SELECT * FROM crew_collisions WHERE id = ?", (row["id"],))
    assert fresh is not None
    await tx.emit(
        crew_id=crew_id,
        type=f"collision.{state}",
        actor=actor,
        payload={"collision": collision_view(fresh)},
        summary=f"collision {row['kind']} {state}",
        refs={"collision_id": row["id"], "zone_id": row.get("zone_id")},
    )
    if state in ("resolved", "dismissed"):
        await resolve_inbox_items(tx, crew_id, ref_ids=[str(row["id"])], resolved_by=by, actor=actor)
    return fresh


# ---------------------------------------------------------------------------
# Footprints and detection
# ---------------------------------------------------------------------------


async def _session(conn: aiosqlite.Connection, session_id: str) -> dict[str, Any] | None:
    return await fetchone(conn, "SELECT * FROM crew_sessions WHERE id = ?", (session_id,))


def _recent(session: Mapping[str, Any]) -> bool:
    if session["state"] in S.LIVE_PRESENCE_STATES:
        return True
    ended = session.get("ended_at")
    if not ended:
        return bool(session["state"] == "lost")
    from remembra.crew.store import parse_iso

    return bool(parse_iso(str(ended)) >= utcnow() - MATCH_ENDED_WITHIN)


async def record_footprints(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    session: Mapping[str, Any],
    footprints: Sequence[Mapping[str, Any]],
    *,
    count_touch: bool = True,
) -> list[dict[str, Any]]:
    """Upsert a session's footprints and open the collisions they cause (inside the caller's transaction).

    Each footprint is ``{path, state, attribution, claim_epoch?, last_commit?, worktree_id?}``;
    ``path`` is repo-relative. ``worktree_id`` defaults to the session's (crewd sets it when the
    write landed in another checkout). Returns the collisions opened now. ``count_touch=False``
    when the caller already upserted the rows in this transaction (the heartbeat sink), so a
    heartbeat counts one touch per path, not two.
    """
    if session["crew_id"] != crew_id:
        raise CrewOpError(422, "cross_crew_reference", "The session is not part of this crew.")
    if len(footprints) > MAX_FOOTPRINTS_PER_CALL:
        raise CrewOpError(422, "too_many_footprints", f"At most {MAX_FOOTPRINTS_PER_CALL} footprints per call.")
    conn = tx.conn
    idx = await zone_index(conn, crew_id)
    now = now_iso()
    opened: list[dict[str, Any]] = []
    touched: list[str] = []
    for fp in footprints:
        path = normalize_rel(str(fp.get("path") or ""))
        state = fp.get("state") or "dirty"
        attribution = fp.get("attribution") or "certain"
        if not S.is_path_rel(path) or path == "." or state not in S.FOOTPRINT_STATES or attribution not in S.ATTRIBUTIONS:
            raise CrewOpError(422, "invalid_footprint", "Footprints need a repo-relative path, a state and an attribution.")
        wt = fp.get("worktree_id") or session.get("worktree_id")
        epoch = fp.get("claim_epoch")
        age = fp.get("age_s")
        # when the write happened (server clock), from the age crewd measured; None when not reported
        written_at = (
            utcnow() - timedelta(seconds=min(int(age), MAX_WRITE_AGE_S))
            if isinstance(age, int) and not isinstance(age, bool) and age >= 0
            else None
        )
        zones = idx.match(path, False)
        zone_ids = [str(z["id"]) for z in zones]
        await conn.execute(
            """INSERT INTO crew_footprints (crew_id, session_id, path, zone_ids, first_at, last_at, touches, state, attribution,
                   claim_epoch, last_commit, worktree_id, content_hash) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(crew_id, session_id, path) DO UPDATE SET last_at = excluded.last_at, touches = touches + ?,
                   state = excluded.state, zone_ids = excluded.zone_ids,
                   attribution = CASE WHEN crew_footprints.attribution = 'certain' THEN 'certain' ELSE excluded.attribution END,
                   claim_epoch = COALESCE(excluded.claim_epoch, crew_footprints.claim_epoch),
                   last_commit = COALESCE(excluded.last_commit, crew_footprints.last_commit),
                   worktree_id = COALESCE(excluded.worktree_id, crew_footprints.worktree_id),
                   content_hash = COALESCE(excluded.content_hash, crew_footprints.content_hash)""",
            (
                crew_id,
                session["id"],
                path,
                dumps(zone_ids),
                now,
                now,
                state,
                attribution,
                epoch,
                fp.get("last_commit"),
                wt,
                fp.get("content_hash") if S.is_sha256_hex(fp.get("content_hash")) else None,  # rider (gap analysis §7)
                1 if count_touch else 0,
            ),
        )
        touched.append(path)
        if state == "landed":
            continue
        stored = await fetchone(
            conn,
            "SELECT attribution FROM crew_footprints WHERE crew_id = ? AND session_id = ? AND path = ?",
            (crew_id, session["id"], path),
        )
        attr = str(stored["attribution"]) if stored else attribution
        opened.extend(await _detect(ops, tx, crew_id, session, path, state, attr, wt, epoch, zones, written_at=written_at))
    await auto_resolve(ops, tx, crew_id, paths=touched)
    return opened


async def _detect(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    session: Mapping[str, Any],
    path: str,
    state: str,
    attribution: str,
    wt: str | None,
    epoch: Any,
    zones: Sequence[Mapping[str, Any]],
    *,
    written_at: datetime | None = None,
) -> list[dict[str, Any]]:
    conn = tx.conn
    me = str(session["id"])
    out: list[dict[str, Any]] = []

    async def add(**kw: Any) -> None:
        row = await open_collision(ops, tx, crew_id, subject=path, session_a=me, attribution=attribution, **kw)
        if row is not None:
            out.append(row)

    # foreign checkout: the write landed in another live session's worktree
    if wt and session.get("worktree_id") and wt != session.get("worktree_id"):
        owner = await fetchone(
            conn,
            "SELECT id FROM crew_sessions WHERE crew_id = ? AND worktree_id = ? AND id != ?"
            f" AND state IN ({','.join('?' for _ in S.LIVE_PRESENCE_STATES)})",
            (crew_id, wt, me, *S.LIVE_PRESENCE_STATES),
        )
        if owner is not None:
            await add(kind="foreign_checkout_write", session_b=str(owner["id"]), evidence={"worktree_id": wt})
    # other sessions' footprints on the same file
    for other in await fetchall(
        conn,
        "SELECT * FROM crew_footprints WHERE crew_id = ? AND path = ? AND session_id != ? AND state IN ('dirty','committed')",
        (crew_id, path, me),
    ):
        osess = await _session(conn, str(other["session_id"]))
        if osess is None or not _recent(osess):
            continue
        same_wt = bool(wt) and wt == other.get("worktree_id")
        if same_wt and state == "dirty" and other["state"] == "dirty" and osess["state"] in S.LIVE_PRESENCE_STATES:
            await add(kind="same_worktree_file", session_b=str(osess["id"]))
        elif not same_wt:
            await add(kind="same_file", session_b=str(osess["id"]))
    # zone claims held by others; epochs
    zone_ids = [str(z["id"]) for z in zones]
    if not zone_ids:
        return out
    marks = ",".join("?" for _ in zone_ids)
    claims = await fetchall(
        conn,
        f"SELECT * FROM crew_claims WHERE crew_id = ? AND zone_id IN ({marks}) AND state IN ('active','offered','reserved')",
        (crew_id, *zone_ids),
    )
    mine = [c for c in claims if c.get("holder_session_id") == me and c["holder_kind"] == "session"]
    for c in claims:
        if c in mine:
            continue
        if (
            c["mode"] == "exclusive"
            and isinstance(epoch, int)
            and written_at is not None
            and await _written_while_held(conn, crew_id, str(c["zone_id"]), me, epoch, written_at)
        ):
            # E2E-f: this session held the zone at that epoch and wrote before its own lease horizon; the
            # footprint only reached the server after the zone moved on (crewd was cut off). Not a breach.
            continue
        if c["mode"] == "exclusive":
            await add(
                kind="exclusive_breach",
                session_b=c.get("holder_session_id"),
                zone_id=c["zone_id"],
                claim_id=c["id"],
                evidence={"holder_kind": c["holder_kind"]},
            )
            if isinstance(epoch, int) and int(c["epoch"]) > epoch:
                await add(
                    kind="stale_epoch_write",
                    session_b=c.get("holder_session_id"),
                    zone_id=c["zone_id"],
                    claim_id=c["id"],
                    evidence={"reported_epoch": epoch, "current_epoch": int(c["epoch"])},
                )
        elif c["mode"] == "shared" and c["state"] in ("active", "offered") and not mine:
            await add(kind="same_zone_shared", session_b=c.get("holder_session_id"), zone_id=c["zone_id"], claim_id=c["id"])
    for c in mine:
        if c["state"] in ("active", "offered") and is_fenced(c):
            await add(
                kind="stale_epoch_write",
                session_b=None,
                zone_id=c["zone_id"],
                claim_id=c["id"],
                evidence={"fenced": True, "epoch": int(c["epoch"])},
            )
    return out


# holding windows are read from the claim events of the zone (lease renewals are never events, so the
# window of a session that stopped renewing ends at its reservation: lease_expires_at − 60 s)
_HOLD_EVENT_TYPES: Final = (
    "claim.granted",
    "claim.adopted",
    "claim.transferred",
    "claim.handover_accepted",
    "claim.reserved",
    "claim.released",
    "claim.revoked",
    "claim.expired",
    "claim.fenced",
)


async def _written_while_held(
    conn: aiosqlite.Connection, crew_id: str, zone_id: str, session_id: str, epoch: int, written_at: datetime
) -> bool:
    """Whether ``session_id`` held ``zone_id`` exclusively at claim ``epoch`` when it wrote at ``written_at``.

    The window opens at the event that made the session the active holder at that epoch and
    closes at the first event after which it no longer was (another holder, another epoch, or no
    longer active), and never later than the lease horizon (``lease_expires_at − 60 s``) of the
    last view of the claim it held.
    """
    from remembra.crew.store import parse_iso

    rows = await fetchall(
        conn,
        f"SELECT ts, payload FROM crew_events WHERE crew_id = ? AND zone_id = ? AND type IN"
        f" ({','.join('?' for _ in _HOLD_EVENT_TYPES)}) ORDER BY seq DESC LIMIT 500",
        (crew_id, zone_id, *_HOLD_EVENT_TYPES),
    )
    windows: list[tuple[datetime, datetime | None]] = []
    start: datetime | None = None
    for row in reversed(rows):
        claim = (loads(row["payload"]) or {}).get("claim")
        if not isinstance(claim, dict) or claim.get("mode") != "exclusive":
            continue
        ts = parse_iso(str(row["ts"]))
        mine = claim.get("holder_session_id") == session_id and int(claim.get("epoch") or 0) == epoch
        if mine and claim.get("state") in ("active", "offered"):
            start = start or ts
            continue
        if start is None:
            continue
        end = ts
        lease = claim.get("lease_expires_at") if mine else None
        if lease:  # the holder stopped renewing: its own writes stopped at the horizon (D31)
            end = min(end, parse_iso(str(lease)) - timedelta(seconds=FENCE_MARGIN_S))
        windows.append((start, end))
        start = None
    if start is not None:
        windows.append((start, None))
    return any(s <= written_at and (e is None or written_at < e) for s, e in windows)


def heartbeat_footprints(footprints: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Heartbeat footprints in the shape :func:`record_footprints` accepts (the WP-4 upsert's defaults).

    Paths that are not repo-relative are skipped (the heartbeat upsert skips them too); a missing
    or unknown state is ``dirty`` and a missing or unknown attribution is ``probable`` (a probable
    breach never drives a Stop block, §5.3). Epochs are kept only as integers.
    """
    out: list[dict[str, Any]] = []
    for fp in footprints[:MAX_FOOTPRINTS_PER_CALL]:
        path = normalize_rel(str(fp.get("path") or ""))
        if not S.is_path_rel(path) or path == ".":
            continue
        epoch = fp.get("claim_epoch")
        out.append(
            {
                "path": path,
                "state": fp.get("state") if fp.get("state") in S.FOOTPRINT_STATES else "dirty",
                "attribution": fp.get("attribution") if fp.get("attribution") in S.ATTRIBUTIONS else "probable",
                "claim_epoch": epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None,
                "last_commit": fp.get("last_commit") if isinstance(fp.get("last_commit"), str) else None,
                "content_hash": fp.get("content_hash") if S.is_sha256_hex(fp.get("content_hash")) else None,
                **({"age_s": fp["age_s"]} if isinstance(fp.get("age_s"), int) and not isinstance(fp.get("age_s"), bool) else {}),
            }
        )
    return out


async def heartbeat_sink(tx: EventTx, session: Mapping[str, Any], footprints: Sequence[Mapping[str, Any]]) -> None:
    """The WP-4 heartbeat footprint sink: detect collisions for the footprints a heartbeat carried (§5.3).

    Runs inside the heartbeat transaction after the heartbeat upserted the rows, so the collision
    rows and their ``collision.detected`` events commit (or roll back) with the heartbeat.
    """
    clean = heartbeat_footprints(footprints)
    if not clean:
        return
    await record_footprints(CrewOps(tx._log), tx, str(session["crew_id"]), session, clean, count_touch=False)


def register_heartbeat_sink() -> None:
    """Register :func:`heartbeat_sink` with ``crew.sessions`` (idempotent). Called at import and by the startup hook."""
    from remembra.crew.sessions import register_footprint_sink

    register_footprint_sink(heartbeat_sink)


async def set_footprint_state(
    ops: CrewOps, tx: EventTx, crew_id: str, session_id: str, paths: Sequence[str], state: str, *, last_commit: str | None = None
) -> int:
    """Move footprints to ``committed`` or ``landed`` (a landed file no longer collides) and auto-resolve."""
    if state not in S.FOOTPRINT_STATES:
        raise CrewOpError(422, "invalid_footprint", f"state must be one of {', '.join(S.FOOTPRINT_STATES)}")
    norm = [normalize_rel(p) for p in paths]
    n = 0
    for p in norm:
        cur = await tx.conn.execute(
            "UPDATE crew_footprints SET state = ?, last_commit = COALESCE(?, last_commit), last_at = ? WHERE "
            "crew_id = ? AND session_id = ? AND path = ?",
            (state, last_commit, now_iso(), crew_id, session_id, p),
        )
        n += max(cur.rowcount or 0, 0)
    await auto_resolve(ops, tx, crew_id, paths=norm)
    return n


async def auto_resolve(
    ops: CrewOps, tx: EventTx, crew_id: str, *, paths: Sequence[str] = (), claim_id: str | None = None
) -> list[str]:
    """Resolve live collisions whose overlap is gone (footprint landed/removed, or the claim ended)."""
    conn = tx.conn
    rows: list[dict[str, Any]] = []
    if claim_id is not None:
        rows += await fetchall(
            conn,
            "SELECT * FROM crew_collisions WHERE crew_id = ? AND claim_id = ? AND state IN ('open','acknowledged')",
            (crew_id, claim_id),
        )
    if paths:
        marks = ",".join("?" for _ in paths)
        rows += await fetchall(
            conn,
            f"SELECT * FROM crew_collisions WHERE crew_id = ? AND subject IN ({marks}) AND state IN ('open','acknowledged')",
            (crew_id, *paths),
        )
    done: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(str(r["id"]))
        if not await _still_overlapping(conn, crew_id, r):
            await _close(tx, crew_id, r, "resolved", "auto", "system", Actor.system())
            done.append(str(r["id"]))
    return done


async def _live_fp(conn: aiosqlite.Connection, crew_id: str, session_id: str | None, path: str) -> bool:
    if not session_id:
        return True
    row = await fetchone(
        conn,
        "SELECT 1 FROM crew_footprints WHERE crew_id = ? AND session_id = ? AND path = ? AND state IN ('dirty','committed')",
        (crew_id, session_id, path),
    )
    return bool(row is not None)


async def _still_overlapping(conn: aiosqlite.Connection, crew_id: str, row: Mapping[str, Any]) -> bool:
    kind = row["kind"]
    if kind in ("unattributed_change", "merge_conflict_risk", "foreign_checkout_write", "stale_epoch_write"):
        return True  # only an agent or a human closes these
    subject = str(row["subject"])
    if not await _live_fp(conn, crew_id, row.get("session_a"), subject):
        return False
    if kind in ("same_file", "same_worktree_file"):
        return await _live_fp(conn, crew_id, row.get("session_b"), subject)
    if row.get("claim_id"):
        claim = await fetchone(conn, "SELECT state FROM crew_claims WHERE id = ?", (row["claim_id"],))
        return claim is not None and claim["state"] in ("active", "offered", "reserved")
    return True


async def record_unattributed_change(
    ops: CrewOps, tx: EventTx, crew_id: str, paths: Sequence[str], *, commit: str | None = None
) -> list[dict[str, Any]]:
    """A commit with no crew trailer (probably a human at a terminal) that touches a held zone: a notice."""
    idx = await zone_index(tx.conn, crew_id)
    out: list[dict[str, Any]] = []
    for raw in paths:
        path = normalize_rel(str(raw))
        zone_ids = [str(z["id"]) for z in idx.match(path, False)]
        if not zone_ids:
            continue
        marks = ",".join("?" for _ in zone_ids)
        for c in await fetchall(
            tx.conn,
            f"SELECT * FROM crew_claims WHERE crew_id = ? AND zone_id IN ({marks}) AND mode = 'exclusive'"
            " AND state IN ('active','offered','reserved')"
            " AND holder_kind = 'session'",
            (crew_id, *zone_ids),
        ):
            row = await open_collision(
                ops,
                tx,
                crew_id,
                kind="unattributed_change",
                subject=path,
                session_a=None,
                session_b=c.get("holder_session_id"),
                attribution="probable",
                zone_id=c["zone_id"],
                claim_id=c["id"],
                evidence={"commit": clip(commit, 40)},
            )
            if row is not None:
                out.append(row)
    return out


async def record_merge_conflict_risk(
    ops: CrewOps, tx: EventTx, crew_id: str, session: Mapping[str, Any], other_session_id: str, paths: Sequence[str]
) -> list[dict[str, Any]]:
    """Pre-push ``git merge-tree`` found conflicts with another live session's branch (§8.4): warn, never deny."""
    other = await _session(tx.conn, other_session_id)
    if other is None or other["crew_id"] != crew_id:
        raise CrewOpError(422, "cross_crew_reference", "The referenced session is not part of this crew.")
    out: list[dict[str, Any]] = []
    for raw in paths[:50]:
        row = await open_collision(
            ops,
            tx,
            crew_id,
            kind="merge_conflict_risk",
            subject=normalize_rel(str(raw)),
            session_a=str(session["id"]),
            session_b=other_session_id,
            attribution="certain",
        )
        if row is not None:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Acknowledge, resolve, dismiss (routes)
# ---------------------------------------------------------------------------


def _party(row: Mapping[str, Any], principal: Principal) -> bool:
    return principal.is_human or principal.session_id in (row.get("session_a"), row.get("session_b"))


async def acknowledge(ops: CrewOps, collision: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
    async with ops.log.transaction() as tx:
        row = await fetchone(tx.conn, "SELECT * FROM crew_collisions WHERE id = ?", (collision["id"],))
        assert row is not None
        if not _party(row, principal):
            raise CrewOpError(403, "not_a_party", "Only the sessions involved, or a human, can acknowledge this collision.")
        if row["state"] != "open":
            raise CrewOpError(409, "collision_not_open", f"This collision is {row['state']}.")
        await tx.conn.execute("UPDATE crew_collisions SET state = 'acknowledged' WHERE id = ?", (row["id"],))
        fresh = await fetchone(tx.conn, "SELECT * FROM crew_collisions WHERE id = ?", (row["id"],))
        assert fresh is not None
        await tx.emit(
            crew_id=str(row["crew_id"]),
            type="collision.acknowledged",
            actor=principal.actor(),
            payload={"collision": collision_view(fresh)},
            summary=f"collision {row['kind']} acknowledged by {principal.name}",
            refs={"collision_id": row["id"]},
        )
        return collision_view(fresh)


async def resolve(ops: CrewOps, collision: Mapping[str, Any], principal: Principal, resolution: str) -> dict[str, Any]:
    text = clip(resolution, 64) or "resolved"
    async with ops.log.transaction() as tx:
        row = await fetchone(tx.conn, "SELECT * FROM crew_collisions WHERE id = ?", (collision["id"],))
        assert row is not None
        if not _party(row, principal):
            raise CrewOpError(403, "not_a_party", "Only the sessions involved, or a human, can resolve this collision.")
        if row["state"] not in ("open", "acknowledged"):
            raise CrewOpError(409, "collision_not_open", f"This collision is {row['state']}.")
        fresh = await _close(
            tx, str(row["crew_id"]), row, "resolved", text, principal.session_id or principal.user_id, principal.actor()
        )
        return collision_view(fresh)


async def dismiss(ops: CrewOps, collision: Mapping[str, Any], human: Principal, reason: str | None = None) -> dict[str, Any]:
    """Human-only (route-enforced): the overlap is fine or already handled."""
    async with ops.log.transaction() as tx:
        row = await fetchone(tx.conn, "SELECT * FROM crew_collisions WHERE id = ?", (collision["id"],))
        assert row is not None
        if row["state"] not in ("open", "acknowledged"):
            raise CrewOpError(409, "collision_not_open", f"This collision is {row['state']}.")
        fresh = await _close(
            tx, str(row["crew_id"]), row, "dismissed", clip(reason, 64) or "dismissed", human.user_id, human.actor()
        )
        ops.audit(
            human.user_id, "crew.collision_dismissed", str(row["id"]), {"kind": row["kind"], "api_key_id": human.api_key_id}
        )
        return collision_view(fresh)


def evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(loads(row.get("evidence"), {}) or {})


# Heartbeat footprints feed collision detection wherever this module is loaded (the crew routers
# import it; the ``crew.claims`` startup hook registers it again, idempotently).
register_heartbeat_sink()
