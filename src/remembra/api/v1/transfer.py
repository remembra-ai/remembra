"""Memory import/export endpoints – /api/v1/transfer."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
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
from remembra.services.memory import MemoryService

router = APIRouter(prefix="/transfer", tags=["import/export"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


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
    project_id: str = Field("default", description="Target project")
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
)
@limiter.limit("5/minute")
async def export_memories(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    format: str = Query("json", description="Export format: json, jsonl, csv"),
    project_id: str = Query("default", description="Project to export"),
    include_metadata: bool = Query(True, description="Include metadata in export"),
    limit: int = Query(10000, ge=1, le=100000),
) -> StreamingResponse:
    """Export all memories as JSON, JSONL, or CSV.

    Streaming download for large datasets.
    """
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
)
@limiter.limit("5/minute")
async def import_memories(
    request: Request,
    body: ImportRequest,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> ImportResponse:
    """Import memories from various formats.

    Supported formats: json, jsonl, csv, chatgpt, claude, plaintext.
    """
    parsed = _parse_import(body.format, body.data, body.split_mode)

    if not parsed:
        return ImportResponse(imported=0, skipped=0, errors=0, details=[])

    return await _store_imported_memories(
        memories=parsed,
        memory_service=memory_service,
        user_id=current_user.user_id,
        project_id=body.project_id,
    )


# ---------------------------------------------------------------------------
# Import (file upload)
# ---------------------------------------------------------------------------


@router.post(
    "/import/file",
    response_model=ImportResponse,
    summary="Import memories from file",
)
@limiter.limit("3/minute")
async def import_from_file(
    request: Request,
    file: UploadFile,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    format: str = Query(
        ...,
        description=f"Source format: {', '.join(SUPPORTED_FORMATS)}",
    ),
    project_id: str = Query("default"),
    split_mode: str = Query("paragraph"),
) -> ImportResponse:
    """Import memories from an uploaded file."""
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

    return await _store_imported_memories(
        memories=parsed,
        memory_service=memory_service,
        user_id=current_user.user_id,
        project_id=project_id,
    )


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


def _parse_import(
    format: str, data: str, split_mode: str = "paragraph"
) -> list[ImportedMemory]:
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
    memories: list[ImportedMemory],
    memory_service: MemoryService,
    user_id: str,
    project_id: str,
) -> ImportResponse:
    """Store a batch of parsed memories and return results."""
    imported = 0
    skipped = 0
    errors = 0
    details: list[dict[str, Any]] = []

    for i, mem in enumerate(memories):
        try:
            req = StoreRequest(
                content=mem.content,
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
                trust_score=0.8,  # Slightly lower trust for imports
            )

            if result.id:
                imported += 1
                details.append({
                    "index": i,
                    "status": "imported",
                    "memory_id": result.id,
                })
            else:
                skipped += 1
                details.append({"index": i, "status": "skipped", "reason": "duplicate"})

        except Exception as e:
            errors += 1
            details.append({"index": i, "status": "error", "reason": str(e)})

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

        memories.append({
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
        })

    return memories
