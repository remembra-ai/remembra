"""
Remembra MCP Server

Standalone MCP server that wraps the Remembra Python SDK,
exposing memory operations as tools for AI assistants.

Supports:
  - stdio transport (Claude Desktop, Claude Code, Cursor)
  - SSE transport (remote/networked connections)
  - streamable-http transport

Configuration via environment variables:
  REMEMBRA_URL        - Remembra server URL (default: http://localhost:8787)
  REMEMBRA_API_KEY    - API key for authentication
  REMEMBRA_USER_ID    - User ID for memory operations (default: "default")
  REMEMBRA_PROJECT    - Project namespace (default: "default")
  REMEMBRA_MCP_TRANSPORT - Transport: "stdio" | "sse" | "streamable-http" (default: "stdio")
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from remembra.client.memory import Memory, MemoryError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REMEMBRA_URL = os.environ.get("REMEMBRA_URL", "http://localhost:8787")
REMEMBRA_API_KEY = os.environ.get("REMEMBRA_API_KEY", "")
REMEMBRA_USER_ID = os.environ.get("REMEMBRA_USER_ID", "default")
REMEMBRA_PROJECT = os.environ.get("REMEMBRA_PROJECT", "default")
REMEMBRA_MCP_TRANSPORT = os.environ.get("REMEMBRA_MCP_TRANSPORT", "stdio")

# ---------------------------------------------------------------------------
# Memory client (lazy-initialized)
# ---------------------------------------------------------------------------

_client: Memory | None = None


def _get_client() -> Memory:
    """Get or create the Memory client singleton."""
    global _client
    if _client is None:
        _client = Memory(
            base_url=REMEMBRA_URL,
            api_key=REMEMBRA_API_KEY or None,
            user_id=REMEMBRA_USER_ID,
            project=REMEMBRA_PROJECT,
        )
    return _client


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="remembra",
    instructions=(
        "Remembra is a persistent memory layer for AI. "
        "Use store_memory to save important facts, decisions, and context. "
        "Use recall_memories BEFORE answering questions about past conversations, "
        "people, projects, or decisions. Memories persist across sessions."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Store Memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def store_memory(
    content: str,
    metadata: dict[str, Any] | None = None,
    ttl: str | None = None,
) -> str:
    """Store information in persistent memory.

    Use this to save important facts, decisions, context, and notes that
    should persist across sessions. The content is automatically processed
    to extract entities (people, organizations, locations) and facts.

    Args:
        content: The text content to memorize. Can be facts, context,
                 decisions, meeting notes, preferences, etc.
        metadata: Optional key-value metadata to attach (e.g., {"source": "meeting", "date": "2024-01-15"}).
        ttl: Optional time-to-live. Examples: "24h" (session), "7d" (week),
             "30d" (month), "1y" (year), or omit for permanent storage.

    Returns:
        JSON string with memory ID, extracted facts, and detected entities.
    """
    try:
        client = _get_client()
        result = client.store(content=content, metadata=metadata, ttl=ttl)

        return json.dumps(
            {
                "status": "stored",
                "id": result.id,
                "extracted_facts": result.extracted_facts,
                "entities": [
                    {
                        "name": e.canonical_name,
                        "type": e.type,
                        "confidence": e.confidence,
                    }
                    for e in result.entities
                ],
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Recall Memories",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def recall_memories(
    query: str,
    limit: int = 5,
    threshold: float = 0.4,
    slim: bool = False,
) -> str:
    """Search persistent memory for relevant information.

    Use this BEFORE answering questions about past decisions, context,
    people, projects, or anything that might have been discussed previously.
    Performs hybrid search (semantic + keyword) across all stored memories.

    Args:
        query: Natural language query. Can be a question or keywords.
               Examples: "What did we decide about the API design?",
               "Alice project preferences", "meeting notes from last week".
        limit: Maximum number of memories to return (1-50, default: 5).
        threshold: Minimum relevance score 0.0-1.0 (default: 0.4).
                   Lower = more results but less relevant.
        slim: If True, returns only the synthesized context string (90% smaller payload).
              Use slim=True when you only need the context, not individual memories.

    Returns:
        JSON string with synthesized context, matching memories, and entities.
        In slim mode, returns only: {"status": "ok", "context": "...", "count": N}
    """
    try:
        client = _get_client()
        result = client.recall(query=query, limit=limit, threshold=threshold)

        # Slim mode: return just context (90% payload reduction)
        if slim:
            return json.dumps(
                {
                    "status": "ok",
                    "context": result.context,
                    "count": len(result.memories),
                },
                indent=2,
            )

        # Full mode: return everything
        return json.dumps(
            {
                "status": "ok",
                "context": result.context,
                "count": len(result.memories),
                "memories": [
                    {
                        "id": m.id,
                        "content": m.content,
                        "relevance": round(m.relevance, 3),
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in result.memories
                ],
                "entities": [
                    {
                        "name": e.canonical_name,
                        "type": e.type,
                    }
                    for e in result.entities
                ],
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Forget Memories",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def forget_memories(
    memory_id: str | None = None,
    entity: str | None = None,
    all_memories: bool = False,
) -> str:
    """Delete memories from persistent storage.

    GDPR-compliant deletion. Provide exactly one of: memory_id (delete specific),
    entity (delete all about an entity), or all_memories=true (delete everything).

    Args:
        memory_id: Delete a specific memory by its ID.
        entity: Delete all memories about a specific entity (person, org, etc).
        all_memories: Set to true to delete ALL memories. Use with extreme caution!

    Returns:
        JSON string with deletion counts.
    """
    try:
        client = _get_client()

        if not memory_id and not entity and not all_memories:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Must specify memory_id, entity, or all_memories=true",
                }
            )

        if all_memories:
            result = client.forget(user_id=client.user_id)
        elif memory_id:
            result = client.forget(memory_id=memory_id)
        elif entity:
            result = client.forget(entity=entity)
        else:
            return json.dumps({"status": "error", "error": "No deletion target specified"})

        return json.dumps(
            {
                "status": "deleted",
                "deleted_memories": result.deleted_memories,
                "deleted_entities": result.deleted_entities,
                "deleted_relationships": result.deleted_relationships,
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Health Check",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def health_check() -> str:
    """Check Remembra server health and connection status.

    Use this to verify the memory server is running and accessible.
    Returns server version, status, and component health.

    Returns:
        JSON string with health status details.
    """
    try:
        client = _get_client()
        health = client.health()

        return json.dumps(
            {
                "status": "ok",
                "server": client.base_url,
                "health": health,
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps(
            {
                "status": "error",
                "server": REMEMBRA_URL,
                "error": str(e),
                "code": e.status_code,
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "status": "error",
                "server": REMEMBRA_URL,
                "error": str(e),
            }
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Ingest Conversation",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def ingest_conversation(
    messages: list[dict[str, Any]],
    session_id: str | None = None,
    min_importance: float = 0.5,
    extract_from: str = "both",
    store: bool = True,
) -> str:
    """Ingest a conversation and automatically extract memories.

    Processes a list of conversation messages and intelligently extracts
    facts worth remembering long-term. Includes deduplication against
    existing memories and entity extraction.

    This is the primary method for AI agents to add conversation context
    to persistent memory without manually calling store for each fact.

    Args:
        messages: List of message dicts with 'role' and 'content'.
                  Optional: 'name' (speaker name), 'timestamp' (ISO format).
                  Example: [{"role": "user", "content": "I work at Google"}]
        session_id: Optional session ID for grouping related conversations.
        min_importance: Minimum importance threshold (0.0-1.0, default: 0.5).
                        Facts below this threshold are filtered out.
        extract_from: Which messages to extract from: "user", "assistant", or "both".
        store: If False, returns extraction results without storing (dry run).

    Returns:
        JSON string with extracted facts, entities, deduplication results, and stats.
    """
    try:
        client = _get_client()

        # Use the SDK's ingest_conversation method (reuses persistent HTTP client)
        result = client.ingest_conversation(
            messages=messages,
            session_id=session_id,
            min_importance=min_importance,
            extract_from=extract_from,
            store=store,
        )

        return json.dumps(
            {
                "status": result.status,
                "session_id": result.session_id,
                "facts_extracted": result.stats.facts_extracted,
                "facts_stored": result.stats.facts_stored,
                "facts_deduped": result.stats.facts_deduped,
                "entities_found": result.stats.entities_found,
                "processing_time_ms": result.stats.processing_time_ms,
                "facts": [
                    {
                        "content": f.content,
                        "importance": f.importance,
                        "speaker": f.speaker,
                        "action": f.action,
                        "stored": f.stored,
                    }
                    for f in result.facts
                ],
                "entities": [
                    {
                        "name": e.name,
                        "type": e.type,
                    }
                    for e in result.entities
                ],
            },
            indent=2,
        )

    except MemoryError as e:
        return json.dumps(
            {
                "status": "error",
                "code": e.status_code,
                "error": str(e),
            }
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def update_memory(
    memory_id: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Update an existing memory's content.

    Re-extracts facts and entities from the new content. Use this to
    correct or update previously stored information without deleting
    and recreating.

    Args:
        memory_id: ID of the memory to update.
        content: New content for the memory.
        metadata: Optional metadata to update or add.

    Returns:
        JSON string with status, updated memory ID, and re-extracted entities.
    """
    try:
        client = _get_client()
        result = client.update(memory_id=memory_id, content=content, metadata=metadata)

        return json.dumps(
            {
                "status": "updated",
                "id": result.get("id", memory_id),
                "updated_entities": result.get("entities", []),
                "extracted_facts": result.get("extracted_facts", []),
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Entities",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def search_entities(
    query: str | None = None,
    entity_type: str | None = None,
    limit: int = 20,
) -> str:
    """Search the entity graph.

    Find people, companies, locations, and concepts that Remembra knows about.
    Use this to explore the knowledge graph and find related information.

    Args:
        query: Optional filter by entity name (case-insensitive partial match).
        entity_type: Filter by type: "person", "organization", "location", "concept".
        limit: Maximum entities to return (default: 20, max: 100).

    Returns:
        JSON string with entities (name, type, aliases, relationship count).
    """
    try:
        client = _get_client()
        result = client.list_entities(entity_type=entity_type, limit=min(limit, 100))

        entities = result.get("entities", [])

        # Filter by query name if provided
        if query:
            query_lower = query.lower()
            entities = [
                e for e in entities
                if query_lower in e.get("canonical_name", "").lower()
                or any(query_lower in alias.lower() for alias in e.get("aliases", []))
            ]

        return json.dumps(
            {
                "status": "ok",
                "count": len(entities),
                "entities": [
                    {
                        "id": e.get("id"),
                        "name": e.get("canonical_name"),
                        "type": e.get("type"),
                        "aliases": e.get("aliases", []),
                        "memory_count": e.get("memory_count", 0),
                    }
                    for e in entities[:limit]
                ],
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Memories",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def list_memories(
    limit: int = 10,
    project_id: str | None = None,
) -> str:
    """Browse stored memories.

    Returns recent memories without requiring a search query. Useful for
    checking what has been stored or getting an overview of memory contents.

    Args:
        limit: Maximum memories to return (1-50, default: 10).
        project_id: Optional project namespace to filter by.

    Returns:
        JSON string with memories (id, content snippet, created_at).
    """
    try:
        client = _get_client()
        
        # Use recall with broad query and zero threshold to get recent memories
        result = client.recall(
            query="recent memories",
            limit=min(limit, 50),
            threshold=0.0,
        )

        return json.dumps(
            {
                "status": "ok",
                "count": len(result.memories),
                "memories": [
                    {
                        "id": m.id,
                        "content": m.content[:200] + ("..." if len(m.content) > 200 else ""),
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in result.memories
                ],
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Share Memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def share_memory(
    memory_id: str,
    space_id: str,
) -> str:
    """Share a memory to a collaborative space.

    Enables cross-agent memory sharing. Add a memory to a shared space
    where other agents/users can access it. Requires Spaces to be enabled.

    Args:
        memory_id: ID of the memory to share.
        space_id: ID of the target space.

    Returns:
        JSON string with share status.
    """
    try:
        client = _get_client()
        
        # Call the spaces API to add memory
        client._request(
            "POST",
            f"/api/v1/spaces/{space_id}/memories",
            json={"memory_id": memory_id},
        )
        
        return json.dumps(
            {
                "status": "shared",
                "memory_id": memory_id,
                "space_id": space_id,
                "message": "Memory shared to space successfully",
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Timeline",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def timeline(
    entity_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> str:
    """Browse memories on a timeline.

    Temporal browsing of memories, optionally filtered by entity.
    Enables queries like "What happened with Alice in January?"

    Args:
        entity_name: Optional filter by entity (person, org, etc).
        start_date: Start of time range (ISO format, e.g., "2024-01-01").
        end_date: End of time range (ISO format, e.g., "2024-02-01").
        limit: Maximum memories to return (default: 20).

    Returns:
        JSON string with chronological list of memories.
    """
    try:
        client = _get_client()
        
        # Build query based on entity and date range
        query_parts = []
        if entity_name:
            query_parts.append(entity_name)
        if start_date:
            query_parts.append(f"after {start_date}")
        if end_date:
            query_parts.append(f"before {end_date}")
        
        query = " ".join(query_parts) if query_parts else "timeline recent"
        
        result = client.recall(query=query, limit=limit, threshold=0.0)
        
        # Sort by created_at for chronological order
        memories = sorted(result.memories, key=lambda m: m.created_at)
        
        return json.dumps(
            {
                "status": "ok",
                "count": len(memories),
                "entity_filter": entity_name,
                "date_range": {
                    "start": start_date,
                    "end": end_date,
                },
                "memories": [
                    {
                        "id": m.id,
                        "content": m.content,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in memories
                ],
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Relationships At",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def relationships_at(
    entity_name: str,
    as_of: str | None = None,
    relationship_type: str | None = None,
    include_history: bool = False,
) -> str:
    """Query relationships at a specific point in time.

    Enables temporal queries like "Where did Alice work in January 2022?"
    or "Who was Bob married to in 2019?" This is Remembra's unique feature
    for bi-temporal relationship tracking.

    Args:
        entity_name: Name of the entity to query relationships for.
        as_of: Point in time (ISO format, e.g., "2022-01-15"). 
               If omitted, returns current relationships.
        relationship_type: Filter by type (WORKS_AT, SPOUSE_OF, ROLE, etc).
        include_history: If true, includes superseded relationships.

    Returns:
        JSON with relationships valid at the specified time.
    """
    try:
        client = _get_client()
        
        # Build the API request for temporal relationship query
        params = {
            "entity_name": entity_name,
            "include_history": include_history,
        }
        if as_of:
            params["as_of"] = as_of
        if relationship_type:
            params["relationship_type"] = relationship_type
        
        # Call the relationships endpoint
        result = client._request(
            "GET",
            "/api/v1/entities/relationships",
            params=params,
        )
        
        relationships = result.get("relationships", [])
        
        return json.dumps(
            {
                "status": "ok",
                "entity": entity_name,
                "as_of": as_of or "current",
                "count": len(relationships),
                "relationships": [
                    {
                        "id": r.get("id"),
                        "type": r.get("type"),
                        "from": r.get("from_entity_name"),
                        "to": r.get("to_entity_name"),
                        "valid_from": r.get("valid_from"),
                        "valid_to": r.get("valid_to"),
                        "is_current": r.get("valid_to") is None,
                        "superseded_by": r.get("superseded_by"),
                    }
                    for r in relationships
                ],
            },
            indent=2,
        )
    except MemoryError as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.status_code})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt(
    name="recall-context",
    title="Recall Context",
    description="Recall recent context for session continuity. Use at the start of a conversation to restore memory.",
)
def recall_context_prompt() -> list[dict[str, str]]:
    """Prompt to recall recent context at session start."""
    return [
        {
            "role": "user",
            "content": (
                "Recall any relevant context from our previous conversations. "
                "What do you remember about me, my projects, preferences, and recent discussions?"
            ),
        }
    ]


@mcp.prompt(
    name="store-summary",
    title="Store Session Summary",
    description="Store a summary of the current session for cross-session continuity.",
)
def store_summary_prompt(session_topic: str = "this conversation") -> list[dict[str, str]]:
    """Prompt to store a session summary."""
    return [
        {
            "role": "user",
            "content": (
                f"Summarize the key points, decisions, and action items from {session_topic}. "
                "Store this summary in memory so we can continue seamlessly next time."
            ),
        }
    ]


@mcp.prompt(
    name="setup-check",
    title="Verify Connection",
    description="Verify Remembra connection and run health check.",
)
def setup_check_prompt() -> list[dict[str, str]]:
    """Prompt to verify Remembra setup."""
    return [
        {
            "role": "user",
            "content": (
                "Run a health check on the Remembra memory server. "
                "Confirm the connection is working and show me the server status."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("memory://recent")
def recent_memories() -> str:
    """Recent memories stored in Remembra.

    Returns the 10 most recently stored memories for quick context.
    """
    try:
        client = _get_client()
        # Use recall with a broad query to get recent items
        result = client.recall(query="*", limit=10, threshold=0.0)

        memories = [
            {
                "id": m.id,
                "content": m.content,
                "relevance": round(m.relevance, 3),
                "created_at": m.created_at.isoformat(),
            }
            for m in result.memories
        ]

        return json.dumps(
            {
                "count": len(memories),
                "memories": memories,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.resource("memory://status")
def memory_status() -> str:
    """Remembra server status and connection info.

    Returns current configuration and server health.
    """
    try:
        client = _get_client()
        health = client.health()

        return json.dumps(
            {
                "server": client.base_url,
                "user_id": client.user_id,
                "project": client.project,
                "health": health,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps(
            {
                "server": REMEMBRA_URL,
                "user_id": REMEMBRA_USER_ID,
                "project": REMEMBRA_PROJECT,
                "error": str(e),
            }
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Remembra MCP server."""
    transport = REMEMBRA_MCP_TRANSPORT.lower()

    if transport not in ("stdio", "sse", "streamable-http"):
        print(
            f"Invalid transport: {transport}. Use 'stdio', 'sse', or 'streamable-http'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate configuration
    if not REMEMBRA_URL:
        print("REMEMBRA_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
