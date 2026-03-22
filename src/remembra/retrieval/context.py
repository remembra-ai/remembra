"""Context window optimization for LLM-friendly output.

This module handles:
1. Token-aware truncation to fit context limits
2. Relevance-based prioritization of chunks
3. Smart formatting for LLM consumption
4. Accurate token counting with tiktoken (optional)
"""

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Approximate tokens per character for English text
# GPT models: ~4 chars per token on average
# This is a conservative estimate (used as fallback)
CHARS_PER_TOKEN = 4

# Lazy-loaded tiktoken encoder
_tiktoken_encoder = None
_tiktoken_loaded = False


def _get_tiktoken_encoder() -> Any:
    """Lazy load tiktoken encoder for accurate token counting."""
    global _tiktoken_encoder, _tiktoken_loaded
    
    if _tiktoken_loaded:
        return _tiktoken_encoder
    
    try:
        import tiktoken
        # Use cl100k_base encoding (used by GPT-4, GPT-3.5-turbo)
        _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
        _tiktoken_loaded = True
        log.debug("tiktoken_loaded", encoding="cl100k_base")
    except ImportError:
        log.debug(
            "tiktoken_not_installed",
            message="Using character-based estimation. Install tiktoken for accuracy."
        )
        _tiktoken_loaded = True
    except Exception as e:
        log.warning("tiktoken_load_failed", error=str(e))
        _tiktoken_loaded = True
    
    return _tiktoken_encoder


@dataclass
class MemoryChunk:
    """A chunk of memory content with metadata."""
    
    id: str
    content: str
    relevance: float
    created_at: str | None = None
    # Approximate token count
    tokens: int = 0
    # Whether this chunk was truncated
    truncated: bool = False


@dataclass
class OptimizedContext:
    """Result of context optimization."""
    
    # Combined context string ready for LLM
    context: str
    # Individual chunks included
    chunks: list[MemoryChunk] = field(default_factory=list)
    # Total tokens used
    total_tokens: int = 0
    # Number of memories truncated
    truncated_count: int = 0
    # Number of memories dropped entirely
    dropped_count: int = 0


