"""
CrewAI integration for Remembra.

Provides RemembraStorage that implements CrewAI's storage interface,
enabling Remembra as the memory backend for CrewAI agents.

Usage:
    from crewai import Crew, Agent, Task
    from remembra.integrations.crewai import RemembraStorage

    storage = RemembraStorage(
        base_url="http://localhost:8787",
        user_id="crew_user",
    )

    crew = Crew(
        agents=[...],
        tasks=[...],
        memory=True,
        short_term_memory=ShortTermMemory(storage=storage),
        long_term_memory=LongTermMemory(storage=storage),
        entity_memory=EntityMemory(storage=storage),
    )

Requires: pip install remembra crewai
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from typing import Any

from remembra.client.memory import Memory, MemoryError


class RemembraStorage:
    """CrewAI-compatible storage backend powered by Remembra.

    Implements the storage interface expected by CrewAI's Memory classes
    (ShortTermMemory, LongTermMemory, EntityMemory). All data is stored
    in Remembra with full entity resolution and hybrid search.

    Args:
        base_url: Remembra server URL.
        user_id: User ID for memory isolation.
        project: Project namespace.
        api_key: API key for authentication.
        type: Memory type label ("short_term", "long_term", "entity").
        timeout: Request timeout in seconds.

    Example:
        >>> from crewai.memory import ShortTermMemory
        >>> storage = RemembraStorage(
        ...     base_url="http://localhost:8787",
        ...     user_id="crew_agent",
        ...     type="short_term",
        ... )
        >>> memory = ShortTermMemory(storage=storage)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        user_id: str = "default",
        project: str = "default",
        api_key: str | None = None,
        type: str = "short_term",
        timeout: float = 30.0,
    ) -> None:
        self._client = Memory(
            base_url=base_url,
            api_key=api_key,
            user_id=user_id,
            project=project,
            timeout=timeout,
        )
        self._type = type

    def save(self, value: Any, metadata: dict[str, Any] | None = None) -> None:
        """Save a value to Remembra.

        Handles all CrewAI memory item types:
        - ShortTermMemoryItem: stores data with agent/metadata
        - LongTermMemoryItem: stores task results with quality scores
        - EntityMemoryItem: stores entity descriptions with relationships
        - Raw strings/dicts: stored directly
        """
        metadata = metadata or {}
        metadata["memory_type"] = self._type
        metadata["stored_at"] = datetime.now().isoformat()

        content, extra_metadata = _extract_content(value)
        metadata.update(extra_metadata)

        # Determine TTL based on memory type
        ttl = None
        if self._type == "short_term":
            ttl = "24h"  # Short-term expires after 24 hours

        try:
            # skip_extraction: each CrewAI memory item (a task result, an
            # entity description, a short-term observation) is a distinct record
            # stored 1:1 — never fact-split or merged/deduped into another item.
            # Without this, distinct items get consolidated away and lost.
            self._client.store(
                content=content,
                metadata=metadata,
                ttl=ttl,
                skip_extraction=True,
            )
        except MemoryError:
            pass  # Don't break the crew pipeline

    async def asave(self, value: Any, metadata: dict[str, Any] | None = None) -> None:
        """Async version of save (delegates to sync for now)."""
        self.save(value, metadata)

    def search(
        self,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.35,
    ) -> list[dict[str, Any]]:
        """Search Remembra for relevant memories.

        Returns results in the format CrewAI expects:
        list of dicts with 'context', 'metadata', and 'score' keys.
        """
        try:
            # Filter to THIS storage's memory type. CrewAI's short-term,
            # long-term, and entity memories share one (user, project)
            # namespace; without the filter a short-term search would also
            # return long-term and entity items and vice versa.
            result = self._client.recall(
                query=query,
                limit=limit,
                threshold=score_threshold,
                filters={"memory_type": self._type},
            )

            return [
                {
                    "context": m.content,
                    "metadata": {
                        "memory_id": m.id,
                        "memory_type": self._type,
                        **(m.metadata or {}),
                    },
                    "score": m.relevance,
                }
                for m in result.memories
            ]
        except MemoryError:
            return []

    async def asearch(
        self,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.35,
    ) -> list[dict[str, Any]]:
        """Async version of search (delegates to sync for now)."""
        return self.search(query, limit, score_threshold)

    def reset(self) -> None:
        """Clear only THIS storage's memories (its memory type).

        Must never wipe the user's other memories. The previous version called
        forget(user_id=...), which deleted ALL of the user's memory — including
        the crew's other memory types and anything else stored under that user.
        Instead, recall this type's memories by metadata filter and delete each,
        looping until empty (recall is capped at 50 with no offset).
        """
        with contextlib.suppress(MemoryError):
            for _ in range(200):  # safety bound
                result = self._client.recall(
                    filters={"memory_type": self._type},
                    limit=50,
                )
                if not result.memories:
                    break
                for m in result.memories:
                    with contextlib.suppress(MemoryError):
                        self._client.forget(memory_id=m.id)


def _extract_content(value: Any) -> tuple[str, dict[str, Any]]:
    """Extract content string and metadata from a CrewAI memory item.

    Returns:
        Tuple of (content_string, extra_metadata)
    """
    metadata: dict[str, Any] = {}

    # ShortTermMemoryItem
    if hasattr(value, "data") and hasattr(value, "agent"):
        content = str(value.data)
        if value.agent:
            metadata["agent"] = value.agent
        if hasattr(value, "metadata") and value.metadata:
            metadata.update(value.metadata)
        return content, metadata

    # LongTermMemoryItem
    if hasattr(value, "task") and hasattr(value, "expected_output"):
        content = f"Task: {value.task}\nOutput: {value.expected_output}"
        if hasattr(value, "agent") and value.agent:
            metadata["agent"] = value.agent
        if hasattr(value, "quality") and value.quality is not None:
            metadata["quality"] = value.quality
        if hasattr(value, "datetime") and value.datetime:
            metadata["task_datetime"] = value.datetime
        if hasattr(value, "metadata") and value.metadata:
            metadata.update(value.metadata)
        return content, metadata

    # EntityMemoryItem
    if hasattr(value, "name") and hasattr(value, "description") and hasattr(value, "relationships"):
        content = f"Entity: {value.name} ({value.type})\nDescription: {value.description}\nRelationships: {value.relationships}"
        metadata["entity_name"] = value.name
        metadata["entity_type"] = value.type
        if hasattr(value, "metadata") and value.metadata:
            metadata.update(value.metadata)
        return content, metadata

    # List of items — concatenate
    if isinstance(value, list):
        parts = []
        combined_metadata: dict[str, Any] = {}
        for item in value:
            c, m = _extract_content(item)
            parts.append(c)
            combined_metadata.update(m)
        return "\n---\n".join(parts), combined_metadata

    # Raw string
    if isinstance(value, str):
        return value, metadata

    # Dict
    if isinstance(value, dict):
        return json.dumps(value, indent=2), metadata

    # Fallback
    return str(value), metadata
