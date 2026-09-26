"""Real crew.db fixture for WP-2 tests.

:func:`open_crew_db` opens a :class:`remembra.crew.db.CrewDatabase` and applies
WP-1's ``CREW_MIGRATIONS``, so the event log, bus, WebSocket layer and retention
job are tested against the schema production runs (including the ``crew_events``
``actor`` / ``refs`` columns the event log requires).
"""

from __future__ import annotations

from pathlib import Path

from remembra.crew.events import Actor, format_ts, utc_now
from remembra.crew.db import CrewDatabase

CREW_A = "crw_aaaaaaaaaaaaaaaa"
CREW_B = "crw_bbbbbbbbbbbbbbbb"


async def open_crew_db(tmp_path: Path, name: str = "crew.db") -> CrewDatabase:
    db = CrewDatabase(str(tmp_path / name))
    await db.init_schema()
    return db


async def seed_crew(db: CrewDatabase, crew_id: str = CREW_A, *, owner: str = "owner-1", project: str = "yaadbooks") -> str:
    now = format_ts(utc_now())
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crews (id, owner_user_id, project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (crew_id, owner, project, project, now, now),
        )
    return crew_id


async def seed_member(db: CrewDatabase, crew_id: str, user_id: str, role: str = "member") -> None:
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, ?, ?)",
            (crew_id, user_id, role, format_ts(utc_now())),
        )


async def seed_session(
    db: CrewDatabase,
    crew_id: str,
    session_id: str,
    *,
    user_id: str = "owner-1",
    callsign: str = "cc-1",
    agent_id: str = "claude-code",
    state: str = "active",
    verified: bool = True,
    ended_at: str | None = None,
) -> Actor:
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_sessions (id, crew_id, user_id, agent_id, session_id, member_key, callsign, state,
                   joined_at, token_hash, agent_verified, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                crew_id,
                user_id,
                agent_id,
                f"client-{session_id}",
                f"{agent_id}:mbp:a1b2c3d4",
                callsign,
                state,
                format_ts(utc_now()),
                "hash",
                1 if verified else 0,
                ended_at,
            ),
        )
    return Actor.session(session_id, callsign=callsign, agent_id=agent_id, user_id=user_id, verified=verified)


def mode_changed(to: str = "multi", live: int = 2) -> dict:
    return {"from": "solo" if to == "multi" else "multi", "to": to, "live_sessions": live}


def state_changed(frm: str = "active", to: str = "idle") -> dict:
    return {"from": frm, "to": to, "reason": "no_activity", "quiet_reason": None}
