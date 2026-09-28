"""Shared fixtures for the WP-4 session, host and reaper tests.

Everything runs against a real ``crew.db`` (WP-1's ``CREW_MIGRATIONS``) and the
real event log (WP-2); the only stand-in is a controllable clock. Zones, tasks
and claims are inserted with the §3.2 columns the way WP-5/WP-6 create them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog, format_ts, verify_crew_chain
from remembra.crew.hosts import register_host
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS, CrewLimits
from remembra.crew.sessions import CrewSessions, JoinRequest
from remembra.crew.store import new_id

T0 = datetime(2026, 9, 25, 20, 0, 0, tzinfo=UTC)
OWNER = "u_owner"
PROJECT = "yaadbooks"


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


@dataclass
class Env:
    db: CrewDatabase
    log: CrewEventLog
    clock: Clock
    svc: CrewSessions
    audits: list[dict[str, Any]]

    @property
    def conn(self):
        return self.db.conn

    async def events(self, crew_id: str, *, types: tuple[str, ...] | None = None, after: int = 0) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT seq, type, payload, moment, actor, summary FROM crew_events WHERE crew_id = ? AND seq > ? ORDER BY seq",
            (crew_id, after),
        )
        rows = await cur.fetchall()
        out = [
            {
                "seq": r[0],
                "type": r[1],
                "payload": json.loads(r[2]),
                "moment": bool(r[3]),
                "actor": json.loads(r[4]),
                "summary": r[5],
            }
            for r in rows
        ]
        return [e for e in out if types is None or e["type"] in types]

    async def last_seq(self, crew_id: str) -> int:
        row = await self.db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
        return int(row["last_seq"]) if row else 0

    async def chain_ok(self, crew_id: str) -> None:
        report = await verify_crew_chain(self.conn, crew_id)
        assert report.ok, report.errors

    async def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return await self.db.fetchone(sql, params)

    async def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await self.db.fetchall(sql, params)


def limits_with(max_live: int) -> CrewLimits:
    return CrewLimits(
        tier="test",
        max_sessions_live=max_live,
        free_sub_agents_per_parent=SELF_HOSTED_CREW_LIMITS.free_sub_agents_per_parent,
        max_zones=SELF_HOSTED_CREW_LIMITS.max_zones,
        events_per_day_soft=SELF_HOSTED_CREW_LIMITS.events_per_day_soft,
        memory_promotions_per_day=SELF_HOSTED_CREW_LIMITS.memory_promotions_per_day,
        teammates=False,
        retention=SELF_HOSTED_CREW_LIMITS.retention,
    )


async def make_env(tmp_path: Path, *, max_live: int = 8, boot_at: datetime | None = None, name: str = "crew.db") -> Env:
    db = CrewDatabase(str(tmp_path / name))
    await db.init_schema()
    log = CrewEventLog(db)
    clock = Clock()
    audits: list[dict[str, Any]] = []

    async def limits_for(_owner: str) -> CrewLimits:
        return limits_with(max_live)

    async def audit(record: Any) -> None:
        audits.append(dict(record))

    svc = CrewSessions(db, log, clock=clock, limits_for=limits_for, audit=audit, boot_at=boot_at or (T0 - timedelta(days=1)))
    return Env(db=db, log=log, clock=clock, svc=svc, audits=audits)


def join_req(
    session_id: str,
    *,
    agent: str = "claude-code",
    verified: bool = True,
    project: str = PROJECT,
    checkout: str | None = "fp-a",
    host_id: str | None = None,
    adapter: str | None = None,
    client_kind: str = "hook",
    source: str = "startup",
    resume_of: str | None = None,
    head: str | None = "abc1234",
) -> JoinRequest:
    return JoinRequest(
        agent_id=agent,
        agent_verified=verified,
        project_id=project,
        session_id=session_id,
        adapter=adapter or agent,
        client_kind=client_kind,
        source=source,
        host_id=host_id,
        checkout_fp=checkout,
        worktree_id=f"wt-{checkout}" if checkout else None,
        branch="main",
        head=head,
        model="opus",
        resume_of=resume_of,
    )


async def host(env: Env, label: str = "mbp1", user: str = OWNER) -> tuple[dict[str, Any], str]:
    reg = await register_host(
        env.db, user_id=user, host_label=label, platform="darwin", crewd_version="1.0", now=format_ts(env.clock())
    )
    return reg.row, reg.token


async def add_zone(env: Env, crew_id: str, slug: str = "pos", *, reserve_for: str | None = None) -> str:
    zid = new_id("zone")
    now = format_ts(env.clock())
    async with env.db.transaction():
        await env.conn.execute(
            """INSERT INTO crew_zones (id, crew_id, slug, title, include_globs, source, reserve_for, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'repo', ?, ?, ?)""",
            (zid, crew_id, slug, slug.upper(), json.dumps([f"src/{slug}/**"]), reserve_for, now, now),
        )
    return zid


async def add_task(
    env: Env,
    crew_id: str,
    number: int,
    *,
    owner: dict[str, Any] | None,
    status: str = "in_progress",
    zones: list[str] | None = None,
) -> str:
    tid = new_id("task")
    now = format_ts(env.clock())
    async with env.db.transaction():
        await env.conn.execute(
            """INSERT INTO crew_tasks (id, crew_id, number, title, status, zone_ids, owner_session_id, owner_user_id,
                   owner_agent_id, acceptance, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                crew_id,
                number,
                f"Task {number}",
                status,
                json.dumps(zones or []),
                owner["id"] if owner else None,
                owner["user_id"] if owner else None,
                owner["agent_id"] if owner else None,
                json.dumps([{"id": "c1", "text": "tests pass", "kind": "test", "match": "npm test", "required": True}]),
                now,
                now,
            ),
        )
        if owner:
            await env.conn.execute("UPDATE crew_sessions SET current_task_id = ? WHERE id = ?", (tid, owner["id"]))
    return tid


async def add_claim(
    env: Env,
    crew_id: str,
    holder: dict[str, Any],
    *,
    zone_id: str | None = None,
    task_id: str | None = None,
    state: str = "active",
    lease_s: int = 600,
    mode: str = "exclusive",
) -> str:
    cid = new_id("claim")
    now = env.clock()
    async with env.db.transaction():
        await env.conn.execute(
            """INSERT INTO crew_claims (id, crew_id, zone_id, mode, holder_kind, holder_session_id, holder_user_id,
                   holder_agent_id, task_id, state, source, epoch, lease_expires_at, granted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'session', ?, ?, ?, ?, ?, 'task', 1, ?, ?, ?, ?)""",
            (
                cid,
                crew_id,
                zone_id,
                mode,
                holder["id"],
                holder["user_id"],
                holder["agent_id"],
                task_id,
                state,
                format_ts(now + timedelta(seconds=lease_s)),
                format_ts(now),
                format_ts(now),
                format_ts(now),
            ),
        )
    return cid


def hb_item(
    session: dict[str, Any], token: str, *, age: int = 5, alive: bool = True, cursor: int = 0, **extra: Any
) -> dict[str, Any]:
    item = {
        "session_id": session["id"],
        "token": token,
        "alive": alive,
        "activity_age_s": age,
        "last_action": None,
        "calls_since_checkpoint": 3,
        "limit": None,
        "footprints": [],
        "cursor": cursor,
        "githook_state": "ok",
    }
    item.update(extra)
    return item
