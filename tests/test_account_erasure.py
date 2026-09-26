"""R-23: account erasure removes every row and vector an account owns, and only those.

* The production schema (the real app booted, plus the tables other modules
  create lazily) is fully classified: every table is erased by a rule or is
  exempt, and every user-keyed column is matched by its table's rule. A new
  table with a ``user_id`` column fails this test until it is registered, and
  is still erased at runtime by the schema scan in the meantime.
* Seeding EVERY table for a victim and a bystander, then erasing the victim,
  leaves no cell anywhere holding the victim's id, email or login key, keeps
  every bystander row, and deletes the victim's Qdrant points (real
  qdrant-client, in-process) and nobody else's.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from remembra.account.erasure import (
    ERASURE_RULES,
    EXEMPT_PREFIXES,
    EXEMPT_TABLES,
    AccountEraser,
    ExtraDatabase,
    TableRule,
    erasure_digest,
    is_exempt,
    registry_problems,
)
from remembra.config import Settings
from remembra.models.memory import Memory
from remembra.security import state as security_state
from remembra.storage.database import Database
from remembra.storage.qdrant import QdrantStore

SRC = Path(__file__).resolve().parents[1] / "src" / "remembra"
_CREATE_TABLE = re.compile(r"CREATE (?:VIRTUAL )?TABLE (?:IF NOT EXISTS )?(\w+)\s*(?:\(|USING)", re.IGNORECASE)
DIM = 8


async def init_every_schema(db: Any) -> None:
    """Create every table any module of the app creates (at boot or lazily)."""
    from remembra.auth import social
    from remembra.auth.rbac import RoleManager
    from remembra.cloud.metering import UsageMeter
    from remembra.cloud.promocodes import PromoCodeManager
    from remembra.connector.store import ConnectorStore
    from remembra.extraction.conflicts import ConflictManager
    from remembra.inbox.manager import InboxManager
    from remembra.spaces.manager import SpaceManager
    from remembra.storage.reindex import ReindexManager, _ensure_state_table
    from remembra.teams.manager import TeamManager
    from remembra.webhooks.manager import WebhookManager

    for manager in (
        RoleManager(db),
        UsageMeter(db),
        ConflictManager(db),
        WebhookManager(db),
        SpaceManager(db),
        TeamManager(db),
        InboxManager(db),
        ReindexManager(db, None, None),
        ConnectorStore(db, rotation_key=b"k" * 32),
    ):
        await manager.init_schema()
    await _ensure_state_table(db)
    await social.ensure_schema(db)
    await security_state._ensure_schema(db)
    await PromoCodeManager(db)._ensure_schema()
    await db.conn.commit()


async def table_columns(conn: Any) -> dict[str, list[tuple[str, str, int]]]:
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    out: dict[str, list[tuple[str, str, int]]] = {}
    for (name,) in await cursor.fetchall():
        info = await conn.execute(f'PRAGMA table_info("{name}")')
        out[str(name)] = [(str(r[1]), str(r[2] or "").upper(), int(r[3])) for r in await info.fetchall()]
    return out


def coverage_problems(schema: dict[str, list[tuple[str, str, int]]]) -> list[str]:
    """Why the erasure registry does not cover ``schema`` (empty = fully covered)."""
    names = {table: [c for c, _t, _n in columns] for table, columns in schema.items()}
    return registry_problems(names, ERASURE_RULES, EXEMPT_TABLES, EXEMPT_PREFIXES)


def declared_tables() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        for name in _CREATE_TABLE.findall(path.read_text()):
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Coverage of the schema
# ---------------------------------------------------------------------------


async def test_every_table_of_the_booted_app_is_covered_by_erasure(tmp_path, monkeypatch) -> None:
    import remembra.config
    import remembra.main as main
    from remembra.core.tasks import get_task_registry

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "erasure_boot_test")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_CLOUD_ENABLED", "true")
    monkeypatch.setenv("REMEMBRA_WEBHOOKS_ENABLED", "true")
    monkeypatch.setenv("REMEMBRA_CONFLICT_DETECTION_ENABLED", "true")
    monkeypatch.setenv("REMEMBRA_PRE_MIGRATION_BACKUP", "false")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self: QdrantStore) -> AsyncQdrantClient:
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)

    app = main.create_app()
    async with app.router.lifespan_context(app):
        # The erasure job is wired and running in the real app.
        assert isinstance(app.state.account_eraser, AccountEraser)
        assert "account-erasure-loop" in get_task_registry().names()
        db = app.state.db
        await init_every_schema(db)  # the lazily created tables too
        schema = await table_columns(db.conn)
    monkeypatch.setattr(remembra.config, "_settings", None)

    assert coverage_problems(schema) == []
    # Every table declared anywhere in src/ exists in that schema, so none can hide from the check.
    assert declared_tables() - set(schema) == set()
    # Registry entries all refer to real tables (no stale names).
    assert {rule.table for rule in ERASURE_RULES} - set(schema) == set()
    assert set(EXEMPT_TABLES) - set(schema) <= {"sqlite_stat1"}


async def test_a_new_table_with_a_user_column_fails_coverage_and_is_still_erased(tmp_path) -> None:
    db = Database(str(tmp_path / "new.db"))
    await db.connect()
    await db.init_schema()
    await init_every_schema(db)
    try:
        await db.conn.execute("CREATE TABLE feature_x (id TEXT PRIMARY KEY, owner_user_id TEXT, note TEXT)")
        await db.conn.executemany(
            "INSERT INTO feature_x VALUES (?, ?, ?)", [("1", "u_victim", "secret"), ("2", "u_other", "keep")]
        )
        await db.conn.commit()
        problems = coverage_problems(await table_columns(db.conn))
        assert problems == ["feature_x: no erasure rule and no exemption"]

        receipt = await AccountEraser(db, None).erase("u_victim")
        assert receipt.unregistered_tables == ["feature_x"]
        cursor = await db.conn.execute("SELECT id FROM feature_x")
        assert [r[0] for r in await cursor.fetchall()] == ["2"]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Erasure of a fully seeded database
# ---------------------------------------------------------------------------

VICTIM = "u_victim_0001"
VICTIM_EMAIL = "victim@example.com"
BYSTANDER = "u_bystander_2"
BYSTANDER_EMAIL = "bystander@example.com"


def _seed_value(owner: str, email: str, column: str, declared: str) -> Any:
    if column == "email":
        return email
    if column == "account_key":
        return security_state.account_key("login", email)
    if any(t in declared for t in ("INT", "REAL", "BOOL", "NUM", "FLOAT")):
        return 2 if owner == BYSTANDER else 1  # distinct integer primary keys per account
    return owner  # ids, user ids, foreign keys and text all carry the owner's id


# Parents first, so the seeded rows satisfy the foreign keys.
_PARENTS = ("users", "memories", "entities", "teams", "memory_spaces", "webhooks", "api_keys", "oauth_grants")


async def _seed_everything(conn: Any, schema: dict[str, list[tuple[str, str, int]]], owner: str, email: str) -> None:
    ordered = [t for t in _PARENTS if t in schema] + [t for t in schema if t not in _PARENTS]
    for table in ordered:
        columns = schema[table]
        if is_exempt(table):
            continue
        names = [c for c, _t, _n in columns]
        values = [_seed_value(owner, email, c, t) for c, t, _n in columns]
        placeholders = ", ".join("?" for _ in names)
        quoted = ", ".join(f'"{n}"' for n in names)
        await conn.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', values)
    await conn.commit()


def _owner_row_predicate(columns: list[tuple[str, str, int]], owner: str, email: str) -> tuple[str, list[Any]]:
    clauses = [f'"{c}" IS ?' for c, _t, _n in columns]
    return " AND ".join(clauses), [_seed_value(owner, email, c, t) for c, t, _n in columns]


async def _cells_holding(conn: Any, table: str, columns: list[tuple[str, str, int]], needles: list[str]) -> int:
    where = " OR ".join(f'CAST("{c}" AS TEXT) = ?' for c, _t, _n in columns for _ in needles)
    params = [n for _c in columns for n in needles]
    cursor = await conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params)
    return int((await cursor.fetchone())[0])


async def _qdrant(tmp_path: Path) -> QdrantStore:
    store = QdrantStore(Settings(openai_api_key="t", embedding_dimensions=DIM, qdrant_collection="erasure_test"))
    store._client = AsyncQdrantClient(location=":memory:")
    await store.init_collection(DIM)
    return store


async def _points(store: QdrantStore, user_id: str) -> int:
    from qdrant_client.http import models as qm

    client = await store._get_client()
    result = await client.count(
        collection_name=store.collection_name,
        count_filter=qm.Filter(must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id))]),
    )
    return int(result.count)


async def test_erasure_removes_every_row_and_vector_of_the_account_and_nothing_else(tmp_path) -> None:
    db = Database(str(tmp_path / "seeded.db"))
    await db.connect()
    await db.init_schema()
    await init_every_schema(db)
    store = await _qdrant(tmp_path)
    try:
        schema = await table_columns(db.conn)
        await _seed_everything(db.conn, schema, VICTIM, VICTIM_EMAIL)
        await _seed_everything(db.conn, schema, BYSTANDER, BYSTANDER_EMAIL)

        # Cross-account links: the victim in the bystander's team, the victim as
        # the inviter of someone else, a team invite to the victim's address,
        # the bystander's feedback on a victim memory.
        await db.conn.execute(
            "INSERT INTO team_members (team_id, user_id, role, invited_by, joined_at, updated_at)"
            " VALUES (?, ?, 'member', ?, 'now', 'now')",
            (BYSTANDER, VICTIM, BYSTANDER),
        )
        await db.conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES ('u_third', 'third@example.com', 'x', 'now')"
        )
        await db.conn.execute(
            "INSERT INTO team_members (team_id, user_id, role, invited_by, joined_at, updated_at)"
            " VALUES (?, 'u_third', 'member', ?, 'now', 'now')",
            (BYSTANDER, VICTIM),
        )
        await db.conn.execute(
            "INSERT INTO team_invites (id, team_id, email, role, invited_by, status, token_hash, expires_at, created_at)"
            " VALUES ('inv_x', ?, ?, 'member', ?, 'pending', 'tok_x', 'later', 'now')",
            (BYSTANDER, VICTIM_EMAIL.upper(), BYSTANDER),
        )
        await db.conn.execute(
            "INSERT INTO memory_feedback (id, memory_id, user_id, signal, created_at) VALUES ('fb_x', ?, ?, 'up', 'now')",
            (VICTIM, BYSTANDER),
        )
        await db.conn.commit()

        for owner, n in ((VICTIM, 3), (BYSTANDER, 2)):
            for i in range(n):
                await store.upsert(Memory(user_id=owner, content=f"note {i} of {owner}", embedding=[0.1 * (i + 1)] * DIM))
        assert (await _points(store, VICTIM), await _points(store, BYSTANDER)) == (3, 2)

        bystander_rows = {}
        for table, columns in schema.items():
            if is_exempt(table):
                continue
            where, params = _owner_row_predicate(columns, BYSTANDER, BYSTANDER_EMAIL)
            cursor = await db.conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params)
            bystander_rows[table] = int((await cursor.fetchone())[0])
        # Every non-exempt table really was seeded for both accounts.
        assert all(count == 1 for count in bystander_rows.values()), bystander_rows
        seeded_tables = len(bystander_rows)
        assert seeded_tables == len({rule.table for rule in ERASURE_RULES})

        receipt = await AccountEraser(db, store).erase(VICTIM)

        victim_needles = [VICTIM, VICTIM_EMAIL, VICTIM_EMAIL.upper(), security_state.account_key("login", VICTIM_EMAIL)]
        leftovers = {
            table: await _cells_holding(db.conn, table, columns, victim_needles)
            for table, columns in schema.items()
            if not table.startswith("memories_fts_") and table != "sqlite_sequence"
        }
        assert {t: n for t, n in leftovers.items() if n} == {}
        # FTS shadow tables no longer index the victim's text.
        cursor = await db.conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?", (f'"{VICTIM}"',))
        assert (await cursor.fetchone())[0] == 0

        for table, columns in schema.items():
            if is_exempt(table):
                continue
            where, params = _owner_row_predicate(columns, BYSTANDER, BYSTANDER_EMAIL)
            cursor = await db.conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params)
            assert int((await cursor.fetchone())[0]) == bystander_rows[table], table
        # The third member the victim invited stays in the bystander's team, inviter cleared.
        cursor = await db.conn.execute("SELECT invited_by FROM team_members WHERE user_id = 'u_third'")
        assert [tuple(r) for r in await cursor.fetchall()] == [(None,)]

        assert (await _points(store, VICTIM), await _points(store, BYSTANDER)) == (0, 2)
        assert receipt.vectors == 3 and receipt.unregistered_tables == []
        assert receipt.total_rows >= seeded_tables
        cursor = await db.conn.execute(
            "SELECT user_id, resource_id, error_message FROM audit_log WHERE action = 'account_erased'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        user_col, resource, detail = rows[0]
        assert resource == f"sha256:{erasure_digest(VICTIM)}" and VICTIM not in user_col
        assert detail == f"rows={receipt.total_rows};vectors=3"
    finally:
        await store.close()
        await db.close()


async def test_a_vector_store_failure_erases_nothing_and_the_next_run_retries(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    db = Database(str(tmp_path / "retry.db"))
    await db.connect()
    await db.init_schema()
    await init_every_schema(db)

    class Down:
        calls = 0

        async def delete_by_user_everywhere(self, user_id: str, also: Any = ()) -> int:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("qdrant unreachable")
            return 4

    try:
        now = datetime.now(UTC)
        await db.conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at, is_active, deleted_at) VALUES (?, ?, 'x', ?, 0, ?)",
            (VICTIM, VICTIM_EMAIL, now.isoformat(), (now - timedelta(days=8)).isoformat()),
        )
        await db.conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at, is_active, deleted_at)"
            " VALUES ('u_recent', 'r@x.io', 'x', ?, 0, ?)",
            (now.isoformat(), (now - timedelta(days=2)).isoformat()),
        )
        await db.conn.execute(
            "INSERT INTO memories (id, user_id, content, created_at, updated_at) VALUES ('m1', ?, 'c', 'n', 'n')", (VICTIM,)
        )
        await db.conn.commit()
        eraser = AccountEraser(db, Down())
        grace = timedelta(days=7)
        assert await eraser.due_accounts(grace) == [VICTIM]  # u_recent is still inside its grace period

        assert await eraser.erase_due(grace) == []  # Qdrant down: nothing touched
        cursor = await db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (VICTIM,))
        assert (await cursor.fetchone())[0] == 1

        receipts = await eraser.erase_due(grace)
        assert [r.digest for r in receipts] == [erasure_digest(VICTIM)] and receipts[0].vectors == 4
        cursor = await db.conn.execute("SELECT id FROM users ORDER BY id")
        assert [r[0] for r in await cursor.fetchall()] == ["u_recent"]
        assert await eraser.erase_due(grace) == []  # nothing left to do
    finally:
        await db.close()


@pytest.mark.parametrize("table", sorted(EXEMPT_TABLES))
def test_exempt_tables_have_a_reason(table: str) -> None:
    assert len(EXEMPT_TABLES[table]) > 10


# ---------------------------------------------------------------------------
# Rollback collections left by a rebuild reindex
# ---------------------------------------------------------------------------


async def _count_in(client: AsyncQdrantClient, collection: str, user_id: str) -> int:
    from qdrant_client.http import models as qm

    result = await client.count(
        collection_name=collection,
        count_filter=qm.Filter(must=[qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id))]),
    )
    return int(result.count)


async def test_erasure_reaches_the_rollback_collection_a_rebuild_kept(tmp_path) -> None:
    """A rebuild swaps to a new collection and keeps the old one: both lose the account's points."""
    from qdrant_client.http import models as qm

    from remembra.core.time import utcnow
    from remembra.storage.reindex import ReindexManager
    from tests.test_rel_reindex_reconcile import FakeEmbedder

    db = Database(str(tmp_path / "rb.db"))
    await db.connect()
    await db.init_schema()
    await init_every_schema(db)
    settings = Settings(openai_api_key="t", embedding_dimensions=4, qdrant_collection="memories")
    store = QdrantStore(settings)
    client = AsyncQdrantClient(location=":memory:")
    store._client = client
    await store.init_collection(4)
    try:
        embedder = FakeEmbedder(4)
        for owner, n in ((VICTIM, 3), (BYSTANDER, 2)):
            for i in range(n):
                memory = Memory(user_id=owner, content=f"{owner} secret {i}", embedding=await embedder.embed(f"{owner}{i}"))
                await db.save_memory_metadata(
                    memory_id=memory.id,
                    user_id=owner,
                    project_id="default",
                    content=memory.content,
                    extracted_facts=[memory.content],
                    metadata={},
                    created_at=utcnow(),
                )
                await store.upsert(memory)

        manager = ReindexManager(db=db, qdrant=store, embeddings=embedder)
        await manager.init_schema()
        job = await manager.start_reindex("openai", "m", "openai", "m2")
        await manager.wait()
        assert job.status == "completed", job.error
        new, old = job.target_collection, "memories"
        assert store.collection_name == new and new.startswith("memories__rb_")
        assert await _count_in(client, old, VICTIM) == 3 and await _count_in(client, new, VICTIM) == 3

        # A collection only a reindex job names (the config was renamed since), and another app's collection.
        for name in ("renamed_src", "other_app"):
            await client.create_collection(name, vectors_config=qm.VectorParams(size=4, distance=qm.Distance.COSINE))
            await client.upsert(
                name, points=[qm.PointStruct(id=str(uuid.uuid4()), vector=[0.1] * 4, payload={"user_id": VICTIM})]
            )
        await db.conn.execute("UPDATE reindex_jobs SET source_collection = 'renamed_src' WHERE id = ?", (job.id,))
        await db.conn.commit()

        receipt = await AccountEraser(db, store).erase(VICTIM)

        for name in (old, new, "renamed_src"):
            assert await _count_in(client, name, VICTIM) == 0, name
        for name in (old, new):
            assert await _count_in(client, name, BYSTANDER) == 2, name
        assert await _count_in(client, "other_app", VICTIM) == 1  # not this app's collection
        assert receipt.vectors == 3 + 3 + 1
        cursor = await db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (VICTIM,))
        assert (await cursor.fetchone())[0] == 0
    finally:
        await store.close()
        await db.close()


