"""Backfill the plain filter fields of existing Qdrant payloads (RET-7 / UPG-1).

Points written before 2026-09-25 carry no ``memory_type``, ``scope``,
``scope_prefixes``, ``valid_from`` or ``valid_to`` in their payload. Recall
itself no longer depends on them (every hit is validated against its SQLite
row), but server-side scope/type filtering and any payload consumer do.

This walks SQLite (the source of truth) with keyset paging, skips source
records, compares each point's payload with what the row says, and - only
with ``apply=True`` - overwrites just those keys (``set_payload``; vectors and
the encrypted content/metadata are untouched). Points without a vector are
reported as ``missing_vector`` (reconcile/the pending worker repairs those).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from remembra.storage.memory_rows import is_source_row, memory_from_row
from remembra.storage.qdrant import filterable_payload

log = structlog.get_logger(__name__)


@dataclass
class BackfillReport:
    apply: bool
    scanned: int = 0
    skipped_source: int = 0
    missing_vector: int = 0
    up_to_date: int = 0
    needs_update: int = 0
    updated: int = 0
    errors: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "apply" if self.apply else "dry_run",
            "scanned": self.scanned,
            "skipped_source": self.skipped_source,
            "missing_vector": self.missing_vector,
            "up_to_date": self.up_to_date,
            "needs_update": self.needs_update,
            "updated": self.updated,
            "errors": self.errors,
            "samples": self.samples,
        }


async def backfill_payload(
    db: Any,
    qdrant: Any,
    *,
    apply: bool = False,
    user_id: str | None = None,
    batch_size: int = 500,
    sample_limit: int = 10,
) -> BackfillReport:
    """Compare (and with ``apply`` fix) the filterable payload of every vector."""
    report = BackfillReport(apply=apply)
    last_id = ""
    while True:
        sql = "SELECT * FROM memories WHERE id > ?"
        params: list[Any] = [last_id]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(batch_size)
        cursor = await db.conn.execute(sql, tuple(params))
        rows = [dict(r) for r in await cursor.fetchall()]
        if not rows:
            return report
        last_id = rows[-1]["id"]

        wanted: dict[str, dict[str, Any]] = {}
        for row in rows:
            report.scanned += 1
            if is_source_row(row):
                report.skipped_source += 1
                continue
            wanted[row["id"]] = filterable_payload(memory_from_row(row))

        payloads = await qdrant.get_raw_payloads(list(wanted))
        for memory_id, fields in wanted.items():
            current = payloads.get(memory_id)
            if current is None:
                report.missing_vector += 1
                continue
            stale = {k: v for k, v in fields.items() if current.get(k) != v}
            if not stale:
                report.up_to_date += 1
                continue
            report.needs_update += 1
            if len(report.samples) < sample_limit:
                report.samples.append({"id": memory_id, "fields": sorted(stale)})
            if apply:
                try:
                    await qdrant.set_payload_fields(memory_id, fields)
                    report.updated += 1
                except Exception as e:  # noqa: BLE001 - keep going, report the count
                    report.errors += 1
                    log.warning("payload_backfill_failed", memory_id=memory_id, error=str(e))
