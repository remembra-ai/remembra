"""Temporal endpoints - TTL, decay, and cleanup operations."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.services.memory import MemoryService
from remembra.temporal.cleanup import TemporalCleanupJob
from remembra.temporal.decay import (
    DecayConfig,
    calculate_memory_decay_info,
)

router = APIRouter(prefix="/temporal", tags=["temporal"])


def get_memory_service(request: Request) -> MemoryService:
    """Dependency to get the memory service from app state."""
    return request.app.state.memory_service


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
        
        memory_reports.append(MemoryDecayInfo(
            id=memory["id"],
            content_preview=memory.get("content", "")[:100] + "..." if len(memory.get("content", "")) > 100 else memory.get("content", ""),
            relevance_score=decay_info["relevance_score"],
            stability=decay_info["stability"],
            days_since_access=decay_info["days_since_access"],
            access_count=decay_info["access_count"],
            should_prune=decay_info["should_prune"],
            ttl_remaining_seconds=decay_info["ttl_remaining_seconds"],
            is_expired=decay_info["is_expired"],
        ))
        
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
    
    start_time = datetime.utcnow()
    
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
    
    duration_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
    
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
