"""Consistent, verifiable snapshots of the main database **and** ``crew.db`` (Crew mode WP-16).

Litestream (``scripts/cloud-entrypoint.sh``) is the continuous backup in Remembra
Cloud. This module is the point-in-time complement: a copy you take on purpose
before a risky change (enabling ``REMEMBRA_CREW_MODE``, a migration, a rollback)
and the backup for self-hosted installs that do not run litestream.

* ``create`` copies each SQLite file with SQLite's online backup API, so it is
  safe while the server is running and writing (WAL included). The copy is
  checked with ``PRAGMA integrity_check``, switched to a self-contained
  rollback journal, hashed, and described in ``manifest.json`` (schema version
  and row counts of the tables a restore drill compares).
* ``verify`` re-hashes and re-checks a snapshot.
* ``restore`` verifies first, never deletes anything (an existing database and
  its ``-wal``/``-shm`` files are renamed to ``*.pre-restore-<stamp>``), writes
  through a temp file plus an atomic rename, and checks the result. **Stop the
  server first**: a running process keeps writing to the file it has open.

``crew.db`` lives next to the main database unless ``REMEMBRA_CREW_DB_PATH``
says otherwise (:func:`remembra.crew.db.resolve_crew_db_path`); a snapshot taken
before Crew mode was ever enabled simply has no ``crew.db`` entry.

CLI (prints JSON on stdout)::

    python -m remembra.storage.snapshot create  [--out DIR] [--db PATH] [--crew-db PATH]
    python -m remembra.storage.snapshot verify  SNAPSHOT_DIR
    python -m remembra.storage.snapshot restore SNAPSHOT_DIR [--db PATH] [--crew-db PATH] [--force]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

FORMAT_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
MAIN_FILE: Final = "remembra.db"
CREW_FILE: Final = "crew.db"
SIDE_SUFFIXES: Final = ("-wal", "-shm", "-journal")
# Row counts recorded for the main database (the crew database records every crew table).
MAIN_COUNT_TABLES: Final = ("memories", "entities", "api_keys", "agent_inbox")


class SnapshotError(Exception):
    """A snapshot could not be created, verified or restored; the message says why."""


@dataclass(frozen=True)
class DbPaths:
    main: Path
    crew: Path


def _utc_stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def _sqlite_path(url_or_path: str) -> str:
    """The file path of a ``sqlite…:///path`` URL (same rule as ``Database``), or the path itself."""
    return url_or_path.split("///")[-1] if url_or_path.startswith("sqlite") else url_or_path


def default_paths(db: str | None = None, crew_db: str | None = None) -> DbPaths:
    """Main database from ``--db`` or the server settings; ``crew.db`` from ``--crew-db`` or the crew rule."""
    from remembra.crew.db import resolve_crew_db_path

    if db is None:
        from remembra.config import get_settings

        db = get_settings().database_url
    main = _sqlite_path(db)
    if main in ("", ":memory:"):
        raise SnapshotError("the main database is in memory; there is nothing to snapshot")
    crew = crew_db if crew_db is not None else resolve_crew_db_path(main)
    return DbPaths(Path(main).expanduser(), Path(_sqlite_path(crew)).expanduser())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SnapshotError(f"{path} does not exist")
    return sqlite3.connect(str(path), timeout=30)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)).fetchone()
    return row is not None


def _describe(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, Any]:
    integrity = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    version = None
    if _table_exists(conn, "schema_version"):
        version = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])
    counts: dict[str, int] = {}
    for table in tables:
        if _table_exists(conn, table):
            counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])  # noqa: S608 - fixed names
    return {"integrity": "ok" if integrity == ["ok"] else "; ".join(integrity[:5]), "schema_version": version, "counts": counts}


def _count_tables(role: str) -> tuple[str, ...]:
    if role == "crew":
        from remembra.crew.db import CREW_TABLES

        return CREW_TABLES
    return MAIN_COUNT_TABLES


def _copy_db(source: Path, dest: Path, role: str) -> dict[str, Any]:
    """Online backup of ``source`` into ``dest`` (a new file), then check and describe the copy."""
    src = _connect(source)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)  # one step: a consistent read transaction over the whole file (WAL-safe)
            dst.execute("PRAGMA journal_mode=DELETE")  # a self-contained file, no -wal next to it
            info = _describe(dst, _count_tables(role))
        finally:
            dst.close()
    finally:
        src.close()
    if info["integrity"] != "ok":
        raise SnapshotError(f"{role} copy failed integrity_check: {info['integrity']}")
    return {
        "role": role,
        "file": dest.name,
        "source": str(source),
        "bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
        **info,
    }


