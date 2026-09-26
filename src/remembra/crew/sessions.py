"""Crew sessions: join, heartbeat, leave, stall, human pause/resume/release-all, recovery (WP-4).

Spec anchors: §2 (crew session, callsign, member_key, presence, lease, reserved,
baton offer), §4.3 (join token rules, heartbeat natural key), §5.1 (claim state
machine: reserve, re-take, adopt; fencing), §5.4 (task stall and restore),
§5.6 (the "always a report" invariant), §8.2 (StopFailure, SessionEnd), §10.1
(presence), §10.2 (leases, batons, recovery), §12 (observe-only seats),
D5, D6, D14, D30, D31, D33.

One clock. Every time here is **server receipt time**; clients send ages
(``activity_age_s``), never absolute times (D31). The lease of a live claim is
the receipt time of the holder's last alive heartbeat + ``lease_ttl_s``; the
holder is fenced at ``lease_expires_at - 60 s`` (:func:`fence_horizon`).

Tokens. ``join`` returns a session token once and stores only its SHA-256.
A repeated join never returns the existing token: it answers 409
``session_exists`` unless the caller presents the current token (no token in the
answer), or crewd presents the host token of the host that owns the session,
which **rotates** the token (``session.token_rotated``). Token-bearing responses
are never cached (§4.3).

Baton (D5, D6, D30, D33). When a holder stops (quota, lost, dirty end,
release with baton, lease expiry, idle park) its claims become ``reserved``.
At ``join`` the server auto-adopts only (a) batons reserved in the **same
checkout** (setting ``auto_adopt: same_checkout``) and (b) batons reserved for
the session this one resumes (``resume_of``); every other adoptable baton is
**offered** (``crew_baton_offers`` via ``brief``, ``claim.offered_in_brief``),
and only an offered session may adopt it (WP-5 ``/claims/{id}/adopt``).
``offline`` (lease expiry), ``idle`` and ``human_hold`` reservations are never
offered or auto-adopted: only the holder re-takes them, or a human hands them on.

Ownership. This module writes rows of tables whose services other work
packages own (claims: WP-5, tasks/checkpoints/reports: WP-6, inbox: WP-7) only
for the transitions WP-4 owns: reserve / re-take / auto-adopt claims, stall /
restore tasks, synthesized checkpoints and stalled/partial reports, and the
baton inbox items. Each such write is one conditional statement inside the
caller's crew transaction, with its event.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import aiosqlite
import structlog

from remembra.crew import schemas
from remembra.crew.db import CrewDatabase
from remembra.crew.events import Actor, CrewEventLog, EventTx, format_ts, parse_ts, utc_now
from remembra.crew.hosts import (
    HOST_SILENT_AFTER_S,
    HostAuthFailed,
    authenticate_host,
    hash_token,
    host_view,
    mark_seen,
    tokens_match,
)
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS, CrewLimits, seat_for_join
from remembra.crew.settings import load_settings
from remembra.crew.store import CrewStore, crew_id_for, new_id
from remembra.relay.handoff import redact

log = structlog.get_logger(__name__)

SESSION_TOKEN_HEADER: Final = "X-Remembra-Crew-Session"  # one header for every crew route (integration)
SESSION_TOKEN_PREFIX: Final = "rcs_"

# Presence thresholds (§10.1). Settings carry the per-crew lease/idle/lost values.
ACTIVE_WINDOW_S: Final = 180
SESSION_SILENT_S: Final = HOST_SILENT_AFTER_S
MCP_QUIET_AFTER_S: Final = 20 * 60
MCP_LOST_AFTER_S: Final = 60 * 60
FENCE_MARGIN_S: Final = 60
HEARTBEAT_INTERVAL_S: Final = 60
CALLSIGN_REUSE_AFTER_S: Final = 24 * 3600
MAX_CALLSIGN_NUMBER: Final = 9999
# Lost sessions with nothing left to pick up are closed after this long (frees the callsign).
LOST_SESSION_CLOSE_AFTER_S: Final = 24 * 3600
# Notices for task-linked batons nobody picked up (D5): (age, label, priority).
BATON_WAITING_LEVELS: Final = ((24 * 3600, "24h", 2), (72 * 3600, "72h", 1))

LIVE_STATES: Final = schemas.LIVE_PRESENCE_STATES
# Reserve reasons another session may be offered or auto-adopt (D6, D33).
ADOPTABLE_REASONS: Final = ("lost", "quota", "ended_dirty", "baton")
# Reserve reasons the holder re-takes when it comes back (§5.1, §10.2).
RETAKE_REASONS: Final = ("lost", "quota", "offline", "idle", "ended_dirty", "baton")
QUOTA_ERRORS: Final = (*schemas.STOPFAILURE_QUOTA_ERRORS, "detected_limit")
UNFINISHED_TASK_STATUSES: Final = ("claimed", "in_progress")
# Reservations a holder makes for itself while it is merely away (lease expiry, idle park).
PARKED_REASONS: Final = ("idle", "offline")
BATON_INBOX_KINDS: Final = ("baton_available", "baton_reserved", "baton_waiting")
MAX_INJECT_CHARS: Final = schemas.TEXT_CAPS["pretool_context"]
MAX_DELTA_EVENTS: Final = 20

AGENT_CALLSIGN_PREFIX: Final[Mapping[str, str]] = {
    "claude-code": "cc",
    "claude": "cc",
    "claude-desktop": "cd",
    "codex": "codex",
    "codex-cli": "codex",
    "cursor": "cursor",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "qwen": "qwen",
    "qwen-code": "qwen",
    "kimi": "kimi",
    "kimi-cli": "kimi",
    "clawdbot": "clawd",
}
# Only the Claude Code hook adapter is verified before-write (§8.3, §8.5); every other lane is advisory.
ENFORCED_ADAPTERS: Final = frozenset({"claude-code"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """A request the session service refuses. The router maps it to a ``CrewError`` body."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def _conflict(code: str, message: str, **extra: Any) -> SessionError:
    return SessionError(409, code, message, **extra)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def new_session_token() -> str:
    return SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _age_s(now: datetime, ts: str | None, floor: datetime | None = None) -> int | None:
    """Seconds since ``ts`` measured from ``max(ts, floor)`` (boot grace, §10.1)."""
    if not ts:
        return None
    moment = parse_ts(ts)
    if floor is not None and floor > moment:
        moment = floor
    return max(0, int((now - moment).total_seconds()))


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _match(pattern: str, value: Any) -> Any:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def fence_horizon(lease_expires_at: str | None) -> str | None:
    """The time after which the holder must stop own-claim writes: ``lease_expires_at - 60 s`` (D31)."""
    if not lease_expires_at:
        return None
    return format_ts(parse_ts(lease_expires_at) - timedelta(seconds=FENCE_MARGIN_S))


def is_fenced(claim: Mapping[str, Any], now: datetime) -> bool:
    horizon = fence_horizon(claim.get("lease_expires_at"))
    return claim.get("state") == "active" and horizon is not None and now >= parse_ts(horizon)


def callsign_prefix(agent_id: str) -> str:
    """``cc`` for Claude Code, ``codex`` for Codex, otherwise a slug of the agent id (D2)."""
    known = AGENT_CALLSIGN_PREFIX.get(agent_id.lower())
    if known:
        return known
    slug = re.sub(r"[^a-z0-9]+", "-", agent_id.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "a" + slug
    return slug[:24].rstrip("-") or "agent"


def member_key_for(
    *, crew_id: str, user_id: str, agent_id: str, session_id: str, host_label: str | None, client_kind: str
) -> str:
    """``<agent>:<salted host label>:<8 hex>`` (D2). The host part is crewd's salted label, never a hostname."""
    agent = re.sub(r"[^A-Za-z0-9._-]", "-", agent_id)[:64]
    if not agent[:1].isalnum():
        agent = ("a" + agent)[:64]
    host = (
        host_label
        if host_label and re.fullmatch(r"[a-z0-9]{2,32}", host_label)
        else (client_kind if client_kind == "mcp" else "nohost")
    )
    digest = hashlib.sha256(f"{crew_id}:{user_id}:{agent_id}:{session_id}".encode()).hexdigest()[:8]
    return f"{agent}:{host}:{digest}"


def _dirty_count(facts: Mapping[str, Any]) -> int:
    for key in ("uncommitted_files", "dirty_files"):
        value = facts.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    value = facts.get("dirty_count")
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _unpushed_count(facts: Mapping[str, Any]) -> int:
    value = facts.get("unpushed_commits")
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _facts_hash(facts: Any) -> str:
    return hashlib.sha256(schemas.canonical_json(facts)).hexdigest()


def clean_facts(facts: Any) -> dict[str, Any]:
    """Redact secrets in every string of client facts (§11 redaction) and cap the size (16 KB)."""
    if not isinstance(facts, dict):
        return {}
    cleaned = redact(dict(facts))
    if not isinstance(cleaned, dict):
        return {}
    if schemas.json_size(cleaned) > schemas.MAX_CHECKPOINT_FACTS_BYTES:
        raise SessionError(422, "facts_too_large", f"facts are larger than {schemas.MAX_CHECKPOINT_FACTS_BYTES} bytes")
    return cleaned


# ---------------------------------------------------------------------------
# Views (closed shapes in schemas; every event payload is validated on emit)
# ---------------------------------------------------------------------------


def session_view(row: Mapping[str, Any]) -> dict[str, Any]:
    limit = None
    if row.get("limit_level") in schemas.LIMIT_LEVELS and row.get("limit_source") in schemas.LIMIT_SOURCES:
        pct = row.get("limit_pct")
        limit = {
            "level": row["limit_level"],
            "pct": float(pct) if isinstance(pct, int | float) else None,
            "source": row["limit_source"],
        }
    return {
        "id": row["id"],
        "callsign": row["callsign"],
        "agent_id": row["agent_id"],
        "member_key": row["member_key"],
        "agent_verified": bool(row.get("agent_verified")),
        "adapter": _clip(row.get("adapter"), 32),
        "adapter_enforcement": row.get("adapter_enforcement")
        if row.get("adapter_enforcement") in schemas.ADAPTER_ENFORCEMENT
        else "advisory",
        "client_kind": row.get("client_kind") if row.get("client_kind") in schemas.CLIENT_KINDS else None,
        "model": _clip(row.get("model"), 64),
        "host_id": row.get("host_id"),
        "state": row["state"],
        "quiet_reason": row.get("quiet_reason")
        if row.get("state") == "quiet" and row.get("quiet_reason") in schemas.QUIET_REASONS
        else None,
        "state_reason": _clip(row.get("state_reason"), 64),
        "stuck": bool(row.get("stuck")),
        "branch": _clip(row.get("branch"), 256),
        "head_commit": _match(schemas.SHA_PATTERN, row.get("head_commit")),
        "worktree_id": _clip(row.get("worktree_id"), 64),
        "githook_state": row.get("githook_state") if row.get("githook_state") in schemas.GITHOOK_STATES else None,
        "current_task_id": row.get("current_task_id") if schemas.is_id("task", row.get("current_task_id")) else None,
        "limit": limit,
        "joined_at": row["joined_at"],
        "last_activity_at": row.get("last_activity_at"),
        "ended_at": row.get("ended_at"),
        "end_reason": _clip(row.get("end_reason"), 64),
    }


def claim_view(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
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
        "source": row["source"] if row.get("source") in schemas.CLAIM_SOURCES else "adopt",
        "epoch": int(row.get("epoch") or 1),
        "unconfirmed": bool(row.get("unconfirmed")),
        "fenced": is_fenced(row, now),
        "lease_expires_at": row.get("lease_expires_at"),
        "reserve_reason": row.get("reserve_reason") if row.get("state") == "reserved" else None,
        "reserved_for": row.get("reserved_for") if row.get("state") == "reserved" else None,
        "offered_to": row.get("offered_to"),
        "queue_pos": row.get("queue_pos"),
        "baton_ref": row.get("baton_ref"),
        "granted_at": row.get("granted_at"),
        "version": int(row.get("version") or 1),
    }


_CRITERION_KEYS: Final = tuple(schemas.CRITERION.fields)


async def task_view(conn: aiosqlite.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    async with conn.execute(
        "SELECT depends_on_id FROM crew_task_deps WHERE crew_id = ? AND task_id = ? ORDER BY depends_on_id LIMIT 50",
        (row["crew_id"], row["id"]),
    ) as cur:
        deps = [str(r[0]) for r in await cur.fetchall()]
    acceptance = []
    for item in _loads(row.get("acceptance"), [])[:20]:
        if isinstance(item, dict):
            acceptance.append({k: item.get(k) for k in _CRITERION_KEYS if k in item})
    priority = row.get("priority")
    return {
        "id": row["id"],
        "number": int(row["number"]),
        "title": str(row["title"])[:200],
        "status": row["status"],
        "status_before_stall": row.get("status_before_stall")
        if row.get("status_before_stall") in schemas.TASK_STATUSES
        else None,
        "phase": _clip(row.get("phase"), 64),
        "priority": int(priority) if isinstance(priority, int) and 0 <= priority <= 4 else 2,
        "zone_ids": [z for z in _loads(row.get("zone_ids"), []) if schemas.is_id("zone", z)][:20],
        "owner_session_id": row.get("owner_session_id"),
        "owner_agent_id": row.get("owner_agent_id"),
        "reviewer": _clip(row.get("reviewer"), 128),
        "depends_on": deps,
        "acceptance": acceptance,
        "acceptance_locked": bool(row.get("acceptance_locked")),
        "started_head": _match(schemas.SHA_PATTERN, row.get("started_head")),
        "current_report_id": row.get("current_report_id"),
        "blocked_reason": _clip(row.get("blocked_reason"), 280),
        "version": int(row.get("version") or 1),
    }


def report_view(row: Mapping[str, Any]) -> dict[str, Any]:
    criteria = [c for c in _loads(row.get("criteria"), []) if isinstance(c, dict)][:20]
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "session_id": row.get("session_id"),
        "kind": row["kind"],
        "verdict": row.get("verdict") if row.get("verdict") in schemas.REPORT_VERDICTS else None,
        "review_state": row.get("review_state") if row.get("review_state") in schemas.REVIEW_STATES else None,
        "is_current": bool(row.get("is_current")),
        "superseded_reason": _clip(row.get("superseded_reason"), 64),
        "facts_source": row["facts_source"],
        "criteria": [{k: c.get(k) for k in ("id", "status", "source") if k in c} for c in criteria],
        "baton_ref": row.get("baton_ref"),
        "handoff_id": row.get("handoff_id"),
    }


def checkpoint_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "task_id": row.get("task_id"),
        "trigger": row["trigger"],
        "headline": str(row.get("headline") or row["trigger"])[:200],
        "facts_source": row["facts_source"],
    }


