"""
LLM-powered entity extraction from text.

Extracts people, organizations, locations, and their relationships.
"""

import json
import structlog
from dataclasses import dataclass
from openai import AsyncOpenAI

log = structlog.get_logger()


# ============================================================================
# Entity Extraction Prompt
# ============================================================================

ENTITY_EXTRACTION_PROMPT = """You are an entity extraction engine. Extract entities and relationships from text.

ENTITY TYPES:
- PERSON: People's names (include titles, roles if mentioned)
- ORG: Companies, organizations, teams, groups
- LOCATION: Cities, countries, addresses, places
- DATE: Specific dates, time periods, deadlines
- PRODUCT: Products, services, projects, software
- MONEY: Monetary amounts, prices, budgets
- CONCEPT: Abstract concepts, topics, skills

RELATIONSHIP TYPES:
- WORKS_AT: Person works at organization
- MANAGES: Person manages another person
- SPOUSE_OF: Married/partner relationship
- LOCATED_IN: Entity is located somewhere
- OWNS: Person/org owns something
- CREATED: Person/org created something
- ROLE: Person has a role (e.g., CEO, Manager)

RULES:
1. Extract ALL named entities, even if mentioned once
2. Infer relationships from context (e.g., "John's wife Lisa" → SPOUSE_OF)
3. Include role/title as part of person description
4. Resolve pronouns when clear (e.g., "He is the CEO" after mentioning John)
5. Return empty arrays if no entities found

OUTPUT FORMAT (strict JSON):
{
  "entities": [
    {
      "name": "John Smith",
      "type": "PERSON",
      "description": "CEO of Acme Corp",
      "aliases": ["John", "Mr. Smith"]
    },
    {
      "name": "Acme Corp",
      "type": "ORG",
      "description": "Company where John works",
      "aliases": ["Acme", "Acme Corporation"]
    }
  ],
  "relationships": [
    {
      "subject": "John Smith",
      "predicate": "WORKS_AT",
      "object": "Acme Corp"
    },
    {
      "subject": "John Smith",
      "predicate": "ROLE",
      "object": "CEO"
    }
  ]
}

EXAMPLES:

Input: "Sarah mentioned that her husband Mike works at Google as a Senior Engineer."
Output: {
  "entities": [
    {"name": "Sarah", "type": "PERSON", "description": "Mentioned Mike", "aliases": []},
    {"name": "Mike", "type": "PERSON", "description": "Senior Engineer at Google, Sarah's husband", "aliases": []},
    {"name": "Google", "type": "ORG", "description": "Tech company", "aliases": []}
  ],
  "relationships": [
    {"subject": "Mike", "predicate": "SPOUSE_OF", "object": "Sarah"},
    {"subject": "Mike", "predicate": "WORKS_AT", "object": "Google"},
    {"subject": "Mike", "predicate": "ROLE", "object": "Senior Engineer"}
  ]
}

Input: "The meeting is scheduled for March 15th in the Denver office."
Output: {
  "entities": [
    {"name": "March 15th", "type": "DATE", "description": "Meeting date", "aliases": []},
    {"name": "Denver office", "type": "LOCATION", "description": "Meeting location", "aliases": ["Denver"]}
  ],
  "relationships": []
}
"""


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ExtractedEntity:
    """An entity extracted from text."""
    name: str
    type: str
    description: str
    aliases: list[str]


@dataclass
class ExtractedRelationship:
    """A relationship extracted from text."""
    subject: str
    predicate: str
    object: str


@dataclass
class ExtractionResult:
    """Result of entity extraction."""
    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]


# ============================================================================
# Entity Extractor
# ============================================================================

class EntityExtractor:
    """
    Extracts entities and relationships from text using LLM.
    
    Usage:
        extractor = EntityExtractor()
        result = await extractor.extract("John is the CEO of Acme Corp")
        # result.entities = [Entity(name="John", type="PERSON", ...), ...]
        # result.relationships = [Relationship(subject="John", predicate="WORKS_AT", ...)]
    """
    
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self._client: AsyncOpenAI | None = None
    
    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.api_key)
            log.info("entity_extractor_initialized", model=self.model)
        return self._client
    
    async def extract(self, content: str) -> ExtractionResult:
        """
        Extract entities and relationships from content.
        
        Args:
            content: Text to extract entities from
            
        Returns:
            ExtractionResult with entities and relationships
        """
        if not content.strip() or len(content.strip()) < 10:
            return ExtractionResult(entities=[], relationships=[])
        
        try:
            client = self._get_client()
            
            log.debug("extracting_entities", content_length=len(content))
            
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": ENTITY_EXTRACTION_PROMPT},
                    {"role": "user", "content": f"Extract entities and relationships from:\n\n{content}"},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=30.0,
            )
            
            result_text = response.choices[0].message.content
            if not result_text:
                return ExtractionResult(entities=[], relationships=[])
            
            data = json.loads(result_text)
            
            # Parse entities
            entities = []
            for e in data.get("entities", []):
                if isinstance(e, dict) and e.get("name"):
                    entities.append(ExtractedEntity(
                        name=e.get("name", ""),
                        type=e.get("type", "CONCEPT"),
                        description=e.get("description", ""),
                        aliases=e.get("aliases", []),
                    ))
            
            # Parse relationships
            relationships = []
            for r in data.get("relationships", []):
                if isinstance(r, dict) and r.get("subject") and r.get("object"):
                    relationships.append(ExtractedRelationship(
                        subject=r.get("subject", ""),
                        predicate=r.get("predicate", "RELATED_TO"),
                        object=r.get("object", ""),
                    ))
            
            log.info(
                "entities_extracted",
                entity_count=len(entities),
                relationship_count=len(relationships),
            )
            
            return ExtractionResult(entities=entities, relationships=relationships)
            
        except json.JSONDecodeError as e:
            log.error("entity_extraction_json_error", error=str(e))
            return ExtractionResult(entities=[], relationships=[])
        except Exception as e:
            log.error("entity_extraction_error", error=str(e))
            return ExtractionResult(entities=[], relationships=[])


# ============================================================================
# Convenience function
# ============================================================================

async def extract_entities(
    content: str,
    model: str = "gpt-4o-mini",
) -> ExtractionResult:
    """
    Extract entities and relationships from content.
    
    Convenience function for one-off extraction.
    """
    extractor = EntityExtractor(model=model)
    return await extractor.extract(content)
