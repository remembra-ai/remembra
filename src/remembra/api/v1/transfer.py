"""Memory import/export endpoints – /api/v1/transfer."""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from remembra.auth.middleware import (
    CurrentUser,
    require_memory_recall,
    require_memory_store,
    resolve_project_or_default,
)
from remembra.cloud.limits import enforce_store_quota, record_store_usage
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter
from remembra.io.export import export_csv, export_json, export_jsonl
from remembra.io.importers import SUPPORTED_FORMATS, ImportedMemory
from remembra.io.importers.chatgpt import parse_chatgpt_export
from remembra.io.importers.claude import parse_claude_export
from remembra.io.importers.plaintext import (
    parse_csv_import,
    parse_json_array,
    parse_jsonl,
    parse_plaintext,
)
from remembra.models.memory import StoreRequest
from remembra.security.content_policy import prepare_content
from remembra.security.secrets import scrub_memory_record
from remembra.services.memory import MemoryService

router = APIRouter(prefix="/transfer", tags=["import/export"])

log = structlog.get_logger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]

# Imported text gets at most this trust (it is third-party content).
_IMPORT_MAX_TRUST = 0.8


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_memory_service(request: Request) -> MemoryService:
    service: MemoryService = request.app.state.memory_service
    return service


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ImportRequest(BaseModel):
    """Inline import (small payloads via JSON body)."""

    format: str = Field(
        description=f"Source format: {', '.join(SUPPORTED_FORMATS)}",
    )
    data: str = Field(description="Raw content to import")
    project_id: str | None = Field(None, description="Target project (single-project keys default to their project)")
    split_mode: str = Field(
        "paragraph",
        description="For plaintext: paragraph, line, heading, none",
    )


class ImportResponse(BaseModel):
    imported: int
    skipped: int
    errors: int
    details: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.get(
    "/export",
    summary="Export memories",
    response_class=StreamingResponse,
    dependencies=[require_memory_recall()],
)
@limiter.limit("5/minute")
async def export_memories(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    format: str = Query("json", description="Export format: json, jsonl, csv"),
    project_id: str | None = Query(None, description="Project to export (single-project keys default to theirs)"),
    include_metadata: bool = Query(True, description="Include metadata in export"),
    limit: int = Query(10000, ge=1, le=100000),
) -> Response:
    """Export all memories as JSON, JSONL, or CSV.

    Streaming download for large datasets. Stored credentials are redacted.
    """
    project_id = resolve_project_or_default(current_user, project_id)

    # Fetch all memories for the user
    memories = await _fetch_all_memories(
        memory_service,
        user_id=current_user.user_id,
        project_id=project_id,
        limit=limit,
    )

    if format == "jsonl":
        content = await export_jsonl(memories, include_metadata=include_metadata)
        media_type = "application/x-ndjson"
        filename = "memories.jsonl"
    elif format == "csv":
        content = await export_csv(memories, include_metadata=include_metadata)
        media_type = "text/csv"
        filename = "memories.csv"
    else:
        content = await export_json(memories, include_metadata=include_metadata)
        media_type = "application/json"
        filename = "memories.json"

    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Import (inline JSON body)
# ---------------------------------------------------------------------------


@router.post(
    "/import",
    response_model=ImportResponse,
    summary="Import memories",
    dependencies=[require_memory_store()],
)
@limiter.limit("5/minute")
async def import_memories(
    request: Request,
    body: ImportRequest,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> ImportResponse:
    """Import memories from various formats.

    Supported formats: json, jsonl, csv, chatgpt, claude, plaintext.
    """
    project_id = resolve_project_or_default(current_user, body.project_id)
    parsed = _parse_import(body.format, body.data, body.split_mode)

    if not parsed:
        return ImportResponse(imported=0, skipped=0, errors=0, details=[])

    await enforce_store_quota(request, current_user.user_id, len(parsed))
    result = await _store_imported_memories(
        request=request,
        memories=parsed,
        memory_service=memory_service,
        user_id=current_user.user_id,
        project_id=project_id,
        sanitization_enabled=settings.sanitization_enabled,
    )
    await record_store_usage(request, current_user.user_id, result.imported)
    return result


# ---------------------------------------------------------------------------
# Import (file upload)
# ---------------------------------------------------------------------------


@router.post(
    "/import/file",
    response_model=ImportResponse,
    summary="Import memories from file",
    dependencies=[require_memory_store()],
)
@limiter.limit("3/minute")
async def import_from_file(
    request: Request,
    file: UploadFile,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    settings: SettingsDep,
    format: str = Query(
        ...,
        description=f"Source format: {', '.join(SUPPORTED_FORMATS)}",
    ),
    project_id: str | None = Query(None),
    split_mode: str = Query("paragraph"),
) -> ImportResponse:
    """Import memories from an uploaded file."""
    project_id = resolve_project_or_default(current_user, project_id)

    # Read file content (limit to 50MB)
    max_size = 50 * 1024 * 1024
    content = await file.read(max_size + 1)
    if len(content) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large. Maximum 50MB.",
        )

    data = content.decode("utf-8", errors="replace")
    parsed = _parse_import(format, data, split_mode)

    if not parsed:
        return ImportResponse(imported=0, skipped=0, errors=0, details=[])

    await enforce_store_quota(request, current_user.user_id, len(parsed))
    result = await _store_imported_memories(
        request=request,
        memories=parsed,
        memory_service=memory_service,
        user_id=current_user.user_id,
        project_id=project_id,
        sanitization_enabled=settings.sanitization_enabled,
    )
    await record_store_usage(request, current_user.user_id, result.imported)
    return result


