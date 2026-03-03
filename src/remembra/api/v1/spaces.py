"""Memory Spaces endpoints – /api/v1/spaces.

Cross-agent memory sharing: named collections that multiple agents
can read from and write to, with per-agent access control.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.models.memory import RecallResponse
from remembra.services.memory import MemoryService
from remembra.spaces.manager import SpaceManager

router = APIRouter(prefix="/spaces", tags=["spaces"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_space_manager(request: Request) -> SpaceManager:
    manager = getattr(request.app.state, "space_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory spaces are not enabled. Set REMEMBRA_ENABLE_SPACES=true to enable.",
        )
    return manager


SpaceManagerDep = Annotated[SpaceManager, Depends(get_space_manager)]


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateSpaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Space name (unique per owner)")
    description: str = Field("", max_length=1024, description="Space description")
    project_id: str = Field("default", description="Project namespace")


class CreateSpaceResponse(BaseModel):
    id: str
    name: str
    description: str
    owner_id: str
    project_id: str
    created_at: str
    members: int


class SpaceDetail(BaseModel):
    id: str
    name: str
    description: str
    owner_id: str
    project_id: str
    created_at: str
    updated_at: str | None = None
    members: int
    memory_count: int


class SpaceSummary(BaseModel):
    id: str
    name: str
    description: str
    owner_id: str
    project_id: str
    created_at: str
    permission: str


class GrantAccessRequest(BaseModel):
    agent_id: str = Field(..., description="Agent or user ID to grant access to")
    permission: str = Field(
        "read",
        description="Permission level: read, write, or admin",
    )


class GrantAccessResponse(BaseModel):
    space_id: str
    agent_id: str
    permission: str
    granted_by: str
    granted_at: str


class RevokeAccessRequest(BaseModel):
    agent_id: str = Field(..., description="Agent or user ID to revoke access from")


class MemberInfo(BaseModel):
    agent_id: str
    permission: str
    granted_by: str
    granted_at: str


class AddMemoryRequest(BaseModel):
    memory_id: str = Field(..., description="Memory ID to add to the space")


class RemoveMemoryRequest(BaseModel):
    memory_id: str = Field(..., description="Memory ID to remove from the space")


class MemorySpaceInfo(BaseModel):
    space_id: str
    space_name: str
    added_at: str


# ---------------------------------------------------------------------------
# Space CRUD
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateSpaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a memory space",
)
@limiter.limit("10/minute")
async def create_space(
    request: Request,
    body: CreateSpaceRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> CreateSpaceResponse:
    """Create a new memory space. The creator automatically gets admin access."""
    try:
        result = await space_manager.create_space(
            name=body.name,
            owner_id=current_user.user_id,
            description=body.description,
            project_id=body.project_id,
        )
        return CreateSpaceResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.get(
    "",
    response_model=list[SpaceSummary],
    summary="List your memory spaces",
)
@limiter.limit("30/minute")
async def list_spaces(
    request: Request,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> list[SpaceSummary]:
    """List all memory spaces you have access to."""
    rows = await space_manager.list_spaces(current_user.user_id)
    return [SpaceSummary(**row) for row in rows]


@router.get(
    "/{space_id}",
    response_model=SpaceDetail,
    summary="Get space details",
)
@limiter.limit("30/minute")
async def get_space(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> SpaceDetail:
    """Get detailed information about a memory space."""
    # Verify the user has at least read access
    if not await space_manager.check_access(space_id, current_user.user_id, "read"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Space not found",
        )

    result = await space_manager.get_space(space_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Space not found",
        )
    return SpaceDetail(**result)


@router.delete(
    "/{space_id}",
    summary="Delete a memory space",
)
@limiter.limit("5/minute")
async def delete_space(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Delete a memory space (requires admin access)."""
    deleted = await space_manager.delete_space(space_id, current_user.user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to delete a space",
        )
    return {"deleted": True, "space_id": space_id}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@router.post(
    "/{space_id}/access",
    response_model=GrantAccessResponse,
    summary="Grant access to a space",
)
@limiter.limit("20/minute")
async def grant_access(
    request: Request,
    space_id: str,
    body: GrantAccessRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> GrantAccessResponse:
    """Grant or update an agent's access to a memory space (requires admin)."""
    try:
        result = await space_manager.grant_access(
            space_id=space_id,
            agent_id=body.agent_id,
            permission=body.permission,
            granted_by=current_user.user_id,
        )
        return GrantAccessResponse(**result)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete(
    "/{space_id}/access",
    summary="Revoke access from a space",
)
@limiter.limit("20/minute")
async def revoke_access(
    request: Request,
    space_id: str,
    body: RevokeAccessRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Revoke an agent's access to a memory space (requires admin)."""
    try:
        revoked = await space_manager.revoke_access(
            space_id=space_id,
            agent_id=body.agent_id,
            revoked_by=current_user.user_id,
        )
        if not revoked:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Access grant not found",
            )
        return {"revoked": True, "space_id": space_id, "agent_id": body.agent_id}
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get(
    "/{space_id}/members",
    response_model=list[MemberInfo],
    summary="List space members",
)
@limiter.limit("30/minute")
async def list_members(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> list[MemberInfo]:
    """List all agents/users with access to a space."""
    if not await space_manager.check_access(space_id, current_user.user_id, "read"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Space not found",
        )
    rows = await space_manager.list_members(space_id)
    return [MemberInfo(**row) for row in rows]


# ---------------------------------------------------------------------------
# Memory membership
# ---------------------------------------------------------------------------


@router.post(
    "/{space_id}/memories",
    summary="Add a memory to a space",
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("30/minute")
async def add_memory_to_space(
    request: Request,
    space_id: str,
    body: AddMemoryRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Add an existing memory to a space (requires write access)."""
    try:
        added = await space_manager.add_memory_to_space(
            memory_id=body.memory_id,
            space_id=space_id,
            added_by=current_user.user_id,
        )
        if not added:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to add memory to space",
            )
        return {
            "added": True,
            "memory_id": body.memory_id,
            "space_id": space_id,
        }
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.delete(
    "/{space_id}/memories",
    summary="Remove a memory from a space",
)
@limiter.limit("30/minute")
async def remove_memory_from_space(
    request: Request,
    space_id: str,
    body: RemoveMemoryRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Remove a memory from a space (requires write access)."""
    try:
        removed = await space_manager.remove_memory_from_space(
            memory_id=body.memory_id,
            space_id=space_id,
            removed_by=current_user.user_id,
        )
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Memory not found in this space",
            )
        return {
            "removed": True,
            "memory_id": body.memory_id,
            "space_id": space_id,
        }
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get(
    "/{space_id}/memories",
    summary="List memories in a space",
)
@limiter.limit("30/minute")
async def list_space_memories(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
    limit: int = Query(100, ge=1, le=1000, description="Maximum memory IDs to return"),
) -> dict[str, Any]:
    """List all memory IDs in a space (requires read access)."""
    if not await space_manager.check_access(space_id, current_user.user_id, "read"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Space not found",
        )
    memory_ids = await space_manager.get_space_memory_ids(space_id, limit=limit)
    return {
        "space_id": space_id,
        "memory_ids": memory_ids,
        "count": len(memory_ids),
    }


# ---------------------------------------------------------------------------
# Cross-space recall (search memories across all accessible spaces)
# ---------------------------------------------------------------------------


@router.get(
    "/memory/{memory_id}/spaces",
    response_model=list[MemorySpaceInfo],
    summary="Get spaces a memory belongs to",
)
@limiter.limit("30/minute")
async def get_memory_spaces(
    request: Request,
    memory_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> list[MemorySpaceInfo]:
    """Get all spaces a specific memory belongs to."""
    rows = await space_manager.get_memory_spaces(memory_id)
    # Filter to spaces the user has access to
    result = []
    for row in rows:
        if await space_manager.check_access(row["space_id"], current_user.user_id, "read"):
            result.append(MemorySpaceInfo(**row))
    return result


class CrossSpaceRecallRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural language query")
    project_id: str = Field("default", description="Project namespace")
    limit: int = Field(10, ge=1, le=50, description="Maximum results")
    threshold: float = Field(0.4, ge=0.0, le=1.0, description="Minimum relevance score")
    max_tokens: int | None = Field(None, ge=100, le=100000, description="Max context tokens")


@router.post(
    "/recall",
    response_model=RecallResponse,
    summary="Recall memories across all accessible spaces",
)
@limiter.limit("30/minute")
async def recall_across_spaces(
    request: Request,
    body: CrossSpaceRecallRequest,
    space_manager: SpaceManagerDep,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> RecallResponse:
    """Search memories across all spaces the current user/agent has access to.

    This is the key endpoint for cross-agent knowledge sharing. It combines:
    - The agent's own memories (standard recall)
    - Memories from all spaces the agent has read access to

    Results are merged and ranked by relevance.
    """
    return await memory_service.recall_across_spaces(
        query=body.query,
        agent_id=current_user.user_id,
        project_id=body.project_id,
        limit=body.limit,
        threshold=body.threshold,
        max_tokens=body.max_tokens,
    )
