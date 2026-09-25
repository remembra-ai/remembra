#!/usr/bin/env python3
"""Entity graph hygiene: report junk / orphan / duplicate entities; archive on request.

SAFE BY DEFAULT. Without ``--apply`` this only reads the database and prints a
report. With ``--apply`` it ARCHIVES (never hard-deletes) the archivable
entities: each one is moved, together with its memory links and
relationships, into the ``entity_archive`` table as a full JSON snapshot, and
can be put back exactly with ``--restore``. A backup copy of the database is
written first unless ``--no-backup`` is given.

What is reported
  junk          names that can't be real entities: 1 character, stopwords
                ("the", "it", "this", ...), pure numbers/punctuation, file
                paths, URLs, sentence-length strings        -> archivable
  orphan        no memory links and no relationships           -> archivable
  duplicate     same (user, project, normalised name): the entity with the
                most links is kept; zero-link duplicates        -> archivable
                duplicates that still have links are reported as merge
                candidates only (merging belongs to the sleep-time worker)
  unknown_type  type outside the known set                       -> report only

Usage
  python scripts/maintenance/entity_hygiene.py --db /data/remembra.db            # report
  python scripts/maintenance/entity_hygiene.py --db ... --user-id u --json       # scoped, JSON
  python scripts/maintenance/entity_hygiene.py --db ... --apply                  # archive
  python scripts/maintenance/entity_hygiene.py --db ... --list-archive
  python scripts/maintenance/entity_hygiene.py --db ... --restore all --apply    # undo

Stop the API (or run during low traffic) before --apply: the server holds its
own SQLite connection and this script writes in one transaction.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "it",
        "its",
        "they",
        "them",
        "their",
        "this",
        "that",
        "these",
        "those",
        "here",
        "there",
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "which",
        "and",
        "or",
        "but",
        "if",
        "then",
        "so",
        "not",
        "no",
        "yes",
        "ok",
        "okay",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "with",
        "about",
        "as",
        "into",
        "over",
        "under",
        "user",
        "assistant",
        "system",
        "someone",
        "something",
        "anything",
        "everything",
        "nothing",
        "thing",
        "things",
        "today",
        "tomorrow",
        "yesterday",
        "now",
        "later",
    ]
)

KNOWN_TYPES = frozenset(
    {
        "person",
        "organization",
        "company",
        "location",
        "place",
        "concept",
        "product",
        "project",
        "technology",
        "event",
        "date",
        "time",
        "money",
        "tool",
        "service",
        "team",
        "role",
        "document",
        "other",
    }
)

# ".js"/".ts" are deliberately absent: "Node.js", "Next.js" are real technologies.
_PATH_RE = re.compile(r"^(~|\.{1,2})?/|^[A-Za-z]:\\|[/\\][\w.-]+[/\\]|\.(py|md|json|yaml|yml|toml|sh|db|txt|log|csv)$", re.I)
# Numeric names are legitimate for these types ("2024", "$5k").
_NUMERIC_TYPES = frozenset({"date", "time", "money"})
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_NO_LETTERS_RE = re.compile(r"^[\W\d_]+$")
MAX_NAME_WORDS = 8
MAX_NAME_CHARS = 80

ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS entity_archive (
    archive_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project_id TEXT,
    canonical_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    entity_json TEXT NOT NULL,
    memory_links_json TEXT NOT NULL,
    relationships_json TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    restored_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entity_archive_user ON entity_archive(user_id, project_id);
"""


def normalize_name(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (name or "").lower()).split())


def junk_reason(name: str, entity_type: str | None = None) -> str | None:
    """Why ``name`` can't be a real entity, or None if it looks legitimate."""
    stripped = (name or "").strip()
    if len(stripped) < 2:
        return "too_short"
    if stripped.lower() in STOPWORDS:
        return "stopword"
    if _NO_LETTERS_RE.match(stripped) and (entity_type or "").lower() not in _NUMERIC_TYPES:
        return "no_letters"
    if _URL_RE.match(stripped):
        return "url"
    if _PATH_RE.search(stripped):
        return "file_path"
    if len(stripped) > MAX_NAME_CHARS or len(stripped.split()) > MAX_NAME_WORDS:
        return "sentence"
    return None


@dataclass
class Finding:
    entity_id: str
    user_id: str
    project_id: str | None
    name: str
    type: str
    links: int
    relationships: int
    category: str
    reason: str
    archivable: bool
    keep_id: str | None = None


