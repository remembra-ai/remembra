"""Entity endpoints - /api/v1/entities."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from remembra.auth.middleware import CurrentUser, resolve_project_access
from remembra.core.limiter import limiter
from remembra.storage.database import Database

router = APIRouter(prefix="/entities", tags=["entities"])


def get_database(request: Request) -> Database:
    """Dependency to get the database from app state."""
    return request.app.state.db


DatabaseDep = Annotated[Database, Depends(get_database)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class EntityResponse(BaseModel):
    """Single entity response."""

    id: str
    canonical_name: str
    type: str
    aliases: list[str] = []
    attributes: dict[str, Any] = {}
    confidence: float = 1.0
    memory_count: int = 0  # Number of memories linked to this entity


class RelationshipResponse(BaseModel):
    """Relationship between entities with temporal validity."""

    id: str
    from_entity_id: str
    from_entity_name: str
    to_entity_id: str
    to_entity_name: str
    type: str
    confidence: float = 1.0
    # Temporal validity (bi-temporal edges)
    valid_from: str | None = None  # When relationship became true
    valid_to: str | None = None  # When relationship ended (None = still valid)
    is_current: bool = True  # Whether relationship is currently valid
    superseded_by: str | None = None  # ID of relationship that supersedes this


class EntitiesListResponse(BaseModel):
    """Response for listing entities."""

    entities: list[EntityResponse]
    total: int
    by_type: dict[str, int]  # Count by entity type


class RelationshipsListResponse(BaseModel):
    """Response for listing relationships."""

    relationships: list[RelationshipResponse]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=EntitiesListResponse,
    summary="List all entities",
)
@limiter.limit("60/minute")
async def list_entities(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None, description="Filter by project (omit to see all projects)"),
    entity_type: str | None = Query(default=None, description="Filter by type (person, company, location, concept)"),
    limit: int = Query(default=100, ge=1, le=500),
) -> EntitiesListResponse:
    """
    List all entities for the authenticated user.

    Entities are automatically extracted from stored memories and include:
    - People (person)
    - Organizations/Companies (company, organization)
    - Locations/Places (location, place)
    - Concepts (concept)
    """
    project_id = resolve_project_access(current_user, project_id)

    # Get entities from database
    if entity_type:
        entities = await db.get_entities_by_type(
            user_id=current_user.user_id,
            project_id=project_id,
            entity_type=entity_type,
        )
    else:
        entities = await db.get_user_entities(
            user_id=current_user.user_id,
            project_id=project_id,
        )

    # Limit results
    entities = entities[:limit]

    # Get memory counts for each entity
    entity_responses = []
    type_counts: dict[str, int] = {}

    for entity in entities:
        # Count linked memories
        memory_ids = await db.get_memories_by_entity(
            entity.id,
            user_id=current_user.user_id,
            project_id=project_id,
        )

        entity_responses.append(
            EntityResponse(
                id=entity.id,
                canonical_name=entity.canonical_name,
                type=entity.type,
                aliases=entity.aliases,
                attributes=entity.attributes,
                confidence=entity.confidence,
                memory_count=len(memory_ids),
            )
        )

        # Track type counts
        t = entity.type.lower()
        type_counts[t] = type_counts.get(t, 0) + 1

    return EntitiesListResponse(
        entities=entity_responses,
        total=len(entity_responses),
        by_type=type_counts,
    )


@router.get(
    "/relationship-search",
    response_model=RelationshipsListResponse,
    summary="Search relationships by entity name",
)
@limiter.limit("60/minute")
async def search_relationships_by_name(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    entity_name: str = Query(..., description="Entity name to search for"),
    relationship_type: str | None = Query(default=None, description="Filter by type (WORKS_AT, SPOUSE_OF, etc)"),
    as_of: str | None = Query(default=None, description="Point-in-time query (ISO format)"),
    include_history: bool = Query(default=False, description="Include superseded relationships"),
    project_id: str | None = Query(default=None),
) -> RelationshipsListResponse:
    """
    Search relationships by entity name with temporal filtering.

    This is the main endpoint for temporal relationship queries like:
    - "Where did Alice work in January 2022?"
    - "Who was Bob married to in 2019?"

    The bi-temporal model tracks both when we learned about a relationship
    (created_at) and when it was actually true (valid_from/valid_to).
    """
    from datetime import datetime
    import structlog
    log = structlog.get_logger(__name__)
    log.info("search_relationships_by_name_called", entity_name=entity_name, user_id=current_user.user_id)

    project_id = resolve_project_access(current_user, project_id)

    # Parse as_of date if provided
    as_of_dt = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format for as_of: {as_of}. Use ISO format (e.g., 2022-01-15)",
            )

    # Find entity by name - if no project_id, search across all user's projects
    if project_id:
        entity = await db.find_entity_by_name(
            name=entity_name,
            user_id=current_user.user_id,
            project_id=project_id,
        )
    else:
        # Search across all projects for this user
        entity = await db.find_entity_by_name_any_project(
            name=entity_name,
            user_id=current_user.user_id,
        )

    if not entity:
        # Return empty list if entity not found (don't error)
        return RelationshipsListResponse(relationships=[], total=0)

    # Get relationships with temporal filtering
    relationships = await db.get_entity_relationships(
        entity.id,
        as_of=as_of_dt,
        include_superseded=include_history,
    )

    # Filter by relationship type if specified
    if relationship_type:
        relationships = [r for r in relationships if r.type.upper() == relationship_type.upper()]

    # Enrich with entity names
    relationship_responses = []
    for rel in relationships:
        from_entity = await db.get_entity(rel.from_entity_id)
        to_entity = await db.get_entity(rel.to_entity_id)

        relationship_responses.append(
            RelationshipResponse(
                id=rel.id,
                from_entity_id=rel.from_entity_id,
                from_entity_name=from_entity.canonical_name if from_entity else "Unknown",
                to_entity_id=rel.to_entity_id,
                to_entity_name=to_entity.canonical_name if to_entity else "Unknown",
                type=rel.type,
                confidence=rel.confidence,
                valid_from=rel.valid_from.isoformat() if rel.valid_from else None,
                valid_to=rel.valid_to.isoformat() if rel.valid_to else None,
                is_current=rel.is_current,
                superseded_by=rel.superseded_by,
            )
        )

    return RelationshipsListResponse(
        relationships=relationship_responses,
        total=len(relationship_responses),
    )


@router.get(
    "/{entity_id}",
    response_model=EntityResponse,
    summary="Get entity by ID",
)
@limiter.limit("60/minute")
async def get_entity(
    request: Request,
    entity_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None, description="Filter to one project for restricted keys"),
) -> EntityResponse:
    """Get a specific entity by ID."""
    project_id = resolve_project_access(current_user, project_id)
    entity = await db.get_entity(entity_id, user_id=current_user.user_id, project_id=project_id)

    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity {entity_id} not found",
        )

    # Get linked memories
    memory_ids = await db.get_memories_by_entity(
        entity_id,
        user_id=current_user.user_id,
        project_id=project_id,
    )

    return EntityResponse(
        id=entity.id,
        canonical_name=entity.canonical_name,
        type=entity.type,
        aliases=entity.aliases,
        attributes=entity.attributes,
        confidence=entity.confidence,
        memory_count=len(memory_ids),
    )

@router.get(
    "/{entity_id}/relationships",
    response_model=RelationshipsListResponse,
    summary="Get relationships for an entity",
)
@limiter.limit("60/minute")
async def get_entity_relationships(
    request: Request,
    entity_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None, description="Filter to one project for restricted keys"),
    as_of: str | None = Query(
        default=None,
        description="Point-in-time query (ISO format, e.g., '2022-01-15'). Returns relationships valid at this time.",
    ),
    include_history: bool = Query(
        default=False,
        description="Include superseded/historical relationships",
    ),
) -> RelationshipsListResponse:
    """
    Get relationships involving a specific entity with temporal filtering.

    Supports point-in-time queries like "Where did Alice work in January 2022?"

    - **as_of**: Query relationships as they were at a specific date
    - **include_history**: Include relationships that have been superseded
    """
    from datetime import datetime

    project_id = resolve_project_access(current_user, project_id)

    # Verify entity exists
    entity = await db.get_entity(entity_id, user_id=current_user.user_id, project_id=project_id)
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity {entity_id} not found",
        )

    # Parse as_of date if provided
    as_of_dt = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format for as_of: {as_of}. Use ISO format (e.g., 2022-01-15)",
            )

    # Get relationships with temporal filtering
    relationships = await db.get_entity_relationships(
        entity_id,
        as_of=as_of_dt,
        include_superseded=include_history,
    )

    # Enrich with entity names
    relationship_responses = []
    for rel in relationships:
        from_entity = await db.get_entity(rel.from_entity_id)
        to_entity = await db.get_entity(rel.to_entity_id)

        relationship_responses.append(
            RelationshipResponse(
                id=rel.id,
                from_entity_id=rel.from_entity_id,
                from_entity_name=from_entity.canonical_name if from_entity else "Unknown",
                to_entity_id=rel.to_entity_id,
                to_entity_name=to_entity.canonical_name if to_entity else "Unknown",
                type=rel.type,
                confidence=rel.confidence,
                valid_from=rel.valid_from.isoformat() if rel.valid_from else None,
                valid_to=rel.valid_to.isoformat() if rel.valid_to else None,
                is_current=rel.is_current,
                superseded_by=rel.superseded_by,
            )
        )

    return RelationshipsListResponse(
        relationships=relationship_responses,
        total=len(relationship_responses),
    )


@router.get(
    "/{entity_id}/memories",
    summary="Get memories linked to an entity",
)
@limiter.limit("60/minute")
async def get_entity_memories(
    request: Request,
    entity_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None, description="Filter to one project for restricted keys"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Get all memories that mention or are linked to a specific entity."""
    project_id = resolve_project_access(current_user, project_id)
    # Verify entity exists
    entity = await db.get_entity(entity_id, user_id=current_user.user_id, project_id=project_id)
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity {entity_id} not found",
        )

    # Get linked memory IDs
    memory_ids = await db.get_memories_by_entity(
        entity_id,
        user_id=current_user.user_id,
        project_id=project_id,
    )

    # Get memory details
    memories = []
    for mid in memory_ids[:limit]:
        memory = await db.get_memory(mid)
        if memory:
            memories.append(
                {
                    "id": memory["id"],
                    "content": memory["content"],
                    "created_at": memory["created_at"],
                }
            )

    return {
        "entity_id": entity_id,
        "entity_name": entity.canonical_name,
        "memories": memories,
        "total": len(memory_ids),
    }
