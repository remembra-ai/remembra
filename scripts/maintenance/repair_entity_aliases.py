#!/usr/bin/env python3
"""Repair ``entities.aliases`` values that are not a JSON array.

Before the fix in ``services/sleep_time.py`` an entity merge joined two JSON
arrays with a comma (``["A"],["B"],[]``). That is not valid JSON, so the server
read the entity's aliases as empty. This job rewrites each such value as one
JSON array holding the same names (order kept, blanks and repeats dropped).
Rows that already hold a valid JSON array are not touched. It cannot undo a
merge: an entity that was deleted by a wrong merge stays deleted.

Dry run (default): no writes, prints counts only (never names):

    python scripts/maintenance/repair_entity_aliases.py --db /data/remembra.db

Apply (one transaction; take a backup first):

    python scripts/maintenance/repair_entity_aliases.py --db /data/remembra.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from remembra.services.sleep_time import parse_entity_aliases


def plan(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(entity id, repaired JSON) for every row whose aliases are not a JSON array."""
    rows = conn.execute("SELECT id, aliases FROM entities WHERE aliases IS NOT NULL AND aliases != ''").fetchall()
    repairs: list[tuple[str, str]] = []
    for entity_id, raw in rows:
        try:
            if isinstance(json.loads(raw), list):
                continue
        except ValueError:
            pass
        repairs.append((entity_id, json.dumps(parse_entity_aliases(raw))))
    return repairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="Path to the Remembra SQLite database")
    parser.add_argument("--apply", action="store_true", help="Write the repairs (default: dry run)")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db if args.apply else f"file:{args.db}?mode=ro", uri=not args.apply)
    try:
        total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        repairs = plan(conn)
        print(json.dumps({"entities": total, "to_repair": len(repairs), "applied": bool(args.apply)}))
        if args.apply and repairs:
            with conn:
                conn.executemany("UPDATE entities SET aliases = ? WHERE id = ?", [(a, i) for i, a in repairs])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
