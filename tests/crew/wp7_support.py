"""Shared runtime fixtures for WP-7 tests: a real crew.db, event log, bus and services."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from remembra.crew import schemas
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.channel import CrewChannel, token_hash
from remembra.crew.db import CrewDatabase
from remembra.crew.decisions import CrewDecisions, CrewRef
from remembra.crew.events import CrewEventLog, format_ts, utc_now
from remembra.crew.inbox import Author, CrewInbox
from remembra.webhooks.manager import ResolvedTarget
from tests.crew.crewdb import open_crew_db

CREW_A = "crw_aaaaaaaaaaaaaaaa"
CREW_B = "crw_bbbbbbbbbbbbbbbb"
OWNER = "owner-1"


@dataclass
class Env:
    db: CrewDatabase
    bus: CrewBus
    log: CrewEventLog
    inbox: CrewInbox
    decisions: CrewDecisions
    channel: CrewChannel
    crew: CrewRef
    events: list[dict[str, Any]] = field(default_factory=list)
    invalid: list[Any] = field(default_factory=list)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


async def make_env(tmp_path: Path, *, owner: str = OWNER, crew_id: str = CREW_A, project: str = "yaadbooks") -> Env:
    db = await open_crew_db(tmp_path)
    await seed_crew(db, crew_id, owner=owner, project=project)
    bus = CrewBus(loader=db_loader(db))
    log = CrewEventLog(db, bus)
    inbox = CrewInbox(log)
    decisions = CrewDecisions(log, inbox)
    channel = CrewChannel(log, inbox=inbox, decisions=decisions, bus=bus)
    env = Env(db, bus, log, inbox, decisions, channel, CrewRef(crew_id, owner, project))

    def collect(envelope: Any) -> None:
        errors = schemas.validate_envelope(envelope)
        if errors:  # the bus swallows listener exceptions, so record and assert at teardown
            env.invalid.append((envelope.get("type"), errors))
        env.events.append(dict(envelope))

    bus.subscribe(collect)
    _ENVS.append(env)
    return env


_ENVS: list[Env] = []


@pytest.fixture(autouse=True)
async def events_honour_the_contract() -> AsyncIterator[None]:
    """Every event any WP-7 test emitted validates against the closed contract (schemas.validate_envelope)."""
    _ENVS.clear()
    try:
        yield
    finally:
        envs = list(_ENVS)
        _ENVS.clear()
        bad = [item for env in envs for item in env.invalid]
        # Bus listeners retain each environment in a cycle. Dropping the list
        # does not stop its non-daemon SQLite worker; close on this test's loop.
        results = await asyncio.gather(*(env.db.close() for env in envs), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        assert bad == []


async def seed_crew(db: CrewDatabase, crew_id: str, *, owner: str = OWNER, project: str = "yaadbooks") -> None:
    now = format_ts(utc_now())
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crews (id, owner_user_id, project_id, name, settings, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, '{}', ?, ?)",
            (crew_id, owner, project, project, now, now),
        )
        await db.conn.execute(
            "INSERT INTO crew_members (crew_id, user_id, role, added_at) VALUES (?, ?, 'owner', ?)", (crew_id, owner, now)
        )


async def add_session(
    db: CrewDatabase,
    crew_id: str,
    session_id: str,
    *,
    callsign: str,
    agent_id: str = "claude-code",
    user_id: str = OWNER,
    verified: bool = True,
    state: str = "active",
    token: str | None = None,
) -> Author:
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_sessions (id, crew_id, user_id, agent_id, session_id, member_key, callsign, state,
                   joined_at, token_hash, agent_verified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                token_hash(token or f"tok-{session_id}"),
                1 if verified else 0,
            ),
        )
    row = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (session_id,))
    assert row is not None
    return Author.session(row)


async def add_zone(db: CrewDatabase, crew_id: str, zone_id: str, slug: str) -> None:
    now = format_ts(utc_now())
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_zones (id, crew_id, slug, title, include_globs, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'api', ?, ?)",
            (zone_id, crew_id, slug, slug.upper(), json.dumps([f"src/{slug}/**"]), now, now),
        )


async def add_claim(db: CrewDatabase, crew_id: str, claim_id: str, zone_id: str, holder: str, *, state: str = "active") -> None:
    now = format_ts(utc_now())
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_claims (id, crew_id, zone_id, mode, holder_kind, holder_session_id, state, source,"
            " created_at, updated_at) VALUES (?, ?, ?, 'exclusive', 'session', ?, ?, 'mcp', ?, ?)",
            (claim_id, crew_id, zone_id, holder, state, now, now),
        )


async def add_task(db: CrewDatabase, crew_id: str, task_id: str, number: int, owner_session: str | None) -> None:
    now = format_ts(utc_now())
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_tasks (id, crew_id, number, title, status, owner_session_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'in_progress', ?, ?, ?)",
            (task_id, crew_id, number, f"Task {number}", owner_session, now, now),
        )


async def set_realtime(db: CrewDatabase, crew_id: str, channels: list[str]) -> None:
    async with db.transaction():
        await db.conn.execute(
            "UPDATE crews SET settings = json_set(settings, '$.notify', json(?)) WHERE id = ?",
            (json.dumps({"realtime": channels}), crew_id),
        )


class Receiver:
    """An httpx MockTransport webhook receiver: records requests; echoes challenges."""

    def __init__(self, *, echo: bool = True, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.echo = echo
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if body.get("type") == "crew.notification.challenge":
            return httpx.Response(200, json={"challenge": body["challenge"] if self.echo else "nope"})
        return httpx.Response(self.status, json={"ok": True})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self, kind: str = "crew.notification") -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if json.loads(r.content).get("type") == kind]


async def public_resolver(url: str) -> ResolvedTarget:
    """Stand-in for DNS: resolves every host to one public address (the SSRF policy itself is tested separately)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return ResolvedTarget(url=url, scheme=parsed.scheme, hostname=parsed.hostname or "", port=443, ips=("93.184.216.34",))