class ContextOptimizer:
    """
    Optimizes retrieved memories for LLM context windows.
    
    Features:
    - Token-aware truncation
    - Relevance-based prioritization
    - Smart formatting with separators
    - Configurable strategies
    """
    
    def __init__(
        self,
        max_tokens: int = 4000,
        min_chunk_tokens: int = 50,
        separator: str = "\n---\n",
        include_metadata: bool = True,
    ) -> None:
        """
        Initialize the context optimizer.
        
        Args:
            max_tokens: Maximum tokens for the entire context
            min_chunk_tokens: Minimum tokens per chunk (below this, drop entirely)
            separator: String to separate memory chunks
            include_metadata: Whether to include timestamps and relevance
        """
        self.max_tokens = max_tokens
        self.min_chunk_tokens = min_chunk_tokens
        self.separator = separator
        self.include_metadata = include_metadata
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Count or estimate token count for text.
        
        Uses tiktoken for accurate counting when available,
        falls back to character-based estimation otherwise.
        
        Args:
            text: Input text
            
        Returns:
            Token count (accurate if tiktoken installed, estimated otherwise)
        """
        if not text:
            return 0
        
        encoder = _get_tiktoken_encoder()
        if encoder is not None:
            try:
                return len(encoder.encode(text))
            except Exception:
                pass
        
        # Fallback: character-based estimation
        return max(1, len(text) // CHARS_PER_TOKEN)
    
    @staticmethod
    def count_tokens_accurate(text: str) -> tuple[int, bool]:
        """
        Count tokens with accuracy indicator.
        
        Returns:
            Tuple of (token_count, is_accurate)
            is_accurate is True if tiktoken was used, False for estimation
        """
        if not text:
            return 0, True
        
        encoder = _get_tiktoken_encoder()
        if encoder is not None:
            try:
                return len(encoder.encode(text)), True
            except Exception:
                pass
        
        return max(1, len(text) // CHARS_PER_TOKEN), False
    
    @staticmethod
    def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
        """
        Truncate text to approximately fit within token limit.
        
        Args:
            text: Input text
            max_tokens: Maximum allowed tokens
            
        Returns:
            Tuple of (truncated_text, was_truncated)
        """
        estimated = ContextOptimizer.estimate_tokens(text)
        
        if estimated <= max_tokens:
            return text, False
        
        # Calculate target character count
        target_chars = max_tokens * CHARS_PER_TOKEN
        
        # Try to truncate at a sentence boundary
        truncated = text[:target_chars]
        
        # Find last sentence boundary
        last_period = truncated.rfind('. ')
        last_newline = truncated.rfind('\n')
        
        boundary = max(last_period, last_newline)
        
        if boundary > target_chars // 2:  # Only use boundary if it's not too early
            truncated = truncated[:boundary + 1]
        
        return truncated.strip() + "...", True
    
    def _format_chunk(
        self,
        content: str,
        relevance: float | None = None,
        created_at: str | None = None,
    ) -> str:
        """
        Format a memory chunk with optional metadata.
        
        Args:
            content: Memory content
            relevance: Relevance score (0-1)
            created_at: ISO timestamp
            
        Returns:
            Formatted string
        """
        if not self.include_metadata:
            return content
        
        parts = []
        
        if created_at:
            # Extract just the date part for brevity
            date_match = re.match(r'(\d{4}-\d{2}-\d{2})', created_at)
            if date_match:
                parts.append(f"[{date_match.group(1)}]")
        
        if relevance is not None:
            parts.append(f"({relevance:.0%})")
        
        if parts:
            header = " ".join(parts)
            return f"{header} {content}"
        
        return content
    
    def optimize(
        self,
        memories: list[dict[str, Any]],
        sort_by_relevance: bool = True,
    ) -> OptimizedContext:
        """
        Optimize memories for LLM context window.
        
        Args:
            memories: List of memory dicts with 'content', 'relevance', 'created_at'
            sort_by_relevance: Whether to sort by relevance before processing
            
        Returns:
            OptimizedContext with optimized chunks and context string
        """
        if not memories:
            return OptimizedContext(context="")
        
        # Sort by relevance if requested
        if sort_by_relevance:
            memories = sorted(
                memories,
                key=lambda m: m.get("relevance", 0),
                reverse=True,
            )
        
        result = OptimizedContext(context="")
        separator_tokens = self.estimate_tokens(self.separator)
        remaining_tokens = self.max_tokens
        
        for memory in memories:
            content = memory.get("content", "")
            relevance = memory.get("relevance", 0.0)
            created_at = memory.get("created_at")
            memory_id = memory.get("id", "unknown")
            
            if not content:
                continue
            
            # Format the chunk with metadata
            formatted = self._format_chunk(content, relevance, created_at)
            chunk_tokens = self.estimate_tokens(formatted)
            
            # Check if we need to account for separator
            needs_separator = len(result.chunks) > 0
            total_needed = chunk_tokens + (separator_tokens if needs_separator else 0)
            
            if total_needed > remaining_tokens:
                # Try to fit a truncated version
                available = remaining_tokens - (separator_tokens if needs_separator else 0)
                
                if available < self.min_chunk_tokens:
                    # Not enough room for even a minimal chunk
                    result.dropped_count += 1
                    continue
                
                truncated, was_truncated = self.truncate_to_tokens(formatted, available)
                
                if was_truncated:
                    result.truncated_count += 1
                
                chunk = MemoryChunk(
                    id=memory_id,
                    content=truncated,
                    relevance=relevance,
                    created_at=created_at,
                    tokens=self.estimate_tokens(truncated),
                    truncated=was_truncated,
                )
                result.chunks.append(chunk)
                result.total_tokens += chunk.tokens + (separator_tokens if needs_separator else 0)
                
                # Stop processing - we're at capacity
                result.dropped_count += len(memories) - memories.index(memory) - 1
                break
            
            # Full chunk fits
            chunk = MemoryChunk(
                id=memory_id,
                content=formatted,
                relevance=relevance,
                created_at=created_at,
                tokens=chunk_tokens,
                truncated=False,
            )
            result.chunks.append(chunk)
            
            remaining_tokens -= total_needed
            result.total_tokens += total_needed
        
        # Build the final context string
        context_parts = [chunk.content for chunk in result.chunks]
        result.context = self.separator.join(context_parts)
        
        log.debug(
            "context_optimized",
            input_count=len(memories),
            output_chunks=len(result.chunks),
            total_tokens=result.total_tokens,
            truncated=result.truncated_count,
            dropped=result.dropped_count,
        )
        
        return result
    
    def optimize_for_query(
        self,
        memories: list[dict[str, Any]],
        query: str,
        prioritize_recent: bool = False,
        recency_weight: float = 0.2,
    ) -> OptimizedContext:
        """
        Optimize memories with query-aware prioritization.
        
        This method reorders memories based on a combined score that
        considers relevance and optionally recency.
        
        Args:
            memories: List of memory dicts
            query: Original search query (for potential future enhancements)
            prioritize_recent: Whether to boost recent memories
            recency_weight: Weight for recency in combined score (0-1)
            
        Returns:
            OptimizedContext
        """
        if not memories:
            return OptimizedContext(context="")
        
        # Calculate combined scores
        scored_memories = []
        
        # Parse dates if prioritizing recent
        from datetime import datetime
        now = datetime.utcnow()
        max_age_days = 365  # Normalize over 1 year
        
        for memory in memories:
            relevance = memory.get("relevance", 0.0)
            
            if prioritize_recent and memory.get("created_at"):
                try:
                    created = datetime.fromisoformat(
                        memory["created_at"].replace("Z", "+00:00").split("+")[0]
                    )
                    age_days = (now - created).days
                    recency_score = max(0, 1 - (age_days / max_age_days))
                except (ValueError, AttributeError):
                    recency_score = 0.5  # Default for unparseable dates
                
                combined = (1 - recency_weight) * relevance + recency_weight * recency_score
            else:
                combined = relevance
            
            scored_memories.append({**memory, "combined_score": combined})
        
        # Sort by combined score
        scored_memories.sort(key=lambda m: m["combined_score"], reverse=True)
        
        # Use base optimize with pre-sorted memories
        return self.optimize(scored_memories, sort_by_relevance=False)
