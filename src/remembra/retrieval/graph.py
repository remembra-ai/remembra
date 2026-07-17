"""Graph-aware retrieval for entity relationship traversal.

This module enables finding memories by traversing the entity graph:
1. Extract entity mentions from the query
2. Match to known entities (including aliases)
3. Traverse relationships to find related entities
4. Surface all memories linked to the entity neighborhood

Performance limits are applied to prevent explosion on large graphs:
- max_entities: Cap on related entities returned
- max_memories: Cap on memory IDs collected
- Query-level caching to deduplicate rapid-fire identical queries
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import structlog

from remembra.models.memory import Entity, EntityRef, Relationship
from remembra.storage.database import Database

log = structlog.get_logger(__name__)

# Query-level cache to deduplicate identical queries within a time window
_query_cache: dict[str, tuple[float, Any]] = {}
_cache_ttl_seconds: float = 5.0  # Cache results for 5 seconds
_cache_lock = asyncio.Lock()


@dataclass
class GraphSearchResult:
    """Results from graph-aware retrieval."""

    # Memory IDs found through graph traversal
    memory_ids: set[str] = field(default_factory=set)
    # Entities that were matched in the query
    matched_entities: list[EntityRef] = field(default_factory=list)
    # Related entities found through traversal
    related_entities: list[EntityRef] = field(default_factory=list)
    # Relationship paths used (for debugging)
    traversal_paths: list[str] = field(default_factory=list)


class GraphRetriever:
    """
    Retrieves memories by traversing the entity graph.

    This enables queries like "David Kim merger" to find memories about
    "Mr. Kim" or memories about companies David Kim is related to.

    Performance limits prevent explosion on large graphs:
    - max_entities: Stop traversal when this many entities collected
    - max_memories: Stop collecting memory IDs at this limit
    """

    def __init__(
        self,
        db: Database,
        max_depth: int = 2,
        max_entities: int = 100,
        max_memories: int = 500,
    ) -> None:
        """
        Initialize the graph retriever.

        Args:
            db: Database instance for entity/relationship queries
            max_depth: Maximum relationship traversal depth (default: 2)
            max_entities: Maximum related entities to return (default: 100)
            max_memories: Maximum memory IDs to collect (default: 500)
        """
        self.db = db
        self.max_depth = max_depth
        self.max_entities = max_entities
        self.max_memories = max_memories

    async def find_entity_mentions(
        self,
        query: str,
        user_id: str,
        project_id: str = "default",
    ) -> list[Entity]:
        """
        Find all entities mentioned in the query.

        Checks canonical names and aliases for matches.

        Args:
            query: Search query text
            user_id: User ID for scoping
            project_id: Project ID for scoping

        Returns:
            List of matched Entity objects
        """
        all_entities = await self.db.get_user_entities(user_id, project_id)
        query_lower = query.lower()

        matched: list[Entity] = []
        matched_ids: set[str] = set()

        for entity in all_entities:
            if entity.id in matched_ids:
                continue

            # Check canonical name
            if entity.canonical_name.lower() in query_lower:
                matched.append(entity)
                matched_ids.add(entity.id)
                continue

            # Check aliases
            for alias in entity.aliases:
                if alias.lower() in query_lower:
                    matched.append(entity)
                    matched_ids.add(entity.id)
                    break

        return matched

    async def get_related_entities(
        self,
        entity_id: str,
        depth: int = 1,
        visited: set[str] | None = None,
        as_of: datetime | None = None,
    ) -> list[tuple[Entity, Relationship]]:
        """
        Get entities related to the given entity up to specified depth.

        Supports temporal queries - filter relationships valid at a specific point in time.
        Respects max_entities limit to prevent explosion on large graphs.

        Args:
            entity_id: Starting entity ID
            depth: Current traversal depth
            visited: Set of already-visited entity IDs
            as_of: Optional point-in-time filter. Only relationships valid at this time
                   are included. If None, only current (non-superseded) relationships.

        Returns:
            List of (Entity, Relationship) tuples
        """
        if visited is None:
            visited = set()

        # Early exit: depth exceeded or already visited
        if depth > self.max_depth or entity_id in visited:
            return []

        # Early exit: entity limit reached
        if len(visited) >= self.max_entities:
            log.debug(
                "graph_entity_limit_reached",
                limit=self.max_entities,
                visited_count=len(visited),
            )
            return []

        visited.add(entity_id)

        # Get all relationships for this entity
        relationships = await self.db.get_entity_relationships(entity_id)

        related: list[tuple[Entity, Relationship]] = []

        for rel in relationships:
            # Check entity limit before processing more
            if len(visited) + len(related) >= self.max_entities:
                log.debug(
                    "graph_entity_limit_during_traversal",
                    limit=self.max_entities,
                    collected=len(related),
                )
                break

            # Temporal filtering - check if relationship is valid
            if as_of is not None:
                # Point-in-time query
                if not rel.is_valid_at(as_of):
                    continue
            else:
                # Current relationships only (not superseded)
                if not rel.is_current:
                    continue

            # Determine the "other" entity in the relationship
            other_id = rel.to_entity_id if rel.from_entity_id == entity_id else rel.from_entity_id

            if other_id in visited:
                continue

            other_entity = await self.db.get_entity(other_id)
            if other_entity:
                related.append((other_entity, rel))

                # Recursive traversal for deeper relationships (only if under limit)
                if depth < self.max_depth and len(visited) + len(related) < self.max_entities:
                    deeper = await self.get_related_entities(other_id, depth + 1, visited, as_of)
                    related.extend(deeper)

        return related

    async def get_entity_neighborhood(
        self,
        entity: Entity,
        user_id: str,
        project_id: str = "default",
    ) -> set[str]:
        """
        Get all entity IDs in the neighborhood of an entity.

        This includes:
        - The entity itself
        - Entities with relationships to/from it
        - Entities with similar aliases (potential duplicates)

        Args:
            entity: Starting entity
            user_id: User ID for scoping
            project_id: Project ID for scoping

        Returns:
            Set of entity IDs in the neighborhood
        """
        neighborhood = {entity.id}

        # Add related entities
        related = await self.get_related_entities(entity.id)
        for related_entity, _ in related:
            neighborhood.add(related_entity.id)

        # Also check for other entities with overlapping aliases
        # This catches cases where the same person might be stored twice
        all_entities = await self.db.get_user_entities(user_id, project_id)
        entity_names = {entity.canonical_name.lower()} | {a.lower() for a in entity.aliases}

        for other in all_entities:
            if other.id in neighborhood:
                continue

            other_names = {other.canonical_name.lower()} | {a.lower() for a in other.aliases}

            # Check for name overlap
            if entity_names & other_names:
                neighborhood.add(other.id)

        return neighborhood

    async def search(
        self,
        query: str,
        user_id: str,
        project_id: str = "default",
    ) -> GraphSearchResult:
        """
        Perform graph-aware retrieval with caching and limits.

        Steps:
        1. Check query cache (deduplicate rapid-fire identical queries)
        2. Find entities mentioned in query
        3. Get neighborhood of each matched entity (limited)
        4. Collect memories linked to neighborhood (limited)

        Performance features:
        - Query-level caching (5 second TTL)
        - Entity count limit (max_entities)
        - Memory ID limit (max_memories)

        Args:
            query: Search query
            user_id: User ID
            project_id: Project ID

        Returns:
            GraphSearchResult with memory IDs and entity info
        """
        # Build cache key from query + user + project
        cache_key = hashlib.md5(f"{query}:{user_id}:{project_id}".encode()).hexdigest()

        # Check cache first (deduplicate rapid-fire identical queries)
        async with _cache_lock:
            if cache_key in _query_cache:
                cached_time, cached_result = _query_cache[cache_key]
                if time.time() - cached_time < _cache_ttl_seconds:
                    log.debug(
                        "graph_search_cache_hit",
                        query=query[:50],
                        age_seconds=round(time.time() - cached_time, 2),
                    )
                    return cast(GraphSearchResult, cached_result)

        result = GraphSearchResult()

        # Step 1: Find entity mentions
        matched_entities = await self.find_entity_mentions(query, user_id, project_id)

        if not matched_entities:
            log.debug("graph_search_no_entities", query=query[:50])
            # Cache empty result too
            async with _cache_lock:
                _query_cache[cache_key] = (time.time(), result)
            return result

        log.debug(
            "graph_search_matched",
            query=query[:50],
            matched_count=len(matched_entities),
            names=[e.canonical_name for e in matched_entities],
        )

        # Build matched entity refs
        result.matched_entities = [
            EntityRef(
                id=e.id,
                canonical_name=e.canonical_name,
                type=e.type,
                confidence=1.0,
            )
            for e in matched_entities
        ]

        # Step 2 & 3: For each matched entity, explore neighborhood (with limits)
        all_entity_ids: set[str] = set()
        global_visited: set[str] = set()  # Share visited set across all traversals

        for entity in matched_entities:
            # Check entity limit
            if len(all_entity_ids) >= self.max_entities:
                log.debug(
                    "graph_search_entity_limit_reached",
                    limit=self.max_entities,
                )
                break

            # Get neighborhood (limited traversal)
            neighborhood = await self.get_entity_neighborhood(entity, user_id, project_id)

            # Only add up to the limit
            for eid in neighborhood:
                if len(all_entity_ids) >= self.max_entities:
                    break
                all_entity_ids.add(eid)

            # Track related entities (not the directly matched ones)
            related = await self.get_related_entities(entity.id, visited=global_visited)

            for related_entity, rel in related:
                # Respect entity limit for related entities too
                if len(result.related_entities) >= self.max_entities:
                    break
                result.related_entities.append(
                    EntityRef(
                        id=related_entity.id,
                        canonical_name=related_entity.canonical_name,
                        type=related_entity.type,
                        confidence=0.8,  # Lower confidence for indirect matches
                    )
                )
                result.traversal_paths.append(f"{entity.canonical_name} --[{rel.type}]--> {related_entity.canonical_name}")

        # Deduplicate related entities
        seen_ids: set[str] = set(e.id for e in result.matched_entities)
        unique_related: list[EntityRef] = []
        for ref in result.related_entities:
            if ref.id not in seen_ids and len(unique_related) < self.max_entities:
                unique_related.append(ref)
                seen_ids.add(ref.id)
        result.related_entities = unique_related

        # Get memory IDs linked to entities (with limit)
        for entity_id in all_entity_ids:
            # Check memory limit
            if len(result.memory_ids) >= self.max_memories:
                log.debug(
                    "graph_search_memory_limit_reached",
                    limit=self.max_memories,
                )
                break

            memory_ids = await self.db.get_memories_by_entity(entity_id)

            # Add only up to the limit
            for mid in memory_ids:
                if len(result.memory_ids) >= self.max_memories:
                    break
                result.memory_ids.add(mid)

        log.info(
            "graph_search_complete",
            matched_entities=len(result.matched_entities),
            related_entities=len(result.related_entities),
            memory_ids=len(result.memory_ids),
        )

        # Cache the result
        async with _cache_lock:
            # Clean old entries (simple TTL cleanup)
            now = time.time()
            expired_keys = [k for k, (t, _) in _query_cache.items() if now - t > _cache_ttl_seconds * 2]
            for k in expired_keys:
                del _query_cache[k]
            _query_cache[cache_key] = (now, result)

        return result


async def entity_boost_score(
    entity_refs: list[EntityRef],
    query: str,
    boost_factor: float = 0.1,
) -> float:
    """
    Calculate a boost score based on entity matches in the query.

    This can be added to relevance scores to prioritize memories
    that match entities mentioned in the query.

    Args:
        entity_refs: Entities linked to a memory
        query: Original search query
        boost_factor: Multiplier for each matched entity (default: 0.1)

    Returns:
        Total boost score (sum of boosts for matched entities)
    """
    if not entity_refs:
        return 0.0

    query_lower = query.lower()
    boost = 0.0

    for ref in entity_refs:
        # Check if entity name appears in query
        if ref.canonical_name.lower() in query_lower:
            boost += boost_factor * ref.confidence

    return min(boost, 0.5)  # Cap at 0.5 to prevent over-boosting
