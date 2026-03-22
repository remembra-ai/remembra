"""
Entity matching and coreference resolution.

Determines if "John", "Mr. Smith", and "the CEO" refer to the same entity.
"""

import json
from dataclasses import dataclass

import structlog
from openai import AsyncOpenAI

from remembra.extraction.entities import ExtractedEntity

log = structlog.get_logger()


# ============================================================================
# Matching Prompt
# ============================================================================

ENTITY_MATCHING_PROMPT = """You are an entity matching engine. Determine if a new entity mention refers to an existing entity in the database.

MATCHING CRITERIA:
1. NAME SIMILARITY: "John Smith" ↔ "Mr. Smith" ↔ "John" ↔ "J. Smith"
2. TYPE COMPATIBILITY: PERSON can't match ORG (unless it's an alias like "Apple" the company)
3. CONTEXT CLUES: Same role, location, or relationships suggest same entity
4. DESCRIPTION OVERLAP: Similar descriptions increase match confidence

CONFIDENCE LEVELS:
- 0.95+: Definite match (exact name or very strong evidence)
- 0.80-0.94: High confidence match (name variation + context match)
- 0.60-0.79: Probable match (some evidence, worth merging)
- 0.40-0.59: Possible match (weak evidence, ask for confirmation)
- Below 0.40: Different entities

OUTPUT FORMAT (strict JSON):
{
  "match": true|false,
  "matched_entity_id": "id of matched entity or null",
  "confidence": 0.0-1.0,
  "reason": "brief explanation",
  "suggested_aliases": ["new aliases to add if matched"]
}

If NO match found, suggest creating new entity:
{
  "match": false,
  "matched_entity_id": null,
  "confidence": 0.0,
  "reason": "No matching entity found",
  "new_entity": {
    "canonical_name": "best name to use",
    "type": "PERSON|ORG|LOCATION|etc",
    "description": "brief description",
    "aliases": ["alternative names"]
  }
}

EXAMPLES:

New mention: {"name": "Mr. Smith", "type": "PERSON", "description": "discussed quarterly targets"}
Existing entities: [
  {"id": "ent_1", "name": "John Smith", "type": "PERSON", "description": "CEO of Acme", "aliases": ["John"]},
  {"id": "ent_2", "name": "Bob Smith", "type": "PERSON", "description": "Sales rep", "aliases": []}
]
Output: {
  "match": true,
  "matched_entity_id": "ent_1",
  "confidence": 0.85,
  "reason": "Mr. Smith likely refers to John Smith (CEO context matches quarterly targets discussion)",
  "suggested_aliases": ["Mr. Smith"]
}

New mention: {"name": "Sarah Johnson", "type": "PERSON", "description": "new hire in marketing"}
Existing entities: [
  {"id": "ent_1", "name": "John Smith", "type": "PERSON", "description": "CEO", "aliases": []}
]
Output: {
  "match": false,
  "matched_entity_id": null,
  "confidence": 0.0,
  "reason": "No matching entity found - Sarah Johnson is a new person",
  "new_entity": {
    "canonical_name": "Sarah Johnson",
    "type": "PERSON",
    "description": "New hire in marketing",
    "aliases": ["Sarah"]
  }
}
"""


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ExistingEntity:
    """An existing entity in the database."""
    id: str
    name: str
    type: str
    description: str
    aliases: list[str]


@dataclass
class MatchResult:
    """Result of entity matching."""
    match: bool
    matched_entity_id: str | None
    confidence: float
    reason: str
    suggested_aliases: list[str]
    new_entity: ExtractedEntity | None


# ============================================================================
# Entity Matcher
# ============================================================================

