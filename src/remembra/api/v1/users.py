"""User profile endpoints - /api/v1/users."""

from collections import Counter
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, has_permission, resolve_project_access
from remembra.core.limiter import limiter
from remembra.storage.database import Database

router = APIRouter(prefix="/users", tags=["users"])


def get_database(request: Request) -> Database:
    """Dependency to get the database from app state."""
    return request.app.state.db


DatabaseDep = Annotated[Database, Depends(get_database)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class UserEntitySummary(BaseModel):
    """Summary of an entity associated with a user."""

    id: str
    name: str
    type: str
    mention_count: int = 0


class UserActivitySummary(BaseModel):
    """Summary of recent user activity."""

    last_memory_at: datetime | None = None
    last_recall_at: datetime | None = None
    memories_last_24h: int = 0
    memories_last_7d: int = 0
    memories_last_30d: int = 0


class TopTopic(BaseModel):
    """A frequently mentioned topic or theme."""

    topic: str
    count: int
    last_mentioned: datetime | None = None


class UserStaticFacts(BaseModel):
    """Static facts extracted about a user from their memories."""

    facts: list[str] = Field(default_factory=list)
    entities: list[UserEntitySummary] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value attributes about the user extracted from memories",
    )


class UserProfileResponse(BaseModel):
    """
    Aggregated user profile with facts, activity, and insights.

    This provides a comprehensive view of a user's memory footprint,
    including extracted entities, recent activity patterns, and top topics.
    """

    user_id: str
    project_id: str | None = None

    # Memory statistics
    total_memories: int = 0
    total_entities: int = 0
    total_relationships: int = 0

    # Static facts and entities
    static_facts: UserStaticFacts = Field(default_factory=UserStaticFacts)

    # Activity summary
    activity: UserActivitySummary = Field(default_factory=UserActivitySummary)

    # Top topics/themes
    top_topics: list[TopTopic] = Field(default_factory=list)

    # Account info
    created_at: datetime | None = None
    last_active: datetime | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{user_id}/profile",
    response_model=UserProfileResponse,
    summary="Get aggregated user profile",
)
@limiter.limit("30/minute")
async def get_user_profile(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(
        default=None,
        description="Filter by project (omit to aggregate across all projects)",
    ),
    include_facts: bool = Query(
        default=True,
        description="Include static facts extracted from memories",
    ),
    include_activity: bool = Query(
        default=True,
        description="Include activity summary",
    ),
    include_topics: bool = Query(
        default=True,
        description="Include top topics",
    ),
    topic_limit: int = Query(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of top topics to return",
    ),
) -> UserProfileResponse:
    """
    Get an aggregated profile for a user based on their memories.

    This endpoint aggregates:
    - **Static facts**: Entities and facts extracted from the user's memories
    - **Activity summary**: Recent memory creation patterns
    - **Top topics**: Most frequently mentioned themes/concepts
    - **Statistics**: Total counts of memories, entities, relationships

    Security: Users can only access their own profile unless they have admin permissions.

    **Use cases:**
    - Agent context: "What do I know about this user?"
    - Dashboard: User profile overview
    - Analytics: User engagement metrics
    """
    # Security: Only allow users to access their own profile
    # (or admin users to access any profile)
    if user_id != current_user.user_id:
        if not has_permission(current_user, "admin:read"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied: can only access your own profile",
            )

    project_id = resolve_project_access(current_user, project_id)

    # Get user info if available
    user_info = await db.get_user_by_id(user_id)
    created_at = None
    if user_info:
        created_at = datetime.fromisoformat(user_info["created_at"]) if user_info.get("created_at") else None

    # Get memory statistics
    total_memories = await db.count_memories(user_id=user_id, project_id=project_id)

    # Get entities for this user
    entities = await db.get_user_entities(user_id=user_id, project_id=project_id)
    total_entities = len(entities)

    # Get relationship count
    total_relationships = 0
    for entity in entities[:100]:  # Limit to avoid excessive queries
        rels = await db.get_entity_relationships(entity.id)
        total_relationships += len(rels)

    # Build static facts
    static_facts = UserStaticFacts()
    if include_facts:
        # Get entity summaries with mention counts
        entity_summaries = []
        for entity in entities[:50]:  # Limit to top 50 entities
            memory_ids = await db.get_memories_by_entity(
                entity.id,
                user_id=user_id,
                project_id=project_id,
            )
            entity_summaries.append(
                UserEntitySummary(
                    id=entity.id,
                    name=entity.canonical_name,
                    type=entity.type,
                    mention_count=len(memory_ids),
                )
            )

        # Sort by mention count
        entity_summaries.sort(key=lambda e: e.mention_count, reverse=True)
        static_facts.entities = entity_summaries[:20]  # Top 20 entities

        # Extract unique facts from recent memories
        recent_memories = await db.get_recent_memories(
            user_id=user_id,
            project_id=project_id,
            limit=100,
        )

        all_facts: list[str] = []
        for memory in recent_memories:
            facts = memory.get("extracted_facts", [])
            if isinstance(facts, str):
                try:
                    import json

                    facts = json.loads(facts)
                except (json.JSONDecodeError, TypeError):
                    facts = []
            all_facts.extend(facts)

        # Dedupe and limit facts
        seen_facts: set[str] = set()
        unique_facts: list[str] = []
        for fact in all_facts:
            if fact.lower() not in seen_facts:
                seen_facts.add(fact.lower())
                unique_facts.append(fact)

        static_facts.facts = unique_facts[:50]  # Top 50 unique facts

    # Build activity summary
    activity = UserActivitySummary()
    last_active = None
    if include_activity:
        now = datetime.utcnow()

        # Get most recent memory
        recent = await db.get_recent_memories(
            user_id=user_id,
            project_id=project_id,
            limit=1,
        )
        if recent:
            last_memory_at = recent[0].get("created_at")
            if last_memory_at:
                if isinstance(last_memory_at, str):
                    activity.last_memory_at = datetime.fromisoformat(last_memory_at.replace("Z", "+00:00"))
                else:
                    activity.last_memory_at = last_memory_at
                last_active = activity.last_memory_at

        # Count memories in time periods
        activity.memories_last_24h = await db.count_memories(
            user_id=user_id,
            project_id=project_id,
            since=now - timedelta(days=1),
        )
        activity.memories_last_7d = await db.count_memories(
            user_id=user_id,
            project_id=project_id,
            since=now - timedelta(days=7),
        )
        activity.memories_last_30d = await db.count_memories(
            user_id=user_id,
            project_id=project_id,
            since=now - timedelta(days=30),
        )

    # Build top topics
    top_topics: list[TopTopic] = []
    if include_topics:
        # Extract topics from entity types and names
        topic_counter: Counter[str] = Counter()
        topic_last_seen: dict[str, datetime] = {}

        # Use entity types as topics
        for entity in entities:
            topic = entity.type.lower()
            topic_counter[topic] += 1
            if entity.updated_at:
                if isinstance(entity.updated_at, str):
                    updated = datetime.fromisoformat(entity.updated_at.replace("Z", "+00:00"))
                else:
                    updated = entity.updated_at
                if topic not in topic_last_seen or updated > topic_last_seen[topic]:
                    topic_last_seen[topic] = updated

        # Also extract from entity names (concepts, categories)
        concept_entities = [e for e in entities if e.type.lower() in ("concept", "topic", "category")]
        for entity in concept_entities:
            topic = entity.canonical_name.lower()
            topic_counter[topic] += 1
            if entity.updated_at:
                if isinstance(entity.updated_at, str):
                    updated = datetime.fromisoformat(entity.updated_at.replace("Z", "+00:00"))
                else:
                    updated = entity.updated_at
                if topic not in topic_last_seen or updated > topic_last_seen[topic]:
                    topic_last_seen[topic] = updated

        # Build top topics list
        for topic, count in topic_counter.most_common(topic_limit):
            top_topics.append(
                TopTopic(
                    topic=topic,
                    count=count,
                    last_mentioned=topic_last_seen.get(topic),
                )
            )

    return UserProfileResponse(
        user_id=user_id,
        project_id=project_id,
        total_memories=total_memories,
        total_entities=total_entities,
        total_relationships=total_relationships,
        static_facts=static_facts,
        activity=activity,
        top_topics=top_topics,
        created_at=created_at,
        last_active=last_active,
    )


@router.get(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Get your own user profile",
)
@limiter.limit("30/minute")
async def get_my_profile(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None),
    include_facts: bool = Query(default=True),
    include_activity: bool = Query(default=True),
    include_topics: bool = Query(default=True),
    topic_limit: int = Query(default=10, ge=1, le=50),
) -> UserProfileResponse:
    """
    Convenience endpoint to get your own profile.

    Equivalent to GET /api/v1/users/{your_user_id}/profile.
    """
    return await get_user_profile(
        request=request,
        user_id=current_user.user_id,
        db=db,
        current_user=current_user,
        project_id=project_id,
        include_facts=include_facts,
        include_activity=include_activity,
        include_topics=include_topics,
        topic_limit=topic_limit,
    )