async def test_erasure_without_any_rebuild_touches_only_the_active_collection(tmp_path) -> None:
    db = Database(str(tmp_path / "plain.db"))
    await db.connect()
    await db.init_schema()
    store = await _qdrant(tmp_path)
    try:
        await store.upsert(Memory(user_id=VICTIM, content="only copy", embedding=[0.2] * DIM))
        receipt = await AccountEraser(db, store).erase(VICTIM)  # no reindex_jobs table at all
        assert receipt.vectors == 1 and await _points(store, VICTIM) == 0
    finally:
        await store.close()
        await db.close()


# ---------------------------------------------------------------------------
# Actor columns and other databases
# ---------------------------------------------------------------------------


async def test_the_safety_net_clears_actor_columns_instead_of_deleting_rows(tmp_path) -> None:
    db = Database(str(tmp_path / "actor.db"))
    await db.connect()
    await db.init_schema()
    try:
        await db.conn.executescript(
            "CREATE TABLE feature_members (group_id TEXT, member_id TEXT, added_by TEXT, owner_user_id TEXT);"
            "CREATE TABLE feature_log (id TEXT, created_by TEXT NOT NULL);"
        )
        await db.conn.executemany(
            "INSERT INTO feature_members VALUES (?, ?, ?, ?)",
            [
                ("g_by", "m_by", VICTIM, BYSTANDER),  # the victim added someone to the bystander's group
                ("g_v", "m_v", VICTIM, VICTIM),  # the victim's own group
            ],
        )
        await db.conn.execute("INSERT INTO feature_log VALUES ('l1', ?)", (VICTIM,))
        await db.conn.commit()

        receipt = await AccountEraser(db, None).erase(VICTIM)

        cursor = await db.conn.execute("SELECT group_id, member_id, added_by, owner_user_id FROM feature_members")
        assert [tuple(r) for r in await cursor.fetchall()] == [("g_by", "m_by", None, BYSTANDER)]
        cursor = await db.conn.execute("SELECT COUNT(*) FROM feature_log")
        assert (await cursor.fetchone())[0] == 1  # NOT NULL actor column: left (logged), never a failed erasure
        assert sorted(receipt.unregistered_tables) == ["feature_log", "feature_members"]
        assert receipt.rows["feature_members"] == 1
    finally:
        await db.close()


async def test_an_extra_database_is_only_accepted_with_explicit_rules(tmp_path) -> None:
    other = Database(str(tmp_path / "other.db"))
    with pytest.raises(ValueError):
        ExtraDatabase("crew", other, rules=())
    with pytest.raises(TypeError):
        AccountEraser(other, None, extra_databases=[other])  # type: ignore[list-item]


def test_registry_problems_reports_uncovered_tables_and_columns() -> None:
    rules = (TableRule("crews", deletes=("owner_user_id = :uid",)),)
    schema = {
        "crews": ["id", "owner_user_id", "created_by"],
        "crew_votes": ["proposal_id", "voter_id"],
        "crew_meta": ["key"],
        "sqlite_sequence": ["name", "seq"],
    }
    assert registry_problems(schema, rules, {"crew_meta": "one row of settings"}, ("sqlite_",)) == [
        "crews: rule does not match user columns ['created_by']",
        "crew_votes: no erasure rule and no exemption",
    ]
