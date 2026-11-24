"""The sleep-time entity pass merges only entities that are the same one, and keeps aliases readable.

At bd612c65 the pass (on by default, every 6 hours) merged any two entities of
one type in one project when one name contained the other, so "Sam",
"Samantha Reed" and "Sam Altman" became a single entity and "Go" joined
"Google". A merge deletes an entity and moves its memory links, and nothing
undoes it. The merge also joined the two JSON alias arrays with a comma
(``["Sammy"],["Ms. Reed"]``), which is not JSON, so the server read the kept
entity's aliases as empty from then on.

Everything here runs the real worker on the real Database (SQLite on disk).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remembra.config import Settings
from remembra.core.time import utcnow
from remembra.models.memory import Entity
from remembra.services.sleep_time import SleepTimeWorker, parse_entity_aliases
from remembra.storage.database import Database

USER = "u1"
REPAIR_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "maintenance" / "repair_entity_aliases.py"


@pytest.fixture()
async def db(tmp_path):
    database = Database(str(tmp_path / "merge.db"))
    await database.connect()
    await database.init_schema()
    yield database
    await database.close()


def _worker(db: Database) -> SleepTimeWorker:
    service = SimpleNamespace(db=db, qdrant=None, embeddings=None, consolidator=None, entity_matcher=None)
    return SleepTimeWorker(settings=Settings(openai_api_key="t"), memory_service=service)


async def _entity(db: Database, name: str, aliases: list[str] | None = None, *, kind: str = "person", project: str = "p1") -> str:
    entity = Entity(canonical_name=name, type=kind, aliases=aliases or [])
    await db.save_entity(entity, USER, project)
    return entity.id


async def _memory(db: Database, memory_id: str) -> None:
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id=USER,
        project_id="p1",
        content=f"text of {memory_id}",
        extracted_facts=[],
        metadata={},
        created_at=utcnow(),
        expires_at=None,
    )


async def _names(db: Database) -> dict[str, Any]:
    cursor = await db.conn.execute(
        "SELECT canonical_name, aliases FROM entities WHERE user_id = ? ORDER BY canonical_name", (USER,)
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def test_a_name_inside_another_name_is_not_merged(db: Database) -> None:
    for name in ("Sam", "Samantha Reed", "Sam Altman"):
        await _entity(db, name)
    await _entity(db, "Go", kind="technology")
    await _entity(db, "Google", kind="technology")

    merged = await _worker(db)._entity_resolution_pass(USER)

    assert merged == 0
    assert set(await _names(db)) == {"Sam", "Samantha Reed", "Sam Altman", "Go", "Google"}


async def test_same_name_and_alias_matches_still_merge_and_links_move(db: Database) -> None:
    keep = await _entity(db, "Samantha Reed", ["Sammy"])
    same_name = await _entity(db, "samantha  reed", ["Ms. Reed"])
    by_alias = await _entity(db, "Sammy", ["S. Reed"])
    await _entity(db, "Sam Altman")
    await _memory(db, "mem-1")
    await _memory(db, "mem-2")
    await db.link_memory_to_entity("mem-1", same_name)
    await db.link_memory_to_entity("mem-2", by_alias)

    merged = await _worker(db)._entity_resolution_pass(USER)

    assert merged == 2
    names = await _names(db)
    assert set(names) == {"Samantha Reed", "Sam Altman"}
    assert json.loads(names["Samantha Reed"]) == ["Sammy", "Ms. Reed", "S. Reed"]
    cursor = await db.conn.execute("SELECT memory_id FROM memory_entities WHERE entity_id = ? ORDER BY memory_id", (keep,))
    assert [row[0] for row in await cursor.fetchall()] == ["mem-1", "mem-2"]
    # The server's own reader sees the aliases again.
    loaded = await db.get_entity(keep)
    assert loaded is not None and loaded.aliases == ["Sammy", "Ms. Reed", "S. Reed"]


async def test_entities_of_another_type_or_project_are_never_merged(db: Database) -> None:
    await _entity(db, "Mercury", kind="planet")
    await _entity(db, "Mercury", kind="company")
    await _entity(db, "Mercury", kind="planet", project="p2")

    assert await _worker(db)._entity_resolution_pass(USER) == 0


async def test_a_merge_into_already_damaged_aliases_leaves_valid_json(db: Database) -> None:
    keep = await _entity(db, "Samantha Reed")
    await _entity(db, "Samantha Reed", ["Ms. Reed"])
    await db.conn.execute("UPDATE entities SET aliases = ? WHERE id = ?", ('["Sammy"],["S. Reed"],[]', keep))
    await db.conn.commit()

    assert await _worker(db)._entity_resolution_pass(USER) == 1

    assert json.loads((await _names(db))["Samantha Reed"]) == ["Sammy", "S. Reed", "Ms. Reed"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("[]", []),
        ('["Sammy", "Ms. Reed"]', ["Sammy", "Ms. Reed"]),
        ('["Sammy"],["Ms. Reed"],[]', ["Sammy", "Ms. Reed"]),
        (',["Sammy"]', ["Sammy"]),
        ('["Sammy"],["sammy", " "],["Acme, Inc."]', ["Sammy", "Acme, Inc."]),
        ("Sammy, Ms. Reed", ["Sammy", "Ms. Reed"]),
        (["Sammy", ["Ms. Reed"]], ["Sammy", "Ms. Reed"]),
        ('{"not": "a list"}', []),
    ],
)
def test_aliases_are_read_from_every_stored_shape(raw: Any, expected: list[str]) -> None:
    assert parse_entity_aliases(raw) == expected


def _repair_module() -> Any:
    spec = importlib.util.spec_from_file_location("repair_entity_aliases", REPAIR_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_repair_script_is_a_dry_run_by_default_and_fixes_only_damaged_rows(db: Database, tmp_path, capsys) -> None:
    damaged = await _entity(db, "Samantha Reed")
    healthy = await _entity(db, "Sam Altman", ["Sama"])
    await db.conn.execute("UPDATE entities SET aliases = ? WHERE id = ?", ('["Sammy"],["Ms. Reed"],[]', damaged))
    await db.conn.commit()
    path = str(tmp_path / "merge.db")
    repair = _repair_module()

    assert repair.main(["--db", path]) == 0
    assert json.loads(capsys.readouterr().out) == {"entities": 2, "to_repair": 1, "applied": False}
    raw = sqlite3.connect(path).execute("SELECT aliases FROM entities WHERE id = ?", (damaged,)).fetchone()[0]
    assert raw == '["Sammy"],["Ms. Reed"],[]'

    assert repair.main(["--db", path, "--apply"]) == 0
    assert "Sammy" not in capsys.readouterr().out  # counts only, never names
    rows = dict(sqlite3.connect(path).execute("SELECT id, aliases FROM entities").fetchall())
    assert json.loads(rows[damaged]) == ["Sammy", "Ms. Reed"]
    assert json.loads(rows[healthy]) == ["Sama"]
    assert repair.main(["--db", path]) == 0
    assert json.loads(capsys.readouterr().out)["to_repair"] == 0
