"""
LLM-powered entity extraction from text.

Extracts people, organizations, locations, trading entities, and their relationships.
Supports OpenAI, Anthropic, and Ollama providers.

Updated: April 10, 2026 - Added trading-specific entity and relationship types
for TradeMind integration.
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

General:
- PERSON: People's names (include titles, roles if mentioned)
- ORG: Companies, organizations, teams, groups
- LOCATION: Cities, countries, addresses, places
- DATE: Specific dates, time periods, deadlines
- PRODUCT: Products, services, projects, software
- MONEY: Monetary amounts, prices, budgets
- CONCEPT: Abstract concepts, topics, skills

Trading/Finance:
- SYMBOL: Trading instruments and tickers (NQ, ES, GC, SPY, AAPL, MNQ, etc.)
- STRATEGY: Trading strategies (ORB breakout, VWAP bounce, gap fill, Globex reversal, mean reversion)
- REGIME: Market regimes/states (trending, mean-reverting, volatile, choppy, range-bound, dislocated)
- INDICATOR: Technical indicators (ATR, VWAP, RSI, volume ratio, moving average, Bollinger bands)
- TRADE: Trade events with entry/exit details (long entry, short exit, stop loss hit, take profit)
- TIMEFRAME: Trading timeframes (1-minute, 5-minute, 15-minute, daily, weekly, RTH, ETH)
- SESSION: Trading sessions (pre-market, regular hours, after-hours, overnight, Globex, Asian, European)

RELATIONSHIP TYPES:

General:
- WORKS_AT: Person works at organization
- MANAGES: Person manages another person
- SPOUSE_OF: Married/partner relationship
- LOCATED_IN: Entity is located somewhere
- OWNS: Person/org owns something
- CREATED: Person/org created something
- ROLE: Person has a role (e.g., CEO, Manager)

Trading/Finance:
- TRADES: Person/agent trades a symbol
- USES_STRATEGY: Agent/system uses strategy for trading
- PERFORMED_IN: Trade or strategy performed in a specific regime
- TRIGGERS: Indicator triggers a strategy or trade signal
- CORRELATES_WITH: Symbol correlates with another symbol
- TRANSITIONS_TO: Regime transitions to another regime
- HAS_WIN_RATE: Strategy has a specific win rate (often in a regime)
- OCCURS_DURING: Event occurs during a session or timeframe
- TARGETS: Trade targets a price level

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
7. TRADING: For trading content, prioritize SYMBOL, STRATEGY, REGIME, and INDICATOR entities
8. WIN RATES: Extract win rates and statistics as relationships with the strategy

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

GENERAL EXAMPLES:

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

TRADING EXAMPLES:

Input: "ORB breakout on NQ in trending regime had 71% win rate across 847 trades"
Output: {
  "entities": [
    {"name": "ORB breakout", "type": "STRATEGY", "description": "Opening range breakout strategy", "aliases": ["ORB", "opening range breakout"]},
    {"name": "NQ", "type": "SYMBOL", "description": "Nasdaq 100 E-mini futures", "aliases": ["Nasdaq futures", "MNQ", "NQ futures"]},
    {"name": "trending", "type": "REGIME", "description": "Trending/momentum market regime", "aliases": ["trend", "momentum", "directional"]}
  ],
  "relationships": [
    {"subject": "ORB breakout", "predicate": "PERFORMED_IN", "object": "trending"},
    {"subject": "ORB breakout", "predicate": "HAS_WIN_RATE", "object": "71%"}
  ]
}

Input: "NQ transitioned from volatile to mean-reverting at 10:30 AM during regular hours"
Output: {
  "entities": [
    {"name": "NQ", "type": "SYMBOL", "description": "Nasdaq 100 E-mini futures", "aliases": []},
    {"name": "volatile", "type": "REGIME", "description": "High volatility market regime", "aliases": ["high vol", "dislocated"]},
    {"name": "mean-reverting", "type": "REGIME", "description": "Range-bound, choppy market regime", "aliases": ["choppy", "range", "reverting"]},
    {"name": "10:30 AM", "type": "DATE", "description": "Time of regime transition", "aliases": []},
    {"name": "regular hours", "type": "SESSION", "description": "Regular trading hours session", "aliases": ["RTH", "cash session"]}
  ],
  "relationships": [
    {"subject": "volatile", "predicate": "TRANSITIONS_TO", "object": "mean-reverting"},
    {"subject": "volatile", "predicate": "OCCURS_DURING", "object": "regular hours"}
  ]
}

Input: "VWAP bounce strategy works best when ATR is below 15 points in choppy regime"
Output: {
  "entities": [
    {"name": "VWAP bounce", "type": "STRATEGY", "description": "Mean reversion strategy using VWAP as support/resistance", "aliases": ["VWAP reversion", "VWAP fade"]},
    {"name": "ATR", "type": "INDICATOR", "description": "Average True Range volatility indicator", "aliases": ["average true range"]},
    {"name": "choppy", "type": "REGIME", "description": "Range-bound, mean-reverting market regime", "aliases": ["mean-reverting", "range-bound"]}
  ],
  "relationships": [
    {"subject": "VWAP bounce", "predicate": "TRIGGERS", "object": "ATR"},
    {"subject": "VWAP bounce", "predicate": "PERFORMED_IN", "object": "choppy"}
  ]
}

Input: "Gap fill on ES after overnight session had 68% success rate, best results when Globex range was narrow"
Output: {
  "entities": [
    {"name": "Gap fill", "type": "STRATEGY", "description": "Strategy to trade gaps filling back to previous close", "aliases": ["gap fade", "gap close"]},
    {"name": "ES", "type": "SYMBOL", "description": "S&P 500 E-mini futures", "aliases": ["S&P futures", "MES", "SPX futures"]},
    {"name": "overnight session", "type": "SESSION", "description": "Overnight/Globex trading session", "aliases": ["Globex", "ETH", "overnight"]},
    {"name": "Globex range", "type": "INDICATOR", "description": "Price range during Globex/overnight session", "aliases": ["overnight range"]}
  ],
  "relationships": [
    {"subject": "Gap fill", "predicate": "OCCURS_DURING", "object": "overnight session"},
    {"subject": "Gap fill", "predicate": "HAS_WIN_RATE", "object": "68%"},
    {"subject": "Gap fill", "predicate": "TRIGGERS", "object": "Globex range"}
  ]
}

Input: "The General agent took a long entry on GC at 2,340 with stop at 2,335 targeting 2,355"
Output: {
  "entities": [
    {"name": "The General", "type": "PERSON", "description": "Trading agent/AI system", "aliases": ["General", "agent"]},
    {"name": "GC", "type": "SYMBOL", "description": "Gold futures", "aliases": ["gold", "gold futures", "MGC"]},
    {"name": "long entry", "type": "TRADE", "description": "Long position entry at 2,340", "aliases": []},
    {"name": "2,340", "type": "MONEY", "description": "Entry price", "aliases": []},
    {"name": "2,335", "type": "MONEY", "description": "Stop loss price", "aliases": []},
    {"name": "2,355", "type": "MONEY", "description": "Target price", "aliases": []}
  ],
  "relationships": [
    {"subject": "The General", "predicate": "TRADES", "object": "GC"},
    {"subject": "long entry", "predicate": "TARGETS", "object": "2,355"}
  ]
}

Input: "ES and NQ regimes aligned in trending state, correlation at 0.94"
Output: {
  "entities": [
    {"name": "ES", "type": "SYMBOL", "description": "S&P 500 E-mini futures", "aliases": []},
    {"name": "NQ", "type": "SYMBOL", "description": "Nasdaq 100 E-mini futures", "aliases": []},
    {"name": "trending", "type": "REGIME", "description": "Trending market state", "aliases": []}
  ],
  "relationships": [
    {"subject": "ES", "predicate": "CORRELATES_WITH", "object": "NQ"},
    {"subject": "ES", "predicate": "PERFORMED_IN", "object": "trending"},
    {"subject": "NQ", "predicate": "PERFORMED_IN", "object": "trending"}
  ]
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
    valid_to: str | None = None  # ISO date string, or None for ongoing


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
                    entities.append(
                        ExtractedEntity(
                            name=e.get("name", ""),
                            type=e.get("type", "CONCEPT"),
                            description=e.get("description", ""),
                            aliases=e.get("aliases", []),
                        )
                    )

            # Parse relationships
            relationships = []
            for r in data.get("relationships", []):
                if isinstance(r, dict) and r.get("subject") and r.get("object"):
                    relationships.append(
                        ExtractedRelationship(
                            subject=r.get("subject", ""),
                            predicate=r.get("predicate", "RELATED_TO"),
                            object=r.get("object", ""),
                            valid_from=r.get("valid_from"),
                            valid_to=r.get("valid_to"),
                        )
                    )

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
            entities.append(
                ExtractedEntity(
                    name=e.get("name", ""),
                    type=e.get("type", "CONCEPT"),
                    description=e.get("description", ""),
                    aliases=e.get("aliases", []),
                )
            )

    # Parse relationships
    relationships: list[ExtractedRelationship] = []
    for r in data.get("relationships", []):
        if isinstance(r, dict) and r.get("subject") and r.get("object"):
            relationships.append(
                ExtractedRelationship(
                    subject=r.get("subject", ""),
                    predicate=r.get("predicate", "RELATED_TO"),
                    object=r.get("object", ""),
                    valid_from=r.get("valid_from"),
                    valid_to=r.get("valid_to"),
                )
            )

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
            result_text = "".join(block.text for block in response.content if block.type == "text")
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
                            "content": (f"Extract entities and relationships from:\n\n{content}"),
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
