"""Temporal endpoints - TTL, decay, archive, and adaptive threshold operations."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.core.time import utcnow
from remembra.services.memory import MemoryService
from remembra.temporal.adaptive import (
    SessionMode,
    create_adaptive_manager,
)
from remembra.temporal.cleanup import TemporalCleanupJob
from remembra.temporal.decay import (
    DecayConfig,
    calculate_memory_decay_info,
)

router = APIRouter(prefix="/temporal", tags=["temporal"])


def get_memory_service(request: Request) -> MemoryService:
    """Dependency to get the memory service from app state."""
    service: MemoryService = request.app.state.memory_service
    return service


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DecayReportRequest(BaseModel):
    """Request for decay report."""

    project_id: str = "default"
    limit: int = Field(default=50, ge=1, le=200)


class MemoryDecayInfo(BaseModel):
    """Decay information for a single memory."""

    id: str
    content_preview: str
    relevance_score: float
    stability: float
    days_since_access: float
    access_count: int
    should_prune: bool
    ttl_remaining_seconds: float | None = None
    is_expired: bool = False


class DecayReportResponse(BaseModel):
    """Response with decay report for user's memories."""

    user_id: str
    project_id: str
    total_memories: int
    prune_candidates: int
    average_relevance: float
    config: dict[str, Any]
    memories: list[MemoryDecayInfo]


class CleanupResponse(BaseModel):
    """Response from cleanup operation."""

    dry_run: bool
    expired_found: int
    expired_deleted: int
    decayed_found: int
    decayed_pruned: int
    decayed_archived: int
    duration_ms: int
    errors: list[str] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/decay/report",
    response_model=DecayReportResponse,
    summary="Get decay report for memories",
)
@limiter.limit("10/minute")
async def get_decay_report(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = Query(default="default"),
    limit: int = Query(default=50, ge=1, le=200),
) -> DecayReportResponse:
    """
    Get a decay report showing relevance scores for all memories.

    Shows which memories are close to being pruned based on:
    - Time since last access
    - Access count (frequency)
    - Importance score
    - TTL expiration

    Use this to understand memory health and identify stale data.
    """
    db = memory_service.db
    config = DecayConfig()

    # Get memories with decay info
    memories = await db.get_memories_with_decay_info(
        user_id=current_user.user_id,
        project_id=project_id,
        limit=limit,
    )

    memory_reports = []
    prune_candidates = 0
    total_relevance = 0.0

    for memory in memories:
        decay_info = calculate_memory_decay_info(memory, config)

        memory_reports.append(
            MemoryDecayInfo(
                id=memory["id"],
                content_preview=(
                    memory.get("content", "")[:100] + "..." if len(memory.get("content", "")) > 100 else memory.get("content", "")
                ),
                relevance_score=decay_info["relevance_score"],
                stability=decay_info["stability"],
                days_since_access=decay_info["days_since_access"],
                access_count=decay_info["access_count"],
                should_prune=decay_info["should_prune"],
                ttl_remaining_seconds=decay_info["ttl_remaining_seconds"],
                is_expired=decay_info["is_expired"],
            )
        )

        total_relevance += decay_info["relevance_score"]
        if decay_info["should_prune"]:
            prune_candidates += 1

    # Sort by relevance (lowest first to show most at-risk memories)
    memory_reports.sort(key=lambda m: m.relevance_score)

    total = len(memories)
    avg_relevance = total_relevance / total if total > 0 else 0.0

    return DecayReportResponse(
        user_id=current_user.user_id,
        project_id=project_id,
        total_memories=total,
        prune_candidates=prune_candidates,
        average_relevance=round(avg_relevance, 4),
        config={
            "prune_threshold": config.prune_threshold,
            "base_decay_rate": config.base_decay_rate,
            "newness_grace_days": config.newness_grace_days,
        },
        memories=memory_reports,
    )


