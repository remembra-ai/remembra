"""Real crew.db fixture for WP-2 tests.

The DDL below is the §3.2 ``CREW_MIGRATIONS`` v1 text for the tables the event
log, bus, WebSocket layer and retention job touch, plus the two ``crew_events``
columns the event log requires (``actor``, ``refs``; see ``remembra.crew.events``).
When WP-1's migration runner lands, :func:`open_crew_db` should apply
``CREW_MIGRATIONS`` instead of this copy.
"""

from __future__ import annotations

from pathlib import Path

from remembra.crew.events import Actor, format_ts, utc_now
from remembra.storage.database import Database

CREW_DDL = """
CREATE TABLE crews (
  id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, project_id TEXT NOT NULL, team_id TEXT,
  name TEXT, settings TEXT NOT NULL DEFAULT '{}', settings_version INTEGER NOT NULL DEFAULT 1,
  last_seq INTEGER NOT NULL DEFAULT 0, last_hash TEXT, active_zones_sha TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(owner_user_id, project_id));
CREATE TABLE crew_members (crew_id TEXT NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('owner','admin','member','viewer')),
  added_by TEXT, added_at TEXT NOT NULL, PRIMARY KEY(crew_id, user_id));
CREATE TABLE crew_sessions (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL, host_id TEXT, member_key TEXT NOT NULL, callsign TEXT NOT NULL,
  client_kind TEXT, adapter TEXT, adapter_enforcement TEXT NOT NULL DEFAULT 'advisory',
  agent_verified INTEGER NOT NULL DEFAULT 0, model TEXT,
  checkout_fp TEXT, toplevel_rel TEXT, worktree_id TEXT, branch TEXT, head_commit TEXT,
  githook_state TEXT,
  state TEXT NOT NULL, quiet_reason TEXT, stuck INTEGER NOT NULL DEFAULT 0, state_reason TEXT,
  joined_at TEXT NOT NULL, last_seen_at TEXT, last_activity_at TEXT, last_heartbeat_at TEXT,
  last_action TEXT,
  calls_since_checkpoint INTEGER NOT NULL DEFAULT 0, next_checkpoint_due_at TEXT, checkpoint_streak INTEGER NOT NULL DEFAULT 0,
  limit_level TEXT, limit_pct REAL, limit_source TEXT,
  current_task_id TEXT, last_checkpoint_id TEXT, delivered_seq INTEGER NOT NULL DEFAULT 0,
  token_hash TEXT NOT NULL, token_version INTEGER NOT NULL DEFAULT 1, agent_pid INTEGER,
  ended_at TEXT, end_reason TEXT,
  UNIQUE(crew_id, user_id, agent_id, session_id));
CREATE INDEX idx_crew_sessions_live ON crew_sessions(crew_id, state);
CREATE TABLE crew_footprints (crew_id TEXT, session_id TEXT, path TEXT, zone_ids TEXT, first_at TEXT, last_at TEXT,
  touches INTEGER DEFAULT 1, state TEXT NOT NULL DEFAULT 'dirty' CHECK(state IN ('dirty','committed','landed')),
  attribution TEXT NOT NULL DEFAULT 'certain' CHECK(attribution IN ('certain','probable')),
  claim_epoch INTEGER, last_commit TEXT, worktree_id TEXT, PRIMARY KEY(crew_id, session_id, path));
CREATE TABLE crew_checkpoints (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT,
  trigger TEXT NOT NULL, facts TEXT, facts_hash TEXT NOT NULL, headline TEXT, facts_source TEXT NOT NULL,
  compacted INTEGER NOT NULL DEFAULT 0, memory_id TEXT, seq INTEGER, created_at TEXT NOT NULL, UNIQUE(session_id, facts_hash));
CREATE TABLE crew_batons (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL, task_id TEXT, from_session TEXT, to_session TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('adopt','handover','same_checkout','human_assign','reserved_for','first_write')),
  offer_id TEXT, handoff_id TEXT, checkpoint_id TEXT, report_id TEXT, baton_ref TEXT, restored INTEGER,
  zone_ids TEXT, brief_text TEXT, seq INTEGER, created_at TEXT NOT NULL);
CREATE TABLE crew_inbox_items (id TEXT PRIMARY KEY, crew_id TEXT NOT NULL,
  audience TEXT NOT NULL CHECK(audience IN ('project','crew','session')), recipient TEXT,
  kind TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'server' CHECK(origin IN ('server','human','agent')),
  ref_type TEXT, ref_id TEXT, priority INTEGER NOT NULL DEFAULT 2, title TEXT NOT NULL,
  primary_action TEXT, state TEXT NOT NULL CHECK(state IN ('open','seen','claimed','resolved','dismissed')),
  claimed_by TEXT, dedupe_key TEXT NOT NULL, coalesced_count INTEGER NOT NULL DEFAULT 1,
  created_seq INTEGER, resolved_seq INTEGER, resolved_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE crew_events (crew_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
  project_id TEXT NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL, v INTEGER NOT NULL DEFAULT 1,
  actor_kind TEXT, actor_id TEXT, session_id TEXT, task_id TEXT, zone_id TEXT, ref_id TEXT,
  severity TEXT NOT NULL DEFAULT 'info', moment INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL,
  payload TEXT NOT NULL, idem_key TEXT, origin TEXT NOT NULL DEFAULT 'server', prev_hash TEXT, hash TEXT,
  actor TEXT NOT NULL DEFAULT '{}', refs TEXT NOT NULL DEFAULT '{}',
  UNIQUE(crew_id, seq));
CREATE UNIQUE INDEX uq_events_idem ON crew_events(crew_id, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX idx_events_moment ON crew_events(crew_id, moment, seq);
CREATE INDEX idx_events_task   ON crew_events(task_id, seq);
CREATE INDEX idx_events_sess   ON crew_events(session_id, seq);
CREATE TABLE crew_digests (crew_id TEXT, day TEXT, counts TEXT, moments TEXT, PRIMARY KEY(crew_id, day));
CREATE TABLE crew_idempotency (key TEXT PRIMARY KEY, principal TEXT NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
"""

CREW_A = "crw_aaaaaaaaaaaaaaaa"
CREW_B = "crw_bbbbbbbbbbbbbbbb"


async def open_crew_db(tmp_path: Path, name: str = "crew.db") -> Database:
    db = Database(str(tmp_path / name))
    await db.connect()
    await db.conn.executescript(CREW_DDL)
    await db.conn.commit()
    return db


async def seed_crew(db: Database, crew_id: str = CREW_A, *, owner: str = "owner-1", project: str = "yaadbooks") -> str:
    now = format_ts(utc_now())
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crews (id, owner_user_id, project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (crew_id, owner, project, project, now, now),
        )
    return crew_id


async def seed_member(db: Database, crew_id: str, user_id: str, role: str = "member") -> None:
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, ?, ?)",
            (crew_id, user_id, role, format_ts(utc_now())),
        )


async def seed_session(
    db: Database,
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
