#!/usr/bin/env python3
"""Backfill memory_type / scope / scope_prefixes / valid_from / valid_to into Qdrant payloads.

Dry run (default) reads SQLite and Qdrant and prints what would change:

    python scripts/maintenance/backfill_payload.py

Apply (overwrites only those payload keys; vectors, content and metadata are
untouched, nothing is deleted):

    python scripts/maintenance/backfill_payload.py --apply

Options: --user-id to limit to one tenant, --batch-size for the keyset page.
Uses the server configuration (REMEMBRA_DATABASE_URL, REMEMBRA_QDRANT_*).
Safe to re-run: up-to-date points are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _main(args: argparse.Namespace) -> int:
    import structlog

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

    from remembra.config import get_settings
    from remembra.storage.database import Database
    from remembra.storage.payload_backfill import backfill_payload
    from remembra.storage.qdrant import QdrantStore
    from remembra.storage.reindex import apply_active_collection

    settings = get_settings()
    db = Database(settings.database_url)
    await db.connect()
    await db.init_schema()
    qdrant = QdrantStore(settings)
    try:
        await apply_active_collection(db, qdrant)
        await qdrant.ensure_indexes()
        report = await backfill_payload(db, qdrant, apply=args.apply, user_id=args.user_id, batch_size=args.batch_size)
    finally:
        await qdrant.close()
        await db.close()
    print(json.dumps(report.to_dict(), indent=2))
    if not args.apply and report.needs_update:
        print("\nDry run only. Re-run with --apply to write the payload fields.", file=sys.stderr)
    return 0 if report.errors == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write payload fields (default: dry run)")
    parser.add_argument("--user-id", default=None, help="Only this user's memories")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per keyset page")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
