"""Row seeding for WP-8 tests: real ``crew.db`` rows written the way the owning services store them.

WP-4..7 own the services that create sessions, zones, claims, tasks, reports and
batons; WP-8 only reads them (and ends sessions on a relay close). These helpers
insert complete, contract-valid rows (ids with the right prefixes, server-format
times) so the read model and the close hook are exercised against the real
schema from ``CREW_MIGRATIONS``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from remembra.crew.db import CrewDatabase
from remembra.crew.settings import default_settings, dumps_settings
from remembra.crew.store import crew_id_for, now_iso


def ts(minutes_ago: float = 0.0, *, now: datetime | None = None) -> str:
    return now_iso((now or datetime.now(UTC)) - timedelta(minutes=minutes_ago))


async def _insert(db: CrewDatabase, table: str, row: dict[str, Any]) -> dict[str, Any]:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    async with db.transaction():
        await db.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    return row


async def crew(db: CrewDatabase, owner: str, project: str, **settings: Any) -> str:
    crew_id = crew_id_for(owner, project)
    doc = {**default_settings(), **settings}
    await _insert(
        db,
        "crews",
        {
            "id": crew_id,
            "owner_user_id": owner,
            "project_id": project,
            "name": project,
            "settings": dumps_settings(doc),
            "settings_version": 1,
            "last_seq": 0,
            "created_at": ts(60),
            "updated_at": ts(60),
        },
    )
    await _insert(db, "crew_members", {"crew_id": crew_id, "user_id": owner, "role": "owner", "added_at": ts(60)})
    return crew_id


async def session(
    db: CrewDatabase,
    crew_id: str,
    sid: str,
    *,
    user_id: str,
    callsign: str,
    agent_id: str = "claude-code",
    client_session_id: str | None = None,
    state: str = "active",
    verified: bool = True,
    joined_minutes_ago: float = 30,
    active_minutes_ago: float = 0.5,
    ended_minutes_ago: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "id": sid,
        "crew_id": crew_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": client_session_id or f"client-{sid}",
        "host_id": None,
        "member_key": f"{agent_id}:mbp:a1b2c3d4",
        "callsign": callsign,
        "client_kind": "hook",
        "adapter": "claude-code",
        "adapter_enforcement": "enforced",
        "agent_verified": 1 if verified else 0,
        "state": state,
        "joined_at": ts(joined_minutes_ago),
        "last_activity_at": ts(active_minutes_ago),
        "last_heartbeat_at": ts(active_minutes_ago),
        "githook_state": "ok",
        "token_hash": "0" * 64,
        "ended_at": ts(ended_minutes_ago) if ended_minutes_ago is not None else None,
        **extra,
    }
    return await _insert(db, "crew_sessions", row)


async def zone(db: CrewDatabase, crew_id: str, zid: str, slug: str, *, globs: list[str], **extra: Any) -> dict[str, Any]:
    row = {
        "id": zid,
        "crew_id": crew_id,
        "slug": slug,
        "title": extra.pop("title", slug.upper()),
        "include_globs": json.dumps(globs),
        "source": extra.pop("source", "repo"),
        "created_at": ts(50),
        "updated_at": ts(50),
        **extra,
    }
    return await _insert(db, "crew_zones", row)


async def task(db: CrewDatabase, crew_id: str, tid: str, number: int, title: str, **extra: Any) -> dict[str, Any]:
    row = {
        "id": tid,
        "crew_id": crew_id,
        "number": number,
        "title": title,
        "status": extra.pop("status", "ready"),
        "zone_ids": json.dumps(extra.pop("zone_ids", [])),
        "created_at": ts(45),
        "updated_at": ts(45),
        **extra,
    }
    return await _insert(db, "crew_tasks", row)


async def claim(
    db: CrewDatabase,
    crew_id: str,
    cid: str,
    *,
    holder: str | None,
    zone_id: str | None = None,
    state: str = "active",
    mode: str = "exclusive",
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "id": cid,
        "crew_id": crew_id,
        "zone_id": zone_id,
        "mode": mode,
        "holder_kind": extra.pop("holder_kind", "session"),
        "holder_session_id": holder,
        "holder_agent_id": extra.pop("holder_agent_id", "claude-code" if holder else None),
        "state": state,
        "source": extra.pop("source", "task"),
        "epoch": 1,
        "lease_expires_at": extra.pop("lease_expires_at", ts(-9)),
        "granted_at": ts(20),
        "created_at": extra.pop("created_at", ts(20)),
        "updated_at": ts(20),
        **extra,
    }
    return await _insert(db, "crew_claims", row)


async def offer(db: CrewDatabase, crew_id: str, oid: str, claim_id: str, to_session: str, task_id: str | None = None) -> None:
    await _insert(
        db,
        "crew_baton_offers",
        {
            "id": oid,
            "crew_id": crew_id,
            "claim_id": claim_id,
            "task_id": task_id,
            "to_session": to_session,
            "via": "brief",
            "created_at": ts(1),
        },
    )


async def checkpoint(
    db: CrewDatabase, crew_id: str, cid: str, session_id: str, *, facts: dict[str, Any], minutes_ago: float = 5, **extra: Any
) -> dict[str, Any]:
    row = {
        "id": cid,
        "crew_id": crew_id,
        "session_id": session_id,
        "trigger": extra.pop("trigger", "commit"),
        "facts": json.dumps(facts),
        "facts_hash": cid,
        "headline": extra.pop("headline", "1 commit, 2 dirty files"),
        "facts_source": extra.pop("facts_source", "relay-cli"),
        "created_at": ts(minutes_ago),
        **extra,
    }
    return await _insert(db, "crew_checkpoints", row)


async def report(db: CrewDatabase, crew_id: str, rid: str, task_id: str, session_id: str, **extra: Any) -> dict[str, Any]:
    row = {
        "id": rid,
        "crew_id": crew_id,
        "task_id": task_id,
        "session_id": session_id,
        "kind": extra.pop("kind", "stalled"),
        "verdict": extra.pop("verdict", "partial"),
        "sections": json.dumps(extra.pop("sections", {})),
        "facts_source": extra.pop("facts_source", "relay-cli"),
        "facts_hash": rid,
        "is_current": extra.pop("is_current", 1),
        "created_at": extra.pop("created_at", ts(4)),
        **extra,
    }
    return await _insert(db, "crew_reports", row)


async def baton(
    db: CrewDatabase, crew_id: str, bid: str, *, to_session: str, from_session: str | None, **extra: Any
) -> dict[str, Any]:
    row = {
        "id": bid,
        "crew_id": crew_id,
        "to_session": to_session,
        "from_session": from_session,
        "kind": extra.pop("kind", "adopt"),
        "zone_ids": json.dumps(extra.pop("zone_ids", [])),
        "brief_text": extra.pop("brief_text", "CREW yaadbooks (multi · 2 live)"),
        "created_at": extra.pop("created_at", ts(2)),
        **extra,
    }
    return await _insert(db, "crew_batons", row)
