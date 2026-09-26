"""Fixtures for the WP-5 tests: a real migrated crew.db, the real event log, sessions with real tokens."""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from remembra.crew import zones as Z
from remembra.crew.claims import hash_session_token
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog, verify_crew_chain
from remembra.crew.limits import crew_limits_for_tier
from remembra.crew.settings import default_settings, dumps_settings
from remembra.crew.store import now_iso

OWNER = "u_owner"
CREW = "crw_0000000000000a01"
OTHER_CREW = "crw_0000000000000b02"


class AuditRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str | None, dict[str, Any]]] = []

    async def __call__(self, user_id: str, action: str, resource_id: str | None, details: Mapping[str, Any]) -> None:
        self.rows.append((user_id, action, resource_id, dict(details)))

    def actions(self) -> list[str]:
        return [r[1] for r in self.rows]


async def open_db(tmp_path: Path) -> CrewDatabase:
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    return db


def make_ops(db: CrewDatabase, *, tier: str = "pro") -> tuple[Z.CrewOps, AuditRecorder]:
    audit = AuditRecorder()
    return Z.CrewOps(CrewEventLog(db, None), audit, crew_limits_for_tier(tier)), audit


async def seed_crew(
    db: CrewDatabase, crew_id: str = CREW, *, owner: str = OWNER, project: str = "yaadbooks", **settings: Any
) -> str:
    now = now_iso()
    s = default_settings()
    s.update(settings)
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crews (id, owner_user_id, project_id, name, settings, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (crew_id, owner, project, project, dumps_settings(s), now, now),
        )
        await db.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, 'owner', ?)", (crew_id, owner, now)
        )
    return crew_id


async def seed_session(
    db: CrewDatabase,
    session_id: str,
    *,
    crew_id: str = CREW,
    user_id: str = OWNER,
    callsign: str = "cc-1",
    agent_id: str = "claude-code",
    worktree_id: str | None = None,
    checkout_fp: str | None = None,
    state: str = "active",
    verified: bool = True,
    adapter_enforcement: str = "enforced",
) -> tuple[dict[str, Any], str]:
    token = "cst_" + secrets.token_urlsafe(24)
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_sessions (id, crew_id, user_id, agent_id, session_id, member_key, callsign, state, joined_at,
                   token_hash, agent_verified, worktree_id, checkout_fp, adapter_enforcement, client_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hook')""",
            (
                session_id,
                crew_id,
                user_id,
                agent_id,
                f"client-{session_id}",
                f"{agent_id}:mbp:a1b2c3d4",
                callsign,
                state,
                now_iso(),
                hash_session_token(token),
                1 if verified else 0,
                worktree_id,
                checkout_fp,
                adapter_enforcement,
            ),
        )
    row = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (session_id,))
    assert row is not None
    return row, token


async def seed_task(db: CrewDatabase, task_id: str, number: int, *, crew_id: str = CREW, status: str = "in_progress") -> None:
    now = now_iso()
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_tasks (id, crew_id, number, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, crew_id, number, f"task {number}", status, now, now),
        )


async def events(db: CrewDatabase, crew_id: str = CREW, type_prefix: str = "") -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT seq, type, moment, payload, actor_kind, summary FROM crew_events WHERE crew_id = ? AND type LIKE ? ORDER BY seq",
        (crew_id, type_prefix + "%"),
    )
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


async def types(db: CrewDatabase, crew_id: str = CREW) -> list[str]:
    return [e["type"] for e in await events(db, crew_id)]


async def assert_chain_ok(db: CrewDatabase, crew_id: str = CREW) -> None:
    report = await verify_crew_chain(db.conn, crew_id)
    assert report.ok, report.errors


async def zone_id(db: CrewDatabase, slug: str, crew_id: str = CREW) -> str:
    row = await db.fetchone("SELECT id FROM crew_zones WHERE crew_id = ? AND slug = ?", (crew_id, slug))
    assert row is not None, slug
    return str(row["id"])


async def inbox(db: CrewDatabase, crew_id: str = CREW, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM crew_inbox_items WHERE crew_id = ?"
    params: list[Any] = [crew_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return await db.fetchall(sql + " ORDER BY created_at", params)


ZONES_YML = """
version: 1
zones:
  app:
    title: App
    include: [src/app/**]
  pos:
    title: POS section
    parent: app
    include: [src/app/pos/**]
    commands: ["supabase db push *"]
  reports:
    parent: app
    include: [src/app/reports/**]
  billing:
    include: [src/billing/**]
    services: [deploy:vercel]
commons:
  package.json: plain
ignore: [docs/**]
"""