@dataclass
class Report:
    scanned: int = 0
    findings: list[Finding] = field(default_factory=list)

    def by_category(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for f in self.findings:
            counts[f.category] += 1
        return dict(counts)

    @property
    def archivable(self) -> list[Finding]:
        return [f for f in self.findings if f.archivable]


def connect(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def scan(conn: sqlite3.Connection, user_id: str | None = None, project_id: str | None = None) -> Report:
    """Classify entities. Read-only."""
    where, params = [], []
    if user_id:
        where.append("e.user_id = ?")
        params.append(user_id)
    if project_id:
        where.append("e.project_id = ?")
        params.append(project_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT e.id, e.user_id, e.project_id, e.canonical_name, e.type, e.created_at,
               (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id = e.id) AS links,
               (SELECT COUNT(*) FROM relationships r
                 WHERE r.from_entity_id = e.id OR r.to_entity_id = e.id) AS rels
        FROM entities e {clause}
        """,
        params,
    ).fetchall()

    report = Report(scanned=len(rows))
    flagged: set[str] = set()

    def add(r: sqlite3.Row, category: str, reason: str, archivable: bool, keep_id: str | None = None) -> None:
        report.findings.append(
            Finding(
                entity_id=r["id"],
                user_id=r["user_id"],
                project_id=r["project_id"],
                name=r["canonical_name"],
                type=r["type"],
                links=r["links"],
                relationships=r["rels"],
                category=category,
                reason=reason,
                archivable=archivable,
                keep_id=keep_id,
            )
        )
        if archivable:
            flagged.add(r["id"])

    for r in rows:
        reason = junk_reason(r["canonical_name"], r["type"])
        if reason:
            add(r, "junk", reason, archivable=True)

    groups: dict[tuple[str, str | None, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["id"] in flagged:
            continue
        key = normalize_name(r["canonical_name"])
        if key:
            groups[(r["user_id"], r["project_id"], key)].append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = sorted(members, key=lambda m: (-(m["links"] + m["rels"]), m["created_at"] or "", m["id"]))[0]
        for m in members:
            if m["id"] == keep["id"]:
                continue
            if m["links"] == 0 and m["rels"] == 0:
                add(m, "duplicate", f"zero-link duplicate of {keep['canonical_name']!r}", archivable=True, keep_id=keep["id"])
            else:
                add(m, "duplicate", f"merge candidate into {keep['canonical_name']!r}", archivable=False, keep_id=keep["id"])

    # Orphans after duplicates, so a zero-link duplicate is reported as a duplicate.
    for r in rows:
        if r["id"] not in flagged and r["links"] == 0 and r["rels"] == 0:
            add(r, "orphan", "no memory links or relationships", archivable=True)

    for r in rows:
        if r["id"] not in flagged and (r["type"] or "").lower() not in KNOWN_TYPES:
            add(r, "unknown_type", f"type {r['type']!r} not in known set", archivable=False)

    return report


def backup(db_path: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"{db_path}.pre-entity-hygiene-{stamp}.bak")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def archive(conn: sqlite3.Connection, findings: list[Finding]) -> int:
    """Move archivable entities (+ links + relationships) into entity_archive, atomically."""
    conn.executescript(ARCHIVE_DDL)
    now = datetime.now(UTC).isoformat()
    moved = 0
    with conn:  # one transaction: all or nothing
        for f in findings:
            if not f.archivable:
                continue
            entity = conn.execute("SELECT * FROM entities WHERE id = ?", (f.entity_id,)).fetchone()
            if entity is None:
                continue
            links = [dict(x) for x in conn.execute("SELECT * FROM memory_entities WHERE entity_id = ?", (f.entity_id,))]
            rels = [
                dict(x)
                for x in conn.execute(
                    "SELECT * FROM relationships WHERE from_entity_id = ? OR to_entity_id = ?", (f.entity_id, f.entity_id)
                )
            ]
            conn.execute(
                """INSERT INTO entity_archive (archive_id, entity_id, user_id, project_id, canonical_name, reason,
                   entity_json, memory_links_json, relationships_json, archived_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"ea_{f.entity_id}_{now}",
                    f.entity_id,
                    f.user_id,
                    f.project_id,
                    f.name,
                    f"{f.category}: {f.reason}",
                    json.dumps(dict(entity)),
                    json.dumps(links),
                    json.dumps(rels),
                    now,
                ),
            )
            conn.execute("DELETE FROM memory_entities WHERE entity_id = ?", (f.entity_id,))
            conn.execute("DELETE FROM relationships WHERE from_entity_id = ? OR to_entity_id = ?", (f.entity_id, f.entity_id))
            conn.execute("DELETE FROM entities WHERE id = ?", (f.entity_id,))
            moved += 1
    return moved


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    data = {k: v for k, v in row.items() if k in columns}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def restore(conn: sqlite3.Connection, which: str) -> dict[str, int]:
    """Put archived entities back (``which`` = archive_id, entity_id, or 'all')."""
    conn.executescript(ARCHIVE_DDL)
    if which == "all":
        rows = conn.execute("SELECT * FROM entity_archive WHERE restored_at IS NULL").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entity_archive WHERE restored_at IS NULL AND (archive_id = ? OR entity_id = ?)", (which, which)
        ).fetchall()
    stats = {"entities": 0, "memory_links": 0, "relationships": 0, "relationships_skipped": 0}
    now = datetime.now(UTC).isoformat()
    with conn:
        for r in rows:  # entities first so relationships between two archived entities can be restored
            _insert(conn, "entities", json.loads(r["entity_json"]))
            stats["entities"] += 1
        for r in rows:
            for link in json.loads(r["memory_links_json"]):
                if conn.execute("SELECT 1 FROM memories WHERE id = ?", (link["memory_id"],)).fetchone():
                    _insert(conn, "memory_entities", link)
                    stats["memory_links"] += 1
            for rel in json.loads(r["relationships_json"]):
                ends = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE id IN (?, ?)", (rel["from_entity_id"], rel["to_entity_id"])
                ).fetchone()[0]
                if ends == (1 if rel["from_entity_id"] == rel["to_entity_id"] else 2):
                    _insert(conn, "relationships", rel)
                    stats["relationships"] += 1
                else:
                    stats["relationships_skipped"] += 1
            conn.execute("UPDATE entity_archive SET restored_at = ? WHERE archive_id = ?", (now, r["archive_id"]))
    return stats


