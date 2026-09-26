"""Crew reaper: server-computed presence, lease expiry, fencing, idle park and baton notices (spec §10.1, §10.2).

Runs every 30 s (:data:`REAPER_INTERVAL_S`). Every threshold is measured on the
server clock from ``max(last_signal, server_boot_at)``, so a server restart never
mass-marks sessions (boot grace). Each transition is a conditional
``UPDATE … WHERE id = ? AND state = ?`` inside ``BEGIN IMMEDIATE`` with its event
in the same transaction, so running a sweep twice is a no-op.

Presence (§10.1):

* **active** – activity within 3 min; **idle** – alive but no activity;
* **quiet** – the host's heartbeat is missing for more than 3 min
  (``host_unreachable``), or an MCP-only session has made no call for 20 min
  (``mcp_silent``);
* **lost** – host unreachable for longer than ``host_lost_after_s`` (30 min,
  ``host_lost``); the host is alive but this session has been missing from its
  heartbeats for longer than its lease (``lease_expired``, D31 "lost = lease
  expiry"); an MCP-only session silent for 60 min (``mcp_silent``). crewd reports
  a dead pid immediately through ``leave {reason: process_exited}``.

Host-wide silence is not a stall (§10.1): when a host stops heartbeating it
becomes ``unreachable`` (``host.unreachable`` moment); its sessions go quiet;
at each claim's lease expiry the claim becomes ``reserved(offline,
reserved_for = holder)``, so others stay denied; tasks are **not** stalled and no
report is synthesized until ``host_lost_after_s`` passes.

Leases and fencing (D31): the holder is fenced at ``lease_expires_at - 60 s``
(``claim.fenced`` once per lease); at expiry the claim is reserved ``offline``.

Idle park (§10.2): a session idle for ``idle_park_s`` (60 min) parks its claims
as ``reserved(idle, reserved_for = self)`` with a low-priority Needs-you notice;
it re-takes them on its next activity.

Reservations (D5): non-task reservations expire after ``reserve_ttl_s``;
task-linked batons never silently expire (unless the owner set
``task_baton_expiry``) and raise Needs-you notices at 24 h and 72 h.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog

from remembra.crew import schemas, startup
from remembra.crew.db import CrewDatabase
from remembra.crew.events import Actor, format_ts, parse_ts
from remembra.crew.hosts import HOST_SILENT_AFTER_S
from remembra.crew.sessions import (
    ACTIVE_WINDOW_S,
    BATON_WAITING_LEVELS,
    HEARTBEAT_INTERVAL_S,
    LOST_SESSION_CLOSE_AFTER_S,
    MCP_LOST_AFTER_S,
    MCP_QUIET_AFTER_S,
    SESSION_SILENT_S,
    CrewSessions,
    _age_s,
    _all,
    _one,
    claim_view,
    fence_horizon,
    get_session,
    session_actor,
)
from remembra.crew.settings import load_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

log = structlog.get_logger(__name__)

REAPER_INTERVAL_S: Final = 30.0
REAPER_ORDER: Final = 40
SWEPT_STATES: Final = ("joining", "active", "idle", "quiet")


@dataclass(frozen=True)
class Presence:
    state: str
    quiet_reason: str | None = None
    lost_reason: str | None = None
    reason: str = ""


def presence_target(
    row: Mapping[str, Any],
    host: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
    *,
    now: datetime,
    boot_at: datetime,
) -> Presence:
    """The §10.1 state a swept session should be in (pure; the reaper applies it)."""
    activity_age = _age_s(now, row.get("last_activity_at") or row.get("joined_at"), boot_at) or 0
    if row.get("host_id"):
        host_age = _age_s(now, host.get("last_seen_at") if host else None, boot_at)
        host_age = host_age if host_age is not None else (_age_s(now, row.get("joined_at"), boot_at) or 0)
        session_age = _age_s(now, row.get("last_heartbeat_at") or row.get("joined_at"), boot_at) or 0
        if host_age > int(settings["host_lost_after_s"]):
            return Presence("lost", lost_reason="host_lost", reason="host_lost")
        if host_age > HOST_SILENT_AFTER_S:
            return Presence("quiet", quiet_reason="host_unreachable", reason="host_silent")
        if session_age > int(settings["lease_ttl_s"]):
            # §10.1 host-wide silence is not a stall: when the host went quiet with the session (its last
            # heartbeat is no newer than one interval after the session's), the host is unreachable, even
            # before HOST_SILENT_AFTER_S when the crew's lease is shorter than that (lease_ttl_s ≥ 120)
            if host_age + HEARTBEAT_INTERVAL_S >= session_age:
                return Presence("quiet", quiet_reason="host_unreachable", reason="host_silent")
            return Presence("lost", lost_reason="lease_expired", reason="lease_expired")
        if session_age > SESSION_SILENT_S:
            return Presence("quiet", quiet_reason="host_unreachable", reason="session_silent")
    else:
        signal_age = _age_s(now, row.get("last_seen_at") or row.get("joined_at"), boot_at) or 0
        if signal_age > MCP_LOST_AFTER_S:
            return Presence("lost", lost_reason="mcp_silent", reason="mcp_silent")
        if signal_age > MCP_QUIET_AFTER_S:
            return Presence("quiet", quiet_reason="mcp_silent", reason="mcp_silent")
    if activity_age <= ACTIVE_WINDOW_S:
        return Presence("active", reason="activity")
    return Presence("idle", reason="no_activity")


@dataclass
class SweepReport:
    hosts_unreachable: int = 0
    state_changes: int = 0
    lost: int = 0
    fenced: int = 0
    leases_expired: int = 0
    idle_parked: int = 0
    reservations_expired: int = 0
    baton_notices: int = 0
    lost_closed: int = 0
    stuck: int = 0
    contested: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class CrewReaper:
    """One sweep = hosts, presence, leases/fencing, idle park, reservations, stale lost sessions."""

    def __init__(self, sessions: CrewSessions) -> None:
        self.sessions = sessions
        self._settings_cache: dict[str, dict[str, Any]] = {}

    @property
    def boot_at(self) -> datetime:
        return self.sessions.boot_at

    async def _settings(self, crew_id: str) -> dict[str, Any]:
        cached = self._settings_cache.get(crew_id)
        if cached is None:
            row = await _one(self.sessions.conn, "SELECT settings FROM crews WHERE id = ?", (crew_id,))
            cached = self._settings_cache[crew_id] = load_settings(row["settings"] if row else None)
        return cached

    async def sweep(self) -> SweepReport:
        self._settings_cache = {}
        now = self.sessions.now()
        report = SweepReport()
        for step in (
            self._hosts,
            self._presence,
            self._leases,
            self._idle_park,
            self._reservations,
            self._close_lost,
            self._alarms,
        ):
            try:
                await step(now, report)
            except Exception as e:  # one failing step must not stop the others
                log.error("crew_reaper_step_failed", step=step.__name__, error_type=type(e).__name__, error=str(e))
                report.errors.append(f"{step.__name__}: {type(e).__name__}: {e}")
        return report

    # -- health alarms (stuck agents, contested zones; §5.8, §9.11) -------------------------

    async def _alarms(self, now: datetime, report: SweepReport) -> None:
        from remembra.crew import alarms

        if self.boot_at > now - timedelta(seconds=HOST_SILENT_AFTER_S):
            return  # boot grace: nobody could check in while the server was down
        got = await alarms.sweep(self.sessions.log, now, self._settings)
        report.stuck += got["stuck"]
        report.contested += got["contested"]

    # -- hosts ------------------------------------------------------------------------

    async def _hosts(self, now: datetime, report: SweepReport) -> None:
        cutoff = format_ts(now - timedelta(seconds=HOST_SILENT_AFTER_S))
        if self.boot_at > now - timedelta(seconds=HOST_SILENT_AFTER_S):
            return  # boot grace: nobody could heartbeat while the server was down
        hosts = await _all(
            self.sessions.conn,
            "SELECT * FROM crew_hosts WHERE state = 'online' AND last_seen_at IS NOT NULL AND last_seen_at < ?",
            (cutoff,),
        )
        live = schemas.LIVE_PRESENCE_STATES
        for host in hosts:
            async with self.sessions.log.transaction() as tx:
                cur = await tx.conn.execute(
                    "UPDATE crew_hosts SET state = 'unreachable' WHERE id = ? AND state = 'online' AND last_seen_at = ?",
                    (host["id"], host["last_seen_at"]),
                )
                if cur.rowcount != 1:
                    continue
                report.hosts_unreachable += 1
                silent = _age_s(now, host["last_seen_at"]) or 0
                rows = await _all(
                    tx.conn,
                    f"SELECT id, crew_id FROM crew_sessions WHERE host_id = ? AND state IN ({', '.join('?' for _ in live)})"
                    " ORDER BY crew_id, id",
                    (host["id"], *live),
                )
                by_crew: dict[str, list[str]] = {}
                for r in rows:
                    by_crew.setdefault(r["crew_id"], []).append(r["id"])
                for crew_id, session_ids in by_crew.items():
                    await tx.emit(
                        crew_id=crew_id,
                        type="host.unreachable",
                        actor=Actor.system(),
                        payload={"host_id": host["id"], "silent_s": silent, "session_ids": session_ids[:50]},
                        summary=f"host {host['host_label']} unreachable ({len(session_ids)} session{'' if len(session_ids) == 1 else 's'} quiet)",
                        refs={"host_id": host["id"]},
                        now=now,
                    )

    # -- presence -------------------------------------------------------------------------

    async def _presence(self, now: datetime, report: SweepReport) -> None:
        marks = ", ".join("?" for _ in SWEPT_STATES)
        rows = await _all(
            self.sessions.conn, f"SELECT * FROM crew_sessions WHERE state IN ({marks}) ORDER BY crew_id, id", SWEPT_STATES
        )
        hosts: dict[str, dict[str, Any] | None] = {}
        for row in rows:
            host = None
            if row.get("host_id"):
                if row["host_id"] not in hosts:
                    hosts[row["host_id"]] = await _one(
                        self.sessions.conn, "SELECT * FROM crew_hosts WHERE id = ?", (row["host_id"],)
                    )
                host = hosts[row["host_id"]]
            target = presence_target(row, host, await self._settings(row["crew_id"]), now=now, boot_at=self.boot_at)
            if target.state == row["state"] and target.quiet_reason == (
                row.get("quiet_reason") if row["state"] == "quiet" else None
            ):
                continue
            async with self.sessions.log.transaction() as tx:
                fresh = await get_session(tx.conn, row["id"])
                if fresh is None or fresh["state"] != row["state"]:
                    continue  # moved since the read (heartbeat, leave): the next sweep re-evaluates
                if target.lost_reason:
                    if await self.sessions.mark_lost(tx, fresh, target.lost_reason, now):
                        report.lost += 1
                elif await self.sessions._set_state(tx, fresh, target.state, target.reason, target.quiet_reason, now):
                    report.state_changes += 1

    # -- leases and fencing (D31) -------------------------------------------------------------

    async def _leases(self, now: datetime, report: SweepReport) -> None:
        claims = await _all(
            self.sessions.conn,
            "SELECT * FROM crew_claims WHERE state = 'active' AND holder_kind = 'session' AND lease_expires_at IS NOT NULL"
            " ORDER BY lease_expires_at",
        )
        for claim in claims:
            settings = await self._settings(claim["crew_id"])
            lease = parse_ts(claim["lease_expires_at"])
            grace = self.boot_at + timedelta(seconds=int(settings["lease_ttl_s"]))
            effective = max(lease, grace)
            horizon = parse_ts(fence_horizon(format_ts(effective)) or claim["lease_expires_at"])
            if now < horizon:
                continue
            async with self.sessions.log.transaction() as tx:
                fresh = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
                if fresh is None or fresh["state"] != "active" or fresh["lease_expires_at"] != claim["lease_expires_at"]:
                    continue
                holder = await get_session(tx.conn, fresh["holder_session_id"]) if fresh.get("holder_session_id") else None
                if now < effective:
                    res = await tx.emit(
                        crew_id=fresh["crew_id"],
                        type="claim.fenced",
                        actor=Actor.system(),
                        payload={"claim_id": fresh["id"], "horizon_at": format_ts(horizon)},
                        summary=f"claim {fresh['id']} fenced: lease unconfirmed" + (f" ({holder['callsign']})" if holder else ""),
                        refs={
                            "claim_id": fresh["id"],
                            "zone_id": fresh.get("zone_id"),
                            "session_id": fresh.get("holder_session_id"),
                        },
                        idem_key=f"claim:{fresh['id']}:fenced:{fresh['epoch']}:{fresh['lease_expires_at']}",
                        now=now,
                    )
                    if not res.replayed:
                        report.fenced += 1
                    continue
                expires = self.sessions._reserve_expiry(fresh, settings, now)
                cur = await tx.conn.execute(
                    """UPDATE crew_claims SET state = 'reserved', reserve_reason = 'offline', reserved_for = holder_session_id,
                           reserve_expires_at = ?, version = version + 1, updated_at = ?
                        WHERE id = ? AND state = 'active' AND lease_expires_at = ?""",
                    (expires, format_ts(now), fresh["id"], fresh["lease_expires_at"]),
                )
                if cur.rowcount != 1:
                    continue
                stored = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (fresh["id"],))
                assert stored is not None
                report.leases_expired += 1
                await tx.emit(
                    crew_id=fresh["crew_id"],
                    type="claim.reserved",
                    actor=Actor.system(),
                    payload={"claim": claim_view(stored, now), "reason": "offline"},
                    summary=f"claim {fresh['id']} reserved: lease expired"
                    + (f" ({holder['callsign']} offline)" if holder else ""),
                    refs={
                        "claim_id": fresh["id"],
                        "zone_id": fresh.get("zone_id"),
                        "task_id": fresh.get("task_id"),
                        "session_id": fresh.get("holder_session_id"),
                    },
                    now=now,
                )

    # -- idle park (§10.2) --------------------------------------------------------------------------

    async def _idle_park(self, now: datetime, report: SweepReport) -> None:
        rows = await _all(self.sessions.conn, "SELECT * FROM crew_sessions WHERE state = 'idle' ORDER BY crew_id, id")
        for row in rows:
            settings = await self._settings(row["crew_id"])
            age = _age_s(now, row.get("last_activity_at") or row.get("joined_at"), self.boot_at) or 0
            if age < int(settings["idle_park_s"]):
                continue
            async with self.sessions.log.transaction() as tx:
                fresh = await get_session(tx.conn, row["id"])
                if fresh is None or fresh["state"] != "idle":
                    continue
                active = await _one(
                    tx.conn, "SELECT 1 FROM crew_claims WHERE holder_session_id = ? AND state = 'active' LIMIT 1", (row["id"],)
                )
                if active is None:
                    continue
                parked = await self.sessions._reserve_claims(tx, fresh, "idle", None, settings, now, release_pending=False)
                if not parked:
                    continue
                report.idle_parked += len(parked)
                await self.sessions._inbox_upsert(
                    tx,
                    row["crew_id"],
                    audience="project",
                    recipient=None,
                    kind="idle_park",
                    title=f"{row['callsign']} idle for {age // 60} min: {len(parked)} claims parked for it",
                    ref_type="session",
                    ref_id=row["id"],
                    priority=3,
                    primary_action="release_all",
                    dedupe_key=f"idle_park:{row['id']}",
                    actor=Actor.system(),
                    origin="server",
                    now=now,
                )

    # -- reservations: expiry and escalating notices (D5) ---------------------------------------------

    async def _reservations(self, now: datetime, report: SweepReport) -> None:
        now_s = format_ts(now)
        for claim in await _all(
            self.sessions.conn,
            "SELECT * FROM crew_claims WHERE state = 'reserved' AND reserve_expires_at IS NOT NULL AND reserve_expires_at < ?",
            (now_s,),
        ):
            async with self.sessions.log.transaction() as tx:
                cur = await tx.conn.execute(
                    """UPDATE crew_claims SET state = 'expired', ended_at = ?, end_reason = 'reserve_ttl', version = version + 1,
                           updated_at = ? WHERE id = ? AND state = 'reserved' AND reserve_expires_at = ?""",
                    (now_s, now_s, claim["id"], claim["reserve_expires_at"]),
                )
                if cur.rowcount != 1:
                    continue
                stored = await _one(tx.conn, "SELECT * FROM crew_claims WHERE id = ?", (claim["id"],))
                assert stored is not None
                report.reservations_expired += 1
                await tx.emit(
                    crew_id=claim["crew_id"],
                    type="claim.expired",
                    actor=Actor.system(),
                    payload={"claim": claim_view(stored, now)},
                    summary=f"reserved claim {claim['id']} expired unclaimed",
                    refs={"claim_id": claim["id"], "zone_id": claim.get("zone_id"), "task_id": claim.get("task_id")},
                    now=now,
                )
                await self.sessions._resolve_baton_items(
                    tx, claim["crew_id"], [claim.get("task_id") or claim["id"]], Actor.system(), now
                )
                # a non-task reservation that ends frees its target: queued claims move up (§10.2, §5.1)
                await self.sessions._promote_queue(tx, claim["crew_id"])
        for claim in await _all(
            self.sessions.conn, "SELECT * FROM crew_claims WHERE state = 'reserved' AND task_id IS NOT NULL ORDER BY updated_at"
        ):
            waited = _age_s(now, claim["updated_at"]) or 0
            level = None
            for threshold, label, priority in BATON_WAITING_LEVELS:
                if waited >= threshold:
                    level = (label, priority)
            if level is None:
                continue
            label, priority = level
            async with self.sessions.log.transaction() as tx:
                exists = await _one(
                    tx.conn,
                    "SELECT 1 FROM crew_inbox_items WHERE crew_id = ? AND dedupe_key = ?",
                    (claim["crew_id"], f"baton_waiting:{claim['task_id']}:{label}"),
                )
                if exists is not None:
                    continue
                task = await _one(tx.conn, "SELECT number FROM crew_tasks WHERE id = ?", (claim["task_id"],))
                name = f"T-{task['number']}" if task else claim["task_id"]
                await self.sessions._inbox_upsert(
                    tx,
                    claim["crew_id"],
                    audience="project",
                    recipient=None,
                    kind="baton_waiting",
                    title=f"{name} still waiting for pickup after {label}",
                    ref_type="task",
                    ref_id=claim["task_id"],
                    priority=priority,
                    primary_action="hand_baton",
                    dedupe_key=f"baton_waiting:{claim['task_id']}:{label}",
                    actor=Actor.system(),
                    origin="server",
                    now=now,
                )
                report.baton_notices += 1

    # -- lost sessions with nothing left to pick up --------------------------------------------------------

    async def _close_lost(self, now: datetime, report: SweepReport) -> None:
        cutoff = format_ts(now - timedelta(seconds=LOST_SESSION_CLOSE_AFTER_S))
        rows = await _all(
            self.sessions.conn,
            "SELECT * FROM crew_sessions WHERE state = 'lost' AND COALESCE(last_heartbeat_at, last_seen_at, joined_at) < ?",
            (cutoff,),
        )
        for row in rows:
            async with self.sessions.log.transaction() as tx:
                held = await _one(
                    tx.conn, "SELECT 1 FROM crew_claims WHERE holder_session_id = ? AND state = 'reserved' LIMIT 1", (row["id"],)
                )
                if held is not None:
                    continue  # its baton is still waiting: keep the lane (and its callsign)
                fresh = await get_session(tx.conn, row["id"])
                if fresh is None or fresh["state"] != "lost":
                    continue
                await self.sessions._end_session(tx, fresh, "lost_expired", now, actor=Actor.system())
                report.lost_closed += 1

    # -- loop -------------------------------------------------------------------------------------------------

    async def run(self, interval_s: float = REAPER_INTERVAL_S) -> None:
        while True:
            report = await self.sweep()
            if report.errors:
                log.warning("crew_reaper_sweep_errors", errors=report.errors[:5])
            await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------
# Startup hook (§14: registered through remembra.crew.startup, never in main.py)
# ---------------------------------------------------------------------------

_TASK_KEY: Final = "task:crew-reaper"


def build_sessions_service(app: FastAPI) -> CrewSessions:
    """The process-wide :class:`CrewSessions` (``app.state.crew_sessions``), created on first use.

    Limits come from the crew owner's plan (``app.state.usage_meter``; self-hosted
    defaults without it). Human-only actions and cross-checkout adopts are
    written to the main ``audit_log`` (never deleted) after the crew commit.
    """
    existing: CrewSessions | None = getattr(app.state, "crew_sessions", None)
    if existing is not None:
        return existing
    from remembra.crew.limits import crew_limits_for_owner

    event_log = getattr(app.state, "crew_events", None)
    if event_log is None:
        from remembra.crew.events import CrewEventLog

        event_log = CrewEventLog(startup.crew_db(app), getattr(app.state, "crew_bus", None))
    meter = getattr(app.state, "usage_meter", None)

    async def limits_for(owner_user_id: str) -> Any:
        return await crew_limits_for_owner(meter, owner_user_id)

    db = startup.crew_db(app)
    if not isinstance(db, CrewDatabase):
        raise RuntimeError("crew mode: app.state.crew_db must be a remembra.crew.db.CrewDatabase")
    service = CrewSessions(db, event_log, limits_for=limits_for, audit=audit_sink(app), pii=_pii_scrubber(app))
    app.state.crew_sessions = service
    return service


def _pii_scrubber(app: FastAPI) -> Any:
    """The per-request PII scrub the relay routes use, for stall/leave facts (§11.2 redaction)."""
    from types import SimpleNamespace
    from typing import cast

    from fastapi import Request

    from remembra.api.v1.relay import pii_scrubber

    return pii_scrubber(cast(Request, SimpleNamespace(app=app)))


def audit_sink(app: FastAPI) -> Any:
    """Write a crew audit record to the main ``audit_log`` (action ``crew.*``), if the app has one."""

    async def write(record: Mapping[str, Any]) -> None:
        audit = getattr(app.state, "audit_logger", None)
        if audit is None:
            return
        import json

        details = {k: v for k, v in record.items() if k not in ("action", "user_id", "session_id")}
        await audit.db.log_audit_event(
            audit_id=audit.generate_audit_id(),
            user_id=str(record.get("user_id") or "system"),
            action=str(record["action"]),
            resource_id=record.get("session_id"),
            success=True,
            error_message=json.dumps(details, sort_keys=True, default=str)[:2000],
        )

    return write


async def _start_reaper(app: FastAPI, rt: startup.CrewRuntime) -> None:
    service = build_sessions_service(app)
    reaper = CrewReaper(service)
    app.state.crew_reaper = reaper
    tasks = getattr(app.state, "tasks", None)
    if tasks is None:
        raise RuntimeError("crew mode: app.state.tasks (TaskRegistry) is required to run the reaper")
    rt.extras[_TASK_KEY] = tasks.spawn(reaper.run(), name="crew-reaper", loop_task=True)
    log.info("crew_reaper_started", interval_s=REAPER_INTERVAL_S, boot_at=format_ts(service.boot_at))


async def _stop_reaper(app: FastAPI, rt: startup.CrewRuntime) -> None:
    task: asyncio.Task[Any] | None = rt.extras.pop(_TASK_KEY, None)
    app.state.crew_reaper = None
    app.state.crew_sessions = None
    if task is None or task.done():
        return
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait({task}, timeout=5.0)


def register_hooks() -> None:
    """Add (or re-add) the ``crew.reaper`` hook (order 40, after the bus and the outbox worker)."""
    startup.add_hook("crew.reaper", order=REAPER_ORDER, start=_start_reaper, stop=_stop_reaper)


register_hooks()

__all__ = [
    "CrewReaper",
    "Presence",
    "SweepReport",
    "build_sessions_service",
    "presence_target",
    "register_hooks",
    "session_actor",
]
