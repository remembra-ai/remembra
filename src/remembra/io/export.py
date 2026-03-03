"""
Memory export in multiple formats: JSON, JSONL, CSV.

Streaming-friendly for large datasets.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


async def export_json(
    memories: list[dict[str, Any]],
    include_metadata: bool = True,
) -> str:
    """Export memories as a formatted JSON string."""
    export_data = {
        "version": "1.0",
        "exported_at": datetime.now(UTC).isoformat(),
        "total": len(memories),
        "memories": _prepare_memories(memories, include_metadata),
    }
    return json.dumps(export_data, indent=2, default=str)


async def export_jsonl(
    memories: list[dict[str, Any]],
    include_metadata: bool = True,
) -> str:
    """Export memories as JSONL (one JSON object per line)."""
    lines: list[str] = []
    for memory in _prepare_memories(memories, include_metadata):
        lines.append(json.dumps(memory, default=str))
    return "\n".join(lines)


async def export_csv(
    memories: list[dict[str, Any]],
    include_metadata: bool = True,
) -> str:
    """Export memories as CSV."""
    output = io.StringIO()
    fields = ["id", "content", "user_id", "project_id", "created_at"]
    if include_metadata:
        fields.extend(["extracted_facts", "entities", "metadata", "expires_at", "source", "trust_score"])

    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()

    for memory in _prepare_memories(memories, include_metadata):
        row = {}
        for f in fields:
            val = memory.get(f, "")
            if isinstance(val, (list, dict)):
                row[f] = json.dumps(val, default=str)
            else:
                row[f] = str(val) if val is not None else ""
        writer.writerow(row)

    return output.getvalue()


def _prepare_memories(
    memories: list[dict[str, Any]],
    include_metadata: bool,
) -> list[dict[str, Any]]:
    """Normalise memory dicts for export (strip internal fields)."""
    result: list[dict[str, Any]] = []
    for m in memories:
        out: dict[str, Any] = {
            "id": m.get("id", ""),
            "content": m.get("content", ""),
            "user_id": m.get("user_id", ""),
            "project_id": m.get("project_id", "default"),
            "created_at": m.get("created_at", ""),
        }
        if include_metadata:
            out["extracted_facts"] = m.get("extracted_facts", [])
            out["entities"] = m.get("entities", [])
            out["metadata"] = m.get("metadata", {})
            out["expires_at"] = m.get("expires_at")
            out["source"] = m.get("source", "unknown")
            out["trust_score"] = m.get("trust_score", 1.0)
        result.append(out)
    return result
