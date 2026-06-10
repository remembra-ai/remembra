"""
E2E API test for /api/v1/debug/entities/graph.

This endpoint is used by the dashboard "Knowledge Graph" view.
We keep it lightweight: validate it returns nodes/edges and respects
the max_nodes / max_edges guardrails to prevent production timeouts.
"""

import os

os.environ["REMEMBRA_AUTH_ENABLED"] = "false"
os.environ["REMEMBRA_RATE_LIMIT_ENABLED"] = "false"

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from remembra.main import app


@pytest.fixture()
async def client():
    class _DB:
        def __init__(self, conn: aiosqlite.Connection) -> None:
            self.conn = conn

    conn = await aiosqlite.connect(":memory:")
    db = _DB(conn)

    await conn.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            type TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            community_id INTEGER
        );

        CREATE TABLE memory_entities (
            memory_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            PRIMARY KEY (memory_id, entity_id)
        );

        CREATE TABLE relationships (
            id TEXT PRIMARY KEY,
            from_entity_id TEXT NOT NULL,
            to_entity_id TEXT NOT NULL,
            type TEXT NOT NULL,
            confidence REAL DEFAULT 1.0
        );
        """
    )

    # The debug endpoint only reads memory_service.db.conn.
    class _MemoryService:
        def __init__(self, db) -> None:
            self.db = db

    app.state.memory_service = _MemoryService(db)
    app.state._test_debug_graph_conn = conn

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    await conn.close()


async def test_entity_graph_respects_limits(client: AsyncClient) -> None:
    user_id = "default_user"
    project_id = "trade-mind"

    # Entity e1 has 3 associated memories, e2 has 2, e3 has 1 → should pick e1/e2 for max_nodes=2.
    conn = app.state._test_debug_graph_conn
    await conn.executemany(
        "INSERT INTO entities (id, user_id, project_id, canonical_name, type, confidence) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("e1", user_id, project_id, "Alpha", "concept", 0.9),
            ("e2", user_id, project_id, "Beta", "concept", 0.8),
            ("e3", user_id, project_id, "Gamma", "concept", 0.7),
        ],
    )
    await conn.executemany(
        "INSERT INTO memory_entities (memory_id, entity_id) VALUES (?, ?)",
        [
            ("m1", "e1"),
            ("m2", "e1"),
            ("m3", "e1"),
            ("m4", "e2"),
            ("m5", "e2"),
            ("m6", "e3"),
        ],
    )
    await conn.executemany(
        "INSERT INTO relationships (id, from_entity_id, to_entity_id, type, confidence) VALUES (?, ?, ?, ?, ?)",
        [
            ("r1", "e1", "e2", "related", 0.9),
            ("r2", "e1", "e3", "related", 0.9),
        ],
    )
    await conn.commit()

    r = await client.get(
        "/api/v1/debug/entities/graph",
        params={"project_id": project_id, "max_nodes": 2, "max_edges": 1},
    )
    assert r.status_code == 200, r.text
    payload = r.json()

    assert len(payload["nodes"]) == 2
    assert len(payload["edges"]) <= 1

    node_ids = {n["id"] for n in payload["nodes"]}
    assert node_ids == {"e1", "e2"}