def create_snapshot(
    paths: DbPaths,
    out_root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Snapshot the main database (required) and ``crew.db`` (when present) into a new directory under ``out_root``."""
    if not paths.main.is_file():
        raise SnapshotError(f"main database {paths.main} does not exist")
    taken = now()
    out_root.mkdir(parents=True, exist_ok=True)
    target = out_root / f"remembra-snapshot-{_utc_stamp(taken)}"
    suffix = 1
    while target.exists():
        suffix += 1
        target = out_root / f"remembra-snapshot-{_utc_stamp(taken)}-{suffix}"
    work = target.with_name(target.name + ".partial")
    work.mkdir(parents=True)
    try:
        databases = [_copy_db(paths.main, work / MAIN_FILE, "main")]
        if paths.crew.is_file():
            databases.append(_copy_db(paths.crew, work / CREW_FILE, "crew"))
        manifest = {
            "format": FORMAT_VERSION,
            "created_at": taken.isoformat(),
            "crew_included": len(databases) == 2,
            "databases": databases,
        }
        (work / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(work, target)  # a snapshot directory only ever exists complete
    except BaseException:
        for child in work.glob("*"):
            child.unlink()
        work.rmdir()
        raise
    return {**manifest, "path": str(target)}


def _read_manifest(snapshot_dir: Path) -> dict[str, Any]:
    path = snapshot_dir / MANIFEST_NAME
    if not path.is_file():
        raise SnapshotError(f"{snapshot_dir} has no {MANIFEST_NAME}")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SnapshotError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        raise SnapshotError(f"{path}: unsupported snapshot format {manifest.get('format')!r}")
    return manifest


def verify_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """Re-hash and re-check every database in a snapshot; raise :class:`SnapshotError` on any mismatch."""
    manifest = _read_manifest(snapshot_dir)
    roles = [str(db.get("role")) for db in manifest.get("databases", [])]
    if "main" not in roles:
        raise SnapshotError(f"{snapshot_dir}: the manifest lists no main database")
    for entry in manifest["databases"]:
        file = snapshot_dir / str(entry["file"])
        if not file.is_file():
            raise SnapshotError(f"{file} is missing")
        if _sha256(file) != entry["sha256"]:
            raise SnapshotError(f"{file} does not match its sha256 in the manifest")
        conn = _connect(file)
        try:
            info = _describe(conn, _count_tables(str(entry["role"])))
        finally:
            conn.close()
        if info["integrity"] != "ok":
            raise SnapshotError(f"{file} failed integrity_check: {info['integrity']}")
        if info["schema_version"] != entry["schema_version"] or info["counts"] != entry["counts"]:
            raise SnapshotError(f"{file} does not match the schema version or row counts in the manifest")
    return {**manifest, "path": str(snapshot_dir), "verified": True}


def _existing_files(target: Path) -> list[Path]:
    return [p for p in [target, *(target.with_name(target.name + s) for s in SIDE_SUFFIXES)] if p.exists()]


def restore_snapshot(
    snapshot_dir: Path,
    paths: DbPaths,
    *,
    force: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Restore every database in ``snapshot_dir`` to ``paths``; the server must be stopped.

    Without ``force`` it refuses when a target (or its ``-wal``/``-shm``) exists. With
    ``force`` the existing files are renamed to ``<name>.pre-restore-<stamp>`` (never deleted).
    """
    manifest = verify_snapshot(snapshot_dir)
    targets = {"main": paths.main, "crew": paths.crew}
    plan = [(entry, targets[str(entry["role"])]) for entry in manifest["databases"]]
    occupied = [str(p) for _entry, target in plan for p in _existing_files(target)]
    if occupied and not force:
        raise SnapshotError(
            "refusing to overwrite existing database files (stop the server, then re-run with --force "
            "to keep them as *.pre-restore-<stamp>): " + ", ".join(occupied)
        )
    stamp = _utc_stamp(now())
    moved: list[str] = []
    restored: list[dict[str, Any]] = []
    for entry, target in plan:
        for existing in _existing_files(target):
            aside = existing.with_name(f"{existing.name}.pre-restore-{stamp}")
            os.replace(existing, aside)
            moved.append(str(aside))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restore-tmp")
        with (snapshot_dir / str(entry["file"])).open("rb") as src, tmp.open("wb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, target)
        if _sha256(target) != entry["sha256"]:
            raise SnapshotError(f"{target} does not match the snapshot after the copy")
        restored.append({"role": entry["role"], "path": str(target), "schema_version": entry["schema_version"]})
    return {
        "snapshot": str(snapshot_dir),
        "restored": restored,
        "moved_aside": moved,
        "crew_included": manifest.get("crew_included", False),
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m remembra.storage.snapshot",
        description="Snapshot, verify or restore the main database and crew.db.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="take a consistent snapshot (safe while the server runs)")
    create.add_argument("--out", help="parent directory (default: <main db dir>/backups)")
    verify = sub.add_parser("verify", help="re-hash and integrity-check a snapshot")
    verify.add_argument("snapshot")
    restore = sub.add_parser("restore", help="restore a snapshot (stop the server first)")
    restore.add_argument("snapshot")
    restore.add_argument("--force", action="store_true", help="move existing database files aside instead of refusing")
    for sp in (create, restore):
        sp.add_argument("--db", help="main database path or sqlite URL (default: REMEMBRA_DATABASE_URL)")
        sp.add_argument("--crew-db", dest="crew_db", help="crew.db path (default: REMEMBRA_CREW_DB_PATH or next to --db)")
    return p


def main(argv: Sequence[str] | None = None, *, out: Any = None, err: Any = None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_snapshot(Path(args.snapshot))
        else:
            paths = default_paths(args.db, args.crew_db)
            if args.command == "create":
                result = create_snapshot(paths, Path(args.out) if args.out else paths.main.parent / "backups")
            else:
                result = restore_snapshot(Path(args.snapshot), paths, force=args.force)
    except (SnapshotError, sqlite3.Error, OSError) as e:
        print(f"snapshot {args.command} failed: {e}", file=err)
        return 1
    with contextlib.suppress(BrokenPipeError):
        print(json.dumps(result, indent=2, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
