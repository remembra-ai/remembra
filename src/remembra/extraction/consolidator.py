"""
Memory consolidation: Decide how to integrate new facts with existing memories.

Prevents duplicates, handles updates, resolves contradictions.
"""

import json
from dataclasses import dataclass
from enum import Enum

import structlog
from openai import AsyncOpenAI

log = structlog.get_logger()


# ============================================================================
# Consolidation Prompt
# ============================================================================

CONSOLIDATION_SYSTEM_PROMPT = """You are a memory consolidation engine. Your job is to decide how to integrate a new fact with existing memories.

ACTIONS:
- ADD: The new fact is genuinely new information. Store it.
- UPDATE: The new fact updates or enhances an existing memory. Merge them.
- DELETE: The new fact contradicts an existing memory. The old one is outdated.
- NOOP: The new fact is already captured by existing memories. Skip it.

DECISION RULES:
1. If no similar memories exist → ADD
2. If new fact adds detail to existing → UPDATE (merge the information)
3. If new fact contradicts existing (e.g., job change, status change) → DELETE old + ADD new
4. If new fact is essentially the same as existing → NOOP
5. When merging, preserve all relevant details from both

OUTPUT FORMAT:
Return a JSON object:
{
  "action": "ADD" | "UPDATE" | "DELETE" | "NOOP",
  "target_id": "memory_id to update/delete, or null for ADD/NOOP",
  "content": "final merged fact text for ADD/UPDATE, or null for DELETE/NOOP",
  "reason": "brief explanation of decision"
}

EXAMPLES:

New fact: "John is VP of Sales"
Existing: [{"id": "m1", "content": "John is Sales Director"}]
Output: {"action": "UPDATE", "target_id": "m1", "content": "John is VP of Sales (promoted from Sales Director)", "reason": "Job title update, preserving history"}

New fact: "Sarah works at Google"
Existing: [{"id": "m2", "content": "Sarah works at Microsoft"}]
Output: {"action": "DELETE", "target_id": "m2", "content": "Sarah works at Google", "reason": "Company change, old info outdated"}

New fact: "User prefers dark mode"
Existing: []
Output: {"action": "ADD", "target_id": null, "content": "User prefers dark mode", "reason": "New preference, no existing memory"}

New fact: "John is the CEO"
Existing: [{"id": "m3", "content": "John is the CEO of Acme Corp"}]
Output: {"action": "NOOP", "target_id": null, "content": null, "reason": "Already captured with more detail"}
"""

CONSOLIDATION_USER_PROMPT = """Decide how to handle this new fact:

NEW FACT: {new_fact}

EXISTING SIMILAR MEMORIES:
{existing_memories}

Return JSON with action, target_id, content, and reason."""


# ============================================================================
# Types
# ============================================================================

class ConsolidationAction(str, Enum):
    """Action to take for memory consolidation."""
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NOOP = "NOOP"


@dataclass
class ConsolidationResult:
    """Result of consolidation decision."""
    action: ConsolidationAction
    target_id: str | None
    content: str | None
    reason: str


@dataclass
class ExistingMemory:
    """Existing memory for comparison."""
    id: str
    content: str
    score: float = 0.0


# ============================================================================
# Memory Consolidator
# ============================================================================

class MemoryConsolidator:
    """
    Decides how to integrate new facts with existing memories.
    
    Usage:
        consolidator = MemoryConsolidator()
        result = await consolidator.consolidate(
            new_fact="John is VP of Sales",
            existing=[ExistingMemory(id="m1", content="John is Sales Director")]
        )
        # result.action == ConsolidationAction.UPDATE
    """
    
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        similarity_threshold: float = 0.5,
    ):
        self.model = model
        self.api_key = api_key
        self.similarity_threshold = similarity_threshold
        self._client: AsyncOpenAI | None = None
    
    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client
    
    async def consolidate(
        self,
        new_fact: str,
        existing: list[ExistingMemory],
    ) -> ConsolidationResult:
        """
        Decide how to integrate a new fact with existing memories.
        
        Args:
            new_fact: The new fact to integrate
            existing: List of existing similar memories
            
        Returns:
            ConsolidationResult with action and details
        """
        # Filter by similarity threshold
        relevant = [m for m in existing if m.score >= self.similarity_threshold]
        
        # If no similar memories, just ADD
        if not relevant:
            log.debug("no_similar_memories", fact=new_fact[:50])
            return ConsolidationResult(
                action=ConsolidationAction.ADD,
                target_id=None,
                content=new_fact,
                reason="No similar existing memories",
            )
        
        try:
            client = self._get_client()
            
            # Format existing memories for prompt
            existing_formatted = json.dumps(
                [{"id": m.id, "content": m.content} for m in relevant],
                indent=2,
            )
            
            log.debug(
                "consolidating_fact",
                fact=new_fact[:50],
                similar_count=len(relevant),
            )
            
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
                    {"role": "user", "content": CONSOLIDATION_USER_PROMPT.format(
                        new_fact=new_fact,
                        existing_memories=existing_formatted,
                    )},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=30.0,
            )
            
            result_text = response.choices[0].message.content
            if not result_text:
                return self._default_add(new_fact)
            
            result = json.loads(result_text)
            
            action = ConsolidationAction(result.get("action", "ADD"))
            
            log.info(
                "consolidation_decision",
                action=action.value,
                reason=result.get("reason", "")[:50],
            )
            
            return ConsolidationResult(
                action=action,
                target_id=result.get("target_id"),
                content=result.get("content"),
                reason=result.get("reason", ""),
            )
            
        except Exception as e:
            log.error("consolidation_error", error=str(e))
            return self._default_add(new_fact)
    
    def _default_add(self, fact: str) -> ConsolidationResult:
        """Default to ADD on error."""
        return ConsolidationResult(
            action=ConsolidationAction.ADD,
            target_id=None,
            content=fact,
            reason="Default ADD (consolidation unavailable)",
        )


# ============================================================================
# Convenience function
# ============================================================================

async def consolidate_memory(
    new_fact: str,
    existing: list[ExistingMemory],
    model: str = "gpt-4o-mini",
) -> ConsolidationResult:
    """
    Decide how to handle a new fact.
    
    Convenience function for one-off consolidation.
    """
    consolidator = MemoryConsolidator(model=model)
    return await consolidator.consolidate(new_fact, existing)