def _print_report(report: Report, limit: int) -> None:
    print(f"Scanned {report.scanned} entities. Findings: {report.by_category() or 'none'}")
    print(f"Archivable (junk + orphan + zero-link duplicates): {len(report.archivable)}")
    for category in ("junk", "orphan", "duplicate", "unknown_type"):
        items = [f for f in report.findings if f.category == category]
        if not items:
            continue
        print(f"\n[{category}] {len(items)}")
        for f in items[:limit]:
            flag = "archive" if f.archivable else "report"
            print(f"  {flag:7} {f.entity_id}  {f.name!r} ({f.type}) links={f.links} rels={f.relationships}  {f.reason}")
        if len(items) > limit:
            print(f"  ... {len(items) - limit} more (use --json for all)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="Path to the Remembra SQLite database")
    parser.add_argument("--user-id", help="Only this user's entities")
    parser.add_argument("--project", help="Only this project's entities")
    parser.add_argument("--apply", action="store_true", help="Actually archive (or restore). Default: report only")
    parser.add_argument("--no-backup", action="store_true", help="Skip the automatic DB backup before --apply")
    parser.add_argument("--restore", metavar="ID|all", help="Restore archived entities (needs --apply)")
    parser.add_argument("--list-archive", action="store_true", help="List archived entities")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--limit", type=int, default=25, help="Rows per category in the text report")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.list_archive:
            conn.executescript(ARCHIVE_DDL)
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT archive_id, entity_id, canonical_name, reason, archived_at, restored_at FROM entity_archive"
                )
            ]
            print(
                json.dumps(rows, indent=2)
                if args.json
                else "\n".join(
                    f"{r['archive_id']}  {r['canonical_name']!r}  {r['reason']}  restored={r['restored_at']}" for r in rows
                )
                or "archive is empty"
            )
            return 0

        if args.restore:
            if not args.apply:
                print("Dry run: --restore needs --apply to write. Nothing changed.")
                return 0
            if not args.no_backup:
                print(f"Backup written: {backup(args.db)}")
            stats = restore(conn, args.restore)
            print(json.dumps(stats) if args.json else f"Restored: {stats}")
            return 0

        report = scan(conn, user_id=args.user_id, project_id=args.project)
        if args.json:
            print(
                json.dumps(
                    {
                        "scanned": report.scanned,
                        "by_category": report.by_category(),
                        "archivable": len(report.archivable),
                        "findings": [f.__dict__ for f in report.findings],
                        "applied": args.apply,
                    },
                    indent=2,
                )
            )
        else:
            _print_report(report, args.limit)

        if not args.apply:
            if not args.json:
                print(
                    "\nDry run: nothing changed. Re-run with --apply to archive the 'archive' rows (reversible with --restore)."
                )
            return 0
        if not args.no_backup:
            print(f"Backup written: {backup(args.db)}", file=sys.stderr if args.json else sys.stdout)
        moved = archive(conn, report.archivable)
        print(f"Archived {moved} entities into entity_archive.", file=sys.stderr if args.json else sys.stdout)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
