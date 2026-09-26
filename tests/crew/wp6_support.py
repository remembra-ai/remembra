"""Shared fixtures for the WP-6 tests: a real crew.db, the real event log, sessions with tokens, zones."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any


from remembra.crew import schemas
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog, fetch_events
from remembra.crew.settings import default_settings, dumps_settings
from remembra.crew.store import new_id, now_iso
from remembra.crew.tasks import token_hash

CREW = "crw_aaaaaaaaaaaaaaaa"
OTHER_CREW = "crw_bbbbbbbbbbbbbbbb"
OWNER = "u_owner"


async def open_db(tmp_path: Path, name: str = "crew.db") -> CrewDatabase:
    db = CrewDatabase(str(tmp_path / name))
    await db.init_schema()
    return db


async def seed_crew(
    db: CrewDatabase,
    crew_id: str = CREW,
    *,
    owner: str = OWNER,
    project: str = "yaadbooks",
    settings: dict[str, Any] | None = None,
) -> str:
    now = now_iso()
    merged = default_settings()
    for k, v in (settings or {}).items():
        merged[k] = v
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crews (id, owner_user_id, project_id, name, settings, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (crew_id, owner, project, project, dumps_settings(merged), now, now),
        )
        await db.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, 'owner', ?)", (crew_id, owner, now)
        )
    return crew_id


async def set_settings(db: CrewDatabase, crew_id: str, **changes: Any) -> None:
    import json

    row = await db.fetchone("SELECT settings FROM crews WHERE id = ?", (crew_id,))
    assert row is not None
    current = json.loads(row["settings"])
    current.update(changes)
    async with db.transaction():
        await db.conn.execute("UPDATE crews SET settings = ? WHERE id = ?", (dumps_settings(current), crew_id))


async def seed_session(
    db: CrewDatabase,
    crew_id: str = CREW,
    *,
    callsign: str = "cc-1",
    agent_id: str = "claude-code",
    user_id: str = OWNER,
    client_kind: str = "hook",
    adapter: str = "claude-code",
    token: str | None = None,
    state: str = "active",
    verified: bool = True,
    checkout_fp: str | None = None,
    worktree_id: str | None = None,
    head: str | None = "a1b2c3d4e5f6",
    joined_at: str | None = None,
) -> dict[str, Any]:
    sid = new_id("session")
    token = token or f"tok-{sid}"
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_sessions (id, crew_id, user_id, agent_id, session_id, member_key, callsign, client_kind,
                   adapter, adapter_enforcement, agent_verified, checkout_fp, worktree_id, head_commit, state, joined_at,
                   token_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'enforced', ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                crew_id,
                user_id,
                agent_id,
                f"client-{sid}",
                f"{agent_id}:mbp:a1b2c3d4",
                callsign,
                client_kind,
                adapter,
                1 if verified else 0,
                checkout_fp,
                worktree_id,
                head,
                state,
                joined_at or now_iso(dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)),
                token_hash(token),
            ),
        )
    row = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (sid,))
    assert row is not None
    row["_token"] = token
    return row


async def seed_zone(
    db: CrewDatabase,
    crew_id: str = CREW,
    *,
    slug: str = "pos",
    includes: list[str] | None = None,
    parent_id: str | None = None,
    mode: str = "exclusive",
    builtin: bool = False,
    protected: bool = False,
    frozen_by: str | None = None,
    reserve_for: str | None = None,
) -> str:
    import json

    zid = new_id("zone")
    now = now_iso()
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_zones (id, crew_id, slug, title, parent_id, is_leaf, builtin, include_globs, mode, protected,
                   frozen_by, reserve_for, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                zid,
                crew_id,
                slug,
                f"{slug} zone",
                parent_id,
                1 if builtin else 0,
                json.dumps(includes or [f"src/app/{slug}/**"]),
                mode,
                1 if protected else 0,
                frozen_by,
                reserve_for,
                "builtin" if builtin else "api",
                now,
                now,
            ),
        )
    return zid


async def events_of(db: CrewDatabase, crew_id: str = CREW, *, after: int = 0) -> list[dict[str, Any]]:
    return await fetch_events(db.conn, crew_id, after_seq=after, limit=10_000)


async def types_of(db: CrewDatabase, crew_id: str = CREW, *, after: int = 0) -> list[str]:
    return [e["type"] for e in await events_of(db, crew_id, after=after)]


async def last_seq(db: CrewDatabase, crew_id: str = CREW) -> int:
    row = await db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
    return int(row["last_seq"]) if row else 0


def event_log(db: CrewDatabase) -> CrewEventLog:
    return CrewEventLog(db)


def valid_envelopes(events: list[dict[str, Any]]) -> None:
    for e in events:
        assert schemas.validate_envelope(e) == [], (e["type"], schemas.validate_envelope(e))
