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


@mcp.tool()
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


@mcp.tool()
def recall_memories(
    query: str,
    limit: int = 5,
    threshold: float = 0.4,
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

    Returns:
        JSON string with synthesized context, matching memories, and entities.
    """
    try:
        client = _get_client()
        result = client.recall(query=query, limit=limit, threshold=threshold)

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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
