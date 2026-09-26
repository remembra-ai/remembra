"""R-23 with Crew mode: account erasure covers ``crew.db`` (remembra.crew.erasure).

* Coverage: the real migrated crew schema is fully classified. Every table has
  a rule or an exemption and every user-keyed or actor column is matched, so a
  crew table added later fails here until it has a rule.
* Behaviour: every crew table gets rows for three contexts: the victim's own
  crew, the victim working in a bystander's crew, and the bystander in their
  own crew. Erasing the victim removes the victim's crew entirely and every
  cell holding the victim's user id, session ids or message ids (except the
  bystander's hash-chained event log, which keeps opaque session ids of its
  own crew), and keeps every bystander row.
* Wiring: with Crew mode on, the booted app's eraser covers ``crew.db`` and
  a real erase through it removes the victim's crew rows; shutdown detaches it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from remembra.account.erasure import AccountEraser, registry_problems
from remembra.crew.db import CrewDatabase
from remembra.crew.erasure import CREW_ERASURE_RULES, CREW_EXEMPT_TABLES, crew_extra_database
from remembra.storage.database import Database

VICTIM, BYSTANDER = "u_victim", "u_bystander"
CREW_V, CREW_B = "crw_victim0000000000", "crw_bystander000000"
# (context name, crew, user, session, message)
# The bystander's rows go in before the victim's guest rows: a table keyed by the crew alone
# (crew_repo_trees, crew_zone_files) holds one row per crew, and that one is the bystander's.
CONTEXTS = (
    ("own", CREW_V, VICTIM, "cs_victim_own", "msg_victim_own"),
    ("bystander", CREW_B, BYSTANDER, "cs_bystander", "msg_bystander"),
    ("guest", CREW_B, VICTIM, "cs_victim_guest", "msg_victim_guest"),
)
VICTIM_VALUES = {VICTIM, "cs_victim_own", "cs_victim_guest", "msg_victim_own", "msg_victim_guest", CREW_V}

_USER_COLUMNS = {
    "user_id",
    "owner_user_id",
    "holder_user_id",
    "author_user_id",
    "uploaded_by_user",
    "created_by",
    "added_by",
    "issued_by",
    "confirmed_by",
    "decided_by",
    "reviewed_by",
    "resolved_by",
    "frozen_by",
    "shared_by",
    "uploaded_by",
}
_SESSION_COLUMNS = {
    "session_id",
    "holder_session_id",
    "author_session_id",
    "owner_session_id",
    "uploaded_by_session",
    "from_session",
    "to_session",
    "session_a",
    "session_b",
    "voter_id",
    "principal",
    "recipient",
    "claimed_by",
}
_CHECK_IN = re.compile(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


async def _open(tmp_path: Path) -> CrewDatabase:
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    return db


async def _schema(db: CrewDatabase) -> dict[str, list[dict[str, Any]]]:
    cursor = await db.conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
    out: dict[str, list[dict[str, Any]]] = {}
    for name, sql in await cursor.fetchall():
        info = await db.conn.execute(f'PRAGMA table_info("{name}")')
        checks = {m.group(1): [v.strip().strip("'") for v in m.group(2).split(",")] for m in _CHECK_IN.finditer(sql or "")}
        out[str(name)] = [
            {"name": r[1], "type": (r[2] or "").upper(), "notnull": r[3], "pk": r[5], "choices": checks.get(r[1])}
            for r in await info.fetchall()
        ]
    return out


def _value(table: str, col: dict[str, Any], ctx: tuple[str, str, str, str, str], n: int) -> Any:
    label, crew, user, session, message = ctx
    name = col["name"]
    if col["choices"]:
        return col["choices"][0]
    if table == "crews" and name == "id":
        return crew
    if table == "crew_sessions" and name == "id":
        return session
    if table == "crew_messages" and name == "id":
        return message
    if name == "crew_id":
        return crew
    if name in ("message_id", "reply_to_id", "thread_root_id") and not (table == "crew_messages" and label != "bystander"):
        return message if table != "crew_messages" else "msg_victim_guest"  # the bystander replies to the victim
    if table == "crew_events" and name == "owner_user_id":
        return VICTIM if crew == CREW_V else BYSTANDER  # events are keyed by the crew's owner
    if name in _USER_COLUMNS:
        return user
    if name in _SESSION_COLUMNS:
        return session
    if name in ("created_at", "updated_at", "joined_at", "added_at", "registered_at", "ts", "first_at", "last_at"):
        return "2026-09-20T10:00:00.000Z"
    if name in ("seq", "number", "first_seq", "last_seq", "version", "v", "epoch", "position", "priority"):
        return n
    if "INT" in col["type"]:
        return 0
    if "REAL" in col["type"]:
        return 0.5
    return f"{table}:{name}:{label}"


async def _seed(db: CrewDatabase) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Every crew table gets one row per context; returns what was written."""
    schema = await _schema(db)
    written: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for n, ctx in enumerate(CONTEXTS, start=1):
        for table, cols in schema.items():
            if table in CREW_EXEMPT_TABLES:
                continue
            if table == "crews" and ctx[0] == "guest":
                continue  # the victim does not own the bystander's crew
            row = {c["name"]: _value(table, c, ctx, n) for c in cols}
            names = ", ".join(f'"{k}"' for k in row)
            marks = ", ".join("?" for _ in row)
            cursor = await db.conn.execute(f'INSERT OR IGNORE INTO "{table}" ({names}) VALUES ({marks})', list(row.values()))
            if cursor.rowcount:
                written.setdefault(table, []).append((ctx[0], row))
            else:
                assert ctx[0] == "guest", (table, ctx[0])  # only a one-row-per-crew table refuses the guest row
    await db.conn.commit()
    return written


