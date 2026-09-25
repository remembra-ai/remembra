"""Memory Spaces endpoints – /api/v1/spaces.

Cross-agent memory sharing: named collections that multiple agents
can read from and write to, with per-agent access control.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from remembra.auth.middleware import (
    AuthenticatedUser,
    CurrentUser,
    require_memory_recall,
    require_memory_store,
    resolve_project_access,
    resolve_project_or_default,
)
from remembra.cloud.limits import enforce_recall_quota, record_recall_usage
from remembra.core.limiter import limiter
from remembra.models.memory import RecallResponse
from remembra.services.memory import MemoryService
from remembra.spaces.manager import SpaceManager

router = APIRouter(prefix="/spaces", tags=["spaces"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_space_manager(request: Request) -> SpaceManager:
    manager: SpaceManager | None = getattr(request.app.state, "space_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory spaces are not enabled. Set REMEMBRA_ENABLE_SPACES=true to enable.",
        )
    return manager


SpaceManagerDep = Annotated[SpaceManager, Depends(get_space_manager)]


def get_memory_service(request: Request) -> MemoryService:
    service: MemoryService = request.app.state.memory_service
    return service


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


async def _require_space_in_scope(space_manager: SpaceManager, user: AuthenticatedUser, space_id: str) -> dict[str, Any]:
    """404 unless the caller can see the space and (for restricted keys) its project."""
    if not await space_manager.check_access(space_id, user.user_id, "read"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    space = await space_manager.get_space(space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    if user.project_ids and space["project_id"] not in user.project_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this space's project")
    return space


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateSpaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Space name (unique per owner)")
    description: str = Field("", max_length=1024, description="Space description")
    project_id: str | None = Field(None, description="Project namespace (single-project keys default to their project)")

    @field_validator("name", "description")
    @classmethod
    def sanitize_html(cls, v: str) -> str:
        """Strip HTML/script tags to prevent XSS."""
        import re

        # Remove HTML tags
        clean = re.sub(r"<[^>]+>", "", v)
        # Remove script patterns
        clean = re.sub(r"javascript:", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"on\w+\s*=", "", clean, flags=re.IGNORECASE)
        return clean.strip()


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
    status: str = Field("active", description="'active', or 'pending' until the invitee accepts")


class SpaceInvite(BaseModel):
    space_id: str
    space_name: str
    permission: str
    invited_by: str
    invited_at: str


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
    dependencies=[require_memory_store()],
)
@limiter.limit("10/minute")
async def create_space(
    request: Request,
    body: CreateSpaceRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> CreateSpaceResponse:
    """Create a new memory space. The creator automatically gets admin access."""
    project_id = resolve_project_or_default(current_user, body.project_id)
    try:
        result = await space_manager.create_space(
            name=body.name,
            owner_id=current_user.user_id,
            description=body.description,
            project_id=project_id,
        )
        return CreateSpaceResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.get(
    "",
    response_model=list[SpaceSummary],
    summary="List your memory spaces",
    dependencies=[require_memory_recall()],
)
@limiter.limit("30/minute")
async def list_spaces(
    request: Request,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> list[SpaceSummary]:
    """List all memory spaces you have access to."""
    rows = await space_manager.list_spaces(current_user.user_id)
    if current_user.project_ids:
        rows = [row for row in rows if row["project_id"] in current_user.project_ids]
    return [SpaceSummary(**row) for row in rows]


@router.get(
    "/{space_id}",
    response_model=SpaceDetail,
    summary="Get space details",
    dependencies=[require_memory_recall()],
)
@limiter.limit("30/minute")
async def get_space(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> SpaceDetail:
    """Get detailed information about a memory space."""
    result = await _require_space_in_scope(space_manager, current_user, space_id)
    return SpaceDetail(**result)


@router.delete(
    "/{space_id}",
    summary="Delete a memory space",
    dependencies=[require_memory_store()],
)
@limiter.limit("5/minute")
async def delete_space(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Delete a memory space (requires admin access)."""
    await _require_space_in_scope(space_manager, current_user, space_id)
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
    dependencies=[require_memory_store()],
)
@limiter.limit("20/minute")
async def grant_access(
    request: Request,
    space_id: str,
    body: GrantAccessRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> GrantAccessResponse:
    """Update a member's access, or invite a new member (requires admin).

    New members get a pending invitation they must accept via
    ``POST /spaces/{space_id}/invite/accept``; nobody can be added to a space
    (and have its content injected into their recall) without consent.
    """
    await _require_space_in_scope(space_manager, current_user, space_id)
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
    dependencies=[require_memory_store()],
)
@limiter.limit("20/minute")
async def revoke_access(
    request: Request,
    space_id: str,
    body: RevokeAccessRequest,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Revoke an agent's access (or pending invite) to a memory space (requires admin)."""
    await _require_space_in_scope(space_manager, current_user, space_id)
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
    dependencies=[require_memory_recall()],
)
@limiter.limit("30/minute")
async def list_members(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> list[MemberInfo]:
    """List all agents/users with access to a space."""
    await _require_space_in_scope(space_manager, current_user, space_id)
    rows = await space_manager.list_members(space_id)
    return [MemberInfo(**row) for row in rows]


# ---------------------------------------------------------------------------
# Memory membership
# ---------------------------------------------------------------------------


@router.post(
    "/{space_id}/memories",
    summary="Add a memory to a space",
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_memory_store()],
)
@limiter.limit("30/minute")
async def add_memory_to_space(
    request: Request,
    space_id: str,
    body: AddMemoryRequest,
    space_manager: SpaceManagerDep,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Add one of your own memories to a space (requires write access)."""
    await _require_space_in_scope(space_manager, current_user, space_id)
    memory = await memory_service.db.get_memory(body.memory_id)
    if not memory or memory.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    if current_user.project_ids and memory.get("project_id") not in current_user.project_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this memory's project")
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
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found") from None
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.delete(
    "/{space_id}/memories",
    summary="Remove a memory from a space",
    dependencies=[require_memory_store()],
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
    await _require_space_in_scope(space_manager, current_user, space_id)
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
    dependencies=[require_memory_recall()],
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
    await _require_space_in_scope(space_manager, current_user, space_id)
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
    dependencies=[require_memory_recall()],
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
    project_id: str | None = Field(None, description="Project namespace (single-project keys default to their project)")
    limit: int = Field(10, ge=1, le=50, description="Maximum results")
    threshold: float = Field(0.4, ge=0.0, le=1.0, description="Minimum relevance score")
    max_tokens: int | None = Field(None, ge=100, le=100000, description="Max context tokens")


@router.post(
    "/recall",
    response_model=RecallResponse,
    summary="Recall memories across all accessible spaces",
    dependencies=[require_memory_recall()],
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
    - The agent's own memories (standard recall, scoped to the resolved project)
    - Memories from all spaces the agent has read access to

    Results are merged and ranked by relevance. Plan recall limits apply and
    usage is metered like ``/memories/recall``.
    """
    project_id = resolve_project_access(current_user, body.project_id) or "default"
    await enforce_recall_quota(request, current_user.user_id)

    result = await memory_service.recall_across_spaces(
        query=body.query,
        agent_id=current_user.user_id,
        project_id=project_id,
        limit=body.limit,
        threshold=body.threshold,
        max_tokens=body.max_tokens,
    )
    if current_user.project_ids:
        result = await _restrict_to_key_projects(space_manager, memory_service, current_user, result)
    await record_recall_usage(request, current_user.user_id)
    return result


async def _restrict_to_key_projects(
    space_manager: SpaceManager, memory_service: MemoryService, user: AuthenticatedUser, result: RecallResponse
) -> RecallResponse:
    """Drop space results that a project-restricted key must not see.

    A shared memory is visible to a restricted key only through a space whose
    project is in the key's allow-list (or when it is the caller's own memory
    in an allowed project).
    """
    allowed = set(user.project_ids or [])
    allowed_ids: set[str] = set()
    for space in await space_manager.list_spaces(user.user_id):
        if space["project_id"] in allowed:
            allowed_ids.update(await space_manager.get_space_memory_ids(space["id"], limit=500))

    kept = []
    for memory in result.memories:
        if memory.id in allowed_ids:
            kept.append(memory)
            continue
        row = await memory_service.db.get_memory(memory.id)
        if row and row.get("user_id") == user.user_id and row.get("project_id") in allowed:
            kept.append(memory)
    if len(kept) == len(result.memories):
        return result
    return result.model_copy(update={"memories": kept, "context": "\n\n".join(m.content for m in kept)})


# ---------------------------------------------------------------------------
# Invitations (consent for cross-user access)
# ---------------------------------------------------------------------------


@router.get(
    "/invites/pending",
    response_model=list[SpaceInvite],
    summary="List space invitations addressed to you",
    dependencies=[require_memory_recall()],
)
@limiter.limit("30/minute")
async def list_space_invites(
    request: Request,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> list[SpaceInvite]:
    return [SpaceInvite(**row) for row in await space_manager.list_invites(current_user.user_id)]


@router.post(
    "/{space_id}/invite/accept",
    response_model=GrantAccessResponse,
    summary="Accept a space invitation",
    dependencies=[require_memory_store()],
)
@limiter.limit("20/minute")
async def accept_space_invite(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> GrantAccessResponse:
    space = await space_manager.get_space(space_id)
    if space and current_user.project_ids and space["project_id"] not in current_user.project_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this space's project")
    try:
        result = await space_manager.accept_invite(space_id, current_user.user_id)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found") from None
    return GrantAccessResponse(**result)


@router.post(
    "/{space_id}/invite/decline",
    summary="Decline a space invitation",
    dependencies=[require_memory_store()],
)
@limiter.limit("20/minute")
async def decline_space_invite(
    request: Request,
    space_id: str,
    space_manager: SpaceManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    if not await space_manager.decline_invite(space_id, current_user.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    return {"declined": True, "space_id": space_id}
