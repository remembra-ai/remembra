"""Re-extract facts that were stored while extraction had fallen back (REL-7 / ING-14).

When the extraction LLM failed (quota, outage, open circuit, store time budget)
the store path kept the input as sentence-split or verbatim facts and marked
them ``metadata.extraction = "fallback"`` (plus ``extraction_error_kind``).
This job finds those facts, groups them by the input they came from (the
verbatim source record when there is one, otherwise the fact itself), runs
extraction again and:

* **same facts** (extraction now agrees with what is stored): only clears the
  marker (``extraction = "reprocessed"``);
* **different facts**: stores the new facts through the normal fact pipeline
  (grounding + consolidation against everything *except* the old fallback
  facts) and supersedes the old fallback facts - they stay queryable as
  history (``include_superseded`` / ``as_of``), nothing is deleted;
* **extraction still failing**: leaves the group untouched.

Dry run (``apply=False``, the default) makes no model calls and no writes; it
only reports what is queued.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from remembra.core.time import utcnow

log = structlog.get_logger(__name__)

MARKER_KEYS = ("extraction", "extraction_error_kind")


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


@dataclass
class ReprocessReport:
    apply: bool
    fallback_facts: int = 0
    groups: int = 0
    by_error_kind: dict[str, int] = field(default_factory=dict)
    unchanged_cleared: int = 0
    replaced_groups: int = 0
    new_memories: int = 0
    superseded: int = 0
    still_failing: int = 0
    errors: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "apply" if self.apply else "dry_run",
            "fallback_facts": self.fallback_facts,
            "groups": self.groups,
            "by_error_kind": self.by_error_kind,
            "unchanged_cleared": self.unchanged_cleared,
            "replaced_groups": self.replaced_groups,
            "new_memories": self.new_memories,
            "superseded": self.superseded,
            "still_failing": self.still_failing,
            "errors": self.errors,
            "samples": self.samples,
        }


async def find_fallback_facts(db: Any, *, user_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    """Live (not superseded, not source) memories marked extraction=fallback, oldest first."""
    sql = """
        SELECT * FROM memories
        WHERE json_valid(metadata) AND json_extract(metadata, '$.extraction') = 'fallback'
          AND superseded_by IS NULL
          AND (memory_type IS NULL OR memory_type != 'source')
          AND (expires_at IS NULL OR expires_at > ?)
    """
    params: list[Any] = [utcnow().isoformat()]
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY created_at, id LIMIT ?"
    params.append(limit)
    cursor = await db.conn.execute(sql, tuple(params))
    return [dict(r) for r in await cursor.fetchall()]


async def _clear_marker(service: Any, row: dict[str, Any]) -> None:
    meta = _meta(row)
    for key in MARKER_KEYS:
        meta.pop(key, None)
    meta["extraction"] = "reprocessed"
    meta["reprocessed_at"] = utcnow().isoformat()
    await service.db.conn.execute(
        "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(meta), utcnow().isoformat(), row["id"]),
    )
    await service.db.conn.commit()
    try:
        await service.qdrant.set_metadata(row["id"], meta)
    except Exception as e:  # noqa: BLE001 - payload metadata is advisory; SQLite is the truth
        log.warning("reprocess_payload_metadata_failed", memory_id=row["id"], error=str(e))


async def reprocess_fallback(
    service: Any,
    *,
    apply: bool = False,
    user_id: str | None = None,
    limit: int = 1000,
    sample_limit: int = 10,
) -> ReprocessReport:
    """Find fallback-extracted facts and (with ``apply``) re-extract them."""
    report = ReprocessReport(apply=apply)
    rows = await find_fallback_facts(service.db, user_id=user_id, limit=limit)
    report.fallback_facts = len(rows)

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        meta = _meta(row)
        kind = str(meta.get("extraction_error_kind") or "unknown")
        report.by_error_kind[kind] = report.by_error_kind.get(kind, 0) + 1
        origin = str(meta.get("source_id") or row["id"])
        groups.setdefault((row["user_id"], row["project_id"], origin), []).append(row)
    report.groups = len(groups)

    for (uid, project_id, origin), members in groups.items():
        sample: dict[str, Any] = {"origin": origin, "facts": len(members)}
        source_row = await service.db.get_memory(origin) if origin != members[0]["id"] else None
        source_text = (source_row or {}).get("content") or " ".join(m["content"] for m in members)
        if not apply:
            if len(report.samples) < sample_limit:
                report.samples.append(sample)
            continue
        try:
            extraction = await service.extractor.extract_detailed(source_text, reference_date=utcnow())
            if extraction.method != "llm" or not extraction.facts:
                report.still_failing += 1
                sample["result"] = "still_failing"
                continue
            old_norm = sorted(_norm(m["content"]) for m in members)
            new_norm = sorted(_norm(f) for f in extraction.facts)
            if old_norm == new_norm:
                for m in members:
                    await _clear_marker(service, m)
                report.unchanged_cleared += len(members)
                sample["result"] = "unchanged"
                continue

            base_meta = {k: v for k, v in _meta(members[0]).items() if k not in MARKER_KEYS}
            base_meta["extraction"] = "reprocessed"
            base_meta["reprocessed_from"] = [m["id"] for m in members]
            if source_row is not None:
                base_meta["source_id"] = origin
            old_ids = {m["id"] for m in members}
            first_new: str | None = None
            for fact in extraction.facts:
                result = await service.store_fact(
                    fact=fact,
                    user_id=uid,
                    project_id=project_id,
                    metadata=dict(base_meta),
                    source="reprocess",
                    grounding_source=source_text,
                    consolidate=True,
                    exclude_ids=set(old_ids),
                    expires_at=_parse_dt(members[0].get("expires_at")),
                )
                target = result.memory_id or result.target_id
                if result.memory_id:
                    report.new_memories += 1
                first_new = first_new or target
            if first_new is None:
                # Every new fact was dropped by grounding: keep the old ones.
                report.still_failing += 1
                sample["result"] = "new_facts_ungrounded"
                continue
            for old_id in old_ids:
                await service.db.mark_memory_superseded(old_id, first_new)
                report.superseded += 1
            report.replaced_groups += 1
            sample["result"] = "replaced"
        except Exception as e:  # noqa: BLE001 - one bad group must not stop the job
            report.errors += 1
            sample["result"] = f"error: {type(e).__name__}"
            log.warning("reprocess_group_failed", origin=origin, error=str(e))
        finally:
            if len(report.samples) < sample_limit:
                report.samples.append(sample)
    return report
