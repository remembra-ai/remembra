"""
LLM-powered fact extraction from text.

Transforms messy conversations into clean, atomic facts.
"""

import json
from dataclasses import dataclass

import structlog
from openai import AsyncOpenAI

log = structlog.get_logger()

# ============================================================================
# Extraction Prompt
# ============================================================================

EXTRACTION_SYSTEM_PROMPT = """You are a memory extraction engine. Your job is to extract atomic facts from text that are worth remembering long-term.

RULES FOR EXTRACTION:
1. Each fact must be SELF-CONTAINED (understandable without context)
2. Each fact must be SPECIFIC (include names, dates, numbers when present)
3. Each fact must be USEFUL (valuable for future recall)
4. Convert relative dates to context (e.g., "yesterday" → include what that means if clear)
5. Preserve important relationships between people/things

DO NOT EXTRACT:
- Greetings, filler words, or pleasantries ("hi", "thanks", "sounds good")
- Vague statements without substance
- Questions (unless they reveal preferences)
- Temporary/transient information with no lasting value

OUTPUT FORMAT:
Return a JSON object with a "facts" array containing strings.
If no facts worth extracting, return {"facts": []}

EXAMPLES:

Input: "Hey! Talked to John today. He mentioned he's leaving Acme Corp next month to join Google as a Senior Engineer."
Output: {"facts": ["John is leaving Acme Corp next month", "John is joining Google as a Senior Engineer"]}

Input: "The meeting went well. Sarah prefers morning standups, ideally around 9am."
Output: {"facts": ["Sarah prefers morning standups around 9am"]}

Input: "Thanks for the update!"
Output: {"facts": []}

Input: "My wife Lisa and I are planning a trip to Japan in April. We've been married for 5 years."
Output: {"facts": ["User's wife is named Lisa", "User has been married to Lisa for 5 years", "User is planning a trip to Japan in April with Lisa"]}
"""

EXTRACTION_USER_PROMPT = """Extract memorable facts from this text:

{content}

Remember: Only extract facts worth remembering long-term. Return JSON with "facts" array."""


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class ExtractionConfig:
    """Configuration for fact extraction."""
    
    enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    max_facts_per_input: int = 10
    temperature: float = 0.1  # Low for consistency
    timeout: float = 30.0


# ============================================================================
# Fact Extractor
# ============================================================================

class FactExtractor:
    """
    Extracts atomic facts from text using LLM.
    
    Usage:
        extractor = FactExtractor(config)
        facts = await extractor.extract("John is the CEO of Acme Corp")
        # Returns: ["John is the CEO of Acme Corp"]
    """
    
    def __init__(self, config: ExtractionConfig | None = None):
        self.config = config or ExtractionConfig()
        self._client: AsyncOpenAI | None = None
    
    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.config.api_key)
            log.info(
                "extraction_client_initialized",
                provider=self.config.provider,
                model=self.config.model,
            )
        return self._client
    
    async def extract(self, content: str) -> list[str]:
        """
        Extract atomic facts from content.
        
        Args:
            content: Raw text to extract facts from
            
        Returns:
            List of atomic fact strings
        """
        if not self.config.enabled:
            # Fallback to simple splitting
            return self._simple_extract(content)
        
        if not content.strip():
            return []
        
        # Skip very short content
        if len(content.strip()) < 10:
            return [content.strip()] if content.strip() else []
        
        try:
            client = self._get_client()
            
            log.debug("extracting_facts", content_length=len(content))
            
            response = await client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": EXTRACTION_USER_PROMPT.format(content=content)},
                ],
                temperature=self.config.temperature,
                response_format={"type": "json_object"},
                timeout=self.config.timeout,
            )
            
            # Parse response
            result_text = response.choices[0].message.content
            if not result_text:
                log.warning("empty_extraction_response")
                return self._simple_extract(content)
            
            result = json.loads(result_text)
            facts = result.get("facts", [])
            
            # Validate and limit
            facts = [f.strip() for f in facts if isinstance(f, str) and f.strip()]
            facts = facts[:self.config.max_facts_per_input]
            
            log.info(
                "facts_extracted",
                input_length=len(content),
                fact_count=len(facts),
            )
            
            return facts
            
        except json.JSONDecodeError as e:
            log.error("extraction_json_error", error=str(e))
            return self._simple_extract(content)
        except Exception as e:
            log.error("extraction_error", error=str(e))
            return self._simple_extract(content)
    
    def _simple_extract(self, content: str) -> list[str]:
        """Fallback: simple sentence splitting."""
        # Split on sentence boundaries
        import re
        sentences = re.split(r'(?<=[.!?])\s+', content)
        facts = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]
        return facts[:self.config.max_facts_per_input]


# ============================================================================
# Convenience function
# ============================================================================

async def extract_facts(
    content: str,
    config: ExtractionConfig | None = None,
) -> list[str]:
    """
    Extract facts from content.
    
    Convenience function for one-off extraction.
    """
    extractor = FactExtractor(config)
    return await extractor.extract(content)
