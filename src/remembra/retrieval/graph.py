"""Graph-aware retrieval for entity relationship traversal.

This module enables finding memories by traversing the entity graph:
1. Extract entity mentions from the query
2. Match to known entities (including aliases)
3. Traverse relationships to find related entities
4. Surface all memories linked to the entity neighborhood
"""

from dataclasses import dataclass, field
from typing import Any

import structlog

from remembra.models.memory import Entity, EntityRef, Relationship
from remembra.storage.database import Database

log = structlog.get_logger(__name__)


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
    """
    
    def __init__(self, db: Database, max_depth: int = 2):
        """
        Initialize the graph retriever.
        
        Args:
            db: Database instance for entity/relationship queries
            max_depth: Maximum relationship traversal depth (default: 2)
        """
        self.db = db
        self.max_depth = max_depth
    
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
    ) -> list[tuple[Entity, Relationship]]:
        """
        Get entities related to the given entity up to specified depth.
        
        Args:
            entity_id: Starting entity ID
            depth: Current traversal depth
            visited: Set of already-visited entity IDs
            
        Returns:
            List of (Entity, Relationship) tuples
        """
        if visited is None:
            visited = set()
        
        if depth > self.max_depth or entity_id in visited:
            return []
        
        visited.add(entity_id)
        
        # Get all relationships for this entity
        relationships = await self.db.get_entity_relationships(entity_id)
        
        related: list[tuple[Entity, Relationship]] = []
        
        for rel in relationships:
            # Determine the "other" entity in the relationship
            other_id = rel.to_entity_id if rel.from_entity_id == entity_id else rel.from_entity_id
            
            if other_id in visited:
                continue
            
            other_entity = await self.db.get_entity(other_id)
            if other_entity:
                related.append((other_entity, rel))
                
                # Recursive traversal for deeper relationships
                if depth < self.max_depth:
                    deeper = await self.get_related_entities(other_id, depth + 1, visited)
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
        Perform graph-aware retrieval.
        
        Steps:
        1. Find entities mentioned in query
        2. Get neighborhood of each matched entity
        3. Collect all memories linked to neighborhood entities
        
        Args:
            query: Search query
            user_id: User ID
            project_id: Project ID
            
        Returns:
            GraphSearchResult with memory IDs and entity info
        """
        result = GraphSearchResult()
        
        # Step 1: Find entity mentions
        matched_entities = await self.find_entity_mentions(query, user_id, project_id)
        
        if not matched_entities:
            log.debug("graph_search_no_entities", query=query[:50])
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
        
        # Step 2 & 3: For each matched entity, explore neighborhood
        all_entity_ids: set[str] = set()
        
        for entity in matched_entities:
            # Get neighborhood
            neighborhood = await self.get_entity_neighborhood(entity, user_id, project_id)
            all_entity_ids.update(neighborhood)
            
            # Track related entities (not the directly matched ones)
            related = await self.get_related_entities(entity.id)
            for related_entity, rel in related:
                result.related_entities.append(EntityRef(
                    id=related_entity.id,
                    canonical_name=related_entity.canonical_name,
                    type=related_entity.type,
                    confidence=0.8,  # Lower confidence for indirect matches
                ))
                result.traversal_paths.append(
                    f"{entity.canonical_name} --[{rel.type}]--> {related_entity.canonical_name}"
                )
        
        # Deduplicate related entities
        seen_ids: set[str] = set(e.id for e in result.matched_entities)
        unique_related: list[EntityRef] = []
        for ref in result.related_entities:
            if ref.id not in seen_ids:
                unique_related.append(ref)
                seen_ids.add(ref.id)
        result.related_entities = unique_related
        
        # Get all memory IDs linked to any entity in the neighborhood
        for entity_id in all_entity_ids:
            memory_ids = await self.db.get_memories_by_entity(entity_id)
            result.memory_ids.update(memory_ids)
        
        log.info(
            "graph_search_complete",
            matched_entities=len(result.matched_entities),
            related_entities=len(result.related_entities),
            memory_ids=len(result.memory_ids),
        )
        
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