def inbox_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "audience": row["audience"],
        "recipient": row.get("recipient"),
        "kind": row["kind"],
        "origin": row["origin"],
        "ref_type": row.get("ref_type"),
        "ref_id": row.get("ref_id"),
        "priority": int(row.get("priority") or 2),
        "title": str(row["title"])[:200],
        "primary_action": row.get("primary_action"),
        "state": row["state"],
        "claimed_by": row.get("claimed_by"),
        "coalesced_count": int(row.get("coalesced_count") or 1),
    }


def offer_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "claim_id": row["claim_id"],
        "task_id": row.get("task_id"),
        "to_session": row["to_session"],
        "via": row["via"],
    }


def session_actor(row: Mapping[str, Any]) -> Actor:
    return Actor.session(
        row["id"],
        callsign=row["callsign"],
        agent_id=row["agent_id"],
        user_id=row["user_id"],
        verified=bool(row.get("agent_verified")),
    )


# ---------------------------------------------------------------------------
# Row access
# ---------------------------------------------------------------------------


async def _one(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    async with conn.execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cur.description]
        return dict(zip(names, tuple(row), strict=True))


async def _all(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    async with conn.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
        names = [d[0] for d in cur.description]
    return [dict(zip(names, tuple(r), strict=True)) for r in rows]


async def get_session(conn: aiosqlite.Connection, session_id: str) -> dict[str, Any] | None:
    if not schemas.is_id("session", session_id):
        return None
    return await _one(conn, "SELECT * FROM crew_sessions WHERE id = ?", (session_id,))


async def live_session_count(conn: aiosqlite.Connection, crew_id: str) -> int:
    marks = ", ".join("?" for _ in LIVE_STATES)
    row = await _one(
        conn, f"SELECT COUNT(*) AS n FROM crew_sessions WHERE crew_id = ? AND state IN ({marks})", (crew_id, *LIVE_STATES)
    )
    return int(row["n"]) if row else 0


async def has_seat(conn: aiosqlite.Connection, crew_id: str, session_id: str, max_live: int) -> bool:
    """True when the session holds one of the crew's ``max_live`` full seats (§12).

    Seats go to live sessions in join order, so a session that joined over the
    cap is observe-only (it cannot claim, adopt or be offered a baton) and gets
    a seat automatically once an earlier session ends. Claim services (WP-5)
    call this before granting.
    """
    row = await get_session(conn, session_id)
    if row is None or row["crew_id"] != crew_id or row["state"] not in LIVE_STATES:
        return False
    marks = ", ".join("?" for _ in LIVE_STATES)
    ahead = await _one(
        conn,
        f"""SELECT COUNT(*) AS n FROM crew_sessions
             WHERE crew_id = ? AND state IN ({marks}) AND (joined_at < ? OR (joined_at = ? AND id < ?))""",
        (crew_id, *LIVE_STATES, row["joined_at"], row["joined_at"], session_id),
    )
    return (int(ahead["n"]) if ahead else 0) < max_live


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

LimitsResolver = Callable[[str], Awaitable[CrewLimits]]
AuditSink = Callable[[Mapping[str, Any]], Awaitable[None]]
FootprintSink = Callable[[EventTx, Mapping[str, Any], Sequence[Mapping[str, Any]]], Awaitable[None]]


async def _self_hosted_limits(_owner: str) -> CrewLimits:
    return SELF_HOSTED_CREW_LIMITS


# Consumers of heartbeat footprints inside the heartbeat transaction (WP-5 collision detection
# registers here). The footprint rows themselves are always upserted by this module.
FOOTPRINT_SINKS: list[FootprintSink] = []


def register_footprint_sink(sink: FootprintSink) -> None:
    """Add a consumer of heartbeat footprints (called in the heartbeat transaction). Idempotent."""
    if sink not in FOOTPRINT_SINKS:
        FOOTPRINT_SINKS.append(sink)


@dataclass
class JoinRequest:
    agent_id: str
    agent_verified: bool
    project_id: str
    session_id: str
    adapter: str
    client_kind: str
    source: str
    host_id: str | None = None
    checkout_fp: str | None = None
    worktree_id: str | None = None
    branch: str | None = None
    head: str | None = None
    zones_sha: str | None = None
    model: str | None = None
    resume_of: str | None = None


@dataclass
class JoinResult:
    crew_id: str
    crew_created: bool
    session: dict[str, Any]
    session_token: str | None
    token_rotated: bool
    rejoined: bool
    observe_only: bool
    upgrade_hint: str | None
    batons_offered: list[dict[str, Any]]
    auto_adopted: list[dict[str, Any]]
    my_tasks: list[dict[str, Any]]
    seq: int

    def to_response(self) -> dict[str, Any]:
        return {
            "crew_id": self.crew_id,
            "crew_created": self.crew_created,
            "session_id": self.session["id"],
            "session_token": self.session_token,
            "token_rotated": self.token_rotated,
            "rejoined": self.rejoined,
            "callsign": self.session["callsign"],
            "member_key": self.session["member_key"],
            "session": session_view(self.session),
            "observe_only": self.observe_only,
            "upgrade_hint": self.upgrade_hint,
            "batons_offered": self.batons_offered,
            "auto_adopted": self.auto_adopted,
            "my_tasks": self.my_tasks,
            "next_heartbeat_after_s": HEARTBEAT_INTERVAL_S,
            "seq": self.seq,
        }


@dataclass
class _Effects:
    """What one stall/lost/leave transition did (for the event payloads and the response)."""

    claims_reserved: list[str] = field(default_factory=list)
    claims_released: list[str] = field(default_factory=list)
    tasks_stalled: list[str] = field(default_factory=list)
    report_ids: list[str] = field(default_factory=list)
    checkpoint_id: str | None = None
    handoff_id: str | None = None


class CrewSessions:
    """Session lifecycle over ``crew.db`` and the crew event log."""

    def __init__(
        self,
        db: CrewDatabase,
        event_log: CrewEventLog,
        *,
        clock: Callable[[], datetime] = utc_now,
        limits_for: LimitsResolver = _self_hosted_limits,
        audit: AuditSink | None = None,
        boot_at: datetime | None = None,
    ) -> None:
        self.db = db
        self.log = event_log
        self.store = CrewStore(db)
        self.clock = clock
        self.limits_for = limits_for
        self.audit = audit
        self.boot_at = boot_at or clock()

    @property
    def conn(self) -> aiosqlite.Connection:
        return self.db.conn

    def now(self) -> datetime:
        return self.clock()

    async def settings(self, crew_id: str) -> dict[str, Any]:
        row = await _one(self.conn, "SELECT settings FROM crews WHERE id = ?", (crew_id,))
        return load_settings(row["settings"] if row else None)

    async def _crew(self, crew_id: str) -> dict[str, Any]:
        row = await _one(self.conn, "SELECT * FROM crews WHERE id = ?", (crew_id,))
        if row is None:
            raise SessionError(404, "not_found", "Not found.")
        return row

    async def _audit(self, record: Mapping[str, Any]) -> None:
        sink = self.audit
        if sink is None:
            return

        async def write() -> None:
            try:
                await sink(record)
            except Exception as e:  # the crew transition already committed; never undo it for the audit copy
                log.error("crew_audit_write_failed", action=record.get("action"), error=str(e))

        if not self.db.after_commit(write):
            await write()

    # -- join -------------------------------------------------------------------------------

    async def join(
        self,
        *,
        user_id: str,
        req: JoinRequest,
        host_token: str | None = None,
        session_token: str | None = None,
    ) -> JoinResult:
        """Join (or re-join) the crew of ``(user_id, req.project_id)``, creating the crew on first join.

        The caller (router) has already checked that the key may use the project
        and has ``crew:write``; this creates the crew lazily (§2) — resolve is read-only.
        """
        crew_id = crew_id_for(user_id, req.project_id)
        existing_crew = await _one(self.conn, "SELECT owner_user_id FROM crews WHERE id = ?", (crew_id,))
        limits = await self.limits_for(existing_crew["owner_user_id"] if existing_crew else user_id)
        now = self.now()
        now_s = format_ts(now)
        async with self.log.transaction() as tx:
            crew, created = await self.store.ensure_crew(user_id, req.project_id)
            if created:
                settings = load_settings(crew["settings"])
                await tx.emit(
                    crew_id=crew_id,
                    type="crew.created",
                    actor=Actor.system(),
                    payload={
                        "crew": {
                            "id": crew_id,
                            "project_id": crew["project_id"],
                            "name": crew.get("name"),
                            "mode": "solo",
                            "enforcement": settings["enforcement"],
                            "settings_version": int(crew["settings_version"]),
                            "last_seq": int(crew["last_seq"]),
                        }
                    },
                    summary=f"crew created for project {_word(crew['project_id'])}",
                    now=now,
                )
            settings = await self.settings(crew_id)
            host = None
            if req.host_id:
                try:
                    host = await authenticate_host(tx.conn, user_id=user_id, host_id=req.host_id, token=host_token)
                except HostAuthFailed as e:
                    raise SessionError(403, "host_token_invalid", e.message) from None
            live_before = await live_session_count(tx.conn, crew_id)
            row = await _one(
                tx.conn,
                "SELECT * FROM crew_sessions WHERE crew_id = ? AND user_id = ? AND agent_id = ? AND session_id = ?",
                (crew_id, user_id, req.agent_id, req.session_id),
            )
            token: str | None = None
            rotated = False
            rejoined = row is not None
            if row is None:
                resume_row = await self._check_resume_of(tx, crew_id, user_id, req)
                token = new_session_token()
                row = await self._insert_session(tx, crew_id, user_id, req, host, token, now_s)
                if host is not None:
                    await self._announce_host(tx, crew_id, host, row, now)
                seated = await has_seat(tx.conn, crew_id, row["id"], limits.max_sessions_live)
                await tx.emit(
                    crew_id=crew_id,
                    type="session.joined",
                    actor=session_actor(row),
                    payload={"session": session_view(row), "resume_of": req.resume_of, "observe_only": not seated},
                    summary=f"{row['callsign']} joined ({_word(req.adapter)}, {_word(req.source)})",
                    refs={"session_id": row["id"], "host_id": row.get("host_id")},
                    now=now,
                )
            else:
                token, rotated = await self._rejoin_credentials(tx, row, host, session_token, now)
                row = await self._refresh_session(tx, row, req, host, now, max_live=limits.max_sessions_live)
                resume_row = None
            observe_only = not await has_seat(tx.conn, crew_id, row["id"], limits.max_sessions_live)
            upgrade_hint = None
            if observe_only:
                seat = seat_for_join(live_before, limits)
                upgrade_hint = seat.upgrade_hint
            actor = session_actor(row)
            adopted: list[dict[str, Any]] = []
            offered: list[dict[str, Any]] = []
            if not observe_only:
                if resume_row is not None:
                    adopted += await self._adopt_reserved_for(tx, crew_id, row, resume_row, settings, now)
                if settings.get("auto_adopt") == "same_checkout" and row.get("checkout_fp"):
                    adopted += await self._adopt_same_checkout(tx, crew_id, row, settings, now)
                offered = await self._offer_batons(tx, crew_id, row, now)
            await self._emit_mode_change(tx, crew_id, live_before, actor, now)
            my_tasks = await self._my_tasks(tx.conn, row["id"])
            row = await get_session(tx.conn, row["id"]) or row
            head = await _one(tx.conn, "SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
        return JoinResult(
            crew_id=crew_id,
            crew_created=created,
            session=row,
            session_token=token,
            token_rotated=rotated,
            rejoined=rejoined,
            observe_only=observe_only,
            upgrade_hint=upgrade_hint,
            batons_offered=offered,
            auto_adopted=adopted,
            my_tasks=my_tasks,
            seq=int(head["last_seq"]) if head else 0,
        )

    async def _check_resume_of(self, tx: EventTx, crew_id: str, user_id: str, req: JoinRequest) -> dict[str, Any] | None:
        if not req.resume_of:
            return None
        prior = await get_session(tx.conn, req.resume_of)
        if prior is None or prior["crew_id"] != crew_id:
            raise SessionError(422, "cross_crew_reference", "The referenced session is not part of this crew.")
        if prior["user_id"] != user_id or prior["agent_id"] != req.agent_id:
            raise SessionError(422, "resume_of_mismatch", "resume_of must name an earlier session of the same agent.")
        if prior["state"] in ("joining", "active"):
            raise _conflict("resume_of_live", "The session to resume is still active; end it before resuming it elsewhere.")
        return prior

    async def _allocate_callsign(self, conn: aiosqlite.Connection, crew_id: str, agent_id: str, now: datetime) -> str:
        prefix = callsign_prefix(agent_id)
        reuse_before = format_ts(now - timedelta(seconds=CALLSIGN_REUSE_AFTER_S))
        rows = await _all(
            conn,
            """SELECT callsign FROM crew_sessions
                WHERE crew_id = ? AND callsign LIKE ? AND (state != 'ended' OR ended_at IS NULL OR ended_at > ?)""",
            (crew_id, f"{prefix}-%", reuse_before),
        )
        taken = set()
        for r in rows:
            tail = str(r["callsign"])[len(prefix) + 1 :]
            if tail.isdigit():
                taken.add(int(tail))
        for n in range(1, MAX_CALLSIGN_NUMBER + 1):
            if n not in taken:
                return f"{prefix}-{n}"
        raise _conflict("callsign_exhausted", "No callsign left for this agent in this crew.")

    async def _insert_session(
        self,
        tx: EventTx,
        crew_id: str,
        user_id: str,
        req: JoinRequest,
        host: Mapping[str, Any] | None,
        token: str,
        now_s: str,
    ) -> dict[str, Any]:
        session_id = new_id("session")
        callsign = await self._allocate_callsign(tx.conn, crew_id, req.agent_id, parse_ts(now_s))
        member_key = member_key_for(
            crew_id=crew_id,
            user_id=user_id,
            agent_id=req.agent_id,
            session_id=req.session_id,
            host_label=host["host_label"] if host else None,
            client_kind=req.client_kind,
        )
        enforcement = "enforced" if req.adapter in ENFORCED_ADAPTERS and req.client_kind == "hook" else "advisory"
        await tx.conn.execute(
            """INSERT INTO crew_sessions (id, crew_id, user_id, agent_id, session_id, host_id, member_key, callsign,
                   client_kind, adapter, adapter_enforcement, agent_verified, model, checkout_fp, worktree_id, branch,
                   head_commit, state, joined_at, last_seen_at, last_activity_at, last_heartbeat_at, token_hash,
                   token_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, 1)""",
            (
                session_id,
                crew_id,
                user_id,
                req.agent_id,
                req.session_id,
                host["id"] if host else None,
                member_key,
                callsign,
                req.client_kind,
                req.adapter,
                enforcement,
                1 if req.agent_verified else 0,
                req.model,
                req.checkout_fp,
                req.worktree_id,
                req.branch,
                req.head,
                now_s,
                now_s,
                now_s,
                now_s if host else None,
                hash_token(token),
            ),
        )
        row = await get_session(tx.conn, session_id)
        assert row is not None
        return row

    async def _announce_host(
        self, tx: EventTx, crew_id: str, host: Mapping[str, Any], row: Mapping[str, Any], now: datetime
    ) -> None:
        """``host.registered`` the first time a session from this host joins this crew."""
        seen = await _one(
            tx.conn,
            "SELECT 1 FROM crew_sessions WHERE crew_id = ? AND host_id = ? AND id != ? LIMIT 1",
            (crew_id, host["id"], row["id"]),
        )
        if seen is not None:
            return
        await tx.emit(
            crew_id=crew_id,
            type="host.registered",
            actor=Actor.system(),
            payload={"host": host_view(dict(host))},
            summary=f"host {host['host_label']} joined the crew",
            refs={"host_id": host["id"]},
            idem_key=f"host:{host['id']}:registered",
            now=now,
        )

    async def _rejoin_credentials(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        host: Mapping[str, Any] | None,
        session_token: str | None,
        now: datetime,
    ) -> tuple[str | None, bool]:
        """§4.3: the current token re-joins (nothing returned); the owning host's crewd rotates; otherwise 409."""
        if tokens_match(session_token, row["token_hash"]):
            return None, False
        if host is not None and row.get("host_id") == host["id"]:
            token = new_session_token()
            version = int(row["token_version"]) + 1
            await tx.conn.execute(
                "UPDATE crew_sessions SET token_hash = ?, token_version = ? WHERE id = ?", (hash_token(token), version, row["id"])
            )
            await tx.emit(
                crew_id=row["crew_id"],
                type="session.token_rotated",
                actor=session_actor(row),
                payload={"token_version": version},
                summary=f"{row['callsign']} session token rotated",
                refs={"session_id": row["id"], "host_id": host["id"]},
                now=now,
            )
            return token, True
        raise _conflict(
            "session_exists",
            "This session already joined. Present its current session token, or re-join through the crewd host that owns it.",
        )

    async def _refresh_session(
        self, tx: EventTx, row: dict[str, Any], req: JoinRequest, host: Mapping[str, Any] | None, now: datetime, *, max_live: int
    ) -> dict[str, Any]:
        """A re-join (SessionStart resume/compact/clear) is activity: update checkout facts, revive the lane."""
        now_s = format_ts(now)
        await tx.conn.execute(
            """UPDATE crew_sessions SET adapter = ?, client_kind = ?, model = COALESCE(?, model),
                   checkout_fp = COALESCE(?, checkout_fp), worktree_id = COALESCE(?, worktree_id),
                   branch = COALESCE(?, branch), head_commit = COALESCE(?, head_commit),
                   host_id = COALESCE(?, host_id), last_seen_at = ?,
                   last_heartbeat_at = CASE WHEN ? IS NOT NULL THEN ? ELSE last_heartbeat_at END
                WHERE id = ?""",
            (
                req.adapter,
                req.client_kind,
                req.model,
                req.checkout_fp,
                req.worktree_id,
                req.branch,
                req.head,
                host["id"] if host else None,
                now_s,
                host["id"] if host else None,
                now_s,
                row["id"],
            ),
        )
        fresh = await get_session(tx.conn, row["id"])
        assert fresh is not None
        if fresh["state"] == "ended":
            callsign = fresh["callsign"]
            clash = await _one(
                tx.conn,
                "SELECT 1 FROM crew_sessions WHERE crew_id = ? AND callsign = ? AND state != 'ended' AND id != ?",
                (fresh["crew_id"], callsign, fresh["id"]),
            )
            if clash is not None:
                callsign = await self._allocate_callsign(tx.conn, fresh["crew_id"], fresh["agent_id"], now)
            await tx.conn.execute(
                """UPDATE crew_sessions SET state = 'active', callsign = ?, ended_at = NULL, end_reason = NULL,
                       state_reason = 'rejoined', quiet_reason = NULL, joined_at = ?, last_activity_at = ?
                    WHERE id = ? AND state = 'ended'""",
                (callsign, now_s, now_s, fresh["id"]),
            )
            fresh = await get_session(tx.conn, row["id"])
            assert fresh is not None
            await tx.emit(
                crew_id=fresh["crew_id"],
                type="session.joined",
                actor=session_actor(fresh),
                payload={
                    "session": session_view(fresh),
                    "resume_of": None,
                    "observe_only": not await has_seat(tx.conn, fresh["crew_id"], fresh["id"], max_live),
                },
                summary=f"{fresh['callsign']} re-joined ({_word(req.adapter)}, {_word(req.source)})",
                refs={"session_id": fresh["id"], "host_id": fresh.get("host_id")},
                now=now,
            )
            # The same session is back: it takes back what it left reserved for itself (nobody adopted it).
            own = await _one(
                tx.conn,
                "SELECT 1 FROM crew_claims WHERE holder_session_id = ? AND state = 'reserved' AND reserved_for = ? LIMIT 1",
                (fresh["id"], fresh["id"]),
            )
            stalled = await _one(
                tx.conn, "SELECT 1 FROM crew_tasks WHERE owner_session_id = ? AND status = 'stalled' LIMIT 1", (fresh["id"],)
            )
            if own is not None or stalled is not None:
                await self._recover(tx, fresh, await self.settings(fresh["crew_id"]), now)
                fresh = await get_session(tx.conn, row["id"])
                assert fresh is not None
            return fresh
        await self._signal(tx, fresh, alive=True, activity_at=now, now=now)
        fresh = await get_session(tx.conn, row["id"])
        assert fresh is not None
        return fresh

    async def _my_tasks(self, conn: aiosqlite.Connection, session_id: str) -> list[dict[str, Any]]:
        rows = await _all(
            conn,
            "SELECT * FROM crew_tasks WHERE owner_session_id = ? AND status NOT IN ('done','cancelled') ORDER BY number",
            (session_id,),
        )
        return [await task_view(conn, r) for r in rows]

    # -- baton: auto-adopt and offers (D6, D33) ---------------------------------------------------

    async def _reserved_claims(self, conn: aiosqlite.Connection, crew_id: str) -> list[dict[str, Any]]:
        return await _all(
            conn,
            "SELECT * FROM crew_claims WHERE crew_id = ? AND state = 'reserved' ORDER BY created_at, id",
            (crew_id,),
        )

    async def _live_exclusive_count(self, conn: aiosqlite.Connection, session_id: str) -> int:
        row = await _one(
            conn,
            """SELECT COUNT(*) AS n FROM crew_claims WHERE holder_session_id = ? AND mode = 'exclusive'
                AND state IN ('requested','queued','active','offered','reserved')""",
            (session_id,),
        )
        return int(row["n"]) if row else 0

    async def _adopt_same_checkout(
        self, tx: EventTx, crew_id: str, joiner: Mapping[str, Any], settings: Mapping[str, Any], now: datetime
    ) -> list[dict[str, Any]]:
        candidates = []
        for claim in await self._reserved_claims(tx.conn, crew_id):
            if claim.get("reserve_reason") not in ADOPTABLE_REASONS or claim.get("holder_session_id") in (None, joiner["id"]):
                continue
            holder = await get_session(tx.conn, claim["holder_session_id"])
            if holder is None or holder.get("checkout_fp") != joiner.get("checkout_fp"):
                continue
            if holder["state"] not in ("lost", "quota_blocked", "ended"):
                continue  # a live holder released it with a baton: only an offer or a human hands it on
            if claim.get("reserved_for") not in (None, holder["id"]):
                continue  # reserved for someone else
            if not await self._zone_allows(tx.conn, claim, joiner):
                continue
            candidates.append(claim)
        return await self._adopt_groups(tx, crew_id, joiner, candidates, "same_checkout", settings, now)

    async def _adopt_reserved_for(
        self,
        tx: EventTx,
        crew_id: str,
        joiner: Mapping[str, Any],
        prior: Mapping[str, Any],
        settings: Mapping[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """``resume_of``: the new session takes over what was reserved for the session it resumes."""
        candidates = [
            c
            for c in await self._reserved_claims(tx.conn, crew_id)
            if c.get("reserved_for") == prior["id"]
            or (c.get("holder_session_id") == prior["id"] and c.get("reserved_for") is None)
        ]
        adopted = await self._adopt_groups(tx, crew_id, joiner, candidates, "reserved_for", settings, now)
        if prior["state"] != "ended":
            await self._end_session(tx, prior, "resumed", now, actor=session_actor(joiner))
        return adopted

    async def _zone_allows(self, conn: aiosqlite.Connection, claim: Mapping[str, Any], session: Mapping[str, Any]) -> bool:
        """``reserve_for`` on a zone matches key-verified agents only (§5.1)."""
        if not claim.get("zone_id"):
            return True
        zone = await _one(
            conn, "SELECT reserve_for FROM crew_zones WHERE id = ? AND crew_id = ?", (claim["zone_id"], claim["crew_id"])
        )
        reserve_for = zone.get("reserve_for") if zone else None
        if not reserve_for:
            return True
        return bool(session.get("agent_verified")) and session.get("agent_id") == reserve_for

    async def _adopt_groups(
        self,
        tx: EventTx,
        crew_id: str,
        joiner: Mapping[str, Any],
        claims: Sequence[Mapping[str, Any]],
        kind: str,
        settings: Mapping[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Adopt claims task by task (all or nothing per task), within the per-session exclusive cap."""
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for claim in claims:
            groups.setdefault(claim.get("task_id") or claim["id"], []).append(claim)
        cap = int(settings.get("max_exclusive_claims_per_session", 3))
        adopted: list[dict[str, Any]] = []
        for group in groups.values():
            exclusive = sum(1 for c in group if c["mode"] == "exclusive")
            if await self._live_exclusive_count(tx.conn, joiner["id"]) + exclusive > cap:
                continue  # over the cap: it stays reserved and is offered instead
            adopted.append(await self._adopt_group(tx, crew_id, joiner, group, kind, None, settings, now))
        return adopted

    async def _adopt_group(
        self,
        tx: EventTx,
        crew_id: str,
        joiner: Mapping[str, Any],
        group: Sequence[Mapping[str, Any]],
        kind: str,
        offer_id: str | None,
        settings: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        now_s = format_ts(now)
        lease = format_ts(now + timedelta(seconds=int(settings["lease_ttl_s"])))
        actor = session_actor(joiner)
        from_session = group[0].get("holder_session_id")
        holder = await get_session(tx.conn, from_session) if from_session else None
        cross = holder is None or holder.get("checkout_fp") != joiner.get("checkout_fp") or not joiner.get("checkout_fp")
        claim_ids: list[str] = []
        zones: list[str] = []
        baton_ref = None
        for claim in group:
            cur = await tx.conn.execute(
                """UPDATE crew_claims SET state = 'active', holder_kind = 'session', holder_session_id = ?,
                       holder_user_id = ?, holder_agent_id = ?, epoch = epoch + 1, source = 'adopt',
                       lease_expires_at = ?, reserve_reason = NULL, reserved_for = NULL, reserve_expires_at = NULL,
                       granted_at = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND state = 'reserved'""",
                (joiner["id"], joiner["user_id"], joiner["agent_id"], lease, now_s, now_s, claim["id"]),
            )
            if cur.rowcount != 1:
                continue
            fresh = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
            assert fresh is not None
            claim_ids.append(fresh["id"])
            if fresh.get("zone_id"):
                zones.append(fresh["zone_id"])
            baton_ref = baton_ref or fresh.get("baton_ref")
            await tx.emit(
                crew_id=crew_id,
                type="claim.adopted",
                actor=actor,
                payload={"claim": claim_view(fresh, now), "cross_checkout": cross, "from_session": from_session},
                summary=f"{joiner['callsign']} adopted claim {fresh['id']} (epoch {fresh['epoch']})",
                refs={
                    "claim_id": fresh["id"],
                    "zone_id": fresh.get("zone_id"),
                    "task_id": fresh.get("task_id"),
                    "session_id": joiner["id"],
                },
                now=now,
            )
        task_id = group[0].get("task_id")
        report_id = handoff_id = None
        task_number = None
        if task_id:
            task = await _one(tx.conn, "SELECT * FROM crew_tasks WHERE id = ? AND crew_id = ?", (task_id, crew_id))
            if task is not None:
                task_number = int(task["number"])
                report_id = task.get("current_report_id")
                if report_id:
                    rep = await _one(tx.conn, "SELECT handoff_id FROM crew_reports WHERE id = ?", (report_id,))
                    handoff_id = rep.get("handoff_id") if rep else None
                await self._assign_task(tx, task, joiner, now)
        baton_id = new_id("baton")
        passed = await tx.emit(
            crew_id=crew_id,
            type="baton.passed",
            actor=actor,
            payload={
                "baton_id": baton_id,
                "task_id": task_id,
                "from_session": from_session,
                "to_session": joiner["id"],
                "kind": kind,
                "handoff_id": handoff_id,
                "zones": zones[:20],
                "baton_ref": baton_ref,
                "restored": None,
            },
            summary=f"baton passed to {joiner['callsign']}" + (f" for T-{task_number}" if task_number else ""),
            refs={"task_id": task_id, "session_id": joiner["id"], "report_id": report_id},
            now=now,
        )
        await tx.conn.execute(
            """INSERT INTO crew_batons (id, crew_id, task_id, from_session, to_session, kind, offer_id, handoff_id,
                   report_id, baton_ref, restored, zone_ids, seq, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                baton_id,
                crew_id,
                task_id,
                from_session,
                joiner["id"],
                kind,
                offer_id,
                handoff_id,
                report_id,
                baton_ref,
                _dumps(zones),
                passed.seq,
                now_s,
            ),
        )
        if offer_id:
            await tx.conn.execute("UPDATE crew_baton_offers SET used_at = ? WHERE id = ?", (now_s, offer_id))
        await self._resolve_baton_items(tx, crew_id, [task_id or c["id"] for c in group], actor, now)
        if cross:
            await self._audit(
                {
                    "action": "crew.adopt_cross_checkout",
                    "user_id": joiner["user_id"],
                    "crew_id": crew_id,
                    "session_id": joiner["id"],
                    "claims": claim_ids,
                }
            )
        return {
            "baton_id": baton_id,
            "kind": kind,
            "task_id": task_id,
            "task": f"T-{task_number}" if task_number else None,
            "claim_ids": claim_ids,
            "zone_ids": zones,
            "from_session": from_session,
            "baton_ref": baton_ref,
            "restore_command": f"remembra-crew adopt T-{task_number}" if task_number and baton_ref else None,
        }

    async def _assign_task(self, tx: EventTx, task: Mapping[str, Any], to: Mapping[str, Any], now: datetime) -> None:
        new_status = "claimed" if task["status"] == "stalled" else task["status"]
        await tx.conn.execute(
            """UPDATE crew_tasks SET owner_session_id = ?, owner_user_id = ?, owner_agent_id = ?, status = ?,
                   status_before_stall = CASE WHEN ? = 'claimed' AND status = 'stalled' THEN NULL ELSE status_before_stall END,
                   version = version + 1, updated_at = ?
                WHERE id = ?""",
            (to["id"], to["user_id"], to["agent_id"], new_status, new_status, format_ts(now), task["id"]),
        )
        await tx.conn.execute("UPDATE crew_sessions SET current_task_id = ? WHERE id = ?", (task["id"], to["id"]))
        fresh = await _one(tx.conn, "SELECT * FROM crew_tasks WHERE id = ?", (task["id"],))
        assert fresh is not None
        view = await task_view(tx.conn, fresh)
        if new_status != task["status"]:
            await tx.emit(
                crew_id=task["crew_id"],
                type="task.status_changed",
                actor=session_actor(to),
                payload={"task": view, "from": task["status"], "to": new_status},
                summary=f"T-{task['number']} {task['status']} -> {new_status} ({to['callsign']})",
                refs={"task_id": task["id"], "session_id": to["id"]},
                now=now,
            )
        else:
            await tx.emit(
                crew_id=task["crew_id"],
                type="task.updated",
                actor=session_actor(to),
                payload={"task": view, "changed": ["owner_session_id"]},
                summary=f"T-{task['number']} now owned by {to['callsign']}",
                refs={"task_id": task["id"], "session_id": to["id"]},
                now=now,
            )

    async def _offer_batons(self, tx: EventTx, crew_id: str, joiner: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
        """Record an offer (via ``brief``) of every adoptable baton to the joining session (D33)."""
        offered: list[dict[str, Any]] = []
        now_s = format_ts(now)
        for claim in await self._reserved_claims(tx.conn, crew_id):
            if claim.get("reserve_reason") not in ADOPTABLE_REASONS or claim.get("holder_session_id") == joiner["id"]:
                continue
            reserved_for = claim.get("reserved_for")
            if reserved_for and reserved_for not in (joiner["id"], claim.get("holder_session_id")):
                target = await get_session(tx.conn, reserved_for)
                if target is not None and target["state"] in LIVE_STATES:
                    continue  # handed to a live session by name
            if reserved_for and reserved_for == claim.get("holder_session_id"):
                holder = await get_session(tx.conn, reserved_for)
                if holder is not None and holder["state"] in ("joining", "active", "idle"):
                    continue  # the holder is back and will re-take it
            if not await self._zone_allows(tx.conn, claim, joiner):
                continue
            offer_id = new_id("offer")
            cur = await tx.conn.execute(
                """INSERT OR IGNORE INTO crew_baton_offers (id, crew_id, claim_id, task_id, to_session, via, created_at)
                   VALUES (?, ?, ?, ?, ?, 'brief', ?)""",
                (offer_id, crew_id, claim["id"], claim.get("task_id"), joiner["id"], now_s),
            )
            offer = await _one(
                tx.conn, "SELECT * FROM crew_baton_offers WHERE claim_id = ? AND to_session = ?", (claim["id"], joiner["id"])
            )
            assert offer is not None
            if cur.rowcount == 1:
                await tx.emit(
                    crew_id=crew_id,
                    type="claim.offered_in_brief",
                    actor=Actor.system(),
                    payload={"offer": offer_view(offer)},
                    summary=f"baton {claim['id']} offered to {joiner['callsign']}",
                    refs={
                        "claim_id": claim["id"],
                        "task_id": claim.get("task_id"),
                        "zone_id": claim.get("zone_id"),
                        "session_id": joiner["id"],
                    },
                    now=now,
                )
            offered.append(await self._offer_brief_item(tx.conn, offer, claim))
        return offered

    async def _offer_brief_item(
        self, conn: aiosqlite.Connection, offer: Mapping[str, Any], claim: Mapping[str, Any]
    ) -> dict[str, Any]:
        holder = await get_session(conn, claim["holder_session_id"]) if claim.get("holder_session_id") else None
        task = (
            await _one(conn, "SELECT number FROM crew_tasks WHERE id = ?", (claim["task_id"],)) if claim.get("task_id") else None
        )
        number = int(task["number"]) if task else None
        return {
            "offer_id": offer["id"],
            "claim_id": claim["id"],
            "task_id": claim.get("task_id"),
            "task": f"T-{number}" if number else None,
            "zone_id": claim.get("zone_id"),
            "from_session": claim.get("holder_session_id"),
            "from_callsign": holder["callsign"] if holder else None,
            "reserve_reason": claim.get("reserve_reason"),
            "baton_ref": claim.get("baton_ref"),
            "adopt_command": f"remembra-crew adopt T-{number}" if number else f"remembra-crew adopt {claim['id']}",
        }

    # -- presence signals (heartbeat, re-join, any authenticated session call) ----------------------

    async def touch(self, session_id: str, *, activity: bool = True) -> dict[str, Any] | None:
        """Record a signal from a session that has no host heartbeat (MCP-only, CLI).

        Other work packages call this on every authenticated session call
        (e.g. an MCP tool): it renews the session's leases and revives the lane.
        Returns the updated row, or None when the session is unknown or ended.
        """
        now = self.now()
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session_id)
            if row is None or row["state"] == "ended":
                return None
            await self._signal(tx, row, alive=True, activity_at=now if activity else None, now=now)
            return await get_session(tx.conn, session_id)

    async def _signal(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        *,
        alive: bool,
        activity_at: datetime | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Apply one liveness signal: stamps, lease renewal, re-takes, presence and recovery.

        Returns ``{"retaken": [...], "recovered": bool}``.
        """
        now_s = format_ts(now)
        crew_id = row["crew_id"]
        if not alive:
            return {"retaken": [], "recovered": False}
        prev_activity = parse_ts(row["last_activity_at"]) if row.get("last_activity_at") else None
        new_activity = activity_at if activity_at and (prev_activity is None or activity_at > prev_activity) else None
        await tx.conn.execute(
            """UPDATE crew_sessions SET last_seen_at = ?,
                   last_heartbeat_at = CASE WHEN host_id IS NOT NULL THEN ? ELSE last_heartbeat_at END,
                   last_activity_at = COALESCE(?, last_activity_at) WHERE id = ?""",
            (now_s, now_s, format_ts(new_activity) if new_activity else None, row["id"]),
        )
        settings = await self.settings(crew_id)
        state = row["state"]
        result: dict[str, Any] = {"retaken": [], "recovered": False}
        if state == "quota_blocked" and new_activity is None:
            return result  # still blocked: nothing new since the stall (D14: cleared only by new activity)
        if state in ("lost", "quota_blocked"):
            await self._recover(tx, row, settings, now)
            result["recovered"] = True
            return result
        # Leases renew only while alive; the holder re-takes its own offline/idle reservations.
        lease = format_ts(now + timedelta(seconds=int(settings["lease_ttl_s"])))
        await tx.conn.execute(
            "UPDATE crew_claims SET lease_expires_at = ?, updated_at = ?"
            " WHERE holder_session_id = ? AND state IN ('active','offered')",
            (lease, now_s, row["id"]),
        )
        retake: list[Mapping[str, Any]] = []
        for claim in await _all(
            tx.conn,
            "SELECT * FROM crew_claims WHERE holder_session_id = ? AND state = 'reserved' AND reserved_for = ?",
            (row["id"], row["id"]),
        ):
            reason = claim.get("reserve_reason")
            if reason == "offline" or (
                reason == "idle" and new_activity is not None and new_activity > parse_ts(claim["updated_at"])
            ):
                retake.append(claim)
        if retake:
            result["retaken"] = await self._retake(tx, row, retake, settings, now)
        if state in ("joining", "active", "idle", "quiet"):
            activity_ref = new_activity or prev_activity
            target = "active" if activity_ref and (now - activity_ref).total_seconds() <= ACTIVE_WINDOW_S else "idle"
            if target != state:
                await self._set_state(tx, row, target, "activity" if target == "active" else "no_activity", None, now)
        return result

    async def _set_state(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        to: str,
        reason: str,
        quiet_reason: str | None,
        now: datetime,
        *,
        actor: Actor | None = None,
    ) -> bool:
        cur = await tx.conn.execute(
            "UPDATE crew_sessions SET state = ?, state_reason = ?, quiet_reason = ? WHERE id = ? AND state = ?",
            (to, reason, quiet_reason, row["id"], row["state"]),
        )
        if cur.rowcount != 1:
            return False
        await tx.emit(
            crew_id=row["crew_id"],
            type="session.state_changed",
            actor=actor or Actor.system(),
            payload={"from": row["state"], "to": to, "reason": reason[:64], "quiet_reason": quiet_reason},
            summary=f"{row['callsign']} {row['state']} -> {to}" + (f" ({quiet_reason})" if quiet_reason else ""),
            refs={"session_id": row["id"]},
            now=now,
        )
        return True

    async def _retake(
        self, tx: EventTx, row: Mapping[str, Any], claims: Sequence[Mapping[str, Any]], settings: Mapping[str, Any], now: datetime
    ) -> list[str]:
        """The holder takes its own reservations back (epoch + 1, §5.1)."""
        now_s = format_ts(now)
        lease = format_ts(now + timedelta(seconds=int(settings["lease_ttl_s"])))
        taken: list[str] = []
        for claim in claims:
            cur = await tx.conn.execute(
                """UPDATE crew_claims SET state = 'active', epoch = epoch + 1, lease_expires_at = ?, reserve_reason = NULL,
                       reserved_for = NULL, reserve_expires_at = NULL, granted_at = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND state = 'reserved' AND holder_session_id = ?""",
                (lease, now_s, now_s, claim["id"], row["id"]),
            )
            if cur.rowcount != 1:
                continue
            fresh = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
            assert fresh is not None
            taken.append(fresh["id"])
            await tx.emit(
                crew_id=row["crew_id"],
                type="claim.granted",
                actor=session_actor(row),
                payload={"claim": claim_view(fresh, now)},
                summary=f"{row['callsign']} re-took claim {fresh['id']} (epoch {fresh['epoch']})",
                refs={
                    "claim_id": fresh["id"],
                    "zone_id": fresh.get("zone_id"),
                    "task_id": fresh.get("task_id"),
                    "session_id": row["id"],
                },
                now=now,
            )
        return taken

    async def _recover(self, tx: EventTx, row: Mapping[str, Any], settings: Mapping[str, Any], now: datetime) -> None:
        """§10.2 recovery: re-take what nobody adopted, restore tasks, supersede synthesized reports, resolve baton items."""
        down_s = _age_s(now, row.get("last_heartbeat_at") or row.get("last_seen_at")) or 0
        claims = [
            c
            for c in await _all(
                tx.conn, "SELECT * FROM crew_claims WHERE holder_session_id = ? AND state = 'reserved'", (row["id"],)
            )
            if c.get("reserve_reason") in RETAKE_REASONS and c.get("reserved_for") in (None, row["id"])
        ]
        retaken = await self._retake(tx, row, claims, settings, now)
        restored: list[str] = []
        superseded: list[str] = []
        actor = session_actor(row)
        for task in await _all(
            tx.conn, "SELECT * FROM crew_tasks WHERE owner_session_id = ? AND status = 'stalled' ORDER BY number", (row["id"],)
        ):
            back_to = (
                task.get("status_before_stall") if task.get("status_before_stall") in schemas.TASK_STATUSES else "in_progress"
            )
            report = None
            if task.get("current_report_id"):
                report = await _one(
                    tx.conn,
                    "SELECT * FROM crew_reports WHERE id = ? AND kind = 'stalled' AND session_id = ? AND is_current = 1",
                    (task["current_report_id"], row["id"]),
                )
            if report is not None:
                await tx.conn.execute(
                    "UPDATE crew_reports SET is_current = 0, superseded_reason = 'recovered' WHERE id = ?", (report["id"],)
                )
                fresh_report = await _one(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (report["id"],))
                assert fresh_report is not None
                superseded.append(report["id"])
                await tx.emit(
                    crew_id=row["crew_id"],
                    type="report.superseded",
                    actor=Actor.system(),
                    payload={"report": report_view(fresh_report)},
                    summary=f"stalled report for T-{task['number']} superseded ({row['callsign']} recovered)",
                    refs={"report_id": report["id"], "task_id": task["id"], "session_id": row["id"]},
                    now=now,
                )
            await tx.conn.execute(
                """UPDATE crew_tasks SET status = ?, status_before_stall = NULL, stalled_at = NULL,
                       current_report_id = CASE WHEN ? THEN NULL ELSE current_report_id END, version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'stalled'""",
                (back_to, 1 if report is not None else 0, format_ts(now), task["id"]),
            )
            fresh_task = await _one(tx.conn, "SELECT * FROM crew_tasks WHERE id = ?", (task["id"],))
            assert fresh_task is not None
            restored.append(task["id"])
            await tx.emit(
                crew_id=row["crew_id"],
                type="task.recovered",
                actor=actor,
                payload={"task": await task_view(tx.conn, fresh_task)},
                summary=f"T-{task['number']} back to {back_to} ({row['callsign']} recovered)",
                refs={"task_id": task["id"], "session_id": row["id"]},
                now=now,
            )
        await self._resolve_baton_items(tx, row["crew_id"], [*restored, *(c["id"] for c in claims)], actor, now)
        await tx.conn.execute(
            """UPDATE crew_sessions SET state = 'active', state_reason = 'recovered', quiet_reason = NULL,
                   limit_level = CASE WHEN state = 'quota_blocked' THEN NULL ELSE limit_level END,
                   limit_pct = CASE WHEN state = 'quota_blocked' THEN NULL ELSE limit_pct END,
                   limit_source = CASE WHEN state = 'quota_blocked' THEN NULL ELSE limit_source END
                WHERE id = ?""",
            (row["id"],),
        )
        await tx.emit(
            crew_id=row["crew_id"],
            type="session.recovered",
            actor=actor,
            payload={
                "from": row["state"],
                "down_s": down_s,
                "claims_retaken": retaken[:50],
                "tasks_restored": restored[:20],
                "superseded_report_ids": superseded[:20],
            },
            summary=f"{row['callsign']} is back ({row['state']} for {down_s}s)",
            refs={"session_id": row["id"]},
            now=now,
        )

    # -- heartbeat --------------------------------------------------------------------------------

    async def heartbeat(self, *, user_id: str, host: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
        """One batched host heartbeat (§6). The host was authenticated by its token.

        Each session is checked against its own token and must belong to this host.
        One bad session never fails the batch: it gets ``{"error": …}``.
        Heartbeats and lease renewals are **never events** (§4.2); only the
        transitions they cause are.
        """
        now = self.now()
        now_s = format_ts(now)
        async with self.log.transaction() as tx:
            previous = await mark_seen(tx.conn, host["id"], now_s)
            if previous == "unreachable":
                await self._host_recovered(tx, host, now)
        per_session: dict[str, Any] = {}
        etags: dict[str, str] = {}
        for item in body.get("sessions") or []:
            sid = item["session_id"]
            try:
                per_session[sid] = await self._heartbeat_session(host, item, now)
                etags[per_session[sid]["crew_id"]] = f'"{per_session[sid]["crew_last_seq"]}"'
            except SessionError as e:
                per_session[sid] = {"error": e.code, "message": e.message}
        return {
            "per_session": per_session,
            "snapshot_etag": etags,
            "server_time": now_s,
            "next_heartbeat_after_s": HEARTBEAT_INTERVAL_S,
        }

    async def _host_recovered(self, tx: EventTx, host: Mapping[str, Any], now: datetime) -> None:
        down_s = _age_s(now, host.get("last_seen_at")) or 0
        for crew in await _all(
            tx.conn,
            "SELECT DISTINCT crew_id FROM crew_sessions WHERE host_id = ? AND state != 'ended' ORDER BY crew_id",
            (host["id"],),
        ):
            await tx.emit(
                crew_id=crew["crew_id"],
                type="host.recovered",
                actor=Actor.system(),
                payload={"host_id": host["id"], "down_s": down_s},
                summary=f"host {host['host_label']} is back after {down_s}s",
                refs={"host_id": host["id"]},
                now=now,
            )

    async def _heartbeat_session(self, host: Mapping[str, Any], item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, item["session_id"])
            if row is None or row.get("host_id") != host["id"] or not tokens_match(item.get("token"), row["token_hash"]):
                raise SessionError(401, "session_token_invalid", "unknown session, wrong host or wrong session token")
            if row["state"] == "ended":
                raise SessionError(409, "session_ended", "this session has ended; join again")
            crew_id = row["crew_id"]
            alive = bool(item["alive"])
            age = int(item.get("activity_age_s") or 0)
            activity_at = now - timedelta(seconds=age) if alive else None
            last_action = item.get("last_action")
            if isinstance(last_action, dict):
                last_action = redact(dict(last_action))
            limit = item.get("limit") if isinstance(item.get("limit"), dict) else None
            await tx.conn.execute(
                """UPDATE crew_sessions SET last_action = COALESCE(?, last_action), calls_since_checkpoint = ?,
                       githook_state = ?, delivered_seq = MAX(delivered_seq, ?) WHERE id = ?""",
                (
                    _dumps(last_action) if last_action else None,
                    int(item.get("calls_since_checkpoint") or 0),
                    item.get("githook_state"),
                    int(item.get("cursor") or 0),
                    row["id"],
                ),
            )
            await self._signal(tx, row, alive=alive, activity_at=activity_at, now=now)
            row = await get_session(tx.conn, row["id"]) or row
            if limit is not None:
                row = await self._apply_limit(tx, row, limit, now)
            footprints = [f for f in item.get("footprints") or [] if isinstance(f, dict)]
            if footprints:
                await self._upsert_footprints(tx, row, footprints, now)
                for sink in list(FOOTPRINT_SINKS):
                    await sink(tx, row, footprints)
            claims = await _all(
                tx.conn,
                "SELECT id, epoch, state, lease_expires_at FROM crew_claims"
                " WHERE holder_session_id = ? AND state IN ('active','offered','reserved')",
                (row["id"],),
            )
            leases = [c["lease_expires_at"] for c in claims if c["state"] in ("active", "offered") and c.get("lease_expires_at")]
            lease_expires_at = min(leases) if leases else None
            deltas = await self._deltas(tx.conn, crew_id, int(item.get("cursor") or 0), row["id"])
            inject = await self._inject_text(tx.conn, row, int(item.get("cursor") or 0))
            head = await _one(tx.conn, "SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
        return {
            "crew_id": crew_id,
            "state": row["state"],
            "lease_expires_at": lease_expires_at,
            "fence_at": fence_horizon(lease_expires_at),
            "epoch_by_claim": {c["id"]: int(c["epoch"]) for c in claims if c["state"] != "reserved"},
            "reserved_claims": [c["id"] for c in claims if c["state"] == "reserved"],
            "deltas_since_cursor": deltas,
            "inject_text": inject,
            "crew_last_seq": int(head["last_seq"]) if head else 0,
        }

    async def _apply_limit(self, tx: EventTx, row: dict[str, Any], limit: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        level = limit.get("level")
        source = limit.get("source")
        pct = limit.get("pct")
        if level not in schemas.LIMIT_LEVELS or source not in schemas.LIMIT_SOURCES:
            return row
        changed = level != row.get("limit_level")
        await tx.conn.execute(
            "UPDATE crew_sessions SET limit_level = ?, limit_pct = ?, limit_source = ? WHERE id = ?",
            (level, pct, source, row["id"]),
        )
        if level == "exhausted" and row["state"] not in ("quota_blocked", "lost", "ended"):
            await self._quota_block(tx, row, error="detected_limit", source=source, facts={}, baton_ref=None, now=now)
        elif changed and level in ("warn", "critical"):
            await tx.emit(
                crew_id=row["crew_id"],
                type="session.limit_warning",
                actor=session_actor(row),
                payload={"level": level, "pct": pct, "source": source},
                summary=f"{row['callsign']} limit {level}",
                refs={"session_id": row["id"]},
                now=now,
            )
        return await get_session(tx.conn, row["id"]) or row

    async def _upsert_footprints(
        self, tx: EventTx, row: Mapping[str, Any], footprints: Sequence[Mapping[str, Any]], now: datetime
    ) -> None:
        now_s = format_ts(now)
        for fp in footprints[:500]:
            path = fp.get("path")
            if not schemas.is_path_rel(path):
                continue
            await tx.conn.execute(
                """INSERT INTO crew_footprints (crew_id, session_id, path, first_at, last_at, touches, state, attribution,
                       claim_epoch, last_commit, worktree_id)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(crew_id, session_id, path) DO UPDATE SET last_at = excluded.last_at,
                       touches = touches + 1, state = excluded.state,
                       attribution = CASE WHEN crew_footprints.attribution = 'certain' THEN 'certain'
                                          ELSE excluded.attribution END,
                       claim_epoch = COALESCE(excluded.claim_epoch, crew_footprints.claim_epoch),
                       last_commit = COALESCE(excluded.last_commit, crew_footprints.last_commit),
                       worktree_id = COALESCE(excluded.worktree_id, crew_footprints.worktree_id)""",
                (
                    row["crew_id"],
                    row["id"],
                    path,
                    now_s,
                    now_s,
                    fp.get("state") if fp.get("state") in schemas.FOOTPRINT_STATES else "dirty",
                    fp.get("attribution") if fp.get("attribution") in schemas.ATTRIBUTIONS else "probable",
                    fp.get("claim_epoch"),
                    fp.get("last_commit"),
                    row.get("worktree_id"),
                ),
            )

    async def _deltas(self, conn: aiosqlite.Connection, crew_id: str, cursor: int, session_id: str) -> dict[str, Any]:
        rows = await _all(
            conn,
            "SELECT seq, type, summary, session_id FROM crew_events WHERE crew_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (crew_id, cursor, MAX_DELTA_EVENTS + 1),
        )
        more = len(rows) > MAX_DELTA_EVENTS
        rows = rows[:MAX_DELTA_EVENTS]
        return {
            "from_seq": cursor,
            "to_seq": rows[-1]["seq"] if rows else cursor,
            "more": more,
            "events": [
                {"seq": r["seq"], "type": r["type"], "summary": r["summary"], "mine": r["session_id"] == session_id} for r in rows
            ],
        }

    async def _inject_text(self, conn: aiosqlite.Connection, row: Mapping[str, Any], cursor: int) -> str | None:
        """Urgent server/human items for this session (session queue, §5.8), ids-only templates, ≤300 chars."""
        lines: list[str] = []
        if row["state"] == "paused":
            lines.append("Crew: this session is PAUSED by Mani. Do not edit files until it is resumed.")
        items = await _all(
            conn,
            """SELECT kind, title FROM crew_inbox_items WHERE crew_id = ? AND audience = 'session' AND recipient = ?
                AND origin IN ('server','human') AND state IN ('open','seen') AND COALESCE(created_seq, 0) > ?
                ORDER BY priority, created_seq LIMIT 3""",
            (row["crew_id"], row["id"], cursor),
        )
        lines.extend(f"Crew: {item['title']}" for item in items)
        if not lines:
            return None
        text = " ".join(lines)
        return text[:MAX_INJECT_CHARS]

    # -- stall (StopFailure / detected limit) ----------------------------------------------------

    async def stall(self, session: Mapping[str, Any], *, error: str, facts: Any, baton_ref: str | None) -> dict[str, Any]:
        """``POST /sessions/{sid}/stall`` (§8.2 StopFailure). Quota errors block and hand the baton on."""
        now = self.now()
        cleaned = clean_facts(facts)
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session["id"])
            if row is None or row["state"] == "ended":
                raise SessionError(409, "session_ended", "this session has ended")
            if error in QUOTA_ERRORS:
                if row["state"] == "quota_blocked":
                    return {"state": "quota_blocked", "already": True, "seq": await self._last_seq(tx.conn, row["crew_id"])}
                effects = await self._quota_block(
                    tx,
                    row,
                    error=error,
                    source="detected" if error == "detected_limit" else "reported",
                    facts=cleaned,
                    baton_ref=baton_ref,
                    now=now,
                )
                return {
                    "state": "quota_blocked",
                    "already": False,
                    "claims_reserved": effects.claims_reserved,
                    "tasks_stalled": effects.tasks_stalled,
                    "report_ids": effects.report_ids,
                    "checkpoint_id": effects.checkpoint_id,
                    "handoff_id": effects.handoff_id,
                    "seq": await self._last_seq(tx.conn, row["crew_id"]),
                }
            # Other StopFailure errors (overloaded, server_error, …) record a checkpoint only (§8.2).
            ckp = await self._checkpoint(
                tx, row, trigger="turn", facts=cleaned, facts_source="relay-cli", headline=f"stop failure {error}", now=now
            )
            return {
                "state": row["state"],
                "already": False,
                "checkpoint_id": ckp,
                "seq": await self._last_seq(tx.conn, row["crew_id"]),
            }

    async def _last_seq(self, conn: aiosqlite.Connection, crew_id: str) -> int:
        head = await _one(conn, "SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
        return int(head["last_seq"]) if head else 0

    async def _quota_block(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        *,
        error: str,
        source: str,
        facts: Mapping[str, Any],
        baton_ref: str | None,
        now: datetime,
    ) -> _Effects:
        now_s = format_ts(now)
        cur = await tx.conn.execute(
            """UPDATE crew_sessions SET state = 'quota_blocked', state_reason = ?, quiet_reason = NULL,
                   limit_level = 'exhausted', limit_source = ?, last_activity_at = ? WHERE id = ? AND state = ?""",
            (error[:64], source, now_s, row["id"], row["state"]),
        )
        if cur.rowcount != 1:
            return _Effects()
        facts_source = "relay-cli" if facts else "server-inferred"
        if not facts:
            facts = await self._inferred_facts(tx.conn, row, now)
        if baton_ref:
            await self._baton_ref_created(tx, row, baton_ref, facts, now)
        effects = await self._stall_work(
            tx,
            row,
            reserve_reason="quota",
            report_kind="stalled",
            trigger="quota",
            facts=dict(facts),
            facts_source=facts_source,
            baton_ref=baton_ref,
            end_reason=f"stalled:{error}",
            handoff=True,
            now=now,
        )
        await tx.emit(
            crew_id=row["crew_id"],
            type="session.quota_blocked",
            actor=session_actor(row),
            payload={
                "error": error[:64],
                "source": source,
                "baton_ref": baton_ref,
                "claims_reserved": effects.claims_reserved[:50],
            },
            summary=f"{row['callsign']} out of credits ({_word(error)}, {source})",
            refs={"session_id": row["id"]},
            now=now,
        )
        return effects

    async def _baton_ref_created(
        self, tx: EventTx, row: Mapping[str, Any], ref: str, facts: Mapping[str, Any], now: datetime
    ) -> None:
        if not re.fullmatch(schemas.BATON_REF_PATTERN, ref):
            raise SessionError(422, "invalid_baton_ref", "baton_ref must be refs/remembra/baton/<T-n|session>/<seq>")
        task_id = row.get("current_task_id") if schemas.is_id("task", row.get("current_task_id")) else None
        await tx.emit(
            crew_id=row["crew_id"],
            type="baton.ref_created",
            actor=session_actor(row),
            payload={"ref": ref, "task_id": task_id, "dirty_files": _dirty_count(facts), "unpushed": _unpushed_count(facts)},
            summary=f"{row['callsign']} saved uncommitted work as {ref}",
            refs={"session_id": row["id"], "task_id": task_id},
            now=now,
        )

    async def _inferred_facts(self, conn: aiosqlite.Connection, row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        """Server-inferred facts (§5.5 ``lost``): last checkpoint + dirty footprints + last heartbeat."""
        last = await _one(
            conn,
            "SELECT facts, headline FROM crew_checkpoints WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (row["id"],),
        )
        dirty = await _all(
            conn,
            "SELECT path FROM crew_footprints WHERE crew_id = ? AND session_id = ? AND state = 'dirty'"
            " ORDER BY last_at DESC LIMIT 20",
            (row["crew_id"], row["id"]),
        )
        count = await _one(
            conn,
            "SELECT COUNT(*) AS n FROM crew_footprints WHERE crew_id = ? AND session_id = ? AND state = 'dirty'",
            (row["crew_id"], row["id"]),
        )
        prior = _loads(last["facts"], {}) if last else {}
        facts: dict[str, Any] = {
            "facts_source": "server-inferred",
            "branch": row.get("branch"),
            "head_commit": row.get("head_commit"),
            "uncommitted_files": [d["path"] for d in dirty],
            "dirty_count": int(count["n"]) if count else 0,
            "last_heartbeat_age_s": _age_s(now, row.get("last_heartbeat_at") or row.get("last_seen_at")),
            "last_checkpoint": last["headline"] if last else None,
        }
        for key in ("commits", "unpushed_commits", "tests", "todos_open"):
            if isinstance(prior, dict) and key in prior:
                facts[key] = prior[key]
        return facts

    async def _checkpoint(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        *,
        trigger: str,
        facts: Mapping[str, Any],
        facts_source: str,
        headline: str,
        now: datetime,
        task_id: str | None = None,
    ) -> str:
        body = dict(facts)
        digest = _facts_hash({"trigger": trigger, "facts": body})
        existing = await _one(
            tx.conn, "SELECT * FROM crew_checkpoints WHERE session_id = ? AND facts_hash = ?", (row["id"], digest)
        )
        if existing is not None:
            return str(existing["id"])
        ckp_id = new_id("checkpoint")
        await tx.conn.execute(
            """INSERT INTO crew_checkpoints (id, crew_id, session_id, task_id, trigger, facts, facts_hash, headline,
                   facts_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ckp_id,
                row["crew_id"],
                row["id"],
                task_id,
                trigger,
                _dumps(body),
                digest,
                headline[:200],
                facts_source,
                format_ts(now),
            ),
        )
        stored = await _one(tx.conn, "SELECT * FROM crew_checkpoints WHERE id = ?", (ckp_id,))
        assert stored is not None
        res = await tx.emit(
            crew_id=row["crew_id"],
            type="checkpoint.created",
            actor=session_actor(row) if facts_source != "server-inferred" else Actor.system(),
            payload={"checkpoint": checkpoint_view(stored)},
            summary=f"{row['callsign']} checkpoint ({trigger})",
            refs={"session_id": row["id"], "task_id": task_id},
            now=now,
        )
        await tx.conn.execute("UPDATE crew_checkpoints SET seq = ? WHERE id = ?", (res.seq, ckp_id))
        await tx.conn.execute(
            "UPDATE crew_sessions SET last_checkpoint_id = ?, calls_since_checkpoint = 0 WHERE id = ?", (ckp_id, row["id"])
        )
        return ckp_id

    async def _stall_work(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        *,
        reserve_reason: str,
        report_kind: str,
        trigger: str,
        facts: dict[str, Any],
        facts_source: str,
        baton_ref: str | None,
        end_reason: str,
        handoff: bool,
        now: datetime,
    ) -> _Effects:
        """The baton path shared by quota, lost and a dirty end (§8.2, §10.2).

        Reserve the holder's claims, stall its unfinished tasks with a report
        (``stalled``, or ``partial`` on a session end), checkpoint, queue the
        relay handoff through the outbox (D35), and raise the baton inbox items.
        """
        effects = _Effects()
        settings = await self.settings(row["crew_id"])
        effects.checkpoint_id = await self._checkpoint(
            tx,
            row,
            trigger=trigger,
            facts=facts,
            facts_source=facts_source,
            headline=f"{trigger}: {_dirty_count(facts)} uncommitted files, {_unpushed_count(facts)} unpushed commits",
            now=now,
            task_id=row.get("current_task_id") if schemas.is_id("task", row.get("current_task_id")) else None,
        )
        if handoff:
            effects.handoff_id = await self.store.enqueue_outbox(
                row["crew_id"],
                "relay_handoff",
                {
                    "user_id": row["user_id"],
                    "project_id": (await self._crew(row["crew_id"]))["project_id"],
                    "agent_id": row["agent_id"],
                    "session_id": row["session_id"],
                    "facts": {**facts, "facts_source": facts_source},
                    "end_reason": end_reason,
                    "agent_verified": bool(row.get("agent_verified")),
                },
                dedupe_key=f"{row['id']}:{end_reason}:{effects.checkpoint_id}",
            )
            await tx.emit(
                crew_id=row["crew_id"],
                type="handoff.created",
                actor=Actor.system(),
                payload={
                    "handoff_id": effects.handoff_id,
                    "end_reason": end_reason[:80],
                    "facts_source": facts_source,
                    "task_id": None,
                },
                summary=f"handoff queued for {row['callsign']} ({_word(end_reason.split(':')[0])})",
                refs={"session_id": row["id"]},
                now=now,
            )
        reserved = await self._reserve_claims(tx, row, reserve_reason, baton_ref, settings, now)
        effects.claims_reserved = [c["id"] for c in reserved]
        tasks = await _all(
            tx.conn,
            f"""SELECT * FROM crew_tasks WHERE owner_session_id = ? AND status IN ({_marks(UNFINISHED_TASK_STATUSES)})
                ORDER BY number""",
            (row["id"], *UNFINISHED_TASK_STATUSES),
        )
        for task in tasks:
            report_id = await self._stall_task(
                tx, row, task, report_kind, facts, facts_source, baton_ref, effects.handoff_id, now
            )
            effects.tasks_stalled.append(task["id"])
            effects.report_ids.append(report_id)
        baton_keys = {c.get("task_id") or c["id"] for c in reserved} | set(effects.tasks_stalled)
        for key in sorted(baton_keys):
            await self._raise_baton_items(tx, row, key, reserve_reason, now)
        return effects

    async def _reserve_claims(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        reason: str,
        baton_ref: str | None,
        settings: Mapping[str, Any],
        now: datetime,
        *,
        release_pending: bool = True,
    ) -> list[dict[str, Any]]:
        """Live claims of the session → ``reserved(reason, reserved_for = session)``; queued requests are released.

        Claims the session already parked for itself (``idle`` or ``offline``
        reservations) take the new reason too, so a baton that became
        pick-up-able (lost, quota, ended_dirty, baton) is offered and adoptable.
        """
        now_s = format_ts(now)
        reserved: list[dict[str, Any]] = []
        parked = reason not in PARKED_REASONS
        for claim in await _all(
            tx.conn,
            "SELECT * FROM crew_claims WHERE holder_session_id = ? AND (state IN ('requested','queued','active','offered')"
            " OR (? AND state = 'reserved' AND reserve_reason IN ('idle','offline') AND reserved_for = holder_session_id))"
            " ORDER BY created_at",
            (row["id"], 1 if parked else 0),
        ):
            if claim["state"] in ("requested", "queued"):
                if release_pending:
                    await self._release_claim(tx, row, claim, f"session_{reason}", now, actor=Actor.system())
                continue
            expires = self._reserve_expiry(claim, settings, now)
            cur = await tx.conn.execute(
                """UPDATE crew_claims SET state = 'reserved', reserve_reason = ?, reserved_for = ?, reserve_expires_at = ?,
                       baton_ref = COALESCE(?, baton_ref), offered_to = NULL, offer_expires_at = NULL,
                       version = version + 1, updated_at = ?
                    WHERE id = ? AND (state IN ('active','offered')
                          OR (state = 'reserved' AND reserve_reason IN ('idle','offline')))""",
                (reason, row["id"], expires, baton_ref, now_s, claim["id"]),
            )
            if cur.rowcount != 1:
                continue
            fresh = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
            assert fresh is not None
            reserved.append(fresh)
            await tx.emit(
                crew_id=row["crew_id"],
                type="claim.reserved",
                actor=Actor.system(),
                payload={"claim": claim_view(fresh, now), "reason": reason},
                summary=f"claim {fresh['id']} reserved ({reason}) for the next pickup",
                refs={
                    "claim_id": fresh["id"],
                    "zone_id": fresh.get("zone_id"),
                    "task_id": fresh.get("task_id"),
                    "session_id": row["id"],
                },
                now=now,
            )
        return reserved

    @staticmethod
    def _reserve_expiry(claim: Mapping[str, Any], settings: Mapping[str, Any], now: datetime) -> str | None:
        """Task-linked batons never silently expire unless the owner set ``task_baton_expiry`` (D5)."""
        if claim.get("task_id"):
            expiry = settings.get("task_baton_expiry", "never")
            return None if expiry == "never" else format_ts(now + timedelta(seconds=int(expiry)))
        return format_ts(now + timedelta(seconds=int(settings["reserve_ttl_s"])))

    async def _release_claim(
        self, tx: EventTx, row: Mapping[str, Any], claim: Mapping[str, Any], end_reason: str, now: datetime, *, actor: Actor
    ) -> bool:
        now_s = format_ts(now)
        cur = await tx.conn.execute(
            """UPDATE crew_claims SET state = 'released', ended_at = ?, end_reason = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND state IN ('requested','queued','active','offered','reserved')""",
            (now_s, end_reason[:64], now_s, claim["id"]),
        )
        if cur.rowcount != 1:
            return False
        fresh = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
        assert fresh is not None
        await tx.emit(
            crew_id=row["crew_id"],
            type="claim.released",
            actor=actor,
            payload={"claim": claim_view(fresh, now), "baton": False},
            summary=f"claim {fresh['id']} released ({_word(end_reason)})",
            refs={
                "claim_id": fresh["id"],
                "zone_id": fresh.get("zone_id"),
                "task_id": fresh.get("task_id"),
                "session_id": row["id"],
            },
            now=now,
        )
        return True

    async def _stall_task(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        task: Mapping[str, Any],
        report_kind: str,
        facts: Mapping[str, Any],
        facts_source: str,
        baton_ref: str | None,
        handoff_id: str | None,
        now: datetime,
    ) -> str:
        """Task → ``stalled`` with exactly one current report (§5.4, §5.6 invariant)."""
        now_s = format_ts(now)
        report_id = new_id("report")
        digest = _facts_hash({"kind": report_kind, "task": task["id"], "facts": dict(facts), "at": now_s})
        for prev in await _all(tx.conn, "SELECT * FROM crew_reports WHERE task_id = ? AND is_current = 1", (task["id"],)):
            await tx.conn.execute(
                "UPDATE crew_reports SET is_current = 0, superseded_reason = ? WHERE id = ?",
                (f"replaced_by_{report_kind}", prev["id"]),
            )
            old = await _one(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (prev["id"],))
            assert old is not None
            await tx.emit(
                crew_id=row["crew_id"],
                type="report.superseded",
                actor=Actor.system(),
                payload={"report": report_view(old)},
                summary=f"report {old['id']} for T-{task['number']} superseded",
                refs={"report_id": old["id"], "task_id": task["id"]},
                now=now,
            )
        commits = _as_list(facts.get("commits"))
        files = _as_list(facts.get("uncommitted_files"))
        tests = _as_list(facts.get("tests"))
        sections = {
            "done": [],
            "not_done": [f"T-{task['number']} unfinished ({report_kind})"],
            "failing": [],
            "next": ["adopt the baton and continue"] if report_kind == "stalled" else [],
            "follow_ups": [],
        }
        await tx.conn.execute(
            """INSERT INTO crew_reports (id, crew_id, task_id, session_id, kind, verdict, criteria, commits, files, tests,
                   sections, facts_source, facts_hash, is_current, handoff_id, baton_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                report_id,
                row["crew_id"],
                task["id"],
                row["id"],
                report_kind,
                "partial" if report_kind == "partial" else None,
                _dumps(commits[:200]),
                _dumps(files[:200]),
                _dumps(tests[:50]),
                _dumps(sections),
                facts_source,
                digest,
                handoff_id,
                baton_ref,
                now_s,
            ),
        )
        await tx.conn.execute(
            """UPDATE crew_tasks SET status = 'stalled', status_before_stall = ?, stalled_at = ?, current_report_id = ?,
                   version = version + 1, updated_at = ?
                WHERE id = ? AND status = ?""",
            (task["status"], now_s, report_id, now_s, task["id"], task["status"]),
        )
        stored = await _one(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (report_id,))
        fresh = await _one(tx.conn, "SELECT * FROM crew_tasks WHERE id = ?", (task["id"],))
        assert stored is not None and fresh is not None
        await tx.emit(
            crew_id=row["crew_id"],
            type="report.submitted",
            actor=Actor.system() if facts_source == "server-inferred" else session_actor(row),
            payload={"report": report_view(stored)},
            summary=f"{report_kind} report for T-{task['number']} ({facts_source})",
            refs={"report_id": report_id, "task_id": task["id"], "session_id": row["id"]},
            now=now,
        )
        await tx.emit(
            crew_id=row["crew_id"],
            type="task.stalled",
            actor=Actor.system(),
            payload={"task": await task_view(tx.conn, fresh), "reason": report_kind},
            summary=f"T-{task['number']} stalled ({row['callsign']})",
            refs={"task_id": task["id"], "session_id": row["id"], "report_id": report_id},
            now=now,
        )
        return report_id

    # -- inbox items for batons (§5.8) ---------------------------------------------------------------

    async def _inbox_upsert(
        self,
        tx: EventTx,
        crew_id: str,
        *,
        audience: str,
        recipient: str | None,
        kind: str,
        title: str,
        ref_type: str | None,
        ref_id: str | None,
        priority: int,
        primary_action: str | None,
        dedupe_key: str,
        actor: Actor,
        origin: str,
        now: datetime,
    ) -> str:
        now_s = format_ts(now)
        existing = await _one(
            tx.conn,
            "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
            (crew_id, dedupe_key),
        )
        if existing is not None:
            await tx.conn.execute(
                "UPDATE crew_inbox_items SET coalesced_count = coalesced_count + 1, updated_at = ? WHERE id = ?",
                (now_s, existing["id"]),
            )
            return str(existing["id"])
        item_id = new_id("inbox_item")
        await tx.conn.execute(
            """INSERT INTO crew_inbox_items (id, crew_id, audience, recipient, kind, origin, ref_type, ref_id, priority, title,
                   primary_action, state, dedupe_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (
                item_id,
                crew_id,
                audience,
                recipient,
                kind,
                origin,
                ref_type,
                ref_id,
                priority,
                title[:200],
                primary_action,
                dedupe_key,
                now_s,
                now_s,
            ),
        )
        stored = await _one(tx.conn, "SELECT * FROM crew_inbox_items WHERE id = ?", (item_id,))
        assert stored is not None
        res = await tx.emit(
            crew_id=crew_id,
            type="inbox.item_created",
            actor=actor,
            payload={"item": inbox_view(stored)},
            summary=f"inbox {audience}: {kind}",
            refs={"inbox_item_id": item_id},
            now=now,
        )
        await tx.conn.execute("UPDATE crew_inbox_items SET created_seq = ? WHERE id = ?", (res.seq, item_id))
        return item_id

    async def _raise_baton_items(self, tx: EventTx, row: Mapping[str, Any], key: str, reason: str, now: datetime) -> None:
        """Needs-you ``baton_available`` and a crew ``baton_reserved`` item for one baton (task or claim)."""
        is_task = key.startswith(schemas.ID_PREFIXES["task"] + "_")
        label = key
        if is_task:
            task = await _one(tx.conn, "SELECT number FROM crew_tasks WHERE id = ?", (key,))
            label = f"T-{task['number']}" if task else key
        who = row["callsign"]
        await self._inbox_upsert(
            tx,
            row["crew_id"],
            audience="project",
            recipient=None,
            kind="baton_available",
            title=f"Baton {label} is waiting for pickup ({who} stopped: {reason})",
            ref_type="task" if is_task else "claim",
            ref_id=key,
            priority=1,
            primary_action="hand_baton",
            dedupe_key=f"baton_available:{key}",
            actor=Actor.system(),
            origin="server",
            now=now,
        )
        await self._inbox_upsert(
            tx,
            row["crew_id"],
            audience="crew",
            recipient=None,
            kind="baton_reserved",
            title=f"Baton {label} reserved for the next pickup ({who}, {reason})",
            ref_type="task" if is_task else "claim",
            ref_id=key,
            priority=2,
            primary_action="adopt",
            dedupe_key=f"baton_reserved:{key}",
            actor=Actor.system(),
            origin="server",
            now=now,
        )

    async def _resolve_baton_items(self, tx: EventTx, crew_id: str, keys: Iterable[str], actor: Actor, now: datetime) -> None:
        wanted = sorted({k for k in keys if k})
        if not wanted:
            return
        marks = ", ".join("?" for _ in wanted)
        items = await _all(
            tx.conn,
            f"""SELECT * FROM crew_inbox_items WHERE crew_id = ? AND ref_id IN ({marks})
                AND kind IN ('baton_available','baton_reserved','baton_waiting') AND state IN ('open','seen','claimed')""",
            (crew_id, *wanted),
        )
        for item in items:
            await tx.conn.execute(
                "UPDATE crew_inbox_items SET state = 'resolved', resolved_by = ?, updated_at = ? WHERE id = ?",
                (actor.id, format_ts(now), item["id"]),
            )
            fresh = await _one(tx.conn, "SELECT * FROM crew_inbox_items WHERE id = ?", (item["id"],))
            assert fresh is not None
            res = await tx.emit(
                crew_id=crew_id,
                type="inbox.item_resolved",
                actor=actor,
                payload={"item": inbox_view(fresh)},
                summary=f"inbox {item['kind']} resolved",
                refs={"inbox_item_id": item["id"]},
                now=now,
            )
            await tx.conn.execute("UPDATE crew_inbox_items SET resolved_seq = ? WHERE id = ?", (res.seq, item["id"]))

    # -- lost ------------------------------------------------------------------------------------------

    async def mark_lost(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        reason: str,
        now: datetime,
        *,
        baton_ref: str | None = None,
        handoff: bool = True,
    ) -> bool:
        """Session → ``lost`` and the lost synthesis (§10.2), in the caller's transaction. False if the state moved."""
        if reason not in schemas.LOST_REASONS:
            raise ValueError(f"unknown lost reason {reason!r}")
        live_before = await live_session_count(tx.conn, row["crew_id"])
        age = _age_s(now, row.get("last_heartbeat_at") or row.get("last_seen_at")) or 0
        cur = await tx.conn.execute(
            "UPDATE crew_sessions SET state = 'lost', state_reason = ?, quiet_reason = NULL WHERE id = ? AND state = ?",
            (reason, row["id"], row["state"]),
        )
        if cur.rowcount != 1:
            return False
        await tx.emit(
            crew_id=row["crew_id"],
            type="session.lost",
            actor=Actor.system(),
            payload={"reason": reason, "last_signal_age_s": age},
            summary=f"{row['callsign']} lost ({reason})",
            refs={"session_id": row["id"]},
            now=now,
        )
        if baton_ref:
            await self._baton_ref_created(tx, row, baton_ref, {}, now)
        if row["state"] == "quota_blocked":
            await self._emit_mode_change(tx, row["crew_id"], live_before, Actor.system(), now)
            return True  # the quota stall already reserved the work and wrote the report
        facts = await self._inferred_facts(tx.conn, row, now)
        await self._stall_work(
            tx,
            row,
            reserve_reason="lost",
            report_kind="stalled",
            trigger="lost",
            facts=facts,
            facts_source="server-inferred",
            baton_ref=baton_ref,
            end_reason="orphaned" if reason == "process_exited" else "auto:silent",
            handoff=handoff,
            now=now,
        )
        await self._emit_mode_change(tx, row["crew_id"], live_before, Actor.system(), now)
        return True

    # -- leave ---------------------------------------------------------------------------------------------

    async def leave(
        self, session: Mapping[str, Any], *, reason: str, facts: Any, summary: str | None, baton: bool, baton_ref: str | None
    ) -> dict[str, Any]:
        """``POST /sessions/{sid}/leave`` (§8.2 SessionEnd, §8.1 orphan).

        ``process_exited`` (crewd found the agent pid dead) marks the session
        ``lost`` at once; crewd already wrote the orphan handoff, so none is
        queued here. Otherwise every claim is reserved (with the baton ref) when
        the work is dirty, the task is unfinished, ``baton`` is set or the reason
        is ``clear``; else released. Unfinished tasks stall with a ``partial``
        report. The session ends.
        """
        now = self.now()
        cleaned = clean_facts(facts)
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session["id"])
            if row is None:
                raise SessionError(404, "not_found", "Not found.")
            if row["state"] == "ended":
                return {"state": "ended", "already": True, "seq": await self._last_seq(tx.conn, row["crew_id"])}
            if reason in ("process_exited", "orphaned"):
                if row["state"] != "lost":
                    await self.mark_lost(tx, row, "process_exited", now, baton_ref=baton_ref, handoff=False)
                return {"state": "lost", "already": False, "seq": await self._last_seq(tx.conn, row["crew_id"])}
            if baton_ref:
                await self._baton_ref_created(tx, row, baton_ref, cleaned, now)
            effects = await self._end_work(tx, row, reason=reason, facts=cleaned, baton=baton, baton_ref=baton_ref, now=now)
            await self._end_session(tx, row, reason, now, actor=session_actor(row), effects=effects)
            return {
                "state": "ended",
                "already": False,
                "claims_released": effects.claims_released,
                "claims_reserved": effects.claims_reserved,
                "tasks_stalled": effects.tasks_stalled,
                "report_ids": effects.report_ids,
                "checkpoint_id": effects.checkpoint_id,
                "seq": await self._last_seq(tx.conn, row["crew_id"]),
            }

    async def _end_work(
        self,
        tx: EventTx,
        row: Mapping[str, Any],
        *,
        reason: str,
        facts: dict[str, Any],
        baton: bool,
        baton_ref: str | None,
        now: datetime,
    ) -> _Effects:
        dirty = bool(baton_ref) or _dirty_count(facts) > 0
        unfinished = await _all(
            tx.conn,
            f"SELECT id FROM crew_tasks WHERE owner_session_id = ? AND status IN ({_marks(UNFINISHED_TASK_STATUSES)})",
            (row["id"], *UNFINISHED_TASK_STATUSES),
        )
        if dirty or unfinished or baton or reason == "clear":
            effects = await self._stall_work(
                tx,
                row,
                reserve_reason="baton" if baton else "ended_dirty",
                report_kind="partial",
                trigger="close",
                facts=facts or await self._inferred_facts(tx.conn, row, now),
                facts_source="relay-cli" if facts else "server-inferred",
                baton_ref=baton_ref,
                end_reason=f"ended:{reason}",
                handoff=False,  # crewd ran the relay close itself (§8.2 SessionEnd step 1)
                now=now,
            )
            return effects
        effects = _Effects()
        if facts:
            effects.checkpoint_id = await self._checkpoint(
                tx, row, trigger="close", facts=facts, facts_source="relay-cli", headline="close: clean", now=now
            )
        for claim in await _all(
            tx.conn,
            "SELECT * FROM crew_claims WHERE holder_session_id = ? AND (state IN ('requested','queued','active','offered')"
            " OR (state = 'reserved' AND reserve_reason IN ('idle','offline') AND reserved_for = holder_session_id))"
            " ORDER BY created_at",
            (row["id"],),
        ):
            if await self._release_claim(tx, row, claim, "session_ended", now, actor=session_actor(row)):
                effects.claims_released.append(claim["id"])
        return effects

    async def _end_session(
        self, tx: EventTx, row: Mapping[str, Any], reason: str, now: datetime, *, actor: Actor, effects: _Effects | None = None
    ) -> None:
        live_before = await live_session_count(tx.conn, row["crew_id"])
        now_s = format_ts(now)
        cur = await tx.conn.execute(
            """UPDATE crew_sessions SET state = 'ended', ended_at = ?, end_reason = ?, state_reason = ?, quiet_reason = NULL
                WHERE id = ? AND state != 'ended'""",
            (now_s, reason[:64], reason[:64], row["id"]),
        )
        if cur.rowcount != 1:
            return
        await tx.emit(
            crew_id=row["crew_id"],
            type="session.left",
            actor=actor,
            payload={
                "reason": reason[:64],
                "claims_released": (effects.claims_released if effects else [])[:50],
                "claims_reserved": (effects.claims_reserved if effects else [])[:50],
            },
            summary=f"{row['callsign']} left ({_word(reason)})",
            refs={"session_id": row["id"]},
            now=now,
        )
        await self._emit_mode_change(tx, row["crew_id"], live_before, actor, now)

    async def _emit_mode_change(self, tx: EventTx, crew_id: str, live_before: int, actor: Actor, now: datetime) -> None:
        live_after = await live_session_count(tx.conn, crew_id)
        before = "multi" if live_before >= 2 else "solo"
        after = "multi" if live_after >= 2 else "solo"
        if before == after:
            return
        await tx.emit(
            crew_id=crew_id,
            type="crew.mode_changed",
            actor=actor if actor.kind != "human" else Actor.system(),
            payload={"from": before, "to": after, "live_sessions": live_after},
            summary="crew assembled" if after == "multi" else "crew back to solo",
            now=now,
        )

    # -- human actions (D27; the router enforces the human principal and role) -------------------------------

    async def pause(self, session: Mapping[str, Any], *, human_user_id: str, reason: str) -> dict[str, Any]:
        now = self.now()
        actor = Actor.human(human_user_id)
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session["id"])
            if row is None or row["state"] in ("ended", "lost"):
                raise _conflict("session_not_live", "Only a live session can be paused.")
            if row["state"] == "paused":
                return {"state": "paused", "already": True, "seq": await self._last_seq(tx.conn, row["crew_id"])}
            await tx.conn.execute(
                "UPDATE crew_sessions SET state = 'paused', state_reason = 'paused_by_human', quiet_reason = NULL"
                " WHERE id = ? AND state = ?",
                (row["id"], row["state"]),
            )
            await tx.emit(
                crew_id=row["crew_id"],
                type="session.paused",
                actor=actor,
                payload={"reason": reason[:280]},
                summary=f"{row['callsign']} paused by a human",
                refs={"session_id": row["id"]},
                now=now,
            )
            await self._human_override(tx, row, "pause", reason, actor, now)
            await self._inbox_upsert(
                tx,
                row["crew_id"],
                audience="session",
                recipient=row["id"],
                kind="override_notice",
                title="Mani paused this session. Stop editing and wait for resume.",
                ref_type="session",
                ref_id=row["id"],
                priority=0,
                primary_action=None,
                dedupe_key=f"pause:{row['id']}",
                actor=actor,
                origin="human",
                now=now,
            )
            seq = await self._last_seq(tx.conn, row["crew_id"])
        await self._audit(
            {
                "action": "crew.pause",
                "user_id": human_user_id,
                "crew_id": row["crew_id"],
                "session_id": row["id"],
                "reason": reason,
            }
        )
        return {"state": "paused", "already": False, "seq": seq}

    async def resume(self, session: Mapping[str, Any], *, human_user_id: str, reason: str) -> dict[str, Any]:
        now = self.now()
        actor = Actor.human(human_user_id)
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session["id"])
            if row is None or row["state"] != "paused":
                raise _conflict("session_not_paused", "Only a paused session can be resumed.")
            activity = parse_ts(row["last_activity_at"]) if row.get("last_activity_at") else None
            to = "active" if activity and (now - activity).total_seconds() <= ACTIVE_WINDOW_S else "idle"
            await tx.conn.execute(
                "UPDATE crew_sessions SET state = ?, state_reason = 'resumed_by_human' WHERE id = ? AND state = 'paused'",
                (to, row["id"]),
            )
            await tx.emit(
                crew_id=row["crew_id"],
                type="session.resumed",
                actor=actor,
                payload={"to": to, "reason": reason[:280]},
                summary=f"{row['callsign']} resumed by a human",
                refs={"session_id": row["id"]},
                now=now,
            )
            await self._human_override(tx, row, "resume", reason, actor, now)
            await self._resolve_session_items(tx, row, [f"pause:{row['id']}"], actor, now)
            seq = await self._last_seq(tx.conn, row["crew_id"])
        await self._audit(
            {
                "action": "crew.resume",
                "user_id": human_user_id,
                "crew_id": row["crew_id"],
                "session_id": row["id"],
                "reason": reason,
            }
        )
        return {"state": to, "seq": seq}

    async def request_checkpoint(self, session: Mapping[str, Any], *, human_user_id: str, reason: str) -> dict[str, Any]:
        """Queue a human checkpoint request for the session (delivered as ``inject_text`` on its next heartbeat)."""
        now = self.now()
        actor = Actor.human(human_user_id)
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session["id"])
            if row is None or row["state"] not in LIVE_STATES:
                raise _conflict("session_not_live", "Only a live session can be asked for a checkpoint.")
            item_id = await self._inbox_upsert(
                tx,
                row["crew_id"],
                audience="session",
                recipient=row["id"],
                kind="override_notice",
                title="Mani asked for a checkpoint now: call crew_checkpoint or run remembra-crew checkpoint.",
                ref_type="session",
                ref_id=row["id"],
                priority=1,
                primary_action="checkpoint",
                dedupe_key=f"checkpoint_request:{row['id']}",
                actor=actor,
                origin="human",
                now=now,
            )
            seq = await self._last_seq(tx.conn, row["crew_id"])
        await self._audit(
            {
                "action": "crew.request_checkpoint",
                "user_id": human_user_id,
                "crew_id": row["crew_id"],
                "session_id": row["id"],
                "reason": reason,
            }
        )
        return {"inbox_item_id": item_id, "seq": seq}

    async def release_all(self, session: Mapping[str, Any], *, human_user_id: str, reason: str) -> dict[str, Any]:
        """Human-only (§5.1): release every live claim of the session, reserved ones included."""
        now = self.now()
        actor = Actor.human(human_user_id)
        async with self.log.transaction() as tx:
            row = await get_session(tx.conn, session["id"])
            if row is None:
                raise SessionError(404, "not_found", "Not found.")
            released: list[str] = []
            for claim in await _all(
                tx.conn,
                "SELECT * FROM crew_claims WHERE holder_session_id = ?"
                " AND state IN ('requested','queued','active','offered','reserved') ORDER BY created_at",
                (row["id"],),
            ):
                if await self._release_claim(tx, row, claim, "human_release_all", now, actor=actor):
                    released.append(claim["id"])
            await self._human_override(tx, row, "release_all", reason, actor, now)
            if row["state"] in LIVE_STATES:
                await self._inbox_upsert(
                    tx,
                    row["crew_id"],
                    audience="session",
                    recipient=row["id"],
                    kind="override_notice",
                    title=f"Mani released all {len(released)} claims of this session. Claim again before editing.",
                    ref_type="session",
                    ref_id=row["id"],
                    priority=0,
                    primary_action=None,
                    dedupe_key=f"release_all:{row['id']}:{new_id('inbox_item')}",
                    actor=actor,
                    origin="human",
                    now=now,
                )
            await self._resolve_baton_items(tx, row["crew_id"], released, actor, now)
            seq = await self._last_seq(tx.conn, row["crew_id"])
        await self._audit(
            {
                "action": "crew.release_all",
                "user_id": human_user_id,
                "crew_id": row["crew_id"],
                "session_id": row["id"],
                "claims": released,
                "reason": reason,
            }
        )
        return {"claims_released": released, "seq": seq}

    async def _human_override(
        self, tx: EventTx, row: Mapping[str, Any], action: str, reason: str, actor: Actor, now: datetime
    ) -> None:
        await tx.emit(
            crew_id=row["crew_id"],
            type="human.override",
            actor=actor,
            payload={"action": action, "reason": reason[:280], "target_kind": "session", "target_id": row["id"]},
            summary=f"human {action.replace('_', ' ')} on {row['callsign']}",
            refs={"session_id": row["id"]},
            now=now,
        )

    async def _resolve_session_items(
        self, tx: EventTx, row: Mapping[str, Any], dedupe_keys: Sequence[str], actor: Actor, now: datetime
    ) -> None:
        for key in dedupe_keys:
            item = await _one(
                tx.conn,
                "SELECT * FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ? AND state IN ('open','seen','claimed')",
                (row["crew_id"], key),
            )
            if item is None:
                continue
            await tx.conn.execute(
                "UPDATE crew_inbox_items SET state = 'resolved', resolved_by = ?, updated_at = ? WHERE id = ?",
                (actor.id, format_ts(now), item["id"]),
            )
            fresh = await _one(tx.conn, "SELECT * FROM crew_inbox_items WHERE id = ?", (item["id"],))
            assert fresh is not None
            await tx.emit(
                crew_id=row["crew_id"],
                type="inbox.item_resolved",
                actor=actor,
                payload={"item": inbox_view(fresh)},
                summary=f"inbox {item['kind']} resolved",
                refs={"inbox_item_id": item["id"]},
                now=now,
            )

    # -- reads -----------------------------------------------------------------------------------------------

    async def list_sessions(self, crew_id: str, state: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        """Sessions of a crew (live first, newest first), with the seat and fence facts the dashboard shows."""
        now = self.now()
        if state is not None and state not in schemas.PRESENCE_STATES and state != "live":
            raise SessionError(422, "invalid_state", f"state must be one of {', '.join(schemas.PRESENCE_STATES)} or live")
        sql = "SELECT * FROM crew_sessions WHERE crew_id = ?"
        params: list[Any] = [crew_id]
        if state == "live":
            sql += f" AND state IN ({', '.join('?' for _ in LIVE_STATES)})"
            params.extend(LIVE_STATES)
        elif state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY CASE WHEN state = 'ended' THEN 1 ELSE 0 END, joined_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = await _all(self.conn, sql, params)
        limits = await self.limits_for((await self._crew(crew_id))["owner_user_id"])
        out = []
        for r in rows:
            view = session_view(r)
            view["observe_only"] = r["state"] in LIVE_STATES and not await has_seat(
                self.conn, crew_id, r["id"], limits.max_sessions_live
            )
            view["last_heartbeat_at"] = r.get("last_heartbeat_at")
            view["last_seen_at"] = r.get("last_seen_at")
            view["calls_since_checkpoint"] = int(r.get("calls_since_checkpoint") or 0)
            view["last_action"] = _loads(r.get("last_action"), None)
            view["activity_age_s"] = _age_s(now, r.get("last_activity_at"))
            out.append(view)
        return out


def _word(value: Any) -> str:
    """Only short identifier-like words reach an event summary (§4.1 templates)."""
    text = str(value or "").lower()
    return text if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,31}", text) else "other"
