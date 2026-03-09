"""
Remembra Memory Client

The main interface for storing and recalling memories.

Usage:
    from remembra import Memory
    
    # Self-hosted
    memory = Memory(
        base_url="http://localhost:8787",
        user_id="user_123"
    )
    
    # Store a memory
    result = memory.store("John is the CTO at Acme Corp")
    
    # Recall memories
    result = memory.recall("Who is John?")
    print(result.context)  # "John is the CTO at Acme Corp."
"""

from datetime import datetime
from typing import Any

import httpx

from remembra.client.types import (
    ChangelogIngestResult,
    ConversationIngestResult,
    EntityItem,
    ExtractedEntityItem,
    ExtractedFactItem,
    ForgetResult,
    IngestStatsItem,
    MemoryItem,
    RecallResult,
    StoreResult,
)


class MemoryError(Exception):
    """Base exception for Memory client errors."""
    
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Memory:
    """
    Remembra Memory Client.
    
    Provides a simple interface for storing and recalling memories
    from a Remembra server (self-hosted or cloud).
    
    Args:
        base_url: URL of the Remembra server (e.g., "http://localhost:8787")
        api_key: API key for cloud authentication (optional for self-hosted)
        user_id: Unique identifier for the user
        project: Project namespace (default: "default")
        timeout: Request timeout in seconds (default: 30)
    
    Example:
        >>> memory = Memory(base_url="http://localhost:8787", user_id="user_123")
        >>> memory.store("Alice works at TechCorp as a software engineer")
        StoreResult(id='01HQ...', extracted_facts=['Alice works at TechCorp...'], entities=[...])
        >>> memory.recall("Where does Alice work?")
        RecallResult(context='Alice works at TechCorp as a software engineer.', ...)
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        api_key: str | None = None,
        user_id: str = "default",
        project: str = "default",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.project = project
        self.timeout = timeout
        
        # Build headers
        self._headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "remembra-python/0.1.0",
        }
        if api_key:
            self._headers["X-API-Key"] = api_key

        # Persistent HTTP client (reuses TCP connections)
        self._client = httpx.Client(
            timeout=self.timeout,
            headers=self._headers,
        )

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the Remembra server."""
        url = f"{self.base_url}{path}"

        response = self._client.request(
            method=method,
            url=url,
            json=json,
            params=params,
        )

        if response.status_code >= 400:
            try:
                error_detail = response.json().get("detail", response.text)
            except Exception:
                error_detail = response.text
            raise MemoryError(
                f"Request failed: {error_detail}",
                status_code=response.status_code,
            )
        
        return response.json()
    
    def store(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        ttl: str | None = None,
    ) -> StoreResult:
        """
        Store a new memory.
        
        The content will be automatically processed to extract facts
        and entities, then stored for later recall.
        
        Args:
            content: The text content to memorize
            metadata: Optional key-value metadata to attach
            ttl: Optional time-to-live (e.g., "30d", "1y")
        
        Returns:
            StoreResult with the memory ID, extracted facts, and entities
        
        Example:
            >>> result = memory.store("John started as CEO of Acme Corp in 2024")
            >>> print(result.id)
            '01HQXYZ...'
            >>> print(result.extracted_facts)
            ['John started as CEO of Acme Corp in 2024.']
        """
        payload: dict[str, Any] = {
            "user_id": self.user_id,
            "project_id": self.project,
            "content": content,
            "metadata": metadata or {},
        }
        if ttl:
            payload["ttl"] = ttl
        
        data = self._request("POST", "/api/v1/memories", json=payload)
        
        entities = [
            EntityItem(
                id=e.get("id", ""),
                canonical_name=e.get("canonical_name", ""),
                type=e.get("type", ""),
                confidence=e.get("confidence", 0.0),
            )
            for e in data.get("entities", [])
        ]
        
        return StoreResult(
            id=data["id"],
            extracted_facts=data.get("extracted_facts", []),
            entities=entities,
        )
    
    def update(
        self,
        memory_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Update an existing memory's content.
        
        Re-extracts facts and entities from the new content.
        
        Args:
            memory_id: ID of the memory to update
            content: New content for the memory
            metadata: Optional metadata to update
            
        Returns:
            Dict with updated memory info including re-extracted entities
            
        Example:
            >>> result = memory.update("01HQXYZ...", "John is now CTO of Acme Corp")
        """
        payload: dict[str, Any] = {"content": content}
        if metadata is not None:
            payload["metadata"] = metadata
        return self._request("PATCH", f"/api/v1/memories/{memory_id}", json=payload)
    
    def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        List entities from the knowledge graph.
        
        Args:
            entity_type: Filter by type (person/company/location/concept)
            limit: Maximum entities to return
            
        Returns:
            Dict with list of entities and their relationships
        """
        params: dict[str, Any] = {"limit": limit}
        if entity_type:
            params["entity_type"] = entity_type
        return self._request("GET", "/api/v1/entities", params=params)
    
    def recall(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.70,
    ) -> RecallResult:
        """
        Recall memories relevant to a query.
        
        Performs semantic search across stored memories and returns
        the most relevant results along with a synthesized context string.
        
        Args:
            query: Natural language query
            limit: Maximum number of memories to return (1-50)
            threshold: Minimum relevance score (0.0-1.0)
        
        Returns:
            RecallResult with context string, matching memories, and entities
        
        Example:
            >>> result = memory.recall("What do I know about John?")
            >>> print(result.context)
            'John is the CEO of Acme Corp since 2024.'
            >>> print(len(result.memories))
            3
        """
        payload = {
            "user_id": self.user_id,
            "project_id": self.project,
            "query": query,
            "limit": limit,
            "threshold": threshold,
        }
        
        data = self._request("POST", "/api/v1/memories/recall", json=payload)
        
        memories = [
            MemoryItem(
                id=m["id"],
                content=m["content"],
                relevance=m["relevance"],
                created_at=datetime.fromisoformat(m["created_at"]),
            )
            for m in data.get("memories", [])
        ]
        
        entities = [
            EntityItem(
                id=e.get("id", ""),
                canonical_name=e.get("canonical_name", ""),
                type=e.get("type", ""),
                confidence=e.get("confidence", 0.0),
            )
            for e in data.get("entities", [])
        ]
        
        return RecallResult(
            context=data.get("context", ""),
            memories=memories,
            entities=entities,
        )
    
    def get(self, memory_id: str) -> dict[str, Any]:
        """
        Get a specific memory by ID.
        
        Args:
            memory_id: The memory ID to retrieve
        
        Returns:
            Memory data as a dictionary
        
        Raises:
            MemoryError: If memory not found (404)
        """
        return self._request("GET", f"/api/v1/memories/{memory_id}")
    
    def forget(
        self,
        memory_id: str | None = None,
        user_id: str | None = None,
        entity: str | None = None,
    ) -> ForgetResult:
        """
        Forget (delete) memories.
        
        GDPR-compliant deletion of memories. At least one filter
        must be provided.
        
        Args:
            memory_id: Delete a specific memory
            user_id: Delete all memories for a user (defaults to current user)
            entity: Delete all memories about an entity (coming soon)
        
        Returns:
            ForgetResult with counts of deleted items
        
        Example:
            >>> result = memory.forget(user_id="user_123")
            >>> print(f"Deleted {result.deleted_memories} memories")
        """
        params: dict[str, str] = {}
        
        if memory_id:
            params["memory_id"] = memory_id
        elif user_id:
            params["user_id"] = user_id
        elif entity:
            params["entity"] = entity
        else:
            # Default to current user
            params["user_id"] = self.user_id
        
        data = self._request("DELETE", "/api/v1/memories", params=params)
        
        return ForgetResult(
            deleted_memories=data.get("deleted_memories", 0),
            deleted_entities=data.get("deleted_entities", 0),
            deleted_relationships=data.get("deleted_relationships", 0),
        )
    
    def health(self) -> dict[str, Any]:
        """
        Check server health.
        
        Returns:
            Health status including version and component states
        """
        return self._request("GET", "/health")
    
    def ingest_changelog(
        self,
        content: str | None = None,
        file_path: str | None = None,
        project_name: str | None = None,
        max_releases: int = 20,
        skip_unreleased: bool = True,
    ) -> ChangelogIngestResult:
        """
        Ingest project history from a CHANGELOG.md.
        
        Each release in the changelog is stored as a separate memory
        with version and date metadata, making project history
        searchable and recallable.
        
        Args:
            content: Raw markdown content of the changelog
            file_path: Path to a CHANGELOG.md file (alternative to content)
            project_name: Human-readable project name for context
            max_releases: Maximum number of releases to ingest (default: 20)
            skip_unreleased: Skip the [Unreleased] section (default: True)
        
        Returns:
            ChangelogIngestResult with counts and memory IDs
        
        Example:
            >>> # From content
            >>> with open("CHANGELOG.md") as f:
            ...     result = memory.ingest_changelog(content=f.read(), project_name="MyProject")
            >>> print(f"Stored {result.memories_stored} releases")
            
            >>> # From file path (server-side)
            >>> result = memory.ingest_changelog(
            ...     file_path="/path/to/CHANGELOG.md",
            ...     project_name="MyProject"
            ... )
        """
        if not content and not file_path:
            raise ValueError("Either 'content' or 'file_path' must be provided")
        
        payload: dict[str, Any] = {
            "project_id": self.project,
            "max_releases": max_releases,
            "skip_unreleased": skip_unreleased,
        }
        
        if content:
            payload["content"] = content
        if file_path:
            payload["file_path"] = file_path
        if project_name:
            payload["project_name"] = project_name
        
        data = self._request("POST", "/api/v1/ingest/changelog", json=payload)
        
        return ChangelogIngestResult(
            releases_parsed=data.get("releases_parsed", 0),
            memories_stored=data.get("memories_stored", 0),
            memory_ids=data.get("memory_ids", []),
            errors=data.get("errors", []),
        )
    
    def ingest_conversation(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        extract_from: str = "both",
        min_importance: float = 0.5,
        dedupe: bool = True,
        store: bool = True,
        infer: bool = True,
    ) -> ConversationIngestResult:
        """
        Ingest a conversation and automatically extract memories.
        
        Processes a list of conversation messages and intelligently extracts
        facts worth remembering long-term. Includes deduplication against
        existing memories and entity extraction.
        
        This is the primary method for AI agents to add conversation context
        to persistent memory without manually calling store for each fact.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
                      Each message should have:
                      - role: "user" | "assistant" | "system"
                      - content: The message text
                      Optional fields:
                      - name: Speaker name (for multi-user chats)
                      - timestamp: ISO format datetime string
            session_id: Optional session ID for grouping related conversations
            extract_from: Which messages to extract from: "user", "assistant", or "both"
            min_importance: Minimum importance threshold (0.0-1.0, default: 0.5)
            dedupe: Enable deduplication against existing memories (default: True)
            store: If False, returns extraction without storing (dry run)
            infer: If False, stores raw messages without LLM extraction
        
        Returns:
            ConversationIngestResult with extracted facts, entities, and stats
        
        Example:
            >>> result = memory.ingest_conversation([
            ...     {"role": "user", "content": "My wife Suzan and I are planning a trip to Japan"},
            ...     {"role": "assistant", "content": "That sounds exciting! When are you going?"},
            ...     {"role": "user", "content": "We're thinking April next year"},
            ... ])
            >>> print(f"Extracted {result.stats.facts_extracted} facts")
            >>> print(f"Stored {result.stats.facts_stored} new memories")
            >>> for fact in result.facts:
            ...     print(f"- {fact.content} (importance: {fact.importance})")
        """
        payload: dict[str, Any] = {
            "messages": messages,
            "user_id": self.user_id,
            "session_id": session_id,
            "project_id": self.project,
            "options": {
                "extract_from": extract_from,
                "min_importance": min_importance,
                "dedupe": dedupe,
                "store": store,
                "infer": infer,
            },
        }
        
        data = self._request("POST", "/api/v1/ingest/conversation", json=payload)
        
        # Parse facts
        facts = [
            ExtractedFactItem(
                content=f.get("content", ""),
                confidence=f.get("confidence", 1.0),
                importance=f.get("importance", 0.5),
                source_message_index=f.get("source_message_index", 0),
                speaker=f.get("speaker"),
                stored=f.get("stored", False),
                memory_id=f.get("memory_id"),
                action=f.get("action", "add"),
                action_reason=f.get("action_reason"),
            )
            for f in data.get("facts", [])
        ]
        
        # Parse entities
        entities = [
            ExtractedEntityItem(
                name=e.get("name", ""),
                type=e.get("type", ""),
                relationship=e.get("relationship"),
            )
            for e in data.get("entities", [])
        ]
        
        # Parse stats
        stats_data = data.get("stats", {})
        stats = IngestStatsItem(
            messages_processed=stats_data.get("messages_processed", 0),
            facts_extracted=stats_data.get("facts_extracted", 0),
            facts_stored=stats_data.get("facts_stored", 0),
            facts_updated=stats_data.get("facts_updated", 0),
            facts_deduped=stats_data.get("facts_deduped", 0),
            facts_skipped=stats_data.get("facts_skipped", 0),
            entities_found=stats_data.get("entities_found", 0),
            processing_time_ms=stats_data.get("processing_time_ms", 0),
        )
        
        return ConversationIngestResult(
            status=data.get("status", "ok"),
            session_id=data.get("session_id"),
            facts=facts,
            entities=entities,
            stats=stats,
        )
    
    def close(self) -> None:
        """Close the persistent HTTP client."""
        self._client.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Memory(base_url='{self.base_url}', user_id='{self.user_id}', project='{self.project}')"
