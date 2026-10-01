"""Versioned persistent open-work schema; content stays in the memory store."""

import hashlib
import json
from typing import Any


def item_digest(user_id: str, project_id: str, kind: str, text: str) -> str:
    digest = hashlib.sha256(json.dumps([user_id, project_id, kind, text], ensure_ascii=False).encode()).hexdigest()
    # Fixed kind prefix puts failures before TODOs in every paginated view.
    return ("0" if kind == "failure" else "1") + digest[:39]


async def backfill_open_work(conn: Any) -> None:
    """Historical, including superseded, relay handoffs; no content is copied."""
    cursor = await conn.execute(
        """SELECT id,user_id,project_id,metadata,created_at FROM memories
           WHERE memory_type='handoff' AND json_valid(metadata)
             AND json_type(metadata,'$.relay')='object'
           ORDER BY julianday(COALESCE(json_extract(metadata,'$.relay.closed_at'),created_at)),id"""
    )
    while rows := await cursor.fetchmany(200):
        for raw in rows:
            row = dict(raw)
            relay = json.loads(row["metadata"])["relay"]
            sections = relay.get("open_work", relay)
            at = relay.get("closed_at") or row["created_at"]
            if not isinstance(sections, dict):
                continue
            for section, kind in (("not_done", "todo"), ("failing", "failure")):
                values = sections.get(section)
                if not isinstance(values, list):
                    continue
                for index, text in enumerate(values):
                    if not isinstance(text, str) or not text.strip() or (kind == "todo" and not text.startswith("TODO: ")):
                        continue
                    digest = item_digest(row["user_id"], row["project_id"], kind, text)
                    await conn.execute(
                        """INSERT INTO continuity_items
                           (id,user_id,project_id,kind,state,source_handoff_id,section_index,first_seen_at,last_seen_at)
                           VALUES (?,?,?,?,'open',?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                           source_handoff_id=excluded.source_handoff_id,section_index=excluded.section_index,
                           last_seen_at=excluded.last_seen_at,version=continuity_items.version+1""",
                        (digest, row["user_id"], row["project_id"], kind, row["id"], index, at, at),
                    )
                    current = await conn.execute_fetchall("SELECT version FROM continuity_items WHERE id=?", (digest,))
                    version = next(iter(current))[0]
                    await conn.execute(
                        """INSERT INTO continuity_events
                           (item_id,user_id,project_id,state,source_memory_id,actor_id,actor_kind,created_at,version)
                           VALUES (?,?,?,'open',?,?,'historical-report',?,?)""",
                        (
                            digest,
                            row["user_id"],
                            row["project_id"],
                            row["id"],
                            str(relay.get("agent_id") or "unknown"),
                            at,
                            version,
                        ),
                    )


CONTINUITY_DDL = [
    """CREATE TABLE continuity_items (
       id TEXT PRIMARY KEY, user_id TEXT NOT NULL, project_id TEXT NOT NULL,
       kind TEXT NOT NULL CHECK(kind IN ('todo','failure')),
       state TEXT NOT NULL CHECK(state IN ('open','resolution_proposed','resolved')),
       source_handoff_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
       section_index INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
       resolution_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL, resolution_digest TEXT,
       confirmed_by TEXT, version INTEGER NOT NULL DEFAULT 1)""",
    "CREATE INDEX idx_continuity_open ON continuity_items(user_id, project_id, state, id)",
    """CREATE TABLE continuity_events (
       id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
       user_id TEXT NOT NULL, project_id TEXT NOT NULL, state TEXT NOT NULL,
       source_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
       actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL,
       created_at TEXT NOT NULL, version INTEGER NOT NULL, evidence_digest TEXT)""",
    "CREATE INDEX idx_continuity_history ON continuity_events(user_id, item_id, version)",
]