@router.post(
    "/cleanup",
    response_model=CleanupResponse,
    summary="Run memory cleanup",
)
@limiter.limit("5/minute")
async def run_cleanup(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = Query(default="default"),
    dry_run: bool = Query(default=True, description="If true, don't actually delete"),
    include_decayed: bool = Query(default=False, description="Also clean up decayed memories"),
) -> CleanupResponse:
    """
    Run cleanup to remove expired and optionally decayed memories.

    - **dry_run**: If true (default), shows what would be deleted without deleting
    - **include_decayed**: If true, also removes memories below decay threshold

    ⚠️ WARNING: Setting dry_run=false will permanently delete memories!
    """

    start_time = utcnow()

    # Create cleanup job
    cleanup = TemporalCleanupJob(
        database=memory_service.db,
        qdrant_store=memory_service.qdrant,
        auto_delete_expired=True,
        auto_prune_decayed=include_decayed,
        prune_to_archive=True,  # Archive instead of hard delete
    )

    # Run cleanup
    result = await cleanup.run_cleanup(
        user_id=current_user.user_id,
        project_id=project_id,
        dry_run=dry_run,
    )

    duration_ms = int((utcnow() - start_time).total_seconds() * 1000)

    return CleanupResponse(
        dry_run=dry_run,
        expired_found=result.get("expired_found", 0),
        expired_deleted=result.get("expired_deleted", 0),
        decayed_found=result.get("decayed_found", 0),
        decayed_pruned=result.get("decayed_pruned", 0),
        decayed_archived=result.get("decayed_archived", 0),
        duration_ms=duration_ms,
        errors=result.get("errors", []),
    )


@router.get(
    "/memory/{memory_id}/decay",
    response_model=MemoryDecayInfo,
    summary="Get decay info for specific memory",
)
@limiter.limit("60/minute")
async def get_memory_decay(
    request: Request,
    memory_id: str,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> MemoryDecayInfo:
    """
    Get detailed decay information for a specific memory.

    Shows:
    - Current relevance score
    - Memory stability (based on access patterns)
    - Days since last access
    - Whether it's a prune candidate
    - TTL remaining (if set)
    """
    db = memory_service.db

    memory = await db.get_memory_with_decay(memory_id)

    if not memory:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )

    decay_info = calculate_memory_decay_info(memory, DecayConfig())

    return MemoryDecayInfo(
        id=memory_id,
        content_preview=memory.get("content", "")[:100] + "...",
        relevance_score=decay_info["relevance_score"],
        stability=decay_info["stability"],
        days_since_access=decay_info["days_since_access"],
        access_count=decay_info["access_count"],
        should_prune=decay_info["should_prune"],
        ttl_remaining_seconds=decay_info["ttl_remaining_seconds"],
        is_expired=decay_info["is_expired"],
    )


# ---------------------------------------------------------------------------
# Archive Models
# ---------------------------------------------------------------------------


class ArchivedMemory(BaseModel):
    """Archived memory from cold storage."""

    id: str
    content: str
    created_at: str
    archived_at: str
    archive_reason: str
    final_relevance_score: float | None = None
    restore_count: int = 0
    metadata: dict[str, Any] | None = None


class ArchiveListResponse(BaseModel):
    """Response with list of archived memories."""

    user_id: str
    project_id: str
    total: int
    memories: list[ArchivedMemory]


class ArchiveStatsResponse(BaseModel):
    """Statistics about archived memories."""

    total_archived: int
    reason_types: int
    avg_final_relevance: float | None
    oldest_archive: str | None
    newest_archive: str | None
    total_restores: int
    by_reason: dict[str, int]


class AdaptiveThresholdResponse(BaseModel):
    """Current adaptive threshold information."""

    user_id: str
    project_id: str
    mode: str
    is_warmup: bool
    queries_count: int
    current_threshold: float
    base_threshold: float
    avg_quality: float
    session_duration_minutes: float


