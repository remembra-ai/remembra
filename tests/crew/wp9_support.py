"""Shared fixtures for the WP-9 local runtime tests (gate, crewd, CLI).

Everything runs against a temporary HOME (never the real ``~/.remembra``, ``~/.claude`` or
LaunchAgents), temporary git repositories, and — for crewd — the real FastAPI crew and relay
routers over a real ``crew.db`` and main database with auth enabled and a real API key
(``tests.security_harness``), reached through an in-process ASGI transport.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from remembra.api.v1 import auth
from remembra.api.v1 import relay as relay_api
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog
from remembra.main import CREW_ROUTER_MODULES
from remembra.relay.config import RelayConfig
from remembra.relay.crew.crewd import Crewd, Peer
from remembra.relay.crew.gate import Layout
from remembra.services.memory import MemoryService
from tests.agent_api_harness import FakeEmbeddings, FakeQdrant, OneFactExtractor
from tests.security_harness import secure_app

ZONES_YML = """version: 1
zones:
  pos:
    title: POS section
    include: [src/app/pos/**]
    mode: exclusive
  reports:
    title: Reports
    include: [src/app/reports/**]
commons:
  package.json: plain
"""


def git(cwd: Path | str, *args: str, env: dict[str, str] | None = None) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", **(env or {})},
    )
    return res.stdout.strip()


def make_repo(root: Path, *, zones: str | None = ZONES_YML, remote: Path | None = None) -> Path:
    """A repo on ``main`` with src/app/{pos,reports}, a commit, and (optionally) zones.yml committed."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    git(root, "config", "commit.gpgsign", "false")
    for rel, text in {
        "src/app/pos/split.ts": "export const split = 1;\n",
        "src/app/pos/tender.ts": "export const tender = 1;\n",
        "src/app/reports/export.ts": "export const x = 1;\n",
        "package.json": "{}\n",
        "README.md": "hi\n",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if zones is not None:
        (root / ".remembra").mkdir(exist_ok=True)
        (root / ".remembra" / "zones.yml").write_text(zones)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    if remote is not None:
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "-q", "-u", "origin", "main")
    return root.resolve()


def add_worktree(repo: Path, path: Path, branch: str) -> Path:
    git(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
    git(path, "config", "user.email", "dev@example.com")
    git(path, "config", "user.name", "Dev")
    return path.resolve()


@dataclass
class Server:
    h: Any
    crew_db: CrewDatabase
    events: CrewEventLog
    key: str
    owner: str
    daemons: list[Any] = field(default_factory=list)

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self.h.app)

    def config_loader(self) -> Any:
        key = self.key

        def load(agent: str | None, prefer: str | None) -> RelayConfig:
            return RelayConfig(url="http://test", api_key=key, agent_id=agent, source="test")

        return load

    async def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = await self.crew_db.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in await cur.fetchall()]

    async def event_types(self, crew_id: str) -> list[str]:
        return [r["type"] for r in await self.rows("SELECT type FROM crew_events WHERE crew_id = ? ORDER BY seq", (crew_id,))]


@asynccontextmanager
async def crew_server(tmp_path: Path):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    bus = CrewBus(loader=db_loader(db))
    log = CrewEventLog(db, bus)
    routers = [auth.router, relay_api.router, *(importlib.import_module(n).router for n in CREW_ROUTER_MODULES)]
    try:
        async with secure_app(tmp_path, routers, state={"crew_db": db, "crew_bus": bus, "crew_events": log}) as h:
            from remembra.config import Settings

            service = MemoryService(
                settings=Settings(openai_api_key="test", enable_entity_resolution=False),
                qdrant=FakeQdrant(),  # type: ignore[arg-type]
                db=h.db,
                embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
            )
            service.extractor = OneFactExtractor()  # type: ignore[assignment]
            h.app.state.memory_service = service

            @h.app.get("/health")
            async def health() -> dict[str, str]:
                return {"status": "ok"}

            owner = await h.create_user("owner@example.com")
            key, _ = await h.api_key(owner, "admin")
            server = Server(h, db, log, key, owner)
            try:
                yield server
            finally:
                for d in server.daemons:
                    await d.drain()
                    await d.shutdown()
    finally:
        await db.close()


def new_crewd(layout: Layout, server: Server, *, alive: set[int] | None = None, **kw: Any) -> Crewd:
    alive_set = alive if alive is not None else set()

    def is_alive(pid: int | None) -> bool:
        return pid is not None and (pid in alive_set or (pid == os.getpid()))

    d = Crewd(
        layout,
        config_loader=server.config_loader(),
        transport=server.transport(),
        is_alive=is_alive,
        hostname="test-host",
        restore_gate=kw.pop("restore_gate", False),
        **kw,
    )
    d.load_state()
    server.daemons.append(d)
    return d


def peer(pid: int) -> Peer:
    return Peer(pid, os.getuid(), (pid,))
