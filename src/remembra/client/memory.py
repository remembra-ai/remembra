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

    # With Smart Auto-Forgetting (v0.12+)
    memory = Memory(
        base_url="http://localhost:8787",
        user_id="user_123",
        auto_expire_temporal=True,  # Auto-detect temporal phrases
    )
    # This memory will auto-expire after ~36 hours
    memory.store("Meeting tomorrow at 3pm with John")

    # With Shadow TTL Cache (v0.12+)
    memory = Memory(
        base_url="http://localhost:8787",
        user_id="user_123",
        enable_shadow_ttl=True,  # Cache TTLs client-side for lower latency
    )
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from remembra.client.shadow_ttl import ShadowTTLCache, parse_ttl_string
from remembra.client.temporal_parser import TemporalParser
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

if TYPE_CHECKING:
    pass


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
        auto_expire_temporal: Auto-detect temporal phrases and set TTL (v0.12+)
        temporal_min_confidence: Minimum confidence for temporal detection (0.0-1.0)
        enable_shadow_ttl: Enable client-side TTL caching for lower latency (v0.12+)
        shadow_ttl_max_entries: Maximum entries in shadow TTL cache

    Example:
        >>> memory = Memory(base_url="http://localhost:8787", user_id="user_123")
        >>> memory.store("Alice works at TechCorp as a software engineer")
        StoreResult(id='01HQ...', extracted_facts=['Alice works at TechCorp...'], entities=[...])
        >>> memory.recall("Where does Alice work?")
        RecallResult(context='Alice works at TechCorp as a software engineer.', ...)

        # Smart Auto-Forgetting
        >>> memory = Memory(base_url="http://localhost:8787", auto_expire_temporal=True)
        >>> memory.store("Meeting tomorrow at 3pm")  # Auto-expires in ~36 hours
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        api_key: str | None = None,
        user_id: str = "default",
        project: str = "default",
        timeout: float = 30.0,
        # v0.12 features
        auto_expire_temporal: bool = False,
        temporal_min_confidence: float = 0.6,
        enable_shadow_ttl: bool = False,
        shadow_ttl_max_entries: int = 10000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.project = project
        self.timeout = timeout

        # v0.12: Smart Auto-Forgetting
        self._auto_expire_temporal = auto_expire_temporal
        self._temporal_parser: TemporalParser | None = None
        if auto_expire_temporal:
            self._temporal_parser = TemporalParser(
                min_confidence=temporal_min_confidence,
            )

        # v0.12: Shadow TTL Cache
        self._enable_shadow_ttl = enable_shadow_ttl
        self._shadow_cache: ShadowTTLCache | None = None
        if enable_shadow_ttl:
            self._shadow_cache = ShadowTTLCache(
                max_entries=shadow_ttl_max_entries,
            )

        # Build headers
        self._headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "remembra-python/0.12.0",
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
        auto_expire: bool | None = None,
    ) -> StoreResult:
        """
        Store a new memory.

        The content will be automatically processed to extract facts
        and entities, then stored for later recall.

        Args:
            content: The text content to memorize
            metadata: Optional key-value metadata to attach
            ttl: Optional time-to-live (e.g., "30d", "1y")
            auto_expire: Override auto_expire_temporal for this call (v0.12+)

        Returns:
            StoreResult with the memory ID, extracted facts, and entities

        Example:
            >>> result = memory.store("John started as CEO of Acme Corp in 2024")
            >>> print(result.id)
            '01HQXYZ...'
            >>> print(result.extracted_facts)
            ['John started as CEO of Acme Corp in 2024.']

            # Smart Auto-Forgetting (when auto_expire_temporal=True)
            >>> result = memory.store("Meeting tomorrow at 3pm")
            # Memory auto-expires in ~36 hours
        """
        # v0.12: Smart Auto-Forgetting
        # Detect temporal phrases and auto-set TTL if no explicit TTL provided
        effective_ttl = ttl
        detected_temporal = None

        should_auto_expire = auto_expire if auto_expire is not None else self._auto_expire_temporal

        if should_auto_expire and effective_ttl is None and self._temporal_parser:
            detected_temporal = self._temporal_parser.detect(content)
            if detected_temporal:
                effective_ttl = detected_temporal.ttl_string

        payload: dict[str, Any] = {
            "user_id": self.user_id,
            "project_id": self.project,
            "content": content,
            "metadata": metadata or {},
        }
        if effective_ttl:
            payload["ttl"] = effective_ttl

        data = self._request("POST", "/api/v1/memories", json=payload)

        memory_id = data["id"]

        # v0.12: Register TTL in shadow cache for latency optimization
        if self._shadow_cache is not None and effective_ttl:
            ttl_seconds = parse_ttl_string(effective_ttl)
            if ttl_seconds:
                self._shadow_cache.register(memory_id, ttl_seconds=ttl_seconds)

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
            id=memory_id,
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
        filters: dict[str, str] | None = None,
    ) -> RecallResult:
        """
        Recall memories relevant to a query.

        Performs semantic search across stored memories and returns
        the most relevant results along with a synthesized context string.

        Args:
            query: Natural language query
            limit: Maximum number of memories to return (1-50)
            threshold: Minimum relevance score (0.0-1.0)
            filters: Optional metadata filters (AND-combined exact-match),
                     e.g. {"project": "trademind", "type": "deploy-config"}.

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
        if filters:
            payload["filters"] = filters

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
            # v0.12: Invalidate shadow cache for deleted memory
            if self._shadow_cache is not None:
                self._shadow_cache.invalidate(memory_id)
        elif user_id:
            params["user_id"] = user_id
            # Clear entire cache when bulk deleting by user
            if self._shadow_cache is not None:
                self._shadow_cache.clear()
        elif entity:
            params["entity"] = entity
            # Clear cache on entity deletion (conservative)
            if self._shadow_cache is not None:
                self._shadow_cache.clear()
        else:
            # Default to current user
            params["user_id"] = self.user_id
            if self._shadow_cache is not None:
                self._shadow_cache.clear()

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

    def list(
        self,
        limit: int = 20,
        offset: int = 0,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        List stored memories (chronological, not semantically ranked).

        Use this for browsing / pagination. For query-driven retrieval
        use ``recall()`` instead.

        Args:
            limit: Maximum memories to return (1-100).
            offset: Pagination offset.
            project_id: Optional project namespace. When None, lists across
                all projects owned by the authenticated user.

        Returns:
            List of memory summary dicts (id, content, created_at, ...).
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if project_id is not None:
            params["project_id"] = project_id
        result = self._request("GET", "/api/v1/memories", params=params)
        # Server returns a JSON array, which _request returns verbatim.
        return result if isinstance(result, list) else []

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
        """Close the persistent HTTP client and clean up resources."""
        self._client.close()
        # Clear shadow cache on close
        if self._shadow_cache is not None:
            self._shadow_cache.clear()

    # -------------------------------------------------------------------------
    # v0.12: Shadow TTL Cache Methods
    # -------------------------------------------------------------------------

    def is_memory_valid(self, memory_id: str) -> bool | None:
        """
        Check if a memory is known to be valid using shadow TTL cache.

        This method checks the local TTL cache without making a server
        request. Useful for optimizing update/delete operations.

        Args:
            memory_id: The memory ID to check

        Returns:
            True if cached and valid, False if cached and expired,
            None if not in cache (unknown)

        Note:
            Requires enable_shadow_ttl=True on client initialization.
        """
        if self._shadow_cache is None:
            return None

        if memory_id not in self._shadow_cache:
            return None

        return self._shadow_cache.is_valid(memory_id)

    def shadow_cache_stats(self) -> dict[str, int | float] | None:
        """
        Get statistics from the shadow TTL cache.

        Returns:
            Dict with entry_count, valid_count, expired_count, max_entries.
            Returns None if shadow cache is disabled.
        """
        if self._shadow_cache is None:
            return None
        return self._shadow_cache.stats()

    def clear_shadow_cache(self) -> int:
        """
        Clear all entries from the shadow TTL cache.

        Returns:
            Number of entries cleared, or 0 if cache disabled.
        """
        if self._shadow_cache is None:
            return 0
        return self._shadow_cache.clear()

    # -------------------------------------------------------------------------
    # v0.12: Temporal Detection Methods
    # -------------------------------------------------------------------------

    def detect_temporal(self, content: str) -> dict[str, Any] | None:
        """
        Detect temporal phrases in content and get suggested TTL.

        This method can be used to preview what TTL would be auto-set
        before actually storing a memory.

        Args:
            content: Text content to analyze

        Returns:
            Dict with phrase, ttl_string, ttl_seconds, confidence, reason.
            Returns None if no temporal phrase detected or feature disabled.

        Example:
            >>> memory = Memory(auto_expire_temporal=True)
            >>> memory.detect_temporal("Meeting tomorrow at 3pm")
            {'phrase': 'tomorrow at 3pm', 'ttl_string': '36h',
             'ttl_seconds': 129600, 'confidence': 0.75,
             'reason': 'reference to tomorrow'}
        """
        if self._temporal_parser is None:
            return None

        detection = self._temporal_parser.detect(content)
        if detection is None:
            return None

        return {
            "phrase": detection.phrase,
            "ttl_string": detection.ttl_string,
            "ttl_seconds": detection.ttl_seconds,
            "confidence": detection.confidence,
            "reason": detection.reason,
            "granularity": detection.granularity.value,
        }

    def __enter__(self) -> Memory:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        features = []
        if self._auto_expire_temporal:
            features.append("auto_expire_temporal")
        if self._enable_shadow_ttl:
            features.append("shadow_ttl")
        feature_str = f", features=[{', '.join(features)}]" if features else ""
        return f"Memory(base_url='{self.base_url}', user_id='{self.user_id}', project='{self.project}'{feature_str})"