@router.get(
    "/formats",
    summary="List supported import formats",
)
async def list_formats(request: Request) -> dict[str, Any]:
    """List supported import/export formats and their descriptions."""
    return {
        "import_formats": {
            "json": "JSON array of memory objects or strings",
            "jsonl": "Newline-delimited JSON (one object per line)",
            "csv": "CSV file with a 'content' column",
            "chatgpt": "ChatGPT conversations.json export",
            "claude": "Claude conversation export",
            "plaintext": "Plain text split by paragraphs, lines, or headings",
        },
        "export_formats": {
            "json": "Formatted JSON with all memory data",
            "jsonl": "Newline-delimited JSON (streaming-friendly)",
            "csv": "Comma-separated values",
        },
        "plaintext_split_modes": {
            "paragraph": "Split on double newlines (default)",
            "line": "One memory per line",
            "heading": "Split on markdown headings (##, ###)",
            "none": "Entire text as one memory",
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_import(format: str, data: str, split_mode: str = "paragraph") -> list[ImportedMemory]:
    """Route to the correct parser based on format."""
    if format == "chatgpt":
        return parse_chatgpt_export(data)
    elif format == "claude":
        return parse_claude_export(data)
    elif format == "json":
        return parse_json_array(data)
    elif format == "jsonl":
        return parse_jsonl(data)
    elif format == "csv":
        return parse_csv_import(data)
    elif format == "plaintext":
        return parse_plaintext(data, split_mode=split_mode)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format: {format}. Supported: {', '.join(SUPPORTED_FORMATS)}",
        )


async def _store_imported_memories(
    request: Request,
    memories: list[ImportedMemory],
    memory_service: MemoryService,
    user_id: str,
    project_id: str,
    sanitization_enabled: bool = True,
) -> ImportResponse:
    """Store a batch of parsed memories and return results."""
    imported = 0
    skipped = 0
    errors = 0
    details: list[dict[str, Any]] = []

    for i, mem in enumerate(memories):
        try:
            # Same PII + injection policy as single store (SEC-11/SEC-13).
            prepared = prepare_content(
                request.app.state,
                mem.content,
                source=f"import_{mem.source_format}",
                sanitization_enabled=sanitization_enabled,
            )
            if prepared.blocked:
                errors += 1
                details.append({"index": i, "status": "error", "reason": "PII_DETECTED"})
                continue
            req = StoreRequest(
                content=prepared.content,
                user_id=user_id,
                project_id=project_id,
                metadata={
                    **(mem.metadata or {}),
                    "import_source": mem.source_format,
                    "import_source_id": mem.source_id,
                },
            )
            result = await memory_service.store(
                req,
                source=f"import_{mem.source_format}",
                trust_score=min(_IMPORT_MAX_TRUST, prepared.trust_score),
                checksum=prepared.checksum,
            )

            if result.id:
                imported += 1
                details.append(
                    {
                        "index": i,
                        "status": "imported",
                        "memory_id": result.id,
                    }
                )
            else:
                skipped += 1
                details.append({"index": i, "status": "skipped", "reason": "duplicate"})

        except Exception as e:
            errors += 1
            log.warning("import_item_failed", index=i, error_type=type(e).__name__)
            details.append({"index": i, "status": "error", "reason": type(e).__name__})

        # Cap detail reporting at 100 entries
        if len(details) >= 100:
            break

    return ImportResponse(
        imported=imported,
        skipped=skipped,
        errors=errors,
        details=details[:100],
    )


async def _fetch_all_memories(
    memory_service: MemoryService,
    user_id: str,
    project_id: str,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch all memories for a user/project from the database."""
    db = memory_service.db
    cursor = await db.conn.execute(
        """
        SELECT id, content, user_id, project_id, extracted_facts,
               metadata, created_at, expires_at, source, trust_score
        FROM memories
        WHERE user_id = ? AND project_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, project_id, limit),
    )
    rows = await cursor.fetchall()

    import json as _json

    memories: list[dict[str, Any]] = []
    for row in rows:
        try:
            facts = _json.loads(row[4]) if row[4] else []
        except (TypeError, _json.JSONDecodeError):
            facts = []
        try:
            metadata = _json.loads(row[5]) if row[5] else {}
        except (TypeError, _json.JSONDecodeError):
            metadata = {}

        memories.append(
            scrub_memory_record(
                {
                    "id": row[0],
                    "content": row[1],
                    "user_id": row[2],
                    "project_id": row[3],
                    "extracted_facts": facts,
                    "metadata": metadata,
                    "created_at": row[6],
                    "expires_at": row[7],
                    "source": row[8],
                    "trust_score": row[9],
                }
            )
        )

    return memories
