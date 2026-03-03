"""Conflict management endpoints – /api/v1/conflicts."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.extraction.conflicts import ConflictManager, ConflictStatus

router = APIRouter(prefix="/conflicts", tags=["conflicts"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_conflict_manager(request: Request) -> ConflictManager:
    manager = getattr(request.app.state, "conflict_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conflict resolution is not enabled on this instance.",
        )
    return manager


ConflictManagerDep = Annotated[ConflictManager, Depends(get_conflict_manager)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ConflictListResponse(BaseModel):
    conflicts: list[dict[str, Any]]
    total: int


class ConflictStatsResponse(BaseModel):
    total: int
    open: int
    resolved: int
    dismissed: int
    by_strategy: dict[str, int] = Field(default_factory=dict)


class ResolveRequest(BaseModel):
    resolved_memory_id: str | None = Field(
        None,
        description="Memory ID that represents the resolved state (optional)",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ConflictListResponse,
    summary="List memory conflicts",
)
@limiter.limit("30/minute")
async def list_conflicts(
    request: Request,
    manager: ConflictManagerDep,
    current_user: CurrentUser,
    project_id: str | None = Query(None, description="Filter by project"),
    conflict_status: str | None = Query(
        None,
        alias="status",
        description="Filter by status: open, resolved, dismissed",
    ),
    limit: int = Query(50, ge=1, le=200),
) -> ConflictListResponse:
    """List memory conflicts for the current user."""
    status_filter = None
    if conflict_status:
        try:
            status_filter = ConflictStatus(conflict_status)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {conflict_status}. Use: open, resolved, dismissed",
            )

    conflicts = await manager.list_conflicts(
        user_id=current_user.user_id,
        project_id=project_id,
        status=status_filter,
        limit=limit,
    )
    return ConflictListResponse(conflicts=conflicts, total=len(conflicts))


@router.get(
    "/stats",
    response_model=ConflictStatsResponse,
    summary="Get conflict statistics",
)
@limiter.limit("30/minute")
async def conflict_stats(
    request: Request,
    manager: ConflictManagerDep,
    current_user: CurrentUser,
) -> ConflictStatsResponse:
    """Get summary statistics of memory conflicts."""
    stats = await manager.get_stats(current_user.user_id)
    return ConflictStatsResponse(**stats)


@router.get(
    "/{conflict_id}",
    summary="Get conflict details",
)
@limiter.limit("30/minute")
async def get_conflict(
    request: Request,
    conflict_id: str,
    manager: ConflictManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Get details of a specific conflict."""
    result = await manager.get_conflict(conflict_id, current_user.user_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conflict {conflict_id} not found",
        )
    return result


@router.post(
    "/{conflict_id}/resolve",
    summary="Resolve a conflict",
)
@limiter.limit("10/minute")
async def resolve_conflict(
    request: Request,
    conflict_id: str,
    body: ResolveRequest,
    manager: ConflictManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Mark a conflict as resolved."""
    result = await manager.resolve(
        conflict_id=conflict_id,
        user_id=current_user.user_id,
        resolved_memory_id=body.resolved_memory_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conflict {conflict_id} not found",
        )
    return result


@router.post(
    "/{conflict_id}/dismiss",
    summary="Dismiss a conflict",
)
@limiter.limit("10/minute")
async def dismiss_conflict(
    request: Request,
    conflict_id: str,
    manager: ConflictManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Dismiss a conflict as not needing resolution."""
    result = await manager.dismiss(
        conflict_id=conflict_id,
        user_id=current_user.user_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conflict {conflict_id} not found",
        )
    return result
