#!/usr/bin/env python3
"""Re-extract memories stored while fact extraction had fallen back.

Facts written during an extraction outage (LLM quota, open circuit, store
time budget) carry ``metadata.extraction = "fallback"``. This job re-runs
extraction on their original input and replaces them through the normal fact
pipeline; the old fallback facts are SUPERSEDED (kept as history), never
deleted.

Dry run (default): no model calls, no writes - counts per error kind and a
sample of the queued groups:

    python scripts/maintenance/reprocess_fallback.py

Apply (calls the extraction LLM once per group, then writes):

    python scripts/maintenance/reprocess_fallback.py --apply [--user-id U] [--limit 200]

Uses the server configuration (REMEMBRA_DATABASE_URL, REMEMBRA_QDRANT_*,
REMEMBRA_OPENAI_API_KEY, embedding provider settings). Take a backup before
--apply. Check /health/ready first: while the provider is still failing the
groups are reported as still_failing and left alone.
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
    from remembra.extraction import background
    from remembra.services.memory import MemoryService
    from remembra.services.reprocess import reprocess_fallback
    from remembra.storage.database import Database
    from remembra.storage.embeddings import EmbeddingService
    from remembra.storage.qdrant import QdrantStore
    from remembra.storage.reindex import apply_active_collection

    settings = get_settings()
    db = Database(settings.database_url)
    await db.connect()
    await db.init_schema()
    qdrant = QdrantStore(settings)
    try:
        await apply_active_collection(db, qdrant)
        service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=EmbeddingService(settings))
        report = await reprocess_fallback(service, apply=args.apply, user_id=args.user_id, limit=args.limit)
        await background.drain(timeout=60.0)  # entity resolution for new facts
    finally:
        await qdrant.close()
        await db.close()
    print(json.dumps(report.to_dict(), indent=2))
    if not args.apply and report.groups:
        print("\nDry run only. Re-run with --apply to re-extract.", file=sys.stderr)
    return 0 if report.errors == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Re-extract and write (default: dry run)")
    parser.add_argument("--user-id", default=None, help="Only this user's memories")
    parser.add_argument("--limit", type=int, default=1000, help="Max fallback facts to consider")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
