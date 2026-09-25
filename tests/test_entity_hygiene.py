"""AGT-9: scripts/maintenance/entity_hygiene.py on a real Remembra schema.

The database is created by the production ``Database.init_schema`` so the
script is tested against the real tables/migrations. Covers: report-only by
default (no writes), archive moves entity + links + relationships into
entity_archive (nothing hard-deleted), restore puts them back exactly, and
scoping by user/project.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from remembra.models.memory import Entity, Relationship
from remembra.storage.database import Database

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "maintenance" / "entity_hygiene.py"


def _load():
    spec = importlib.util.spec_from_file_location("entity_hygiene", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["entity_hygiene"] = module
    spec.loader.exec_module(module)
    return module


eh = _load()


@pytest.fixture()
async def db_path(tmp_path) -> str:
    path = str(tmp_path / "remembra.db")
    db = Database(path)
    await db.connect()
    await db.init_schema()

    async def mem(mid: str, user: str = "u1", project: str = "p1") -> None:
        await db.save_memory_metadata(
            memory_id=mid,
            user_id=user,
            project_id=project,
            content=mid,
            extracted_facts=[mid],
            metadata={},
            created_at=datetime(2026, 1, 1),
        )

    async def ent(name: str, etype: str = "concept", user: str = "u1", project: str = "p1") -> Entity:
        e = Entity(canonical_name=name, type=etype)
        await db.save_entity(e, user_id=user, project_id=project)
        return e

    for mid in ("m1", "m2", "m3"):
        await mem(mid)
    await mem("m-other", user="u2")

    alice = await ent("Alice", "person")
    acme = await ent("Acme", "organization")
    await db.link_memory_to_entity("m1", alice.id)
    await db.link_memory_to_entity("m2", acme.id)
    await db.save_relationship(Relationship(from_entity_id=alice.id, to_entity_id=acme.id, type="WORKS_AT"))

    junk_a = await ent("A")  # linked junk: must be archived WITH its link
    await db.link_memory_to_entity("m3", junk_a.id)
    await ent("~/clawd/memory/remembra-fixes.md")
    await ent("the")
    await ent("Node.js", "technology")  # legit, linked
    node = await db.find_entity_by_name("Node.js", "u1", "p1")
    assert node is not None
    await db.link_memory_to_entity("m3", node.id)
    await ent("Orphan Corp", "organization")  # orphan
    await ent("alice", "person")  # zero-link duplicate of Alice
    dup_linked = await ent("ACME", "organization")  # linked duplicate -> merge candidate only
    await db.link_memory_to_entity("m3", dup_linked.id)
    weird = await ent("Weirdo", "spaceship")  # unknown type, linked
    await db.link_memory_to_entity("m1", weird.id)
    await ent("the", "concept", user="u2")  # other tenant's junk
    await db.close()
    return path


def _counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("entities", "memory_entities", "relationships")
        }
    finally:
        conn.close()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_report_classifies_and_is_read_only(db_path):
    before = _counts(db_path)
    out = _run("--db", db_path, "--user-id", "u1", "--json")
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    by_name = {(f["name"], f["category"]): f for f in report["findings"]}

    assert by_name[("A", "junk")]["archivable"] and by_name[("A", "junk")]["reason"] == "too_short"
    assert by_name[("the", "junk")]["reason"] == "stopword"
    assert by_name[("~/clawd/memory/remembra-fixes.md", "junk")]["reason"] == "file_path"
    assert by_name[("Orphan Corp", "orphan")]["archivable"]
    assert by_name[("alice", "duplicate")]["archivable"]
    acme_dup = by_name[("ACME", "duplicate")]
    assert acme_dup["archivable"] is False and "merge candidate" in acme_dup["reason"]
    assert by_name[("Weirdo", "unknown_type")]["archivable"] is False
    names = {f["name"] for f in report["findings"]}
    assert "Node.js" not in names and "Alice" not in names
    assert all(f["user_id"] == "u1" for f in report["findings"])
    assert report["applied"] is False
    assert _counts(db_path) == before  # dry run wrote nothing
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'entity_archive'").fetchone() is None
    conn.close()


def test_apply_archives_then_restore_puts_everything_back(db_path):
    before = _counts(db_path)
    applied = _run("--db", db_path, "--user-id", "u1", "--apply")
    assert applied.returncode == 0, applied.stderr
    assert "Backup written" in applied.stdout
    assert list(Path(db_path).parent.glob("remembra.db.pre-entity-hygiene-*.bak"))

    after = _counts(db_path)
    # A, the, the md path, Orphan Corp, alice -> 5 entities archived; A's link to m3 moved too
    assert after["entities"] == before["entities"] - 5
    assert after["memory_entities"] == before["memory_entities"] - 1
    assert after["relationships"] == before["relationships"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    archived = {r["canonical_name"]: r for r in conn.execute("SELECT * FROM entity_archive")}
    assert set(archived) == {"A", "the", "~/clawd/memory/remembra-fixes.md", "Orphan Corp", "alice"}
    assert json.loads(archived["A"]["memory_links_json"])[0]["memory_id"] == "m3"
    # other tenant untouched
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE user_id = 'u2'").fetchone()[0] == 1
    conn.close()

    listed = _run("--db", db_path, "--list-archive", "--json")
    assert len(json.loads(listed.stdout)) == 5

    dry_restore = _run("--db", db_path, "--restore", "all")
    assert "needs --apply" in dry_restore.stdout
    assert _counts(db_path) == after

    restored = _run("--db", db_path, "--restore", "all", "--apply", "--no-backup", "--json")
    assert restored.returncode == 0, restored.stderr
    stats = json.loads(restored.stdout)
    assert stats["entities"] == 5 and stats["memory_links"] == 1
    assert _counts(db_path) == before


def test_archive_keeps_relationships_restorable(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = eh.scan(conn, user_id="u1")
    alice = conn.execute("SELECT * FROM entities WHERE canonical_name = 'Alice'").fetchone()
    finding = eh.Finding(
        entity_id=alice["id"],
        user_id="u1",
        project_id="p1",
        name="Alice",
        type="person",
        links=1,
        relationships=1,
        category="junk",
        reason="forced for test",
        archivable=True,
    )
    assert eh.archive(conn, [finding]) == 1
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0
    stats = eh.restore(conn, alice["id"])
    assert stats == {"entities": 1, "memory_links": 1, "relationships": 1, "relationships_skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1
    assert report.scanned > 0
    conn.close()


def test_project_scope(db_path):
    out = _run("--db", db_path, "--project", "nope", "--json")
    assert json.loads(out.stdout)["scanned"] == 0


def test_missing_db_fails_cleanly(tmp_path):
    out = _run("--db", str(tmp_path / "absent.db"))
    assert out.returncode != 0 and "database not found" in out.stderr
