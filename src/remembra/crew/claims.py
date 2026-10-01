"""Crew claims and the server guard (WP-5).

Spec anchors: §5.1 (claim state machine, grant, caps, task link, fencing), §5.2 (guard table),
§5.9 (human override), D5 (task batons never silently expire), D6/D33 (who may adopt a
reserved baton), D11 (auto-claim), D27 (human principal), D31 (one clock, one lease, epochs).

State machine (§5.1)::

    requested ─compatible & under cap─▶ active ─release─▶ released
              ─incompatible & wait─▶ queued ─blocker ends─▶ active (FIFO)
              ─incompatible & !wait─▶ denied (claim.denied, no row)
    active ─release(baton) | lost | quota | ended dirty | idle | lease expiry─▶ reserved
    reserved ─adopt (authorised, D33)─▶ active (new holder, epoch+1)
             ─holder re-takes─▶ active (epoch+1) ; ─human release─▶ released
             ─reserve_ttl (non-task only)─▶ expired
    active ─handover(X)─▶ offered ─accept─▶ active(X, epoch+1) | ─decline / 10 min─▶ active
    any live ─human override─▶ revoked | transferred (active for X) | reserved(human_hold)

**Epochs are per target** (zone, path glob or resource) and strictly increase across claims,
so a footprint written under an older epoch is detectably stale after an adopt, a handover or
a re-take (``stale_epoch_write``, D31).

Interface for WP-4 (sessions, heartbeat, reaper): :func:`renew_session_leases` inside the
heartbeat, :func:`reserve_session_claims` on stall / lost / dirty leave / idle park,
:func:`release_session_claims` on a clean leave, :func:`retake_session_claims` on recovery and
:func:`record_baton_offer` when the SessionStart brief offers a baton. Session tokens are
matched as ``sha256(token)`` hex (:func:`hash_session_token`).
"""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
import re
import shlex
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import aiosqlite
import structlog

from remembra.crew import gatecore as G
from remembra.crew import policy as P
from remembra.crew import schemas as S
from remembra.crew.events import Actor, EventTx
from remembra.crew.limits import OBSERVE_ONLY_MESSAGE, CrewLimits, seat_upgrade_hint
from remembra.crew.sessions import has_seat
from remembra.crew.settings import load_settings
from remembra.crew.store import dumps, is_accountable_for, loads, new_id, now_iso, parse_iso
from remembra.crew.zones import (
    CrewOpError,
    CrewOps,
    Principal,
    active_commons,
    active_ignore,
    clip,
    crew_row,
    ensure_policy_zone,
    fetchall,
    fetchone,
    is_frozen,
    load_zone_rows,
    raise_inbox_item,
    related_zone_ids,
    resolve_inbox_items,
    utcnow,
    zone_view,
)

log = structlog.get_logger(__name__)

LIVE_HOLD: Final = ("active", "offered")
BLOCKING: Final = ("active", "offered", "reserved")
LIVE: Final = ("requested", "queued", "active", "offered", "reserved")
HANDOVER_TTL_S: Final = 600
MICRO_LEASE_S: Final = 300
FENCE_MARGIN_S: Final = 60
HOARD_ZONES: Final = 3
HOARD_FILES_FRACTION: Final = 0.5
SESSION_HEADER: Final = "X-Remembra-Crew-Session"
NOT_LIVE_SESSION_STATES: Final = ("ended", "lost")
GUARD_BLOCK_COALESCE_S: Final = 300
SERVER_ROOT: Final = "/__crew__"
SERVER_HOME: Final = "/__crew_home__"
_SLUG_RE: Final = re.compile(S.SLUG_PATTERN)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def hash_session_token(token: str) -> str:
    """How ``crew_sessions.token_hash`` stores a session token (sha256 hex; the raw token is never stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def authenticate_session(
    conn: aiosqlite.Connection,
    token: str | None,
    *,
    user_id: str,
    crew_id: str | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """The session behind a session token, owned by ``user_id`` (and in ``crew_id`` / equal to ``session_id``).

    ``agent_id`` is the calling key's agent scope (``AuthenticatedUser.agent_id``): an agent-scoped
    key acts only as sessions of its own agent (§11.2), whatever token it carries.
    401 ``invalid_session`` for a missing, unknown, foreign, other-agent or ended token (one answer for all).
    """
    if not token or len(token) > 512:
        raise CrewOpError(401, "session_required", f"A crew session token is required ({SESSION_HEADER} header).")
    row = await fetchone(conn, "SELECT * FROM crew_sessions WHERE token_hash = ?", (hash_session_token(token),))
    ok = (
        row is not None
        and row["user_id"] == user_id
        and row["state"] != "ended"
        and (crew_id is None or row["crew_id"] == crew_id)
        and (session_id is None or row["id"] == session_id)
        and (agent_id is None or row["agent_id"] == agent_id)
    )
    if not ok or row is None:
        raise CrewOpError(401, "invalid_session", "The crew session token is not valid for this request.")
    return row


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _ts(value: Any) -> datetime | None:
    return parse_iso(str(value)) if value else None


def is_fenced(row: Mapping[str, Any], now: datetime | None = None) -> bool:
    """The holder's own writes stop at ``lease_expires_at − 60 s`` when the lease was not renewed (D31)."""
    exp = _ts(row.get("lease_expires_at"))
    if exp is None or row.get("state") not in LIVE_HOLD:
        return False
    return (now or utcnow()) >= exp - timedelta(seconds=FENCE_MARGIN_S)