# ---------------------------------------------------------------------------
# Archive Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/archive",
    response_model=ArchiveListResponse,
    summary="List archived memories",
)
@limiter.limit("30/minute")
async def list_archived_memories(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = Query(default="default"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    reason: str | None = Query(default=None, description="Filter by archive reason"),
) -> ArchiveListResponse:
    """
    List memories in cold archive storage.

    Archived memories are those that decayed below threshold but were
    preserved instead of deleted. They can be restored if needed.
    """
    db = memory_service.db

    archived = await db.get_archived_memories(
        user_id=current_user.user_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
        reason=reason,
    )

    memories = [
        ArchivedMemory(
            id=m["id"],
            content=m["content"],
            created_at=m["created_at"],
            archived_at=m["archived_at"],
            archive_reason=m.get("archive_reason", "unknown"),
            final_relevance_score=m.get("final_relevance_score"),
            restore_count=m.get("restore_count", 0),
            metadata=m.get("metadata"),
        )
        for m in archived
    ]

    return ArchiveListResponse(
        user_id=current_user.user_id,
        project_id=project_id,
        total=len(memories),
        memories=memories,
    )


@router.get(
    "/archive/stats",
    response_model=ArchiveStatsResponse,
    summary="Get archive statistics",
)
@limiter.limit("30/minute")
async def get_archive_stats(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = Query(default="default"),
) -> ArchiveStatsResponse:
    """
    Get statistics about archived memories.

    Shows total counts, breakdown by archive reason, and restore activity.
    """
    db = memory_service.db

    stats = await db.get_archive_stats(
        user_id=current_user.user_id,
        project_id=project_id,
    )

    return ArchiveStatsResponse(
        total_archived=stats.get("total_archived", 0) or 0,
        reason_types=stats.get("reason_types", 0) or 0,
        avg_final_relevance=stats.get("avg_final_relevance"),
        oldest_archive=stats.get("oldest_archive"),
        newest_archive=stats.get("newest_archive"),
        total_restores=stats.get("total_restores", 0) or 0,
        by_reason=stats.get("by_reason", {}),
    )


@router.get(
    "/archive/{memory_id}",
    response_model=ArchivedMemory,
    summary="Get archived memory",
)
@limiter.limit("60/minute")
async def get_archived_memory(
    request: Request,
    memory_id: str,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> ArchivedMemory:
    """Get a specific archived memory by ID."""
    db = memory_service.db

    memory = await db.get_archived_memory(memory_id)

    if not memory:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Archived memory {memory_id} not found",
        )

    # Verify ownership
    if memory.get("user_id") != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    return ArchivedMemory(
        id=memory["id"],
        content=memory["content"],
        created_at=memory["created_at"],
        archived_at=memory["archived_at"],
        archive_reason=memory.get("archive_reason", "unknown"),
        final_relevance_score=memory.get("final_relevance_score"),
        restore_count=memory.get("restore_count", 0),
        metadata=memory.get("metadata"),
    )


@router.post(
    "/archive/{memory_id}/restore",
    summary="Restore memory from archive",
)
@limiter.limit("10/minute")
async def restore_memory(
    request: Request,
    memory_id: str,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """
    Restore a memory from cold archive back to active storage.

    The memory will be re-indexed and available for semantic search again.
    """
    db = memory_service.db

    # Check if memory exists in archive
    memory = await db.get_archived_memory(memory_id)
    if not memory:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Archived memory {memory_id} not found",
        )

    # Verify ownership
    if memory.get("user_id") != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Restore the memory
    success = await db.restore_memory(memory_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restore memory",
        )

    # Re-index in vector store
    if memory_service.qdrant and memory.get("content"):
        try:
            embedding = await memory_service._get_embedding(memory["content"])
            await memory_service.qdrant.upsert(
                memory_id=memory_id,
                embedding=embedding,
                metadata={
                    "user_id": memory["user_id"],
                    "project_id": memory.get("project_id", "default"),
                },
            )
        except Exception:
            # Log but don't fail - memory is restored, just not searchable yet
            pass

    # Re-index in FTS
    try:
        await db.index_memory_fts(
            memory_id=memory_id,
            content=memory["content"],
            user_id=memory["user_id"],
            project_id=memory.get("project_id", "default"),
        )
    except Exception:
        pass

    return {
        "status": "restored",
        "memory_id": memory_id,
        "message": "Memory restored to active storage",
    }


@router.get(
    "/archive/search",
    response_model=ArchiveListResponse,
    summary="Search archived memories",
)
@limiter.limit("20/minute")
async def search_archive(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    q: str = Query(..., description="Search query"),
    project_id: str = Query(default="default"),
    limit: int = Query(default=20, ge=1, le=100),
) -> ArchiveListResponse:
    """
    Search archived memories by keyword.

    Note: This is keyword search only, not semantic search.
    For full semantic search, restore the memory first.
    """
    db = memory_service.db

    results = await db.search_archived_memories(
        user_id=current_user.user_id,
        query=q,
        project_id=project_id,
        limit=limit,
    )

    memories = [
        ArchivedMemory(
            id=m["id"],
            content=m["content"],
            created_at=m["created_at"],
            archived_at=m["archived_at"],
            archive_reason=m.get("archive_reason", "unknown"),
            final_relevance_score=m.get("final_relevance_score"),
            restore_count=m.get("restore_count", 0),
            metadata=m.get("metadata"),
        )
        for m in results
    ]

    return ArchiveListResponse(
        user_id=current_user.user_id,
        project_id=project_id,
        total=len(memories),
        memories=memories,
    )


# ---------------------------------------------------------------------------
# Adaptive Threshold Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/adaptive/threshold",
    response_model=AdaptiveThresholdResponse,
    summary="Get current adaptive threshold",
)
@limiter.limit("60/minute")
async def get_adaptive_threshold(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = Query(default="default"),
) -> AdaptiveThresholdResponse:
    """
    Get the current adaptive prune threshold for this session.

    The threshold is dynamically adjusted based on:
    - Session mode (exploratory vs operational)
    - Query patterns and result quality
    - Memory density
    - Warm-up calibration phase
    """
    db = memory_service.db
    manager = create_adaptive_manager(db)

    stats = manager.get_session_stats(
        user_id=current_user.user_id,
        project_id=project_id,
    )

    return AdaptiveThresholdResponse(
        user_id=stats["user_id"],
        project_id=stats["project_id"],
        mode=stats["mode"],
        is_warmup=stats["is_warmup"],
        queries_count=stats["queries_count"],
        current_threshold=stats["current_threshold"],
        base_threshold=stats["base_threshold"],
        avg_quality=stats["avg_quality"],
        session_duration_minutes=stats["session_duration_minutes"],
    )