async def _rows(db: CrewDatabase) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for table in await _schema(db):
        cursor = await db.conn.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cursor.description]
        out[table] = [dict(zip(cols, r, strict=True)) for r in await cursor.fetchall()]
    return out


async def test_every_crew_table_is_covered_by_the_rules(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        schema = {t: [c["name"] for c in cols] for t, cols in (await _schema(db)).items()}
    finally:
        await db.close()
    assert registry_problems(schema, CREW_ERASURE_RULES, CREW_EXEMPT_TABLES, ("sqlite_",)) == []
    assert {rule.table for rule in CREW_ERASURE_RULES} - set(schema) == set()
    assert set(CREW_EXEMPT_TABLES) - set(schema) <= {"sqlite_sequence", "sqlite_stat1"}


async def test_erasure_removes_the_victims_crew_rows_and_keeps_the_bystanders(tmp_path: Path) -> None:
    crew = await _open(tmp_path)
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        written = await _seed(crew)
        assert len(written) == len(await _schema(crew)) - len(set(CREW_EXEMPT_TABLES) & set(await _schema(crew)))
        eraser = AccountEraser(main, None, extra_databases=[crew_extra_database(crew)])
        receipt = await eraser.erase(VICTIM)
        after = await _rows(crew)

        assert receipt.unregistered_tables == []
        assert receipt.rows.get("crew:crews") == 1 and receipt.rows.get("crew:crew_sessions") == 2
        for table, rows in after.items():
            for row in rows:
                assert row.get("crew_id") != CREW_V, (table, row)
                leaked = {k: v for k, v in row.items() if v in VICTIM_VALUES}
                if table == "crew_events":
                    # The bystander's event log is their record: it may keep the opaque id of the
                    # victim's session in their crew, never the victim's user id.
                    assert set(leaked) <= {"session_id", "actor_id", "ref_id"} and VICTIM not in leaked.values(), row
                    continue
                assert leaked == {}, (table, row)
        for table, rows in written.items():
            kept = [r for label, r in rows if label == "bystander"]
            for row in kept:
                key = {k: row[k] for k in ("id", "crew_id", "seq", "user_id", "key", "principal") if k in row}
                assert any(all(a.get(k) == v for k, v in key.items()) for a in after[table]), (table, key)
        # The victim's guest work in the bystander's crew is gone; the bystander's task it created stays, unattributed.
        tasks = {r["id"]: r for r in after["crew_tasks"]}
        assert "crew_tasks:id:guest" in tasks and tasks["crew_tasks:id:guest"]["created_by"] is None
        assert tasks["crew_tasks:id:guest"]["owner_user_id"] is None and tasks["crew_tasks:id:guest"]["owner_session_id"] is None
        assert [r["id"] for r in after["crew_sessions"]] == ["cs_bystander"]
        assert [r["id"] for r in after["crew_messages"]] == ["msg_bystander"]
        assert [r["id"] for r in after["crews"]] == [CREW_B]
    finally:
        await crew.close()
        await main.close()


async def test_a_second_erase_finds_nothing(tmp_path: Path) -> None:
    crew = await _open(tmp_path)
    main = Database(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    await main.connect()
    await main.init_schema()
    try:
        await _seed(crew)
        eraser = AccountEraser(main, None, extra_databases=[crew_extra_database(crew)])
        await eraser.erase(VICTIM)
        again = await eraser.erase(VICTIM)
        assert {k: v for k, v in again.rows.items() if k.startswith("crew:")} == {}
    finally:
        await crew.close()
        await main.close()


def test_eraser_attach_replaces_by_name_and_detach_removes() -> None:
    eraser = AccountEraser(object(), None)
    first, second = crew_extra_database(object()), crew_extra_database(object())
    eraser.attach(first)
    eraser.attach(second)
    assert eraser.databases == ["crew"] and eraser._extra == [second]
    eraser.detach("crew")
    eraser.detach("crew")
    assert eraser.databases == []
    with pytest.raises(TypeError):
        eraser.attach(object())  # type: ignore[arg-type]


async def test_booted_app_with_crew_mode_erases_crew_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qdrant_client import AsyncQdrantClient

    import remembra.config
    import remembra.main as main
    from remembra.storage.qdrant import QdrantStore

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "crew_erasure_boot_test")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_PRE_MIGRATION_BACKUP", "false")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_CREW_MODE", "true")
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self: QdrantStore) -> AsyncQdrantClient:
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    app = main.create_app()
    async with app.router.lifespan_context(app):
        eraser = app.state.account_eraser
        assert eraser.databases == ["crew"]
        crew = app.state.crew_db
        await _seed(crew)
        receipt = await eraser.erase(VICTIM)
        assert receipt.rows.get("crew:crews") == 1
        cursor = await crew.conn.execute("SELECT COUNT(*) FROM crew_sessions WHERE user_id = ?", (VICTIM,))
        assert (await cursor.fetchone())[0] == 0
        cursor = await crew.conn.execute("SELECT COUNT(*) FROM crew_sessions WHERE user_id = ?", (BYSTANDER,))
        assert (await cursor.fetchone())[0] == 1
    assert eraser.databases == []
    monkeypatch.setattr(remembra.config, "_settings", None)
