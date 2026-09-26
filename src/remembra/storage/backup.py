"""Pre-migration SQLite backups: one consistent copy per deploy, taken before
init_schema() touches the schema.

Production runs a single SQLite file on a Docker volume. Without Litestream a
failed or wrong migration has no way back, so startup copies the database with
the SQLite online-backup API before any DDL runs. The copy is keyed by build
(git SHA or package version): a restart of the same build does not copy again.
If a copy is needed and cannot be made (disk full, I/O error, corrupt source),
startup stops before migrating instead of changing unprotected data.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
from pathlib import Path

import structlog

from remembra.core.time import utcnow

log = structlog.get_logger(__name__)

BACKUP_PREFIX = "remembra-predeploy-"
# Free space needed on the backup volume: the database plus its WAL, times this
# factor, plus a fixed margin so the live database keeps room to grow.
FREE_SPACE_FACTOR = 1.2
FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024

_UNSAFE_LABEL_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class BackupError(RuntimeError):
    """A pre-migration backup was needed but could not be written."""


def backup_label(build_sha: str | None, fallback: str) -> str:
    """Filesystem-safe label for this build (short git SHA, else the version)."""
    raw = (build_sha or "").strip()[:12] or (fallback or "").strip() or "unknown"
    return _UNSAFE_LABEL_CHARS.sub("_", raw)


def _is_in_memory(db_path: str) -> bool:
    return db_path in ("", ":memory:") or db_path.startswith("file::memory:") or "mode=memory" in db_path


def _source_bytes(src: Path) -> int:
    total = src.stat().st_size
    wal = src.with_name(src.name + "-wal")
    if wal.is_file():
        total += wal.stat().st_size
    return total


def _has_tables(src: Path) -> bool:
    """False for a database with no tables yet (a fresh volume: connect() creates the file first)."""
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return True  # unreadable: let the copy below report it rather than skip silently
    return bool(row and row[0])


def _prune(target_dir: Path, keep: int) -> list[Path]:
    backups = sorted(target_dir.glob(f"{BACKUP_PREFIX}*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in backups[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError as exc:  # a stale backup we cannot delete must not block startup
            log.warning("pre_migration_backup_prune_failed", path=str(old), error=str(exc))
    return removed


def pre_migration_backup(
    db_path: str,
    *,
    label: str,
    keep: int = 3,
    backup_dir: str | None = None,
) -> Path | None:
    """Copy the database at ``db_path`` before schema changes run.

    Returns the new backup's path, or ``None`` when there is nothing to protect
    (in-memory database, no file yet, empty file, a file with no tables yet) or
    this build already has a backup. Raises :class:`BackupError` when a backup is needed but cannot be
    written; nothing partial is left behind.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if _is_in_memory(db_path):
        return None
    src = Path(db_path)
    if not src.is_file() or src.stat().st_size == 0 or not _has_tables(src):
        return None

    target_dir = Path(backup_dir) if backup_dir else src.parent / "backups"
    try:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create backup directory {target_dir}: {exc}") from exc

    if any(target_dir.glob(f"{BACKUP_PREFIX}{label}-*.db")):
        log.info("pre_migration_backup_exists", label=label, dir=str(target_dir))
        return None

    needed = int(_source_bytes(src) * FREE_SPACE_FACTOR) + FREE_SPACE_MARGIN_BYTES
    free = shutil.disk_usage(target_dir).free
    if free < needed:
        raise BackupError(
            f"not enough free space in {target_dir} for a pre-migration backup "
            f"(need {needed} bytes, have {free}); free space or set REMEMBRA_PRE_MIGRATION_BACKUP=false"
        )

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = target_dir / f"{BACKUP_PREFIX}{label}-{stamp}.db"
    partial = target_dir / f"{dest.name}.partial"
    try:
        source = sqlite3.connect(str(src))
        try:
            copy = sqlite3.connect(str(partial))
            try:
                source.backup(copy)
                result = copy.execute("PRAGMA quick_check").fetchone()
            finally:
                copy.close()
        finally:
            source.close()
        if not result or result[0] != "ok":
            raise BackupError(f"backup copy failed its integrity check: {result[0] if result else 'no result'}")
        os.chmod(partial, 0o600)
        os.replace(partial, dest)
    except BackupError:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise
    except (sqlite3.Error, OSError) as exc:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise BackupError(f"pre-migration backup of {src} failed: {exc}") from exc

    removed = _prune(target_dir, keep)
    log.info(
        "pre_migration_backup_written",
        path=str(dest),
        bytes=dest.stat().st_size,
        label=label,
        pruned=[p.name for p in removed],
    )
    return dest