class EntityMatcher:
    """
    Matches new entity mentions to existing entities.
    
    Usage:
        matcher = EntityMatcher()
        result = await matcher.match(
            new_entity=ExtractedEntity(name="Mr. Smith", type="PERSON", ...),
            existing_entities=[ExistingEntity(id="1", name="John Smith", ...)]
        )
        if result.match:
            # Add alias to existing entity
        else:
            # Create new entity
    """
    
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        min_confidence: float = 0.6,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.min_confidence = min_confidence
        self._client: AsyncOpenAI | None = None
    
    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client
    
    async def match(
        self,
        new_entity: ExtractedEntity,
        existing_entities: list[ExistingEntity],
    ) -> MatchResult:
        """
        Match a new entity mention to existing entities.
        
        Args:
            new_entity: The new entity to match
            existing_entities: List of existing entities to match against
            
        Returns:
            MatchResult indicating if match found and details
        """
        # If no existing entities, definitely create new
        if not existing_entities:
            return MatchResult(
                match=False,
                matched_entity_id=None,
                confidence=0.0,
                reason="No existing entities to match against",
                suggested_aliases=[],
                new_entity=new_entity,
            )
        
        # Quick check: exact name match
        for existing in existing_entities:
            if new_entity.name.lower() == existing.name.lower():
                return MatchResult(
                    match=True,
                    matched_entity_id=existing.id,
                    confidence=0.99,
                    reason="Exact name match",
                    suggested_aliases=[],
                    new_entity=None,
                )
            # Check aliases
            for alias in existing.aliases:
                if new_entity.name.lower() == alias.lower():
                    return MatchResult(
                        match=True,
                        matched_entity_id=existing.id,
                        confidence=0.95,
                        reason=f"Matches existing alias: {alias}",
                        suggested_aliases=[],
                        new_entity=None,
                    )
        
        # Use LLM for fuzzy matching
        try:
            client = self._get_client()
            
            # Format entities for prompt
            new_json = {
                "name": new_entity.name,
                "type": new_entity.type,
                "description": new_entity.description,
            }
            
            existing_json = [
                {
                    "id": e.id,
                    "name": e.name,
                    "type": e.type,
                    "description": e.description,
                    "aliases": e.aliases,
                }
                for e in existing_entities[:10]  # Limit to top 10
            ]
            
            log.debug(
                "matching_entity",
                new_entity=new_entity.name,
                candidates=len(existing_json),
            )
            
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": ENTITY_MATCHING_PROMPT},
                    {"role": "user", "content": f"""
New entity mention: {json.dumps(new_json)}

Existing entities in database:
{json.dumps(existing_json, indent=2)}

Does the new mention match any existing entity?
"""},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=30.0,
            )
            
            result_text = response.choices[0].message.content
            if not result_text:
                return self._create_new_entity(new_entity)
            
            data = json.loads(result_text)
            
            is_match = data.get("match", False)
            confidence = data.get("confidence", 0.0)
            
            # Only accept matches above threshold
            if is_match and confidence >= self.min_confidence:
                log.info(
                    "entity_matched",
                    new_name=new_entity.name,
                    matched_id=data.get("matched_entity_id"),
                    confidence=confidence,
                )
                return MatchResult(
                    match=True,
                    matched_entity_id=data.get("matched_entity_id"),
                    confidence=confidence,
                    reason=data.get("reason", ""),
                    suggested_aliases=data.get("suggested_aliases", []),
                    new_entity=None,
                )
            else:
                # Create new entity
                new_entity_data = data.get("new_entity", {})
                return MatchResult(
                    match=False,
                    matched_entity_id=None,
                    confidence=0.0,
                    reason=data.get("reason", "No match found"),
                    suggested_aliases=[],
                    new_entity=ExtractedEntity(
                        name=new_entity_data.get("canonical_name", new_entity.name),
                        type=new_entity_data.get("type", new_entity.type),
                        description=new_entity_data.get("description", new_entity.description),
                        aliases=new_entity_data.get("aliases", new_entity.aliases),
                    ),
                )
            
        except Exception as e:
            log.error("entity_matching_error", error=str(e))
            return self._create_new_entity(new_entity)
    
    def _create_new_entity(self, entity: ExtractedEntity) -> MatchResult:
        """Create result for new entity."""
        return MatchResult(
            match=False,
            matched_entity_id=None,
            confidence=0.0,
            reason="Creating new entity",
            suggested_aliases=[],
            new_entity=entity,
        )
