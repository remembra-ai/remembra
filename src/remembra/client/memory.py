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
    EntityItem,
    ForgetResult,
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
            self._headers["Authorization"] = f"Bearer {api_key}"
    
    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the Remembra server."""
        url = f"{self.base_url}{path}"
        
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(
                method=method,
                url=url,
                headers=self._headers,
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
    
    def __repr__(self) -> str:
        return f"Memory(base_url='{self.base_url}', user_id='{self.user_id}', project='{self.project}')"