@router.post(
    "/adaptive/mode",
    summary="Set session mode",
)
@limiter.limit("10/minute")
async def set_session_mode(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    mode: str = Query(..., description="Session mode: exploratory, operational, or balanced"),
    project_id: str = Query(default="default"),
) -> dict[str, Any]:
    """
    Explicitly set the session mode for adaptive thresholds.

    Modes:
    - **exploratory**: Lower threshold, keep more memories (browsing/research)
    - **operational**: Higher threshold, prune more (focused work)
    - **balanced**: Auto-adjust based on behavior (default)
    """
    valid_modes = ["exploratory", "operational", "balanced"]
    if mode not in valid_modes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid mode. Must be one of: {valid_modes}",
        )

    db = memory_service.db
    manager = create_adaptive_manager(db)

    manager.set_mode(
        user_id=current_user.user_id,
        mode=SessionMode(mode),
        project_id=project_id,
    )

    new_threshold = manager.calculate_threshold(
        user_id=current_user.user_id,
        project_id=project_id,
    )

    return {
        "status": "mode_set",
        "mode": mode,
        "new_threshold": new_threshold,
        "message": f"Session mode set to {mode}",
    }


@router.post(
    "/adaptive/reset",
    summary="Reset adaptive session",
)
@limiter.limit("5/minute")
async def reset_adaptive_session(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = Query(default="default"),
) -> dict[str, Any]:
    """
    Reset the adaptive threshold session.

    This clears all calibration data and starts fresh with
    a new warm-up period.
    """
    db = memory_service.db
    manager = create_adaptive_manager(db)

    manager.reset_session(
        user_id=current_user.user_id,
        project_id=project_id,
    )

    return {
        "status": "reset",
        "message": "Adaptive session reset. New calibration will begin.",
    }
