"""Entity endpoints - /api/v1/entities."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
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
    """Relationship between entities."""
    id: str
    from_entity_id: str
    from_entity_name: str
    to_entity_id: str
    to_entity_name: str
    type: str
    confidence: float = 1.0


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
    project_id: str = Query(default="default"),
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
        memory_ids = await db.get_memories_by_entity(entity.id)
        
        entity_responses.append(EntityResponse(
            id=entity.id,
            canonical_name=entity.canonical_name,
            type=entity.type,
            aliases=entity.aliases,
            attributes=entity.attributes,
            confidence=entity.confidence,
            memory_count=len(memory_ids),
        ))
        
        # Track type counts
        t = entity.type.lower()
        type_counts[t] = type_counts.get(t, 0) + 1
    
    return EntitiesListResponse(
        entities=entity_responses,
        total=len(entity_responses),
        by_type=type_counts,
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
) -> EntityResponse:
    """Get a specific entity by ID."""
    entity = await db.get_entity(entity_id)
    
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity {entity_id} not found",
        )
    
    # Get linked memories
    memory_ids = await db.get_memories_by_entity(entity_id)
    
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
) -> RelationshipsListResponse:
    """Get all relationships involving a specific entity."""
    # Verify entity exists
    entity = await db.get_entity(entity_id)
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity {entity_id} not found",
        )
    
    # Get relationships
    relationships = await db.get_entity_relationships(entity_id)
    
    # Enrich with entity names
    relationship_responses = []
    for rel in relationships:
        from_entity = await db.get_entity(rel.from_entity_id)
        to_entity = await db.get_entity(rel.to_entity_id)
        
        relationship_responses.append(RelationshipResponse(
            id=rel.id,
            from_entity_id=rel.from_entity_id,
            from_entity_name=from_entity.canonical_name if from_entity else "Unknown",
            to_entity_id=rel.to_entity_id,
            to_entity_name=to_entity.canonical_name if to_entity else "Unknown",
            type=rel.type,
            confidence=rel.confidence,
        ))
    
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
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Get all memories that mention or are linked to a specific entity."""
    # Verify entity exists
    entity = await db.get_entity(entity_id)
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity {entity_id} not found",
        )
    
    # Get linked memory IDs
    memory_ids = await db.get_memories_by_entity(entity_id)
    
    # Get memory details
    memories = []
    for mid in memory_ids[:limit]:
        memory = await db.get_memory(mid)
        if memory:
            memories.append({
                "id": memory["id"],
                "content": memory["content"],
                "created_at": memory["created_at"],
            })
    
    return {
        "entity_id": entity_id,
        "entity_name": entity.canonical_name,
        "memories": memories,
        "total": len(memory_ids),
    }
