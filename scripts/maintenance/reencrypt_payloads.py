#!/usr/bin/env python3
"""Encrypt the plaintext text fields left in existing Qdrant payloads.

Points written before 2026-09-27 keep their extracted facts, entities and the
strings inside metadata lists in plaintext, even with REMEMBRA_ENCRYPTION_KEY
set. Points written while no key was set are plaintext throughout. Deploy the
code that encrypts these fields first, then run this.

Dry run (default) reads Qdrant and prints how many points still hold plaintext,
per field, with sample ids (never any text):

    python scripts/maintenance/reencrypt_payloads.py

Apply (overwrites only the text fields that still hold plaintext; vectors and
filter fields are untouched, nothing is deleted):

    python scripts/maintenance/reencrypt_payloads.py --apply

Options: --user-id to limit to one tenant, --batch-size for the scroll page,
--collection to name a collection (default: the active one, which follows a
rebuild reindex; a collection kept for rollback after a rebuild still holds
its old payloads until you run this on it or delete it), and --qdrant-path to
use a local embedded Qdrant folder instead of REMEMBRA_QDRANT_URL (for a copy
or a test).

Uses the server configuration (REMEMBRA_DATABASE_URL, REMEMBRA_QDRANT_*,
REMEMBRA_ENCRYPTION_KEY). Refuses to run without REMEMBRA_ENCRYPTION_KEY.
Take a Qdrant snapshot before --apply. Safe to re-run: points that are already
encrypted are skipped. Exit code 0 when no write failed, 1 when it cannot run,
2 when some writes failed.
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
    from remembra.storage.payload_reencrypt import reencrypt_payloads
    from remembra.storage.qdrant import QdrantStore

    settings = get_settings()
    if not settings.encryption_key:
        print("REMEMBRA_ENCRYPTION_KEY is not set: there is no key to encrypt with.", file=sys.stderr)
        return 1

    qdrant = QdrantStore(settings)
    if args.qdrant_path:
        from qdrant_client import AsyncQdrantClient

        qdrant._client = AsyncQdrantClient(path=args.qdrant_path)
    try:
        if args.collection:
            qdrant.collection_name = args.collection
        else:
            from remembra.storage.database import Database
            from remembra.storage.reindex import apply_active_collection

            db = Database(settings.database_url)
            await db.connect()
            try:
                await apply_active_collection(db, qdrant)
            finally:
                await db.close()
        client = await qdrant._get_client()
        if not await client.collection_exists(qdrant.collection_name):
            print(f"Qdrant collection {qdrant.collection_name!r} does not exist.", file=sys.stderr)
            return 1
        report = await reencrypt_payloads(qdrant, apply=args.apply, user_id=args.user_id, batch_size=args.batch_size)
    finally:
        await qdrant.close()

    print(json.dumps(report.to_dict(), indent=2))
    if not args.apply and report.needs_update:
        print("\nDry run only. Re-run with --apply to encrypt these fields.", file=sys.stderr)
    return 0 if report.errors == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the encrypted fields (default: dry run)")
    parser.add_argument("--user-id", default=None, help="Only this user's points")
    parser.add_argument("--batch-size", type=int, default=256, help="Points per scroll page")
    parser.add_argument("--collection", default=None, help="Collection to process (default: the active one)")
    parser.add_argument("--qdrant-path", default=None, help="Local embedded Qdrant folder instead of REMEMBRA_QDRANT_URL")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
