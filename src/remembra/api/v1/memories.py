"""Memory CRUD endpoints – /api/v1/memories."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from remembra.config import Settings, get_settings
from remembra.models.memory import (
    ForgetResponse,
    RecallRequest,
    RecallResponse,
    StoreRequest,
    StoreResponse,
    UpdateRequest,
    UpdateResponse,
)
from remembra.services.memory import MemoryService

router = APIRouter(prefix="/memories", tags=["memories"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_memory_service(request: Request) -> MemoryService:
    """Dependency to get the memory service from app state."""
    return request.app.state.memory_service


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=StoreResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store a new memory",
)
async def store_memory(
    body: StoreRequest,
    memory_service: MemoryServiceDep,
) -> StoreResponse:
    """
    Accept raw text, extract facts and entities, embed, and persist.
    
    - **user_id**: Unique identifier for the user
    - **content**: The text content to memorize
    - **project_id**: Optional project namespace (default: "default")
    - **metadata**: Optional key-value metadata
    - **ttl**: Optional time-to-live (e.g., "30d", "1y")
    """
    try:
        return await memory_service.store(body)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to store memory: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


@router.post(
    "/recall",
    response_model=RecallResponse,
    summary="Retrieve memories relevant to a query",
)
async def recall_memories(
    body: RecallRequest,
    memory_service: MemoryServiceDep,
) -> RecallResponse:
    """
    Embed the query, perform semantic search, synthesise a context string.
    
    - **user_id**: User whose memories to search
    - **query**: Natural language query
    - **project_id**: Optional project namespace (default: "default")
    - **limit**: Maximum results to return (1-50, default: 5)
    - **threshold**: Minimum relevance score (0.0-1.0, default: 0.70)
    """
    try:
        return await memory_service.recall(body)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to recall memories: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Get by ID
# ---------------------------------------------------------------------------


@router.get(
    "/{memory_id}",
    summary="Get a specific memory by ID",
)
async def get_memory(
    memory_id: str,
    memory_service: MemoryServiceDep,
) -> dict:
    """Retrieve a specific memory by its ID."""
    result = await memory_service.get(memory_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory {memory_id} not found",
        )
    return result


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.patch(
    "/{memory_id}",
    response_model=UpdateResponse,
    summary="Update an existing memory",
)
async def update_memory(
    memory_id: str,
    body: UpdateRequest,
    memory_service: MemoryServiceDep,
) -> UpdateResponse:
    """
    Re-extract facts from updated content and merge entity graph.

    Full implementation arrives in Week 4.
    """
    # TODO(week-4): fetch, re-embed, merge entity diff
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Update endpoint coming in Week 4",
    )


# ---------------------------------------------------------------------------
# Forget (delete)
# ---------------------------------------------------------------------------


@router.delete(
    "",
    response_model=ForgetResponse,
    summary="Forget memories (GDPR-compliant deletion)",
)
async def forget_memories(
    memory_service: MemoryServiceDep,
    memory_id: Annotated[
        str | None, Query(description="Delete a specific memory by ID")
    ] = None,
    entity: Annotated[
        str | None, Query(description="Delete all memories about an entity")
    ] = None,
    user_id: Annotated[
        str | None, Query(description="Delete all memories for a user")
    ] = None,
) -> ForgetResponse:
    """
    Delete memories matching the given filter.

    At least one of `memory_id`, `entity`, or `user_id` is required.
    """
    if not any([memory_id, entity, user_id]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of: memory_id, entity, user_id",
        )

    try:
        return await memory_service.forget(
            memory_id=memory_id,
            user_id=user_id,
            entity=entity,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to forget memories: {str(e)}",
        )