def claim_view(row: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """``ClaimView`` (schemas) of a ``crew_claims`` row."""
    return {
        "id": row["id"],
        "zone_id": row.get("zone_id"),
        "path_glob": row.get("path_glob"),
        "resource": row.get("resource"),
        "mode": row["mode"],
        "holder_kind": row["holder_kind"],
        "holder_session_id": row.get("holder_session_id"),
        "holder_agent_id": row.get("holder_agent_id"),
        "holder_user_id": row.get("holder_user_id"),
        "task_id": row.get("task_id"),
        "state": row["state"],
        "source": row["source"],
        "epoch": int(row.get("epoch") or 1),
        "unconfirmed": bool(row.get("unconfirmed")),
        "fenced": is_fenced(row, now),
        "lease_expires_at": row.get("lease_expires_at"),
        "reserve_reason": row.get("reserve_reason"),
        "reserved_for": row.get("reserved_for"),
        "offered_to": row.get("offered_to"),
        "queue_pos": row.get("queue_pos"),
        "baton_ref": row.get("baton_ref"),
        "granted_at": row.get("granted_at"),
        "version": int(row.get("version") or 1),
    }


async def get_claim(conn: aiosqlite.Connection, claim_id: str) -> dict[str, Any]:
    row = await fetchone(conn, "SELECT * FROM crew_claims WHERE id = ?", (claim_id,))
    if row is None:
        raise CrewOpError(404, "not_found", "Not found.")
    return row


async def list_claims(conn: aiosqlite.Connection, crew_id: str, state: str | None = None) -> list[dict[str, Any]]:
    if state is not None and state not in S.CLAIM_STATES and state != "live":
        raise CrewOpError(422, "invalid_state", f"state must be live or one of {', '.join(S.CLAIM_STATES)}")
    if state is None or state == "live":
        marks = ",".join("?" for _ in LIVE)
        rows = await fetchall(
            conn, f"SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ({marks}) ORDER BY created_at", (crew_id, *LIVE)
        )
    else:
        rows = await fetchall(
            conn, "SELECT * FROM crew_claims WHERE crew_id = ? AND state = ? ORDER BY created_at DESC LIMIT 500", (crew_id, state)
        )
    now = utcnow()
    return [claim_view(r, now) for r in rows]


async def _session(conn: aiosqlite.Connection, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    return await fetchone(conn, "SELECT * FROM crew_sessions WHERE id = ?", (session_id,))


async def _task_label(conn: aiosqlite.Connection, task_id: str | None) -> str | None:
    if not task_id:
        return None
    row = await fetchone(conn, "SELECT number FROM crew_tasks WHERE id = ?", (task_id,))
    return f"T-{row['number']}" if row else None


async def _target_label(conn: aiosqlite.Connection, row: Mapping[str, Any]) -> str:
    if row.get("zone_id"):
        z = await fetchone(conn, "SELECT slug FROM crew_zones WHERE id = ?", (row["zone_id"],))
        return f"zone {z['slug']}" if z else "a zone"
    if row.get("resource"):
        return str(row["resource"])
    return "a file claim"


async def _holder_name(conn: aiosqlite.Connection, row: Mapping[str, Any]) -> str:
    if row.get("holder_kind") == "human":
        return "a human"
    s = await _session(conn, row.get("holder_session_id"))
    return str(s["callsign"]) if s else "another session"


async def blocker_view(conn: aiosqlite.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    """``Blocker`` (schemas): ids and callsigns only."""
    s = await _session(conn, row.get("holder_session_id"))
    reason = "reserved" if row["state"] == "reserved" else ("human" if row["holder_kind"] == "human" else str(row["mode"]))
    return {
        "claim_id": row["id"],
        "zone_id": row.get("zone_id"),
        "holder_session_id": row.get("holder_session_id"),
        "holder_callsign": s["callsign"] if s else None,
        "task_id": row.get("task_id"),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Targets, conflicts, epochs and caps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    zone: Mapping[str, Any] | None = None
    path_glob: str | None = None
    resource: str | None = None

    @property
    def zone_id(self) -> str | None:
        return str(self.zone["id"]) if self.zone else None

    def same(self, row: Mapping[str, Any]) -> bool:
        return (
            row.get("zone_id") == self.zone_id and row.get("path_glob") == self.path_glob and row.get("resource") == self.resource
        )

    @classmethod
    async def of_row(cls, conn: aiosqlite.Connection, row: Mapping[str, Any]) -> Target:
        zone = await fetchone(conn, "SELECT * FROM crew_zones WHERE id = ?", (row["zone_id"],)) if row.get("zone_id") else None
        return cls(zone=zone, path_glob=row.get("path_glob"), resource=row.get("resource"))


def mode_blocks(requested: str, held: str) -> bool:
    """Compatibility matrix: watch never blocks; exclusive blocks exclusive and shared; shared blocks exclusive."""
    if requested == "watch" or held == "watch":
        return False
    return requested == "exclusive" or held == "exclusive"


def _globs(zone: Mapping[str, Any]) -> list[str]:
    return [str(g) for g in loads(zone.get("include_globs"), []) or []]


def _services(zone: Mapping[str, Any]) -> list[str]:
    return [str(s) for s in loads(zone.get("services"), []) or []]


async def _overlaps(
    conn: aiosqlite.Connection, crew_id: str, target: Target, other: Mapping[str, Any], cache: dict[str, Any]
) -> bool:
    zones: dict[str, Mapping[str, Any]] = cache.setdefault(
        "zones", {str(z["id"]): z for z in await load_zone_rows(conn, crew_id, include_archived=True)}
    )
    ozone = zones.get(str(other.get("zone_id") or ""))
    if target.zone is not None:
        if other.get("zone_id"):
            key = f"rel:{target.zone_id}"
            if key not in cache:
                cache[key] = await related_zone_ids(conn, crew_id, str(target.zone_id))
            return str(other["zone_id"]) in cache[key]
        if other.get("path_glob"):
            return P.any_overlap(_globs(target.zone), [str(other["path_glob"])])
        return bool(other.get("resource")) and str(other["resource"]) in _services(target.zone)
    if target.path_glob is not None:
        if ozone is not None:
            return P.any_overlap([target.path_glob], _globs(ozone))
        if other.get("path_glob"):
            return P.globs_overlap(target.path_glob, str(other["path_glob"]))
        return False
    if target.resource is not None:
        if other.get("resource"):
            return str(other["resource"]) == target.resource
        return ozone is not None and target.resource in _services(ozone)
    return False


def _mine(row: Mapping[str, Any], principal: Principal) -> bool:
    if principal.kind == "session":
        return row.get("holder_kind") == "session" and row.get("holder_session_id") == principal.session_id
    if principal.kind == "human":
        return row.get("holder_kind") == "human" and row.get("holder_user_id") == principal.user_id
    return False


async def conflicts(
    conn: aiosqlite.Connection,
    crew_id: str,
    target: Target,
    mode: str,
    principal: Principal,
    *,
    exclude: Iterable[str] = (),
    states: Sequence[str] = BLOCKING,
) -> list[dict[str, Any]]:
    """Other principals' claims in ``states`` that block ``mode`` on ``target`` (own claims never block)."""
    if mode == "watch":
        return []
    skip = set(exclude)
    marks = ",".join("?" for _ in states)
    blocking_modes = ("exclusive",) if mode == "shared" else ("exclusive", "shared")
    mode_marks = ",".join("?" for _ in blocking_modes)
    rows = await fetchall(
        conn,
        f"SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ({marks}) AND mode IN ({mode_marks}) ORDER BY created_at",
        (crew_id, *states, *blocking_modes),
    )
    cache: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["id"] in skip or _mine(r, principal) or not mode_blocks(mode, str(r["mode"])):
            continue
        if await _overlaps(conn, crew_id, target, r, cache):
            out.append(r)
    return out


async def next_epoch(conn: aiosqlite.Connection, crew_id: str, target: Target) -> int:
    """Per-target fencing token: one more than any epoch ever issued on this zone, glob or resource (D31)."""
    param: str | None
    if target.zone_id:
        sql, param = "zone_id = ?", target.zone_id
    elif target.path_glob:
        sql, param = "zone_id IS NULL AND path_glob = ?", target.path_glob
    else:
        sql, param = "zone_id IS NULL AND resource = ?", target.resource
    row = await fetchone(
        conn, f"SELECT COALESCE(MAX(epoch), 0) AS m FROM crew_claims WHERE crew_id = ? AND {sql}", (crew_id, param)
    )
    return int(row["m"] if row else 0) + 1


async def exclusive_counts(conn: aiosqlite.Connection, session: Mapping[str, Any]) -> tuple[int, int]:
    """(live exclusive claims of this session, live exclusive claims of this agent id) for the §5.1 caps."""
    counts = await fetchone(
        conn,
        """SELECT
             (SELECT COUNT(*) FROM crew_claims WHERE holder_session_id = ?
                AND mode = 'exclusive' AND source != 'micro_lease'
                AND (state IN ('active','offered') OR (state = 'reserved' AND reserved_for = ?))) AS per_session,
             (SELECT COUNT(*) FROM crew_claims WHERE crew_id = ? AND holder_user_id = ? AND holder_agent_id = ?
                AND holder_kind = 'session' AND mode = 'exclusive' AND source != 'micro_lease'
                AND state IN ('active','offered')) AS per_agent""",
        (session["id"], session["id"], session["crew_id"], session["user_id"], session["agent_id"]),
    )
    return int(counts["per_session"] if counts else 0), int(counts["per_agent"] if counts else 0)


async def _over_cap(conn: aiosqlite.Connection, session: Mapping[str, Any], settings: Mapping[str, Any], adding: int = 1) -> bool:
    per_session, per_agent = await exclusive_counts(conn, session)
    return per_session + adding > int(settings["max_exclusive_claims_per_session"]) or per_agent + adding > int(
        settings["max_exclusive_claims_per_agent"]
    )


def _lease(settings: Mapping[str, Any], now: datetime, *, source: str) -> str:
    seconds = MICRO_LEASE_S if source == "micro_lease" else int(settings["lease_ttl_s"])
    return now_iso(now + timedelta(seconds=seconds))


def _session_live(session: Mapping[str, Any]) -> None:
    if session["state"] in NOT_LIVE_SESSION_STATES:
        raise CrewOpError(409, "session_not_live", "This session is not live; start a new session to claim.")
    if session["state"] == "paused":
        raise CrewOpError(423, "paused", "This session is paused by a human.")


async def has_claim_seat(conn: aiosqlite.Connection, crew_id: str, session: Mapping[str, Any], limits: CrewLimits | None) -> bool:
    """True when the session holds one of the plan's live-session seats (a sub-agent within its
    top-level session's allowance holds that session's seat).

    ``limits`` is the crew owner's plan (``CrewOps.limits``, set by the routes); without it nothing is refused.
    """
    return limits is None or await has_seat(
        conn, crew_id, str(session["id"]), limits.max_sessions_live, free_sub_agents=limits.free_sub_agents_per_parent
    )


async def require_seat(conn: aiosqlite.Connection, crew_id: str, session: Mapping[str, Any], limits: CrewLimits | None) -> None:
    """403 ``observe_only`` for a session that joined over the live-session cap: it cannot claim (§12)."""
    if not await has_claim_seat(conn, crew_id, session, limits):
        assert limits is not None
        raise CrewOpError(403, "observe_only", OBSERVE_ONLY_MESSAGE, upgrade_hint=seat_upgrade_hint(limits))


# ---------------------------------------------------------------------------
# Waiters (long-poll wait_s) and event helpers
# ---------------------------------------------------------------------------

_waiters: dict[str, asyncio.Event] = {}


def _wake(ops: CrewOps, claim_ids: Iterable[str]) -> None:
    ids = list(claim_ids)
    if not ids:
        return

    async def fire() -> None:
        for cid in ids:
            ev = _waiters.get(cid)
            if ev is not None:
                ev.set()

    ops.db.after_commit(fire)


async def wait_for_claim(ops: CrewOps, claim_id: str, wait_s: float) -> dict[str, Any]:
    """Block (outside any transaction) until the claim leaves ``queued`` or ``wait_s`` passes; return the row."""
    ev = _waiters.setdefault(claim_id, asyncio.Event())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, min(float(wait_s), S.CLAIM_WAIT_MAX_S))
    try:
        while True:
            row = await get_claim(ops.db.conn, claim_id)
            remaining = deadline - loop.time()
            if row["state"] != "queued" or remaining <= 0:
                return row
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(1.0, remaining))
            except TimeoutError:
                pass
            ev.clear()
    finally:
        _waiters.pop(claim_id, None)


async def _emit_claim(
    tx: EventTx, crew_id: str, type_: str, row: Mapping[str, Any], actor: Actor, summary: str, **extra: Any
) -> int:
    res = await tx.emit(
        crew_id=crew_id,
        type=type_,
        actor=actor,
        payload={"claim": claim_view(row), **extra},
        summary=summary,
        refs={
            "claim_id": row["id"],
            "zone_id": row.get("zone_id"),
            "task_id": row.get("task_id"),
            "session_id": row.get("holder_session_id"),
        },
    )
    return res.seq


async def _reload(conn: aiosqlite.Connection, claim_id: str) -> dict[str, Any]:
    return await get_claim(conn, claim_id)


# ---------------------------------------------------------------------------
# Request (grant / queue / deny)
# ---------------------------------------------------------------------------


@dataclass
class ClaimOutcome:
    status: str  # granted | existing | retaken | queued | denied
    claim: dict[str, Any] | None = None
    blockers: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None  # conflict | claim_cap (denied)
    seq: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def http_status(self) -> int:
        return {"granted": 201, "retaken": 201, "existing": 200, "queued": 202}.get(self.status, 409)

    def body(self) -> dict[str, Any]:
        if self.status == "denied":
            msg = (
                "Claim limit reached: release a claim first."
                if self.error == "claim_cap"
                else "Refused: another session holds this area. Work elsewhere or ask the holder with crew_say."
            )
            return {"error": self.error or "conflict", "message": msg, "blockers": self.blockers, **self.extra, "seq": self.seq}
        return {
            "status": self.status,
            "claim": claim_view(self.claim) if self.claim else None,
            "blockers": self.blockers,
            "seq": self.seq,
        }


async def _check_agent_glob(conn: aiosqlite.Connection, crew_id: str, glob: str, source: str) -> None:
    """File claims by an agent (§5.1, ``undeclared_policy: file_claim``) are for paths that no zone covers.

    A path glob must be anchored under a literal top-level name (no ``**`` / ``*.md`` squats on the
    whole repo) and must not overlap any zone, the built-in ``crew-policy`` zone included: zone
    paths are claimed through their zone, where protected, frozen, ``reserve_for`` and
    task-for-parent rules apply. The one exception is a serialize micro-lease on a commons file
    (§5.2 row 12): a literal path that a commons entry names.
    """
    if not P.glob_is_anchored(glob):
        raise CrewOpError(
            422, "glob_too_broad", "A path_glob claim must start with a literal top-level folder or file name (no ** or *)."
        )
    if source == "micro_lease":
        if P.glob_is_literal(glob) and any(
            G.glob_match(str(c.get("glob") or ""), glob) for c in await active_commons(conn, crew_id)
        ):
            return
        raise CrewOpError(422, "invalid_claim", "A micro-lease is taken on one commons file.")
    for z in await load_zone_rows(conn, crew_id):
        if P.any_overlap([glob], _globs(z)):
            raise CrewOpError(
                422,
                "zone_path",
                f"path_glob overlaps zone {z['slug']}: claim the zone instead (file claims are for paths outside every zone).",
            )


async def _resolve_target(
    conn: aiosqlite.Connection,
    crew_id: str,
    *,
    zone_id: str | None,
    path_glob: str | None,
    resource: str | None,
    principal: Principal | None = None,
    source: str = "mcp",
) -> Target:
    given = [x for x in (zone_id, path_glob, resource) if x]
    if len(given) != 1:
        raise CrewOpError(422, "invalid_claim", "Give exactly one of zone_id, path_glob or resource.")
    if zone_id:
        zone = await fetchone(conn, "SELECT * FROM crew_zones WHERE id = ? AND crew_id = ?", (zone_id, crew_id))
        if zone is None or zone["archived_at"] is not None:
            raise CrewOpError(422, "cross_crew_reference", "The referenced zone is not part of this crew.")
        return Target(zone=zone)
    if path_glob:
        reason = P.check_glob(path_glob)
        if reason:
            raise CrewOpError(422, "invalid_claim", f"path_glob {reason}.")
        if principal is not None and not principal.is_privileged:
            await _check_agent_glob(conn, crew_id, path_glob, source)
        return Target(path_glob=path_glob)
    return Target(resource=resource)


async def _check_task(conn: aiosqlite.Connection, crew_id: str, task_id: str | None) -> None:
    if task_id is None:
        return
    row = await fetchone(conn, "SELECT crew_id FROM crew_tasks WHERE id = ?", (task_id,)) if S.is_id("task", task_id) else None
    if row is None or row["crew_id"] != crew_id:
        raise CrewOpError(422, "cross_crew_reference", "The referenced task is not part of this crew.")


def _zone_rules(target: Target, principal: Principal, settings: Mapping[str, Any], task_id: str | None, source: str) -> None:
    zone = target.zone
    if zone is None:
        return
    if zone["builtin"]:
        raise CrewOpError(423, "crew_policy", "The crew-policy zone is never claimable.")
    if principal.is_privileged:
        return
    if is_frozen(zone):
        raise CrewOpError(423, "frozen", f"Zone {zone['slug']} is frozen by a human.")
    if zone["protected"]:
        raise CrewOpError(423, "protected", f"Zone {zone['slug']} is protected; only a human can grant it.")
    if zone.get("reserve_for"):
        session = principal.session or {}
        if session.get("agent_id") != zone["reserve_for"] or not session.get("agent_verified"):
            raise CrewOpError(403, "reserved_for_agent", f"Zone {zone['slug']} is reserved for a key-verified agent.")
    if principal.is_human:
        return  # a crew member's own claim: the protections above apply, the agent task rules below do not
    if not zone["is_leaf"] and task_id is None:
        raise CrewOpError(422, "task_required", f"Zone {zone['slug']} has child zones: claim it with a task (crew_task start).")
    if source == "first_write" and settings.get("auto_claim_leaf_only", True) and not zone["is_leaf"]:
        raise CrewOpError(422, "task_required", "Auto-claim applies to leaf zones only.")


async def request_claim(
    ops: CrewOps,
    crew_id: str,
    principal: Principal,
    *,
    zone_id: str | None = None,
    path_glob: str | None = None,
    resource: str | None = None,
    mode: str = "exclusive",
    task_id: str | None = None,
    reason: str | None = None,
    wait: bool = False,
    source: str = "mcp",
) -> ClaimOutcome:
    """``POST /crews/{id}/claims``. Returns the outcome; a refusal still commits ``claim.denied``."""
    if mode not in S.CLAIM_MODES:
        raise CrewOpError(422, "invalid_claim", f"mode must be one of {', '.join(S.CLAIM_MODES)}")
    if source not in S.CLAIM_SOURCES:
        raise CrewOpError(422, "invalid_claim", f"source must be one of {', '.join(S.CLAIM_SOURCES)}")
    if principal.kind == "system":
        raise CrewOpError(422, "invalid_claim", "claims need a session or a human")
    if principal.is_human:
        source = "dashboard"
    actor = principal.actor()
    async with ops.log.transaction() as tx:
        conn = tx.conn
        await ensure_policy_zone(tx, crew_id)
        settings = load_settings((await crew_row(conn, crew_id))["settings"])
        session = principal.session
        if session is not None:
            session = await _session(conn, str(session["id"])) or session
            principal = Principal.for_session(session, api_key_id=principal.api_key_id)
            _session_live(session)
            if source != "micro_lease":  # a serialize micro-lease orders writes; it is not a claim of work
                await require_seat(conn, crew_id, session, ops.limits)
            if settings.get("require_verified_agents_for_claims") and not session.get("agent_verified"):
                raise CrewOpError(403, "unverified_agent", "This crew only accepts claims from key-verified agents.")
        target = await _resolve_target(
            conn, crew_id, zone_id=zone_id, path_glob=path_glob, resource=resource, principal=principal, source=source
        )
        await _check_task(conn, crew_id, task_id)
        _zone_rules(target, principal, settings, task_id, source)
        now = utcnow()
        # own claim on the same target: idempotent, or a re-take of an own reservation
        holder_column = "holder_session_id" if principal.kind == "session" else "holder_user_id"
        holder_id = principal.session_id if principal.kind == "session" else principal.user_id
        own = await fetchall(
            conn,
            "SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ('queued','active','offered','reserved') "
            f"AND holder_kind = ? AND {holder_column} IS ? "
            "AND zone_id IS ? AND path_glob IS ? AND resource IS ? ORDER BY created_at",
            (crew_id, principal.kind, holder_id, target.zone_id, target.path_glob, target.resource),
        )
        for r in own:
            if not (_mine(r, principal) and target.same(r)):
                continue
            if r["state"] == "queued":
                # a blocker may have ended on a path that did not run the queue: promote now (never brick a waiter)
                if str(r["id"]) in await promote_queue(ops, tx, crew_id):
                    fresh = await _reload(conn, str(r["id"]))
                    return ClaimOutcome("granted", fresh)
                return ClaimOutcome("queued", r)
            if r["state"] in ("active", "offered"):
                return ClaimOutcome("existing", r)
            if r["state"] == "reserved" and r.get("reserved_for") in (None, principal.session_id):
                row = await _retake(ops, tx, crew_id, r, principal, settings, now)
                return ClaimOutcome("retaken", row, seq=None)
        blockers = await conflicts(conn, crew_id, target, mode, principal)
        if not blockers and session is not None and mode == "exclusive" and source != "micro_lease":
            if await _over_cap(conn, session, settings):
                res = await tx.emit(
                    crew_id=crew_id,
                    type="claim.denied",
                    actor=actor,
                    payload={"claim": None, "blockers": []},
                    summary=f"{principal.name} refused a claim: claim limit reached",
                    refs={"zone_id": target.zone_id, "session_id": principal.session_id},
                )
                return ClaimOutcome("denied", None, [], "claim_cap", res.seq)
        if blockers:
            views = [await blocker_view(conn, b) for b in blockers[:10]]
            target_label = await _target_label(conn, {"zone_id": target.zone_id, "resource": target.resource})
            if wait:
                row = await _insert_claim(tx, crew_id, target, mode, principal, task_id, "queued", source, reason, settings, now)
                seq = await _emit_claim(
                    tx, crew_id, "claim.queued", row, actor, f"{principal.name} queued for {await _target_label(conn, row)}"
                )
                return ClaimOutcome("queued", row, views, seq=seq)
            res = await tx.emit(
                crew_id=crew_id,
                type="claim.denied",
                actor=actor,
                payload={"claim": None, "blockers": views},
                summary=f"{principal.name} refused {target_label}: held by {await _holder_name(conn, blockers[0])}",
                refs={"zone_id": target.zone_id, "session_id": principal.session_id},
            )
            first = blockers[0]
            holder = await _holder_name(conn, first)
            extra = {
                "holder": holder,
                "claim": claim_view(first),
                "task": await _task_label(conn, first.get("task_id")),
                "lease_expires_at": first.get("lease_expires_at"),
                "how_to_request": (
                    "The baton is reserved: work elsewhere, or ask the owner to hand it over from the dashboard."
                    if first["state"] == "reserved"
                    else f'crew_say(to="@{holder}", kind="request_release")'
                    if first.get("holder_kind") == "session"
                    else "Ask the owner in the dashboard."
                ),
            }
            return ClaimOutcome("denied", None, views, "conflict", res.seq, extra)
        try:
            row = await _insert_claim(tx, crew_id, target, mode, principal, task_id, "active", source, reason, settings, now)
        except sqlite3.IntegrityError:
            # uq_claim_exclusive: lost a race with a concurrent grant (the index is the last line of defence)
            res = await tx.emit(
                crew_id=crew_id,
                type="claim.denied",
                actor=actor,
                payload={"claim": None, "blockers": []},
                summary=f"{principal.name} lost a claim race",
                refs={"zone_id": target.zone_id},
            )
            return ClaimOutcome("denied", None, [], "conflict", res.seq)
        seq = await _emit_claim(
            tx, crew_id, "claim.granted", row, actor, f"{principal.name} claimed {await _target_label(conn, row)} ({mode})"
        )
        if session is not None:
            await _hoarding_check(tx, crew_id, session, actor)
        return ClaimOutcome("granted", row, seq=seq)


async def _insert_claim(
    tx: EventTx,
    crew_id: str,
    target: Target,
    mode: str,
    principal: Principal,
    task_id: str | None,
    state: str,
    source: str,
    reason: str | None,
    settings: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    conn = tx.conn
    claim_id = new_id("claim")
    stamp = now_iso(now)
    session = principal.session
    queue_pos = None
    epoch = 1
    lease = None
    granted_at = None
    if state == "queued":
        row = await fetchone(
            conn,
            "SELECT COALESCE(MAX(queue_pos), 0) + 1 AS n FROM crew_claims WHERE crew_id = ? AND state = 'queued'",
            (crew_id,),
        )
        queue_pos = int(row["n"]) if row else 1
    else:
        epoch = await next_epoch(conn, crew_id, target)
        granted_at = stamp
        if session is not None:
            lease = _lease(settings, now, source=source)
    await conn.execute(
        """INSERT INTO crew_claims (id, crew_id, zone_id, path_glob, resource, mode, holder_kind, holder_session_id,
               holder_user_id, holder_agent_id, task_id, state, source, epoch, reason, lease_expires_at, queue_pos, granted_at,
               created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            claim_id,
            crew_id,
            target.zone_id,
            target.path_glob,
            target.resource,
            mode,
            "session" if session is not None else "human",
            session["id"] if session is not None else None,
            principal.user_id,
            session["agent_id"] if session is not None else None,
            task_id,
            state,
            source,
            epoch,
            clip(reason, 280),
            lease,
            queue_pos,
            granted_at,
            stamp,
            stamp,
        ),
    )
    return await _reload(conn, claim_id)


async def _retake(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    row: Mapping[str, Any],
    principal: Principal,
    settings: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    target = await Target.of_row(tx.conn, row)
    epoch = await next_epoch(tx.conn, crew_id, target)
    await tx.conn.execute(
        """UPDATE crew_claims SET state = 'active', epoch = ?, lease_expires_at = ?, reserve_reason = NULL, reserved_for = NULL,
               reserve_expires_at = NULL, granted_at = ?, version = version + 1, updated_at = ? WHERE id = ? AND
                   state = 'reserved'""",
        (epoch, _lease(settings, now, source=str(row["source"])), now_iso(now), now_iso(now), row["id"]),
    )
    fresh = await _reload(tx.conn, str(row["id"]))
    await _emit_claim(
        tx, crew_id, "claim.granted", fresh, principal.actor(), f"{principal.name} re-took {await _target_label(tx.conn, fresh)}"
    )
    await resolve_inbox_items(tx, crew_id, ref_ids=[str(row["id"])], kinds=["baton_reserved", "baton_available", "baton_waiting"])
    return fresh


async def _hoarding_check(tx: EventTx, crew_id: str, session: Mapping[str, Any], actor: Actor) -> None:
    """Needs-you "zone hoarding" when a session holds more than 3 zones or more than half of the estimated files (§5.1)."""
    counts = await fetchone(
        tx.conn,
        """WITH held_zones AS (
             SELECT z.files_estimate FROM crew_claims c JOIN crew_zones z ON z.id = c.zone_id
               WHERE c.holder_session_id = ? AND c.state IN ('active','offered')
           ) SELECT
             (SELECT COUNT(*) FROM held_zones) AS zone_areas,
             (SELECT COALESCE(SUM(files_estimate), 0) FROM held_zones) AS held_files,
             (SELECT COALESCE(SUM(files_estimate), 0) FROM crew_zones
                WHERE crew_id = ? AND archived_at IS NULL AND builtin = 0) AS all_files,
             (SELECT COUNT(*) FROM crew_claims WHERE holder_session_id = ?
                AND zone_id IS NULL AND path_glob IS NOT NULL AND source != 'micro_lease'
                AND mode != 'watch' AND state IN ('active','offered')) AS glob_areas""",
        (session["id"], crew_id, session["id"]),
    )
    # Keep both cap decisions inside the caller's writer transaction, but avoid
    # repeated SQLite worker handoffs. File globs still count as areas too.
    assert counts is not None  # aggregate SELECT always yields one row
    held_files = int(counts["held_files"])
    all_files = int(counts["all_files"])
    areas = int(counts["zone_areas"]) + int(counts["glob_areas"])
    if areas > HOARD_ZONES or (all_files > 0 and held_files > HOARD_FILES_FRACTION * all_files):
        await raise_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="zone_hoarding",
            title=f"{session['callsign']} holds {areas} zones or file claims",
            dedupe_key=f"hoarding:{session['id']}",
            ref_type="session",
            ref_id=str(session["id"]),
            primary_action="release_all",
            actor=actor,
        )


# ---------------------------------------------------------------------------
# Queue promotion (FIFO) and endings
# ---------------------------------------------------------------------------


async def promote_queue(ops: CrewOps, tx: EventTx, crew_id: str) -> list[str]:
    """Grant queued claims whose blockers ended, oldest first; a waiting claim is never overtaken by a later one."""
    conn = tx.conn
    queued = await fetchall(
        conn, "SELECT * FROM crew_claims WHERE crew_id = ? AND state = 'queued' ORDER BY queue_pos, created_at", (crew_id,)
    )
    if not queued:
        return []
    settings = load_settings((await crew_row(conn, crew_id))["settings"])
    granted: list[str] = []
    waiting: list[dict[str, Any]] = []
    now = utcnow()
    for q in queued:
        session = await _session(conn, q.get("holder_session_id"))
        principal = Principal.for_session(session) if session else Principal.human(str(q["holder_user_id"]))
        if session is not None and session["state"] in NOT_LIVE_SESSION_STATES:
            await conn.execute(
                "UPDATE crew_claims SET state = 'released', ended_at = ?, end_reason = 'holder_gone', version = version + 1,"
                " updated_at = ? WHERE id = ? AND state = 'queued'",
                (now_iso(now), now_iso(now), q["id"]),
            )
            await _emit_claim(
                tx,
                crew_id,
                "claim.released",
                await _reload(conn, str(q["id"])),
                Actor.system(),
                "queued claim cancelled: holder gone",
                baton=False,
            )
            continue
        target = await Target.of_row(conn, q)
        blocked = bool(await conflicts(conn, crew_id, target, str(q["mode"]), principal, exclude=[q["id"]]))
        if not blocked:
            cache: dict[str, Any] = {}
            for w in waiting:
                if (
                    not _mine(w, principal)
                    and mode_blocks(str(q["mode"]), str(w["mode"]))
                    and await _overlaps(conn, crew_id, target, w, cache)
                ):
                    blocked = True
                    break
        if not blocked and session is not None and q["mode"] == "exclusive" and await _over_cap(conn, session, settings):
            blocked = True
        if blocked:
            waiting.append(q)
            continue
        epoch = await next_epoch(conn, crew_id, target)
        lease = _lease(settings, now, source=str(q["source"])) if session is not None else None
        await conn.execute(
            """UPDATE crew_claims SET state = 'active', epoch = ?, lease_expires_at = ?, granted_at = ?, queue_pos = NULL,
                   version = version + 1, updated_at = ? WHERE id = ? AND state = 'queued'""",
            (epoch, lease, now_iso(now), now_iso(now), q["id"]),
        )
        row = await _reload(conn, str(q["id"]))
        await _emit_claim(tx, crew_id, "claim.granted", row, Actor.system(), f"queued claim granted to {principal.name}")
        if session is not None:
            await raise_inbox_item(
                tx,
                crew_id,
                audience="session",
                recipient=str(session["id"]),
                kind="claim_granted",
                title=f"Your queued claim on {await _target_label(conn, row)} was granted",
                dedupe_key=f"granted:{row['id']}",
                ref_type="claim",
                ref_id=str(row["id"]),
            )
        granted.append(str(q["id"]))
    _wake(ops, granted)
    return granted


async def promote_queue_in(tx: EventTx, crew_id: str) -> list[str]:
    """:func:`promote_queue` for services that hold only the transaction (sessions, tasks, close-out, reaper).

    Every path that ends, expires or releases a claim calls this in the same transaction, so a
    queued claim becomes active as soon as its blocker ends (§5.1 ``queued ─blocker ends─▶ active``).
    """
    return await promote_queue(CrewOps(tx._log), tx, crew_id)


async def _end(tx: EventTx, row: Mapping[str, Any], state: str, end_reason: str) -> dict[str, Any]:
    stamp = now_iso()
    await tx.conn.execute(
        """UPDATE crew_claims SET state = ?, ended_at = ?, end_reason = ?, offered_to = NULL, offer_expires_at = NULL,
               queue_pos = NULL, version = version + 1, updated_at = ? WHERE id = ?""",
        (state, stamp, end_reason, stamp, row["id"]),
    )
    return await _reload(tx.conn, str(row["id"]))


async def _reserve(
    tx: EventTx,
    row: Mapping[str, Any],
    reason: str,
    *,
    reserved_for: str | None,
    settings: Mapping[str, Any],
    baton_ref: str | None = None,
) -> dict[str, Any]:
    now = utcnow()
    expires = None if row.get("task_id") else now_iso(now + timedelta(seconds=int(settings["reserve_ttl_s"])))
    await tx.conn.execute(
        """UPDATE crew_claims SET state = 'reserved', reserve_reason = ?, reserved_for = ?, reserve_expires_at = ?,
               offered_to = NULL, offer_expires_at = NULL, baton_ref = COALESCE(?, baton_ref), version = version + 1,
               updated_at = ? WHERE id = ?""",
        (reason, reserved_for, expires, baton_ref, now_iso(now), row["id"]),
    )
    return await _reload(tx.conn, str(row["id"]))


async def _after_end(ops: CrewOps, tx: EventTx, crew_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
    from remembra.crew import collisions as CO

    await promote_queue(ops, tx, crew_id)
    for r in rows:
        await CO.auto_resolve(ops, tx, crew_id, claim_id=str(r["id"]))


async def release_claim(
    ops: CrewOps, claim: Mapping[str, Any], principal: Principal, *, baton: bool = False, note: str | None = None
) -> dict[str, Any]:
    """``POST /claims/{cid}/release {baton?}``: the holder ends its claim, or leaves it reserved as a baton."""
    crew_id = str(claim["crew_id"])
    actor = principal.actor()
    async with ops.log.transaction() as tx:
        row = await _reload(tx.conn, str(claim["id"]))
        accountable = (
            principal.kind == "session"
            and row.get("holder_kind") == "session"
            and await is_accountable_for(tx.conn, principal.session_id, row.get("holder_session_id"))
        )
        if not (_mine(row, principal) or accountable):
            raise CrewOpError(
                403,
                "not_holder",
                "Only the holder (or the session that started it, for a sub-agent) can release this claim;"
                " a human can override it.",
            )
        settings = load_settings((await crew_row(tx.conn, crew_id))["settings"])
        state = row["state"]
        label = await _target_label(tx.conn, row)
        if state == "queued":
            fresh = await _end(tx, row, "released", "cancelled")
            baton = False
        elif state in LIVE_HOLD and baton:
            fresh = await _reserve(tx, row, "baton", reserved_for=None, settings=settings)
        elif state in LIVE_HOLD or (
            state == "reserved" and row.get("reserved_for") in (None, principal.session_id, row.get("holder_session_id"))
        ):
            fresh = await _end(tx, row, "released", clip(note, 64) or "released")
            baton = False
        else:
            raise CrewOpError(409, "claim_not_live", f"This claim is {state}.")
        seq = await _emit_claim(
            tx,
            crew_id,
            "claim.released",
            fresh,
            actor,
            f"{principal.name} released {label}{' as a baton' if baton else ''}",
            baton=baton,
        )
        if baton:
            await raise_inbox_item(
                tx,
                crew_id,
                audience="crew",
                kind="baton_reserved",
                title=f"Baton on {label} is waiting for pickup",
                dedupe_key=f"baton:{fresh['id']}",
                ref_type="claim",
                ref_id=str(fresh["id"]),
                actor=actor,
            )
        await _after_end(ops, tx, crew_id, [fresh])
        return {"claim": claim_view(fresh), "seq": seq}


# ---------------------------------------------------------------------------
# Handover
# ---------------------------------------------------------------------------


async def handover(
    ops: CrewOps, claim: Mapping[str, Any], principal: Principal, *, to: str, note: str | None = None
) -> dict[str, Any]:
    """``POST /claims/{cid}/handover {to}``: offer a live claim to a named session (10 min to accept)."""
    crew_id = str(claim["crew_id"])
    async with ops.log.transaction() as tx:
        row = await _reload(tx.conn, str(claim["id"]))
        if not _mine(row, principal) or row["state"] != "active":
            raise CrewOpError(409, "not_holder", "Only the holder of an active claim can hand it over.")
        target = await _session(tx.conn, to) if S.is_id("session", to) else None
        if target is None or target["crew_id"] != crew_id:
            raise CrewOpError(422, "cross_crew_reference", "The referenced session is not part of this crew.")
        if target["id"] == principal.session_id or target["state"] in NOT_LIVE_SESSION_STATES:
            raise CrewOpError(422, "invalid_handover", "Hand over to another live session.")
        await check_taker_zones(tx.conn, [row], target, human_granted=False)
        now = utcnow()
        await tx.conn.execute(
            "UPDATE crew_claims SET state = 'offered', offered_to = ?, offer_expires_at = ?, version = version "
            "+ 1, updated_at = ? WHERE id = ?",
            (to, now_iso(now + timedelta(seconds=HANDOVER_TTL_S)), now_iso(now), row["id"]),
        )
        fresh = await _reload(tx.conn, str(row["id"]))
        label = await _target_label(tx.conn, fresh)
        seq = await _emit_claim(
            tx,
            crew_id,
            "claim.handover_offered",
            fresh,
            principal.actor(),
            f"{principal.name} offered {label} to {target['callsign']}",
            to_session=to,
        )
        await raise_inbox_item(
            tx,
            crew_id,
            audience="session",
            recipient=to,
            kind="handover_offer",
            title=f"{principal.name} offers you {label}",
            dedupe_key=f"handover:{fresh['id']}:{to}",
            ref_type="claim",
            ref_id=str(fresh["id"]),
            primary_action="accept",
            actor=principal.actor(),
        )
        return {"claim": claim_view(fresh), "seq": seq}


async def _record_baton(
    tx: EventTx,
    crew_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    from_session: str | None,
    to_session: str,
    kind: str,
    actor: Actor,
    offer_id: str | None = None,
) -> str:
    baton_id = new_id("baton")
    task_id = next((str(r["task_id"]) for r in rows if r.get("task_id")), None)
    baton_ref = next((str(r["baton_ref"]) for r in rows if r.get("baton_ref")), None)
    zones = [str(r["zone_id"]) for r in rows if r.get("zone_id")]
    res = await tx.emit(
        crew_id=crew_id,
        type="baton.passed",
        actor=actor,
        payload={
            "baton_id": baton_id,
            "task_id": task_id,
            "from_session": from_session,
            "to_session": to_session,
            "kind": kind,
            "handoff_id": None,
            "zones": zones[:20],
            "baton_ref": baton_ref,
            "restored": None,
        },
        summary=f"baton passed ({kind})",
        refs={"task_id": task_id, "session_id": to_session},
    )
    await tx.conn.execute(
        """INSERT INTO crew_batons (id, crew_id, task_id, from_session, to_session, kind, offer_id, baton_ref,
            zone_ids, seq, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (baton_id, crew_id, task_id, from_session, to_session, kind, offer_id, baton_ref, dumps(zones), res.seq, now_iso()),
    )
    return baton_id


async def _take_over(
    tx: EventTx, crew_id: str, row: Mapping[str, Any], session: Mapping[str, Any], settings: Mapping[str, Any], source: str
) -> dict[str, Any]:
    target = await Target.of_row(tx.conn, row)
    epoch = await next_epoch(tx.conn, crew_id, target)
    now = utcnow()
    await tx.conn.execute(
        """UPDATE crew_claims SET state = 'active', holder_kind = 'session', holder_session_id = ?, holder_user_id = ?,
               holder_agent_id = ?, epoch = ?, source = ?, lease_expires_at = ?, reserve_reason = NULL, reserved_for = NULL,
               reserve_expires_at = NULL, offered_to = NULL, offer_expires_at = NULL, granted_at = ?, unconfirmed = 0,
               version = version + 1, updated_at = ? WHERE id = ?""",
        (
            session["id"],
            session["user_id"],
            session["agent_id"],
            epoch,
            source,
            _lease(settings, now, source=source),
            now_iso(now),
            now_iso(now),
            row["id"],
        ),
    )
    return await _reload(tx.conn, str(row["id"]))


async def accept_handover(ops: CrewOps, claim: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
    crew_id = str(claim["crew_id"])
    async with ops.log.transaction() as tx:
        row = await _reload(tx.conn, str(claim["id"]))
        session = principal.session
        if session is None or row["state"] != "offered" or row.get("offered_to") != principal.session_id:
            raise CrewOpError(409, "not_offered", "This claim is not offered to this session.")
        if _ts(row.get("offer_expires_at")) and _ts(row["offer_expires_at"]) <= utcnow():  # type: ignore[operator]
            raise CrewOpError(409, "offer_expired", "The handover offer expired.")
        session = await _session(tx.conn, str(session["id"])) or session
        _session_live(session)
        await require_seat(tx.conn, crew_id, session, ops.limits)
        await check_taker_zones(tx.conn, [row], session, human_granted=False)
        settings = load_settings((await crew_row(tx.conn, crew_id))["settings"])
        if row["mode"] == "exclusive" and await _over_cap(tx.conn, session, settings):
            raise CrewOpError(409, "claim_cap", "Claim limit reached: release a claim before accepting.")
        from_session = row.get("holder_session_id")
        fresh = await _take_over(tx, crew_id, row, session, settings, "handover")
        actor = principal.actor()
        seq = await _emit_claim(
            tx,
            crew_id,
            "claim.handover_accepted",
            fresh,
            actor,
            f"{principal.name} accepted {await _target_label(tx.conn, fresh)}",
        )
        await _record_baton(
            tx, crew_id, [fresh], from_session=from_session, to_session=str(session["id"]), kind="handover", actor=actor
        )
        await resolve_inbox_items(tx, crew_id, ref_ids=[str(fresh["id"])], kinds=["handover_offer"], actor=actor)
        return {"claim": claim_view(fresh), "seq": seq}


async def decline_handover(
    ops: CrewOps, claim: Mapping[str, Any], principal: Principal, *, reason: str = "declined"
) -> dict[str, Any]:
    """Decline (the offered session) or time out (system): the claim stays with the original holder."""
    crew_id = str(claim["crew_id"])
    async with ops.log.transaction() as tx:
        row = await _reload(tx.conn, str(claim["id"]))
        if row["state"] != "offered" or (principal.kind != "system" and row.get("offered_to") != principal.session_id):
            raise CrewOpError(409, "not_offered", "This claim is not offered to this session.")
        await tx.conn.execute(
            "UPDATE crew_claims SET state = 'active', offered_to = NULL, offer_expires_at = NULL, version = "
            "version + 1, updated_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
        fresh = await _reload(tx.conn, str(row["id"]))
        seq = await _emit_claim(
            tx,
            crew_id,
            "claim.handover_declined",
            fresh,
            principal.actor(),
            f"handover of {await _target_label(tx.conn, fresh)} {reason}",
            reason=reason,
        )
        await resolve_inbox_items(tx, crew_id, ref_ids=[str(row["id"])], kinds=["handover_offer"])
        return {"claim": claim_view(fresh), "seq": seq}


# ---------------------------------------------------------------------------
# Adopt (D33) and baton offers
# ---------------------------------------------------------------------------


async def record_baton_offer(
    ops: CrewOps, tx: EventTx, crew_id: str, claim_id: str, to_session: str, *, via: str = "brief"
) -> dict[str, Any]:
    """Record that a reserved baton was offered to ``to_session`` (SessionStart brief, a human, or reserved_for).

    The offer is what authorises a cross-checkout adopt (D33). Idempotent per (claim, session).
    """
    if via not in S.OFFER_VIA:
        raise ValueError(f"via must be one of {S.OFFER_VIA}")
    claim = await _reload(tx.conn, claim_id)
    if claim["crew_id"] != crew_id or claim["state"] != "reserved":
        raise CrewOpError(409, "not_reserved", "Only a reserved baton can be offered.")
    existing = await fetchone(
        tx.conn, "SELECT * FROM crew_baton_offers WHERE claim_id = ? AND to_session = ?", (claim_id, to_session)
    )
    if existing is not None:
        if via == "human" and existing["via"] != "human" and existing["used_at"] is None:
            # a human hand-over upgrades an earlier brief offer: it is now a human grant (protected zones, D33)
            await tx.conn.execute("UPDATE crew_baton_offers SET via = 'human' WHERE id = ?", (existing["id"],))
            fresh = await fetchone(tx.conn, "SELECT * FROM crew_baton_offers WHERE id = ?", (existing["id"],))
            assert fresh is not None
            return fresh
        return existing
    offer_id = new_id("offer")
    await tx.conn.execute(
        "INSERT INTO crew_baton_offers (id, crew_id, claim_id, task_id, to_session, via, created_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?)",
        (offer_id, crew_id, claim_id, claim.get("task_id"), to_session, via, now_iso()),
    )
    await tx.emit(
        crew_id=crew_id,
        type="claim.offered_in_brief",
        actor=Actor.system(),
        payload={
            "offer": {"id": offer_id, "claim_id": claim_id, "task_id": claim.get("task_id"), "to_session": to_session, "via": via}
        },
        summary="baton offered to a session",
        refs={"claim_id": claim_id, "session_id": to_session, "task_id": claim.get("task_id")},
    )
    row = await fetchone(tx.conn, "SELECT * FROM crew_baton_offers WHERE id = ?", (offer_id,))
    assert row is not None
    return row


def _same_checkout(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    if a is None or b is None:
        return False
    if a.get("checkout_fp") and a.get("checkout_fp") == b.get("checkout_fp"):
        return True
    return bool(a.get("worktree_id")) and a.get("worktree_id") == b.get("worktree_id")


async def check_taker_zones(
    conn: aiosqlite.Connection,
    claims: Sequence[Mapping[str, Any]],
    session: Mapping[str, Any],
    *,
    human_granted: bool,
) -> None:
    """The zone rules (§5.1) for a session taking over claims it did not request: adopt, handover, task adopt.

    A baton never launders a zone rule: the built-in ``crew-policy`` zone is never taken; a
    frozen zone is not taken while frozen; ``reserve_for`` still needs that **key-verified**
    agent; a ``protected`` zone moves only when a human offered or assigned it to this session
    (``human_granted``). Raises :class:`CrewOpError` (403/423) naming the zone slug only.
    """
    zone_ids = sorted({str(c["zone_id"]) for c in claims if c.get("zone_id")})
    if not zone_ids:
        return
    marks = ",".join("?" for _ in zone_ids)
    for zone in await fetchall(conn, f"SELECT * FROM crew_zones WHERE id IN ({marks}) ORDER BY slug", zone_ids):
        if zone["builtin"]:
            raise CrewOpError(423, "crew_policy", "The crew-policy zone is never claimable.")
        if is_frozen(zone):
            raise CrewOpError(423, "frozen", f"Zone {zone['slug']} is frozen by a human.")
        if zone.get("reserve_for") and (session.get("agent_id") != zone["reserve_for"] or not session.get("agent_verified")):
            raise CrewOpError(403, "reserved_for_agent", f"Zone {zone['slug']} is reserved for a key-verified agent.")
        if zone["protected"] and not human_granted:
            raise CrewOpError(423, "protected", f"Zone {zone['slug']} is protected; only a human can hand it to this session.")


async def adopt(ops: CrewOps, claim: Mapping[str, Any], principal: Principal, *, task_id: str | None = None) -> dict[str, Any]:
    """``POST /claims/{cid}/adopt``: take over a reserved baton (and its task's sibling batons) when authorised (D33).

    Authorised when: the baton is ``reserved_for`` this session; this session holds a recorded
    offer; it works in the same checkout as the previous holder; or the human-enabled
    ``adopt_on_first_write`` setting is on (never for a human hold). Otherwise 403
    ``not_offered`` (the message never teaches the adopt command).
    """
    crew_id = str(claim["crew_id"])
    session = principal.session
    if session is None:
        raise CrewOpError(
            422, "session_required", "Adopt is done by a session; a human hands batons over with override transfer."
        )
    actor = principal.actor()
    async with ops.log.transaction() as tx:
        conn = tx.conn
        session = await _session(conn, str(session["id"])) or session
        _session_live(session)
        await require_seat(conn, crew_id, session, ops.limits)
        row = await _reload(conn, str(claim["id"]))
        if row["state"] != "reserved":
            raise CrewOpError(409, "not_reserved", f"This claim is {row['state']}, not a reserved baton.")
        if task_id is not None and task_id != row.get("task_id"):
            raise CrewOpError(422, "cross_crew_reference", "The task does not match this baton.")
        settings = load_settings((await crew_row(conn, crew_id))["settings"])
        if row.get("holder_session_id") == session["id"] and row.get("reserved_for") in (None, session["id"]):
            fresh = await _retake(ops, tx, crew_id, row, Principal.for_session(session), settings, utcnow())
            return {"claims": [claim_view(fresh)], "kind": "retake", "baton_id": None}
        group = [row]
        if row.get("task_id"):
            group = await fetchall(
                conn,
                """SELECT * FROM crew_claims WHERE crew_id = ? AND task_id = ? AND state = 'reserved'
                     AND COALESCE(holder_session_id, '') = COALESCE(?, '') ORDER BY created_at""",
                (crew_id, row["task_id"], row.get("holder_session_id")),
            )
        ids = [str(r["id"]) for r in group]
        marks = ",".join("?" for _ in ids)
        offer = await fetchone(
            conn,
            f"SELECT * FROM crew_baton_offers WHERE claim_id IN ({marks}) AND to_session = ?"
            " AND used_at IS NULL ORDER BY CASE via WHEN 'human' THEN 0 WHEN 'reserved_for' THEN 1 ELSE 2 END, created_at",
            (*ids, session["id"]),
        )
        from_session = await _session(conn, row.get("holder_session_id"))
        human_hold = row.get("reserve_reason") == "human_hold"
        if row.get("reserved_for") == session["id"]:
            kind = "reserved_for"
        elif offer is not None:
            kind = {"brief": "adopt", "human": "human_assign", "reserved_for": "reserved_for"}[str(offer["via"])]
        elif not human_hold and _same_checkout(session, from_session):
            kind = "same_checkout"
        elif not human_hold and settings.get("adopt_on_first_write"):
            kind = "first_write"
        else:
            raise CrewOpError(
                403,
                "not_offered",
                "This baton was not offered to this session. Work elsewhere, or ask the owner to hand it over "
                "from the dashboard.",
            )
        # the baton does not bypass the zone: reserve_for, protected (human grant only), frozen, crew-policy
        human_granted = (offer is not None and offer["via"] == "human") or row.get("reserved_for") == session["id"]
        await check_taker_zones(conn, group, session, human_granted=human_granted)
        exclusive = sum(1 for r in group if r["mode"] == "exclusive")
        if exclusive and await _over_cap(conn, session, settings, adding=exclusive):
            raise CrewOpError(409, "claim_cap", "Claim limit reached: release a claim before adopting this baton.")
        cross = kind != "same_checkout" and not _same_checkout(session, from_session)
        adopted: list[dict[str, Any]] = []
        for r in group:
            fresh = await _take_over(tx, crew_id, r, session, settings, "adopt")
            adopted.append(fresh)
            await _emit_claim(
                tx,
                crew_id,
                "claim.adopted",
                fresh,
                actor,
                f"{session['callsign']} adopted {await _target_label(conn, fresh)}{' across checkouts' if cross else ''}",
                cross_checkout=cross,
                from_session=r.get("holder_session_id"),
            )
        if offer is not None:
            await conn.execute("UPDATE crew_baton_offers SET used_at = ? WHERE id = ?", (now_iso(), offer["id"]))
        baton_id = await _record_baton(
            tx,
            crew_id,
            adopted,
            from_session=row.get("holder_session_id"),
            to_session=str(session["id"]),
            kind=kind,
            actor=actor,
            offer_id=str(offer["id"]) if offer else None,
        )
        refs = [*ids, *([str(row["task_id"])] if row.get("task_id") else [])]
        await resolve_inbox_items(
            tx, crew_id, ref_ids=refs, kinds=["baton_reserved", "baton_available", "baton_waiting"], actor=actor
        )
        if cross:
            ops.audit(
                principal.user_id, "crew.adopt_cross_checkout", baton_id, {"claims": ids, "kind": kind, "session": session["id"]}
            )
        return {"claims": [claim_view(r) for r in adopted], "kind": kind, "baton_id": baton_id, "cross_checkout": cross}


# ---------------------------------------------------------------------------
# Human override (§5.9)
# ---------------------------------------------------------------------------


async def override(
    ops: CrewOps, claim: Mapping[str, Any], human: Principal, *, action: str, to: str | None, reason: str
) -> dict[str, Any]:
    """``POST /claims/{cid}/override {revoke|transfer|hold}`` (human principal, step-up; route-enforced)."""
    if action not in ("revoke", "transfer", "hold"):
        raise CrewOpError(422, "invalid_override", "action must be revoke, transfer or hold.")
    crew_id = str(claim["crew_id"])
    actor = human.actor()
    note = clip(reason, 280) or action
    async with ops.log.transaction() as tx:
        conn = tx.conn
        row = await _reload(conn, str(claim["id"]))
        if row["state"] not in LIVE:
            raise CrewOpError(409, "claim_not_live", f"This claim is {row['state']}.")
        settings = load_settings((await crew_row(conn, crew_id))["settings"])
        displaced = row.get("holder_session_id")
        label = await _target_label(conn, row)
        if action == "revoke":
            fresh = await _end(tx, row, "revoked", "revoked")
            seq = await _emit_claim(tx, crew_id, "claim.revoked", fresh, actor, f"human revoked {label}", reason=note)
        elif action == "hold":
            fresh = await _reserve(tx, row, "human_hold", reserved_for=None, settings=settings)
            seq = await _emit_claim(
                tx, crew_id, "claim.reserved", fresh, actor, f"human put {label} on hold", reason="human_hold"
            )
        else:
            target = await _session(conn, to) if to and S.is_id("session", to) else None
            if target is None or target["crew_id"] != crew_id:
                raise CrewOpError(422, "cross_crew_reference", "The referenced session is not part of this crew.")
            if target["state"] in NOT_LIVE_SESSION_STATES:
                raise CrewOpError(422, "invalid_override", "Transfer to a live session.")
            fresh = await _take_over(tx, crew_id, row, target, settings, "dashboard")
            seq = await _emit_claim(
                tx,
                crew_id,
                "claim.transferred",
                fresh,
                actor,
                f"human transferred {label} to {target['callsign']}",
                from_session=displaced,
                reason=note,
            )
            await _record_baton(
                tx, crew_id, [fresh], from_session=displaced, to_session=str(target["id"]), kind="human_assign", actor=actor
            )
            await raise_inbox_item(
                tx,
                crew_id,
                audience="session",
                recipient=str(target["id"]),
                kind="override_notice",
                title=f"A human handed you {label}",
                dedupe_key=f"override:{fresh['id']}:{target['id']}:{fresh['version']}",
                ref_type="claim",
                ref_id=str(fresh["id"]),
                actor=actor,
            )
        await tx.emit(
            crew_id=crew_id,
            type="human.override",
            actor=actor,
            payload={"action": action, "reason": note, "target_kind": "claim", "target_id": row["id"]},
            summary=f"human override: {action} {label}",
            refs={"claim_id": row["id"], "zone_id": row.get("zone_id")},
        )
        if displaced and displaced != (to or ""):
            await raise_inbox_item(
                tx,
                crew_id,
                audience="session",
                recipient=str(displaced),
                kind="override_notice",
                title=f"A human did {action} on your claim on {label}",
                dedupe_key=f"override:{row['id']}:{displaced}:{fresh['version']}",
                ref_type="claim",
                ref_id=str(row["id"]),
                actor=actor,
            )
        if action == "revoke":
            await _after_end(ops, tx, crew_id, [fresh])
        ops.audit(
            human.user_id, f"crew.claim_{action}", str(row["id"]), {"reason": note, "to": to, "api_key_id": human.api_key_id}
        )
        return {"claim": claim_view(fresh), "seq": seq}


# ---------------------------------------------------------------------------
# Zone-level helpers (freeze, archive) used by crew/zones.py
# ---------------------------------------------------------------------------


async def hold_zone_for_human(ops: CrewOps, tx: EventTx, crew_id: str, zone_id: str, human: Principal) -> dict[str, Any] | None:
    """Freeze ⇒ a human exclusive claim, unless an exclusive claim already holds the zone (the freeze still denies it)."""
    held = await fetchone(
        tx.conn,
        "SELECT id FROM crew_claims WHERE crew_id = ? AND zone_id = ? AND mode = 'exclusive' AND state IN "
        "('active','offered','reserved')",
        (crew_id, zone_id),
    )
    if held is not None:
        return None
    zone = await fetchone(tx.conn, "SELECT * FROM crew_zones WHERE id = ?", (zone_id,))
    settings = load_settings((await crew_row(tx.conn, crew_id))["settings"])
    row = await _insert_claim(
        tx, crew_id, Target(zone=zone), "exclusive", human, None, "active", "dashboard", "frozen", settings, utcnow()
    )
    await _emit_claim(
        tx, crew_id, "claim.granted", row, human.actor(), f"human holds zone {zone['slug'] if zone else ''} (frozen)"
    )
    return row


async def release_human_holds(ops: CrewOps, tx: EventTx, crew_id: str, zone_id: str, *, actor: Actor) -> list[str]:
    rows = await fetchall(
        tx.conn,
        "SELECT * FROM crew_claims WHERE crew_id = ? AND zone_id = ? AND holder_kind = 'human' AND state IN "
        "('active','offered','reserved')",
        (crew_id, zone_id),
    )
    out: list[str] = []
    for r in rows:
        fresh = await _end(tx, r, "released", "unfrozen")
        await _emit_claim(tx, crew_id, "claim.released", fresh, actor, "human hold released", baton=False)
        out.append(str(r["id"]))
    return out


async def notify_zone_holders(ops: CrewOps, tx: EventTx, crew_id: str, zone_id: str, title: str, actor: Actor) -> None:
    rows = await fetchall(
        tx.conn,
        "SELECT DISTINCT holder_session_id FROM crew_claims WHERE crew_id = ? AND zone_id = ? AND holder_session_id IS NOT NULL"
        " AND state IN ('active','offered','reserved','queued')",
        (crew_id, zone_id),
    )
    for r in rows:
        await raise_inbox_item(
            tx,
            crew_id,
            audience="session",
            recipient=str(r["holder_session_id"]),
            kind="override_notice",
            title=title,
            dedupe_key=f"zone-notice:{zone_id}:{r['holder_session_id']}:{now_iso()}",
            ref_type="zone",
            ref_id=zone_id,
            actor=actor,
        )


async def end_zone_claims(ops: CrewOps, tx: EventTx, crew_id: str, zone_id: str, *, actor: Actor, reason: str) -> list[str]:
    rows = await fetchall(
        tx.conn,
        "SELECT * FROM crew_claims WHERE crew_id = ? AND zone_id = ? AND state IN "
        "('requested','queued','active','offered','reserved')",
        (crew_id, zone_id),
    )
    for r in rows:
        fresh = await _end(tx, r, "released", reason)
        await _emit_claim(tx, crew_id, "claim.released", fresh, actor, f"claim ended: {reason}", baton=False)
    return [str(r["id"]) for r in rows]


# ---------------------------------------------------------------------------
# Session-level interface (WP-4)
# ---------------------------------------------------------------------------


async def renew_session_leases(
    conn: aiosqlite.Connection, crew_id: str, session_id: str, *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Heartbeat (server receipt time): extend the live claims' leases. No event (heartbeats are never events)."""
    stamp = now or utcnow()
    settings = load_settings((await crew_row(conn, crew_id))["settings"])
    lease = _lease(settings, stamp, source="session")
    await conn.execute(
        "UPDATE crew_claims SET lease_expires_at = ? WHERE crew_id = ? AND holder_session_id = ? AND state IN "
        "('active','offered')"
        " AND source != 'micro_lease'",
        (lease, crew_id, session_id),
    )
    rows = await fetchall(
        conn,
        "SELECT id, epoch, lease_expires_at FROM crew_claims WHERE crew_id = ? AND holder_session_id = ? AND "
        "state IN ('active','offered')",
        (crew_id, session_id),
    )
    return {str(r["id"]): {"epoch": int(r["epoch"]), "lease_expires_at": r["lease_expires_at"]} for r in rows}


async def reserve_session_claims(
    ops: CrewOps,
    tx: EventTx,
    crew_id: str,
    session_id: str,
    reason: str,
    *,
    baton_ref: str | None = None,
    reserved_for: str | None = None,
) -> list[str]:
    """Stall, lost, dirty leave, idle park or offline: every live hold becomes a reserved baton; queued claims are cancelled.

    ``idle`` and ``offline`` reservations are kept for the same session (it re-takes on activity);
    others are reserved for the next authorised pickup (D5, D33).
    """
    if reason not in S.RESERVE_REASONS:
        raise ValueError(f"reason must be one of {S.RESERVE_REASONS}")
    settings = load_settings((await crew_row(tx.conn, crew_id))["settings"])
    keep_for = reserved_for if reserved_for is not None else (session_id if reason in ("idle", "offline") else None)
    rows = await fetchall(
        tx.conn,
        "SELECT * FROM crew_claims WHERE crew_id = ? AND holder_session_id = ? AND state IN ('active','offered','queued')",
        (crew_id, session_id),
    )
    out: list[str] = []
    for r in rows:
        if r["state"] == "queued":
            fresh = await _end(tx, r, "released", "holder_gone")
            await _emit_claim(tx, crew_id, "claim.released", fresh, Actor.system(), "queued claim cancelled", baton=False)
            continue
        if r["source"] == "micro_lease":
            fresh = await _end(tx, r, "expired", reason)
            await _emit_claim(tx, crew_id, "claim.expired", fresh, Actor.system(), "micro-lease ended")
            continue
        fresh = await _reserve(tx, r, reason, reserved_for=keep_for, settings=settings, baton_ref=baton_ref)
        await _emit_claim(
            tx,
            crew_id,
            "claim.reserved",
            fresh,
            Actor.system(),
            f"{await _target_label(tx.conn, fresh)} reserved ({reason})",
            reason=reason,
        )
        out.append(str(r["id"]))
    await promote_queue(ops, tx, crew_id)
    return out


async def release_session_claims(
    ops: CrewOps, tx: EventTx, crew_id: str, session_id: str, *, reason: str, actor: Actor | None = None
) -> list[str]:
    """Clean leave or human "release all claims of session X": end every live claim of the session."""
    rows = await fetchall(
        tx.conn,
        "SELECT * FROM crew_claims WHERE crew_id = ? AND holder_session_id = ? AND state IN "
        "('queued','active','offered','reserved')"
        " AND (state != 'reserved' OR reserved_for IS NULL OR reserved_for = holder_session_id)",
        (crew_id, session_id),
    )
    ended: list[dict[str, Any]] = []
    for r in rows:
        fresh = await _end(tx, r, "released", clip(reason, 64) or "released")
        await _emit_claim(
            tx, crew_id, "claim.released", fresh, actor or Actor.system(), f"claim released ({clip(reason, 40)})", baton=False
        )
        ended.append(fresh)
    await _after_end(ops, tx, crew_id, ended)
    return [str(r["id"]) for r in ended]


async def retake_session_claims(ops: CrewOps, tx: EventTx, crew_id: str, session_id: str) -> list[str]:
    """Recovery: the same session is back and nobody adopted, so its reservations become active again (epoch+1)."""
    session = await _session(tx.conn, session_id)
    if session is None:
        return []
    settings = load_settings((await crew_row(tx.conn, crew_id))["settings"])
    rows = await fetchall(
        tx.conn,
        "SELECT * FROM crew_claims WHERE crew_id = ? AND holder_session_id = ? AND state = 'reserved' AND "
        "reserve_reason != 'human_hold'"
        " AND (reserved_for IS NULL OR reserved_for = ?)",
        (crew_id, session_id, session_id),
    )
    out: list[str] = []
    for r in rows:
        await _retake(ops, tx, crew_id, r, Principal.for_session(session), settings, utcnow())
        out.append(str(r["id"]))
    return out


# ---------------------------------------------------------------------------
# Sweeper: handover timeouts, lease expiry, reservation expiry, freeze expiry
# ---------------------------------------------------------------------------


async def sweep(ops: CrewOps, *, now: datetime | None = None, boot_at: datetime | None = None) -> dict[str, int]:
    """One pass of the claim timers (idempotent; each transition is a conditional update with its event).

    * offered handovers past 10 min → back to the holder (``claim.handover_declined{timeout}``);
    * leases past expiry (measured from ``max(lease, boot_at + lease_ttl)``, so a server restart
      never mass-expires) → ``reserved(offline, reserved_for=holder)``; micro-leases → ``expired``;
    * non-task reservations past ``reserve_expires_at`` → ``expired`` (task batons never expire, D5);
    * freezes past ``frozen_until`` → unfrozen.
    """
    from remembra.crew import zones as Z

    stamp = now or utcnow()
    iso = now_iso(stamp)
    counts = {"handover_timeouts": 0, "leases_expired": 0, "reservations_expired": 0, "freezes_expired": 0}
    conn = ops.db.conn
    for r in await fetchall(conn, "SELECT * FROM crew_claims WHERE state = 'offered' AND offer_expires_at < ?", (iso,)):
        try:
            await decline_handover(ops, r, Principal.system(), reason="timeout")
            counts["handover_timeouts"] += 1
        except CrewOpError:
            pass
    crews = await fetchall(
        conn,
        "SELECT DISTINCT crew_id FROM crew_claims WHERE (state IN ('active','offered') AND lease_expires_at < ?)"
        " OR (state = 'reserved' AND task_id IS NULL AND reserve_expires_at < ?)",
        (iso, iso),
    )
    for c in crews:
        crew_id = str(c["crew_id"])
        async with ops.log.transaction() as tx:
            settings = load_settings((await crew_row(tx.conn, crew_id))["settings"])
            grace = (boot_at + timedelta(seconds=int(settings["lease_ttl_s"]))) if boot_at else None
            changed = False
            for r in await fetchall(
                tx.conn,
                "SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ('active','offered') AND lease_expires_at < ?",
                (crew_id, iso),
            ):
                if grace is not None and stamp < grace and r["source"] != "micro_lease":
                    continue
                if r["source"] == "micro_lease":
                    fresh = await _end(tx, r, "expired", "lease_expired")
                    await _emit_claim(tx, crew_id, "claim.expired", fresh, Actor.system(), "micro-lease expired")
                else:
                    fresh = await _reserve(tx, r, "offline", reserved_for=r.get("holder_session_id"), settings=settings)
                    await _emit_claim(
                        tx,
                        crew_id,
                        "claim.reserved",
                        fresh,
                        Actor.system(),
                        f"{await _target_label(tx.conn, fresh)} reserved (lease expired)",
                        reason="offline",
                    )
                counts["leases_expired"] += 1
                changed = True
            for r in await fetchall(
                tx.conn,
                "SELECT * FROM crew_claims WHERE crew_id = ? AND state = 'reserved' AND task_id IS NULL AND "
                "reserve_expires_at < ?",
                (crew_id, iso),
            ):
                fresh = await _end(tx, r, "expired", "reserve_ttl")
                await _emit_claim(
                    tx,
                    crew_id,
                    "claim.expired",
                    fresh,
                    Actor.system(),
                    f"reservation on {await _target_label(tx.conn, fresh)} expired",
                )
                await resolve_inbox_items(
                    tx, crew_id, ref_ids=[str(r["id"])], kinds=["baton_reserved", "baton_available", "baton_waiting"]
                )
                counts["reservations_expired"] += 1
                changed = True
            if changed:
                await promote_queue(ops, tx, crew_id)
    for z in await fetchall(
        conn, "SELECT * FROM crew_zones WHERE frozen_by IS NOT NULL AND frozen_until IS NOT NULL AND frozen_until < ?", (iso,)
    ):
        await Z.unfreeze_zone(ops, z, Principal.system(), reason="freeze expired")
        counts["freezes_expired"] += 1
    return counts


SWEEP_INTERVAL_S: Final = 30.0


async def sweep_loop(ops: CrewOps, *, interval_s: float = SWEEP_INTERVAL_S, boot_at: datetime | None = None) -> None:
    started = boot_at or utcnow()
    while True:
        try:
            await sweep(ops, boot_at=started)
        except Exception as e:  # never let one bad row stop the timers
            log.error("crew_claims_sweep_failed", error_type=type(e).__name__, error=str(e)[:200])
        await asyncio.sleep(interval_s)


def register_hooks() -> None:
    """Startup hook ``crew.claims`` (order 45): runs :func:`sweep_loop` on the app's task registry."""
    from remembra.crew import startup

    async def start(app: Any, rt: Any) -> None:
        from remembra.crew import collisions

        event_log = getattr(app.state, "crew_events", None)
        if event_log is None:
            raise RuntimeError("crew.claims needs app.state.crew_events (crew.bus hook)")
        # heartbeat footprints → collision detection (§5.3), in the heartbeat transaction
        collisions.register_heartbeat_sink()
        ops = CrewOps(event_log)
        startup._spawn(app, rt, sweep_loop(ops), "crew-claims-sweeper")

    async def stop(app: Any, rt: Any) -> None:
        await startup._cancel(rt, "crew-claims-sweeper")

    startup.add_hook("crew.claims", order=45, start=start, stop=stop)


register_hooks()


# ---------------------------------------------------------------------------
# Server guard (§5.2 via the shared gatecore)
# ---------------------------------------------------------------------------


class _ServerFs:
    """The server has no working tree: paths are what the request says; existence comes from footprints."""

    def __init__(self, known: set[str], dirs: set[str]) -> None:
        self.known = known
        self.dirs = dirs

    def realpath(self, path: str) -> str:
        return posixpath.normpath(path)

    def exists(self, path: str) -> bool:
        return posixpath.normpath(path) in self.known

    def is_dir(self, path: str) -> bool:
        return posixpath.normpath(path) in self.dirs

    def inode(self, path: str) -> tuple[int, int, int] | None:
        return None

    def read_text(self, path: str, limit: int = 262_144) -> str | None:
        return None


def _toplevel(session: Mapping[str, Any]) -> str:
    wt = session.get("worktree_id")
    return f"{SERVER_ROOT}/wt-{wt}" if wt else f"{SERVER_ROOT}/s-{session['id']}"


def _session_view(s: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": s["id"],
        "callsign": s["callsign"],
        "agent_id": s["agent_id"],
        "state": s["state"],
        "host_id": s.get("host_id"),
        "adapter_enforcement": s.get("adapter_enforcement") or "advisory",
        "worktree_id": s.get("worktree_id"),
    }


async def guard_snapshot(conn: aiosqlite.Connection, crew_id: str, caller: Mapping[str, Any]) -> dict[str, Any]:
    """A ``LocalSnapshot``-shaped view for gatecore: one synthetic checkout per worktree (repo-relative paths map to it)."""
    crew = await crew_row(conn, crew_id)
    settings = load_settings(crew["settings"])
    now = utcnow()
    since = now_iso(now - timedelta(hours=24))
    sessions = await fetchall(
        conn, "SELECT * FROM crew_sessions WHERE crew_id = ? AND (state != 'ended' OR ended_at >= ?)", (crew_id, since)
    )
    if not any(s["id"] == caller["id"] for s in sessions):
        sessions.append(dict(caller))
    marks = ",".join("?" for _ in LIVE)
    claims = await fetchall(conn, f"SELECT * FROM crew_claims WHERE crew_id = ? AND state IN ({marks})", (crew_id, *LIVE))
    zones = await load_zone_rows(conn, crew_id)
    tasks = await fetchall(
        conn, "SELECT id, number FROM crew_tasks WHERE crew_id = ? AND status NOT IN ('done','cancelled')", (crew_id,)
    )
    offers = await fetchall(
        conn,
        "SELECT id, claim_id, task_id, to_session, via FROM crew_baton_offers WHERE crew_id = ? AND used_at IS NULL",
        (crew_id,),
    )
    session_ids = {str(s["id"]) for s in sessions}
    fps = await fetchall(
        conn,
        "SELECT session_id, worktree_id, path, state, attribution FROM crew_footprints WHERE crew_id = ? AND "
        "state IN ('dirty','committed')",
        (crew_id,),
    )
    checkouts: list[dict[str, Any]] = []
    for s in sessions:
        if s["state"] == "ended" and s["id"] != caller["id"]:
            continue
        top = _toplevel(s)
        checkouts.append(
            {
                "toplevel": top,
                "worktree_id": s.get("worktree_id") or f"s-{s['id']}",
                "git_common_dir": top + "/.git",
                "case_insensitive": False,
                "session_id": s["id"],
            }
        )
    return {
        "crew": {"id": crew_id, "project_id": crew["project_id"], "enforcement": settings["enforcement"]},
        "settings": {
            "enforcement": settings["enforcement"],
            "undeclared_policy": settings["undeclared_policy"],
            "auto_claim": settings["auto_claim"],
            "auto_claim_leaf_only": settings["auto_claim_leaf_only"],
            "max_exclusive_claims_per_session": settings["max_exclusive_claims_per_session"],
            "interactive_override": False,
            "fail_closed_zones": settings["fail_closed_zones"],
            "lease_ttl_s": settings["lease_ttl_s"],
        },
        "sessions": [_session_view(s) for s in sessions],
        "claims": [claim_view(c, now) for c in claims],
        "zones": [zone_view(z) for z in zones],
        "commons": await active_commons(conn, crew_id),
        "ignore": await active_ignore(conn, crew_id),
        "tasks": [{"id": t["id"], "number": t["number"]} for t in tasks],
        "offers": offers,
        "footprints": [
            {**f, "worktree_id": f.get("worktree_id") or f"s-{f['session_id']}"}
            for f in fps
            if str(f["session_id"]) in session_ids
        ],
        "checkouts": checkouts,
        "synced_at": now_iso(now),
        "server_time": now_iso(now),
        "skew_s": 0.0,
        "host_id": None,
        "as_of_seq": int(crew["last_seq"]),
    }


def _norm_path(raw: str) -> str:
    rel = G.normalize_rel(str(raw))
    if not S.is_path_rel(rel) or rel == "." or rel.startswith("../"):
        raise CrewOpError(422, "invalid_path", "Guard paths must be repo-relative (no leading /, ~ or '..').")
    return rel


def _calls(
    op: str, paths: Sequence[str], command_tokens: Sequence[str] | None, mcp_tool: str | None, top: str
) -> list[tuple[str, dict[str, Any]]]:
    if op == "write":
        return [("Write", {"file_path": f"{top}/{_norm_path(p)}"}) for p in paths]
    if op == "delete":
        return [("Bash", {"command": f"rm -f -- {shlex.quote(top + '/' + _norm_path(p))}"}) for p in paths]
    if op == "command":
        if not command_tokens:
            raise CrewOpError(422, "invalid_guard", "op=command needs command_tokens.")
        return [("Bash", {"command": shlex.join(list(command_tokens))})]
    if not mcp_tool or not mcp_tool.startswith("mcp__"):
        raise CrewOpError(422, "invalid_guard", "op=mcp needs an mcp__<server>__<tool> name.")
    if not paths:
        return [(mcp_tool, {})]
    return [(mcp_tool, {"path": f"{top}/{_norm_path(p)}"}) for p in paths]


_RANK: Final = {"deny": 3, "ask": 2, "warn": 1, "allow": 0}


async def _guard_blocked_event(tx: EventTx, crew_id: str, caller: Principal, verdict: G.Verdict, op: str, decision: str) -> None:
    """``guard.blocked`` (surface server), coalesced per (session, zone) per 5 min."""
    since = now_iso(utcnow() - timedelta(seconds=GUARD_BLOCK_COALESCE_S))
    rows = await fetchall(
        tx.conn,
        "SELECT payload FROM crew_events WHERE crew_id = ? AND session_id = ? AND type = 'guard.blocked' AND "
        "ts >= ? ORDER BY seq DESC LIMIT 50",
        (crew_id, caller.session_id, since),
    )
    zone = verdict.zone_slug
    if decision == "deny" and caller.session_id:
        from remembra.crew.alarms import note_guard_block

        # §10.3 false-deny storm: every deny counts, including the ones coalesced into an earlier event
        await note_guard_block(tx, crew_id, caller.session_id, caller.name, now=utcnow())
    if any(loads(r["payload"], {}).get("zone") == zone for r in rows):
        return
    target = verdict.target
    path_rel = target.display if target is not None and target.display and S.is_path_rel(target.display) else None
    holder = None
    if verdict.holder_session_id:
        s = await _session(tx.conn, verdict.holder_session_id)
        holder = s["callsign"] if s else None
    await tx.emit(
        crew_id=crew_id,
        type="guard.blocked",
        actor=caller.actor(),
        payload={
            "path_rel": path_rel,
            "zone": zone if zone and _SLUG_RE.fullmatch(zone) else None,
            "holder": holder,
            "rule": max(1, min(19, verdict.rule)),
            "op": op,
            "decision": "would_deny" if decision == "warn" else decision,
            "surface": "server",
            "coalesced": 1,
        },
        summary=f"{caller.name} {'denied' if decision == 'deny' else 'warned'} by guard rule {verdict.rule}"
        + (f" in zone {zone}" if zone else ""),
        severity="notice",
        refs={"session_id": caller.session_id},
    )


async def server_guard(
    ops: CrewOps,
    crew_id: str,
    caller: Principal,
    *,
    op: str,
    paths: Sequence[str],
    command_tokens: Sequence[str] | None = None,
    mcp_tool: str | None = None,
) -> dict[str, Any]:
    """``POST /crews/{id}/guard``: the §5.2 decision for a session, with the same gatecore as the hook gate.

    The project enforcement level applies (advisory MCP sessions ask exactly because they cannot be
    gated locally). Auto-claims (row 17) and serialize micro-leases (row 12) are made for real.
    Returns ``{decision, rule, variant, reasons[], auto_claimed[], blockers[], effects[], snapshot_seq}``.
    """
    if op not in S.GUARD_OPS:
        raise CrewOpError(422, "invalid_guard", f"op must be one of {', '.join(S.GUARD_OPS)}")
    session = caller.session
    if session is None:
        raise CrewOpError(422, "session_required", "The guard decides for a session.")
    conn = ops.db.conn
    async with ops.log.transaction() as tx:
        await ensure_policy_zone(tx, crew_id)
    session = await _session(conn, str(session["id"])) or session
    caller = Principal.for_session(session, api_key_id=caller.api_key_id)
    # an observe-only session (over the plan's live-session cap) is still denied by others' claims,
    # but never auto-claims: its write in a free zone is allowed without a claim (§12)
    seated = await has_claim_seat(conn, crew_id, session, ops.limits)
    snapshot = await guard_snapshot(conn, crew_id, session)
    level = snapshot["settings"]["enforcement"]
    top = _toplevel(session)
    calls = _calls(op, paths, command_tokens, mcp_tool, top)
    if level == "off":
        return {
            "decision": "allow",
            "rule": 0,
            "variant": "crew_off",
            "reasons": [],
            "auto_claimed": [],
            "blockers": [],
            "effects": [],
            "snapshot_seq": snapshot["as_of_seq"],
        }
    mode = "observe" if level == "observe" else "enforce"
    known = {
        f"{c['toplevel']}/{f['path']}"
        for f in snapshot["footprints"]
        for c in snapshot["checkouts"]
        if c["worktree_id"] == f["worktree_id"]
    }
    if op == "delete":
        known |= {f"{top}/{_norm_path(p)}" for p in paths}
    fs = _ServerFs(known, {f"{top}/{_norm_path(p)}" for p in paths if str(p).endswith("/")} if op != "command" else set())
    requested: dict[tuple[str | None, str | None], G.ClaimRequest] = {}

    def record(req: G.ClaimRequest) -> G.ClaimResult:
        if seated:
            requested[(req.zone_id, req.path_glob)] = req
        return G.ClaimResult("granted")

    def run(claim_fn: Any) -> list[G.Verdict]:
        return [
            G.evaluate(
                name,
                tool_input,
                snapshot=snapshot,
                caller=str(session["id"]),
                cwd=top,
                home=SERVER_HOME,
                mode=mode,
                fs=fs,
                claim=claim_fn,
                human="the owner",
            )
            for name, tool_input in calls
        ]

    verdicts = run(record)
    results: dict[tuple[str | None, str | None], G.ClaimResult] = {}
    auto_claimed: list[dict[str, Any]] = []
    for key, req in requested.items():
        try:
            outcome = await request_claim(
                ops,
                crew_id,
                caller,
                zone_id=req.zone_id,
                path_glob=req.path_glob,
                mode=req.mode,
                source="first_write",
                reason="auto-claim (server guard)",
            )
        except CrewOpError:
            # a zone rule refuses this session the claim (reserve_for, protected, frozen, task-for-parent):
            # the write is denied like any other conflict, never an error of the guard itself
            results[key] = G.ClaimResult("conflict", winner_session_id=None)
            continue
        if outcome.status in ("granted", "existing", "retaken") and outcome.claim is not None:
            results[key] = G.ClaimResult("granted", claim_id=str(outcome.claim["id"]))
            auto_claimed.append(claim_view(outcome.claim))
        else:
            winner = outcome.blockers[0].get("holder_session_id") if outcome.blockers else None
            results[key] = G.ClaimResult("cap" if outcome.error == "claim_cap" else "conflict", winner_session_id=winner)
    if requested:
        snapshot = await guard_snapshot(conn, crew_id, session)
        verdicts = run(lambda req: results.get((req.zone_id, req.path_glob), G.ClaimResult("timeout")))
    worst = max(verdicts, key=lambda v: _RANK.get(v.decision, 0))
    decision = worst.decision
    effects = sorted({e for v in verdicts for e in v.effects})
    variant = worst.variant
    if not seated:
        effects = [e for e in effects if e != "claim.granted"]
        variant = "observe_only" if variant == "auto_claimed" else variant
    # row 12: serialize micro-lease and the schema claim for migrations, made for real
    extra_reasons: list[str] = []
    if decision in ("allow", "warn") and op in ("write", "delete", "mcp"):
        for v in verdicts:
            if v.target is None or not v.target.rel:
                continue
            extra: list[tuple[str | None, str | None, str]] = []
            if "micro_lease" in v.effects:
                extra.append((v.target.rel, None, "micro_lease"))
            if "schema_claim" in v.effects:
                extra.append((None, f"schema:{S.DEFAULT_SCHEMA_DB}", "first_write"))
            for glob, resource, source in extra:
                try:
                    outcome = await request_claim(
                        ops,
                        crew_id,
                        caller,
                        path_glob=glob,
                        resource=resource,
                        mode="exclusive",
                        source=source,
                        reason="serialized write",
                    )
                except CrewOpError:
                    outcome = ClaimOutcome("denied", None, [], "conflict")
                if outcome.claim is not None and outcome.status != "denied":
                    auto_claimed.append(claim_view(outcome.claim))
                elif mode == "enforce":
                    decision = "deny"
                    who = outcome.blockers[0].get("holder_callsign") if outcome.blockers else None
                    extra_reasons.append(
                        f"BLOCKED by Remembra Crew: {glob or resource} is being changed by {who or 'another session'} right now."
                        " Retry in a few minutes."
                    )
    reasons = list(dict.fromkeys([*(v.reason for v in verdicts if v.reason and v.decision != "allow"), *extra_reasons]))
    blockers: list[dict[str, Any]] = []
    for v in verdicts:
        if v.holder_session_id and v.decision in ("deny", "warn", "ask"):
            s = await _session(conn, v.holder_session_id)
            blockers.append(
                {
                    "claim_id": None,
                    "zone_id": next((z["id"] for z in snapshot["zones"] if z["slug"] == v.zone_slug), None),
                    "holder_session_id": v.holder_session_id,
                    "holder_callsign": s["callsign"] if s else None,
                    "task_id": None,
                    "reason": v.variant or "held",
                }
            )
    if decision in ("deny", "warn", "ask") or any(v.tamper_kinds for v in verdicts):
        async with ops.log.transaction() as tx:
            tamper = next((v for v in verdicts if v.tamper_kinds or v.rule == 2), None)
            if tamper is not None and tamper.decision == "deny":
                kind = tamper.tamper_kinds[0] if tamper.tamper_kinds else "crew_policy_write"
                if kind not in (*S.TAMPER_KINDS, "crew_policy_write"):
                    kind = "crew_policy_write"
                await tx.emit(
                    crew_id=crew_id,
                    type="guard.tamper_blocked",
                    actor=caller.actor(),
                    payload={"kind": kind, "surface": "mcp"},
                    summary=f"{caller.name} tamper attempt blocked: {kind} (server)",
                    severity="high",
                    refs={"session_id": caller.session_id},
                )
                await raise_inbox_item(
                    tx,
                    crew_id,
                    audience="project",
                    kind="tamper_blocked",
                    title=f"{caller.name} tried to change crew policy ({kind})",
                    dedupe_key=f"tamper:{caller.session_id}:{kind}",
                    ref_type="session",
                    ref_id=caller.session_id,
                    priority=1,
                    actor=caller.actor(),
                )
            elif decision in ("deny", "warn", "ask"):
                await _guard_blocked_event(tx, crew_id, caller, worst, op, decision)
    seq_row = await crew_row(conn, crew_id)
    return {
        "decision": decision,
        "rule": worst.rule,
        "variant": variant,
        "reasons": reasons[:10],
        "auto_claimed": auto_claimed,
        "blockers": blockers[:10],
        "effects": effects,
        "snapshot_seq": int(seq_row["last_seq"]),
    }


__all__ = [
    "ClaimOutcome",
    "Target",
    "accept_handover",
    "adopt",
    "authenticate_session",
    "check_taker_zones",
    "claim_view",
    "decline_handover",
    "handover",
    "has_claim_seat",
    "hash_session_token",
    "list_claims",
    "override",
    "promote_queue",
    "promote_queue_in",
    "record_baton_offer",
    "release_claim",
    "release_session_claims",
    "renew_session_leases",
    "request_claim",
    "require_seat",
    "reserve_session_claims",
    "retake_session_claims",
    "server_guard",
    "sweep",
    "wait_for_claim",
]
