"""Find and redact credentials already stored in memories (SEC-23 backfill).

Scans ``memories`` and ``archived_memories`` (content, extracted_facts and the
string values of metadata, such as a relay handoff's Failing section), the FTS
index, and the Qdrant payload of each affected point. Dry-run by default:
it only reports counts per secret type. With ``apply=True`` it rewrites the text
in place with ``[REDACTED:<kind>]`` placeholders — an in-place redaction, never a
delete. Secret values are never logged or returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.security.secrets import redact_secrets

log = structlog.get_logger(__name__)

_BATCH = 500


@dataclass
class ScanReport:
    applied: bool
    rows_scanned: int = 0
    rows_with_secrets: int = 0
    rows_redacted: int = 0
    qdrant_payloads_updated: int = 0
    qdrant_errors: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    affected_ids: list[str] = field(default_factory=list)

    def add(self, counts: dict[str, int]) -> None:
        for kind, n in counts.items():
            self.counts[kind] = self.counts.get(kind, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rows_scanned": self.rows_scanned,
            "rows_with_secrets": self.rows_with_secrets,
            "rows_redacted": self.rows_redacted,
            "qdrant_payloads_updated": self.qdrant_payloads_updated,
            "qdrant_errors": self.qdrant_errors,
            "counts_by_type": dict(sorted(self.counts.items())),
        }


def _redact_facts(raw: str | None) -> tuple[str | None, dict[str, int]]:
    if not raw:
        return raw, {}
    try:
        facts = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        result = redact_secrets(raw)
        return result.text, result.counts
    if not isinstance(facts, list):
        return raw, {}
    counts: dict[str, int] = {}
    cleaned = []
    for fact in facts:
        if isinstance(fact, str):
            result = redact_secrets(fact)
            for kind, n in result.counts.items():
                counts[kind] = counts.get(kind, 0) + n
            cleaned.append(result.text)
        else:
            cleaned.append(fact)
    return (json.dumps(cleaned) if counts else raw), counts


# Metadata values that identify things (the relay row key, session and agent ids,
# commit shas, checksums): a long random session id reads like a token, and
# rewriting it would break the lookups that use it.
_METADATA_ID_KEYS = frozenset(
    {
        "relay_key",
        "session_id",
        "agent_id",
        "user_id",
        "project_id",
        "memory_id",
        "source_id",
        "id",
        "sha",
        "head_commit",
        "checksum",
        "content_checksum",
    }
)


def _redact_value(value: Any, counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        result = redact_secrets(value)
        for kind, n in result.counts.items():
            counts[kind] = counts.get(kind, 0) + n
        return result.text
    if isinstance(value, dict):
        return {k: v if k in _METADATA_ID_KEYS else _redact_value(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, counts) for v in value]
    return value


def _redact_metadata(raw: str | None) -> tuple[str | None, dict[str, Any] | None, dict[str, int]]:
    """``(metadata_json, metadata, counts)`` with credentials redacted from its string values (keys and ids kept)."""
    if not raw:
        return raw, None, {}
    try:
        metadata = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw, None, {}
    if not isinstance(metadata, dict):
        return raw, None, {}
    counts: dict[str, int] = {}
    cleaned = _redact_value(metadata, counts)
    if not counts:
        return raw, None, {}
    return json.dumps(cleaned), cleaned, counts


async def _update_qdrant(
    qdrant: Any, memory_id: str, content: str, facts_json: str | None, metadata: dict[str, Any] | None = None
) -> None:
    """Overwrite the text fields of one Qdrant point (encrypted like QdrantStore.upsert)."""
    from remembra.storage.qdrant import encrypt_text_fields

    client = await qdrant._get_client()
    fields: dict[str, Any] = {"content": content}
    if facts_json:
        try:
            fields["extracted_facts"] = json.loads(facts_json)
        except (TypeError, json.JSONDecodeError):
            pass
    if metadata is not None:
        fields["metadata"] = metadata
    payload = encrypt_text_fields(qdrant._encryptor, fields)
    await client.set_payload(collection_name=qdrant.collection_name, payload=payload, points=[memory_id])


async def scan_and_redact(
    db: Any,
    qdrant: Any | None = None,
    *,
    apply: bool = False,
    user_id: str | None = None,
) -> ScanReport:
    """Scan stored memories for credentials; redact them in place when ``apply``."""
    report = ScanReport(applied=apply)
    for table, has_vectors in (("memories", True), ("archived_memories", False)):
        last_rowid = 0
        while True:
            params: list[Any] = [last_rowid]
            where = "rowid > ?"
            if user_id:
                where += " AND user_id = ?"
                params.append(user_id)
            cursor = await db.conn.execute(
                f"SELECT rowid, id, user_id, project_id, content, extracted_facts, metadata FROM {table} "
                f"WHERE {where} ORDER BY rowid LIMIT {_BATCH}",
                params,
            )
            rows = await cursor.fetchall()
            if not rows:
                break
            for row in rows:
                last_rowid = row[0]
                memory_id, owner, project_id, content, facts_raw = row[1], row[2], row[3], row[4] or "", row[5]
                report.rows_scanned += 1
                content_result = redact_secrets(content)
                new_facts, fact_counts = _redact_facts(facts_raw)
                new_meta_json, new_meta, meta_counts = _redact_metadata(row[6])
                if not content_result.redacted and not fact_counts and not meta_counts:
                    continue
                report.rows_with_secrets += 1
                report.add(content_result.counts)
                report.add(fact_counts)
                report.add(meta_counts)
                report.affected_ids.append(memory_id)
                if not apply:
                    continue

                await db.conn.execute(
                    f"UPDATE {table} SET content = ?, extracted_facts = ?, metadata = ?, updated_at = ? WHERE id = ?",
                    (content_result.text, new_facts, new_meta_json, datetime.now(UTC).isoformat(), memory_id),
                )
                if has_vectors:
                    await db.conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
                    await db.conn.execute(
                        "INSERT INTO memories_fts (id, user_id, project_id, content) VALUES (?, ?, ?, ?)",
                        (memory_id, owner, project_id, content_result.text),
                    )
                await db.conn.commit()
                report.rows_redacted += 1

                if has_vectors and qdrant is not None:
                    try:
                        await _update_qdrant(qdrant, memory_id, content_result.text, new_facts, new_meta)
                        report.qdrant_payloads_updated += 1
                    except Exception as e:
                        report.qdrant_errors += 1
                        log.warning("secret_redaction_qdrant_update_failed", memory_id=memory_id, error_type=type(e).__name__)
    log.info("secret_scan_complete", **report.to_dict())
    return report
