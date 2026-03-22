"""
LLM-powered entity extraction from text.

Extracts people, organizations, locations, and their relationships.
Supports OpenAI, Anthropic, and Ollama providers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from remembra.config import Settings

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
6. TEMPORAL: Extract WHEN relationships started/ended if mentioned
   - "worked at" (past) vs "works at" (present)
   - "since 2020", "from 2018 to 2022", "until last year"
   - If no temporal info, omit valid_from/valid_to (defaults to present)

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
      "object": "Acme Corp",
      "valid_from": "2020-01-01",
      "valid_to": null
    },
    {
      "subject": "John Smith",
      "predicate": "ROLE",
      "object": "CEO"
    }
  ]
}

TEMPORAL EXAMPLES:

Input: "Alice used to work at Meta from 2019 to 2022. She now works at Google."
Output: {
  "entities": [
    {"name": "Alice", "type": "PERSON", "description": "Former Meta employee, now at Google", "aliases": []},
    {"name": "Meta", "type": "ORG", "description": "Tech company (former employer)", "aliases": ["Facebook"]},
    {"name": "Google", "type": "ORG", "description": "Tech company (current employer)", "aliases": []}
  ],
  "relationships": [
    {"subject": "Alice", "predicate": "WORKS_AT", "object": "Meta", "valid_from": "2019-01-01", "valid_to": "2022-12-31"},
    {"subject": "Alice", "predicate": "WORKS_AT", "object": "Google", "valid_from": "2022-01-01", "valid_to": null}
  ]
}

Input: "Bob has been married to Carol since 2015."
Output: {
  "entities": [
    {"name": "Bob", "type": "PERSON", "description": "Married to Carol", "aliases": []},
    {"name": "Carol", "type": "PERSON", "description": "Bob's spouse", "aliases": []}
  ],
  "relationships": [
    {"subject": "Bob", "predicate": "SPOUSE_OF", "object": "Carol", "valid_from": "2015-01-01", "valid_to": null}
  ]
}

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
    """A relationship extracted from text with optional temporal bounds."""
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None  # ISO date string, e.g., "2020-01-01"
    valid_to: str | None = None    # ISO date string, or None for ongoing


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
    ) -> None:
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
                        valid_from=r.get("valid_from"),
                        valid_to=r.get("valid_to"),
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
# Shared JSON Parsing Helper
# ============================================================================

def _parse_extraction_json(raw_text: str) -> ExtractionResult:
    """
    Parse an LLM response into an ExtractionResult.

    Handles both clean JSON and JSON wrapped in markdown fences.
    Returns an empty result on any parse failure.
    """
    # Strip markdown code fences (```json ... ```) if present
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    data = json.loads(cleaned)

    # Parse entities
    entities: list[ExtractedEntity] = []
    for e in data.get("entities", []):
        if isinstance(e, dict) and e.get("name"):
            entities.append(ExtractedEntity(
                name=e.get("name", ""),
                type=e.get("type", "CONCEPT"),
                description=e.get("description", ""),
                aliases=e.get("aliases", []),
            ))

    # Parse relationships
    relationships: list[ExtractedRelationship] = []
    for r in data.get("relationships", []):
        if isinstance(r, dict) and r.get("subject") and r.get("object"):
            relationships.append(ExtractedRelationship(
                subject=r.get("subject", ""),
                predicate=r.get("predicate", "RELATED_TO"),
                object=r.get("object", ""),
                valid_from=r.get("valid_from"),
                valid_to=r.get("valid_to"),
            ))

    return ExtractionResult(entities=entities, relationships=relationships)


# ============================================================================
# Anthropic Entity Extractor
# ============================================================================

class AnthropicEntityExtractor:
    """Entity extraction using Anthropic Claude."""

    def __init__(self, model: str = "claude-sonnet-4-5", api_key: str | None = None) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        log.info("anthropic_entity_extractor_initialized", model=self.model)

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
            log.debug("extracting_entities", provider="anthropic", content_length=len(content))

            response = await self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.1,
                system=ENTITY_EXTRACTION_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": f"Extract entities and relationships from:\n\n{content}",
                    },
                ],
            )

            # Claude returns content blocks; concatenate text blocks
            result_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            if not result_text:
                return ExtractionResult(entities=[], relationships=[])

            result = _parse_extraction_json(result_text)

            log.info(
                "entities_extracted",
                provider="anthropic",
                entity_count=len(result.entities),
                relationship_count=len(result.relationships),
            )
            return result

        except json.JSONDecodeError as e:
            log.error("entity_extraction_json_error", provider="anthropic", error=str(e))
            return ExtractionResult(entities=[], relationships=[])
        except Exception as e:
            log.error("entity_extraction_error", provider="anthropic", error=str(e))
            return ExtractionResult(entities=[], relationships=[])


# ============================================================================
# Ollama Entity Extractor
# ============================================================================

class OllamaEntityExtractor:
    """Entity extraction using local Ollama models."""

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
    ) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=120.0)
        log.info(
            "ollama_entity_extractor_initialized",
            model=self.model,
            base_url=self._base_url,
        )

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
            log.debug("extracting_entities", provider="ollama", content_length=len(content))

            response = await self._client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": ENTITY_EXTRACTION_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Extract entities and relationships from:\n\n{content}"
                            ),
                        },
                    ],
                    "format": "json",
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                    },
                },
            )
            response.raise_for_status()

            data = response.json()
            result_text = data.get("message", {}).get("content", "")
            if not result_text:
                return ExtractionResult(entities=[], relationships=[])

            result = _parse_extraction_json(result_text)

            log.info(
                "entities_extracted",
                provider="ollama",
                entity_count=len(result.entities),
                relationship_count=len(result.relationships),
            )
            return result

        except json.JSONDecodeError as e:
            log.error("entity_extraction_json_error", provider="ollama", error=str(e))
            return ExtractionResult(entities=[], relationships=[])
        except Exception as e:
            log.error("entity_extraction_error", provider="ollama", error=str(e))
            return ExtractionResult(entities=[], relationships=[])


# ============================================================================
# Factory
# ============================================================================

def create_entity_extractor(
    settings: Settings,
) -> EntityExtractor | AnthropicEntityExtractor | OllamaEntityExtractor:
    """Create the appropriate entity extractor based on config.

    Reads ``settings.llm_provider`` to decide which backend to use and
    passes through the relevant model / API-key / URL settings.
    """
    provider = getattr(settings, "llm_provider", "openai").lower()

    if provider == "anthropic":
        return AnthropicEntityExtractor(
            model=getattr(settings, "extraction_model", "claude-sonnet-4-5"),
            api_key=getattr(settings, "anthropic_api_key", None),
        )

    if provider == "ollama":
        return OllamaEntityExtractor(
            model=getattr(settings, "extraction_model", "llama3.1"),
            base_url=getattr(settings, "ollama_url", "http://localhost:11434"),
        )

    # Default: OpenAI
    return EntityExtractor(
        model=getattr(settings, "extraction_model", "gpt-4o-mini"),
        api_key=getattr(settings, "openai_api_key", None),
    )


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
