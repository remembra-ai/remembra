"""WP-16: ``python -m remembra.storage.snapshot`` takes, verifies and restores the main DB and crew.db.

Both databases are the real ones: the main database through ``Database.init_schema`` (v1..v5) and
the agent inbox, ``crew.db`` through ``open_crew_db`` (``CREW_MIGRATIONS``) with a crew and a session
seeded the way the services store them. Snapshots are taken while those connections are open and
hold committed rows only in the WAL, then restored and reopened by the same production code.
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remembra.crew.db import CREW_MIGRATION_RUNNER, CrewDatabase, open_crew_db
from remembra.inbox.manager import InboxManager
from remembra.storage import snapshot as snap
from remembra.storage.database import Database, main_migrations
from tests.crew import wp8_seed as seed

FIXED = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _crew_db_next_to_main(monkeypatch):
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)


async def _open_world(volume: Path) -> tuple[Database, CrewDatabase, str]:
    volume.mkdir(parents=True, exist_ok=True)
    db = Database(f"sqlite+aiosqlite:///{volume / 'remembra.db'}")
    await db.connect()
    await db.init_schema()
    inbox = InboxManager(db)
    await inbox.init_schema()
    crew_db = await open_crew_db(str(volume / "remembra.db"))
    crew_id = await seed.crew(crew_db, "owner-1", "yaadbooks")
    await seed.session(crew_db, crew_id, "cs_a", user_id="owner-1", callsign="cc-1", client_session_id="sess-a")
    await inbox.send("owner-1", "codex", "claude-code", "hello", "body", project_id="yaadbooks", crew_id=crew_id)
    return db, crew_db, crew_id


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - fixed names
    finally:
        conn.close()


async def test_snapshot_while_running_captures_wal_rows_and_restores_into_the_real_schema(tmp_path: Path) -> None:
    volume = tmp_path / "data"
    db, crew_db, crew_id = await _open_world(volume)
    try:
        # committed rows still in the WAL (the connections are open, nothing checkpointed)
        assert (volume / "crew.db-wal").stat().st_size > 0 and (volume / "remembra.db-wal").stat().st_size > 0
        paths = snap.default_paths(f"sqlite+aiosqlite:///{volume / 'remembra.db'}")
        assert paths == snap.DbPaths(volume / "remembra.db", volume / "crew.db")
        result = snap.create_snapshot(paths, tmp_path / "backups", now=lambda: FIXED)
    finally:
        await crew_db.close()
        await db.close()

    out = Path(result["path"])
    assert out.name == "remembra-snapshot-20260926T120000Z" and result["crew_included"] is True
    assert sorted(p.name for p in out.iterdir()) == ["crew.db", "manifest.json", "remembra.db"]
    by_role = {d["role"]: d for d in result["databases"]}
    assert by_role["main"]["schema_version"] == main_migrations().latest_version
    assert by_role["crew"]["schema_version"] == CREW_MIGRATION_RUNNER.latest_version
    assert by_role["main"]["counts"]["agent_inbox"] == 1
    assert by_role["crew"]["counts"]["crews"] == 1 and by_role["crew"]["counts"]["crew_sessions"] == 1
    assert all(d["integrity"] == "ok" for d in result["databases"])
    # the copies are self-contained files (no WAL needed next to them)
    for name in ("remembra.db", "crew.db"):
        conn = sqlite3.connect(out / name)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        conn.close()
    assert snap.verify_snapshot(out)["verified"] is True

    # disaster: the volume is gone; restore into a fresh one and reopen with the production code
    fresh = tmp_path / "fresh"
    restored = snap.restore_snapshot(out, snap.DbPaths(fresh / "remembra.db", fresh / "crew.db"))
    assert [r["role"] for r in restored["restored"]] == ["main", "crew"] and restored["moved_aside"] == []
    db2 = Database(str(fresh / "remembra.db"))
    await db2.connect()
    await db2.init_schema()  # the server's boot path: every migration is already recorded
    assert await db2.get_schema_version() == main_migrations().latest_version
    await db2.close()
    crew2 = await open_crew_db(str(fresh / "remembra.db"))
    try:
        assert await crew2.get_schema_version() == CREW_MIGRATION_RUNNER.latest_version
        crews = await crew2.fetchall("SELECT id, project_id FROM crews")
        assert crews == [{"id": crew_id, "project_id": "yaadbooks"}]
        assert (await crew2.fetchone("SELECT callsign FROM crew_sessions WHERE id = 'cs_a'")) == {"callsign": "cc-1"}
    finally:
        await crew2.close()
    assert _count(fresh / "remembra.db", "agent_inbox") == 1


async def test_snapshot_before_crew_mode_has_no_crew_db_and_restore_leaves_crew_db_alone(tmp_path: Path) -> None:
    volume = tmp_path / "data"
    volume.mkdir()
    db = Database(str(volume / "remembra.db"))
    await db.connect()
    await db.init_schema()
    await db.close()
    result = snap.create_snapshot(snap.default_paths(str(volume / "remembra.db")), tmp_path / "b", now=lambda: FIXED)
    assert result["crew_included"] is False and [d["role"] for d in result["databases"]] == ["main"]

    # later a crew.db exists; restoring the pre-crew snapshot replaces only the main database
    (volume / "crew.db").write_bytes(b"crew-state")
    restored = snap.restore_snapshot(Path(result["path"]), snap.default_paths(str(volume / "remembra.db")), force=True)
    assert [r["role"] for r in restored["restored"]] == ["main"]
    assert (volume / "crew.db").read_bytes() == b"crew-state"


async def test_restore_refuses_to_overwrite_then_force_moves_everything_aside(tmp_path: Path) -> None:
    volume = tmp_path / "data"
    db, crew_db, _ = await _open_world(volume)
    paths = snap.default_paths(str(volume / "remembra.db"))
    try:
        result = snap.create_snapshot(paths, tmp_path / "b", now=lambda: FIXED)
        # the world moves on after the snapshot
        await seed.crew(crew_db, "owner-1", "second-project")
    finally:
        await crew_db.close()
        await db.close()
    before = {p.name for p in volume.iterdir()}
    with pytest.raises(snap.SnapshotError, match="refusing to overwrite"):
        snap.restore_snapshot(Path(result["path"]), paths)
    assert {p.name for p in volume.iterdir()} == before  # nothing touched

    later = datetime(2026, 9, 27, 8, 30, 0, tzinfo=UTC)
    restored = snap.restore_snapshot(Path(result["path"]), paths, force=True, now=lambda: later)
    aside = sorted(Path(p).name for p in restored["moved_aside"])
    assert "crew.db.pre-restore-20260927T083000Z" in aside and "remembra.db.pre-restore-20260927T083000Z" in aside
    # the newer state is kept aside, the restored one is the snapshot
    assert _count(volume / "crew.db.pre-restore-20260927T083000Z", "crews") == 2
    assert _count(volume / "crew.db", "crews") == 1
    assert not (volume / "crew.db-wal").exists() and not (volume / "remembra.db-wal").exists()


async def test_verify_detects_a_changed_file_and_a_missing_manifest(tmp_path: Path) -> None:
    volume = tmp_path / "data"
    db, crew_db, _ = await _open_world(volume)
    try:
        result = snap.create_snapshot(snap.default_paths(str(volume / "remembra.db")), tmp_path / "b", now=lambda: FIXED)
    finally:
        await crew_db.close()
        await db.close()
    out = Path(result["path"])
    crew_copy = out / "crew.db"
    conn = sqlite3.connect(crew_copy)
    conn.execute("DELETE FROM crew_sessions")
    conn.commit()
    conn.close()
    with pytest.raises(snap.SnapshotError, match="sha256"):
        snap.verify_snapshot(out)
    with pytest.raises(snap.SnapshotError, match="sha256"):
        snap.restore_snapshot(out, snap.DbPaths(tmp_path / "x" / "remembra.db", tmp_path / "x" / "crew.db"))
    assert not (tmp_path / "x").exists()  # a bad snapshot never reaches the target
    (out / "manifest.json").unlink()
    with pytest.raises(snap.SnapshotError, match="no manifest.json"):
        snap.verify_snapshot(out)


def test_create_needs_the_main_database(tmp_path: Path) -> None:
    with pytest.raises(snap.SnapshotError, match="does not exist"):
        snap.create_snapshot(snap.DbPaths(tmp_path / "none.db", tmp_path / "crew.db"), tmp_path / "b")
    assert not any((tmp_path / "b").glob("*"))
    with pytest.raises(snap.SnapshotError, match="in memory"):
        snap.default_paths("sqlite:///:memory:")


def test_crew_db_path_override_is_honoured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMEMBRA_CREW_DB_PATH", str(tmp_path / "elsewhere" / "crew.db"))
    assert snap.default_paths(str(tmp_path / "remembra.db")).crew == tmp_path / "elsewhere" / "crew.db"
    assert snap.default_paths(str(tmp_path / "remembra.db"), str(tmp_path / "c.db")).crew == tmp_path / "c.db"


async def test_cli_create_verify_restore(tmp_path: Path, monkeypatch) -> None:
    volume = tmp_path / "data"
    db, crew_db, crew_id = await _open_world(volume)
    await crew_db.close()
    await db.close()
    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite+aiosqlite:///{volume / 'remembra.db'}")
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    import remembra.config as config_module

    monkeypatch.setattr(config_module, "_settings", None)

    out, err = io.StringIO(), io.StringIO()
    assert snap.main(["create", "--out", str(tmp_path / "b")], out=out, err=err) == 0, err.getvalue()
    created = json.loads(out.getvalue())
    assert created["crew_included"] is True

    out = io.StringIO()
    assert snap.main(["verify", created["path"]], out=out, err=err) == 0
    assert json.loads(out.getvalue())["verified"] is True

    err = io.StringIO()
    assert snap.main(["restore", created["path"]], out=io.StringIO(), err=err) == 1
    assert "refusing to overwrite" in err.getvalue()

    target = tmp_path / "restored"
    out = io.StringIO()
    code = snap.main(["restore", created["path"], "--db", str(target / "remembra.db")], out=out, err=err)
    assert code == 0, err.getvalue()
    assert [r["path"] for r in json.loads(out.getvalue())["restored"]] == [
        str(target / "remembra.db"),
        str(target / "crew.db"),
    ]
    assert _count(target / "crew.db", "crews") == 1

    err = io.StringIO()
    assert snap.main(["verify", str(tmp_path / "nope")], out=io.StringIO(), err=err) == 1
    assert "no manifest.json" in err.getvalue()


async def test_scheduled_create_with_keep_covers_crew_db_and_prunes_only_old_snapshots(tmp_path: Path, monkeypatch) -> None:
    """The scheduled task (`create --out /data/backups --keep N`) on a 0.17.0 volume: the main database at
    schema 12 with v5, and crew.db. Each run snapshots both files; only older snapshot folders are deleted."""
    volume = tmp_path / "data"
    db, crew_db, _crew_id = await _open_world(volume)
    applied = sorted(await main_migrations().applied_versions(db.conn))
    await crew_db.close()
    await db.close()
    assert applied == list(range(1, 13))  # Includes v11 claim fences and v12 durable erasure.
    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite+aiosqlite:///{volume / 'remembra.db'}")
    import remembra.config as config_module

    monkeypatch.setattr(config_module, "_settings", None)
    backups = volume / "backups"
    backups.mkdir(mode=0o700)
    # What else lives in /data/backups: pre-migration copies, a crashed run, a folder without a manifest.
    predeploy = backups / "remembra-predeploy-4d335ce0000-20260926T000000Z.db"
    predeploy.write_bytes(b"predeploy")
    (backups / "remembra-snapshot-20200101T000000Z.partial").mkdir()
    (backups / "remembra-snapshot-20200101T000000Z").mkdir()  # no manifest: not a snapshot, never deleted
    (backups / "notes").mkdir()

    stamps = iter(datetime(2026, 9, 26 + day, 3, 0, 0, tzinfo=UTC) for day in range(4))
    monkeypatch.setattr(snap, "datetime", type("_Clock", (), {"now": staticmethod(lambda tz=None: next(stamps))}))
    runs = []
    for _ in range(4):
        out, err = io.StringIO(), io.StringIO()
        assert snap.main(["create", "--out", str(backups), "--keep", "2"], out=out, err=err) == 0, err.getvalue()
        runs.append(json.loads(out.getvalue()))

    assert [r["crew_included"] for r in runs] == [True] * 4
    by_role = {d["role"]: d for d in runs[-1]["databases"]}
    assert by_role["main"]["schema_version"] == 12 and by_role["crew"]["counts"]["crews"] == 1
    assert [len(r["pruned"]) for r in runs] == [0, 0, 1, 1]
    assert [Path(p).name for r in runs for p in r["pruned"]] == [
        "remembra-snapshot-20260926T030000Z",
        "remembra-snapshot-20260927T030000Z",
    ]
    assert sorted(p.name for p in backups.iterdir()) == [
        "notes",
        "remembra-predeploy-4d335ce0000-20260926T000000Z.db",
        "remembra-snapshot-20200101T000000Z",
        "remembra-snapshot-20200101T000000Z.partial",
        "remembra-snapshot-20260928T030000Z",
        "remembra-snapshot-20260929T030000Z",
    ]
    assert predeploy.read_bytes() == b"predeploy"
    newest = backups / "remembra-snapshot-20260929T030000Z"
    assert snap.verify_snapshot(newest)["verified"] is True
    # A snapshot holds every account's data: owner-only, like the pre-migration copies.
    assert newest.stat().st_mode & 0o777 == 0o700
    assert {p.name: p.stat().st_mode & 0o777 for p in newest.iterdir()} == {
        "crew.db": 0o600,
        "manifest.json": 0o600,
        "remembra.db": 0o600,
    }


def test_keep_refuses_nonsense_before_taking_a_snapshot(tmp_path: Path) -> None:
    main_db = tmp_path / "remembra.db"
    sqlite3.connect(main_db).close()
    for bad in ("0", "-3", "1001"):
        err = io.StringIO()
        code = snap.main(
            ["create", "--db", str(main_db), "--out", str(tmp_path / "b"), "--keep", bad], out=io.StringIO(), err=err
        )
        assert code == 1 and "--keep must be between 1 and 1000" in err.getvalue()
    assert not (tmp_path / "b").exists()
    with pytest.raises(snap.SnapshotError, match="at least 1"):
        snap.prune_snapshots(tmp_path, 0)


def test_prune_orders_same_second_snapshots_by_their_suffix(tmp_path: Path) -> None:
    for name in (
        "remembra-snapshot-20260926T030000Z",
        "remembra-snapshot-20260926T030000Z-2",
        "remembra-snapshot-20260926T030000Z-10",
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "manifest.json").write_text("{}")
    deleted = snap.prune_snapshots(tmp_path, 1)
    assert sorted(Path(p).name for p in deleted) == ["remembra-snapshot-20260926T030000Z", "remembra-snapshot-20260926T030000Z-2"]
    assert [p.name for p in tmp_path.iterdir()] == ["remembra-snapshot-20260926T030000Z-10"]
