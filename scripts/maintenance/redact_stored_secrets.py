#!/usr/bin/env python3
"""Report (and optionally redact) credentials stored in Remembra memories.

Dry run (default) prints counts per secret type and never modifies data:

    python scripts/maintenance/redact_stored_secrets.py

Apply the redaction in place (SQLite content + extracted_facts, FTS index, and
the Qdrant payload of each affected point; archived memories too):

    python scripts/maintenance/redact_stored_secrets.py --apply

Options: --user-id to limit to one tenant, --no-qdrant to skip vector payloads,
--show-ids to list affected memory ids (never the secret values).

Uses the server's normal configuration (REMEMBRA_DATABASE_URL, REMEMBRA_QDRANT_*,
REMEMBRA_ENCRYPTION_KEY). Take a backup (litestream snapshot / sqlite .backup)
before --apply. Vectors are not re-embedded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _main(args: argparse.Namespace) -> int:
    import structlog

    # Keep stdout clean for the JSON report; logs go to stderr.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

    from remembra.config import get_settings
    from remembra.security.secret_scan import scan_and_redact
    from remembra.storage.database import Database

    settings = get_settings()
    db = Database(settings.database_url)
    await db.connect()
    qdrant = None
    if not args.no_qdrant:
        from remembra.storage.qdrant import QdrantStore

        qdrant = QdrantStore(settings)
    try:
        report = await scan_and_redact(db, qdrant, apply=args.apply, user_id=args.user_id)
    finally:
        await db.close()
        if qdrant is not None:
            await qdrant.close()

    output = report.to_dict()
    if args.show_ids:
        output["affected_memory_ids"] = report.affected_ids
    print(json.dumps(output, indent=2))
    if not args.apply and report.rows_with_secrets:
        print("\nDry run only. Re-run with --apply to redact in place.", file=sys.stderr)
    return 0 if report.qdrant_errors == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Rewrite stored text (default: dry run)")
    parser.add_argument("--user-id", default=None, help="Only scan this user's memories")
    parser.add_argument("--no-qdrant", action="store_true", help="Do not touch Qdrant payloads")
    parser.add_argument("--show-ids", action="store_true", help="List affected memory ids")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
