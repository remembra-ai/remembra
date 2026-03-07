# MCP Server

Use Remembra as persistent memory for AI assistants via the [Model Context Protocol](https://modelcontextprotocol.io).

Works with **Claude Code**, **Claude Desktop**, **Cursor**, and any MCP-compatible client.

!!! tip "v0.8.2 Features"
    New in v0.8.2: AES-256-GCM encryption at rest, MCP Registry published (`io.github.remembra-ai/remembra`), PII detection & redaction, one-command quick start, multi-provider entity extraction (OpenAI + Anthropic + Ollama), and persistent HTTP connections for faster performance.

## Installation

```bash
pip install remembra[mcp]
```

This installs both the Remembra SDK and the MCP server binary (`remembra-mcp`).

## Quick Setup

=== "Claude Code"

    ```bash
    claude mcp add remembra \
      -e REMEMBRA_URL=http://localhost:8787 \
      -e REMEMBRA_API_KEY=your_key \
      -- remembra-mcp
    ```

    Verify it's connected:

    ```bash
    claude mcp list
    # remembra: remembra-mcp - ✓ Connected
    ```

=== "Claude Desktop"

    Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

    ```json
    {
      "mcpServers": {
        "remembra": {
          "command": "remembra-mcp",
          "env": {
            "REMEMBRA_URL": "http://localhost:8787",
            "REMEMBRA_API_KEY": "your_key"
          }
        }
      }
    }
    ```

=== "Cursor"

    Add to `.cursor/mcp.json` in your project:

    ```json
    {
      "mcpServers": {
        "remembra": {
          "command": "remembra-mcp",
          "env": {
            "REMEMBRA_URL": "http://localhost:8787",
            "REMEMBRA_API_KEY": "your_key"
          }
        }
      }
    }
    ```

=== "Project Config (.mcp.json)"

    Add `.mcp.json` to your project root (share via git):

    ```json
    {
      "mcpServers": {
        "remembra": {
          "command": "remembra-mcp",
          "env": {
            "REMEMBRA_URL": "${REMEMBRA_URL:-http://localhost:8787}",
            "REMEMBRA_API_KEY": "${REMEMBRA_API_KEY}",
            "REMEMBRA_USER_ID": "${REMEMBRA_USER_ID:-default}",
            "REMEMBRA_PROJECT": "${REMEMBRA_PROJECT:-default}"
          }
        }
      }
    }
    ```

---

## Tools Reference

### store_memory

Store information in persistent memory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | ✅ | Text content to memorize |
| `metadata` | object | ❌ | Key-value metadata (e.g., `{"source": "meeting"}`) |
| `ttl` | string | ❌ | Time-to-live: `24h`, `7d`, `30d`, `1y`, or omit for permanent |

**Example:**
```
Claude: I'll remember that.
[Tool: store_memory]
content: "User prefers TypeScript over JavaScript and uses Tailwind CSS"
metadata: {"source": "preferences"}

Result:
{
  "status": "stored",
  "id": "mem_abc123",
  "extracted_facts": [
    "User prefers TypeScript over JavaScript",
    "User uses Tailwind CSS"
  ],
  "entities": [
    {"name": "TypeScript", "type": "TECHNOLOGY"},
    {"name": "Tailwind CSS", "type": "TECHNOLOGY"}
  ]
}
```

---

### recall_memories

Search persistent memory for relevant information.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | ✅ | - | Natural language query or keywords |
| `limit` | int | ❌ | 5 | Max results (1-50) |
| `threshold` | float | ❌ | 0.4 | Min relevance (0.0-1.0) |

**Example:**
```
User: What framework do I prefer?
Claude: Let me check my memory...
[Tool: recall_memories]
query: "user framework preferences"

Result:
{
  "status": "ok",
  "context": "User prefers TypeScript over JavaScript and uses Tailwind CSS.",
  "memories": [
    {
      "id": "mem_abc123",
      "content": "User prefers TypeScript over JavaScript",
      "relevance": 0.92
    }
  ]
}
```

---

### forget_memories

Delete memories from persistent storage. GDPR-compliant.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `memory_id` | string | ❌ | Delete specific memory by ID |
| `entity` | string | ❌ | Delete all memories about an entity |
| `all_memories` | bool | ❌ | Delete ALL memories (use with caution!) |

!!! warning
    Exactly one parameter must be provided. `all_memories=true` is destructive!

**Examples:**
```
# Delete specific memory
[Tool: forget_memories]
memory_id: "mem_abc123"

# Delete all about a person
[Tool: forget_memories]
entity: "John Smith"

# Nuclear option
[Tool: forget_memories]
all_memories: true
```

---

### health_check

Check Remembra server health and connection status.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | - | - | No parameters needed |

**Example:**
```
[Tool: health_check]

Result:
{
  "status": "ok",
  "server": "http://localhost:8787",
  "health": {
    "status": "healthy",
    "version": "0.8.2",
    "qdrant": "connected",
    "database": "connected"
  }
}
```

---

### ingest_conversation <span class="md-tag">v0.8.0</span>

Automatically extract memories from a conversation. This is the **primary method** for agents to add context to memory.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `messages` | array | ✅ | - | List of `{role, content}` message objects |
| `session_id` | string | ❌ | auto | Session ID for grouping |
| `min_importance` | float | ❌ | 0.5 | Importance threshold (0.0-1.0) |
| `extract_from` | string | ❌ | "both" | `"user"`, `"assistant"`, or `"both"` |
| `store` | bool | ❌ | true | Set `false` for dry-run |

**Example:**
```
[Tool: ingest_conversation]
messages: [
  {"role": "user", "content": "My wife Sarah and I are moving to Seattle next month"},
  {"role": "assistant", "content": "That's exciting! What brings you to Seattle?"},
  {"role": "user", "content": "I got a job at Amazon as a senior engineer"}
]
min_importance: 0.5

Result:
{
  "status": "ok",
  "facts_extracted": 4,
  "facts_stored": 3,
  "facts_deduped": 1,
  "entities_found": 3,
  "facts": [
    {"content": "User's wife is named Sarah", "importance": 0.8},
    {"content": "User is moving to Seattle next month", "importance": 0.7},
    {"content": "User works at Amazon as a senior engineer", "importance": 0.9}
  ],
  "entities": [
    {"name": "Sarah", "type": "PERSON"},
    {"name": "Seattle", "type": "LOCATION"},
    {"name": "Amazon", "type": "ORGANIZATION"}
  ]
}
```

---

## Resources

MCP resources provide quick access to memory data without tool calls.

### memory://recent

Returns the 10 most recently stored memories.

```json
{
  "count": 10,
  "memories": [
    {
      "id": "mem_xyz",
      "content": "User prefers dark mode",
      "relevance": 1.0,
      "created_at": "2026-03-03T12:00:00Z"
    }
  ]
}
```

### memory://status

Returns server status and configuration.

```json
{
  "server": "http://localhost:8787",
  "user_id": "user_123",
  "project": "default",
  "health": {"status": "healthy", "version": "0.8.0"}
}
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_URL` | `http://localhost:8787` | Remembra server URL |
| `REMEMBRA_API_KEY` | - | API key for authentication |
| `REMEMBRA_USER_ID` | `default` | User ID for memory isolation |
| `REMEMBRA_PROJECT` | `default` | Project namespace |
| `REMEMBRA_MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, or `streamable-http` |

---

## Transport Modes

### stdio (Default)

Standard I/O transport for local MCP clients.

```bash
remembra-mcp
# or explicitly:
REMEMBRA_MCP_TRANSPORT=stdio remembra-mcp
```

### SSE (Server-Sent Events)

For remote/networked connections:

```bash
REMEMBRA_MCP_TRANSPORT=sse remembra-mcp
```

### Streamable HTTP

For HTTP-based transports:

```bash
REMEMBRA_MCP_TRANSPORT=streamable-http remembra-mcp
```

---

## Troubleshooting

### "Connection refused" error

1. Ensure Remembra server is running:
   ```bash
   docker ps | grep remembra
   # or
   curl http://localhost:8787/health
   ```

2. Check the URL in your config matches the running server.

### "Unauthorized" or 401 errors

1. Verify your API key is correct
2. Check if auth is enabled on the server:
   ```bash
   curl -H "X-API-Key: your_key" http://localhost:8787/health
   ```

### Tool not appearing in Claude

1. Restart Claude Code/Desktop after config changes
2. Verify MCP server is running:
   ```bash
   claude mcp list
   ```
3. Check logs for errors:
   ```bash
   claude mcp logs remembra
   ```

### Memory not persisting

1. Check user_id is consistent across sessions
2. Verify store operations return success
3. Check server logs for errors

---

## Best Practices

### 1. Use ingest_conversation for chat context

Instead of manually storing individual facts, ingest the full conversation:

```python
# ❌ Manual (tedious)
store_memory("User likes TypeScript")
store_memory("User works at Acme")

# ✅ Automatic (recommended)
ingest_conversation(messages=[...], min_importance=0.5)
```

### 2. Recall before answering

Always check memory before answering questions about past context:

```
User: What's my preferred stack?
Claude: [recalls memories first, then answers]
```

### 3. Use appropriate TTL

- `24h` — Session context, temporary preferences
- `7d` — Short-term project context
- `30d` — Monthly goals, ongoing work
- `1y` — Long-term preferences, relationships
- (none) — Permanent facts

### 4. Namespace with projects

Use `REMEMBRA_PROJECT` to separate memory spaces:

```bash
REMEMBRA_PROJECT=work-assistant    # Work memories
REMEMBRA_PROJECT=personal-assistant # Personal memories
```

---

## Example: Full Session

```
User: Remember that I'm working on the Acme project with Sarah. 
      We're using React and PostgreSQL. The deadline is March 15th.

Claude: I'll save that context.
[Tool: store_memory]
content: "Working on Acme project with Sarah. Stack: React + PostgreSQL. Deadline: March 15th."
metadata: {"project": "acme", "type": "context"}

Result: ✓ Stored (3 facts extracted, 2 entities found)

---

[Next session]

User: What's the deadline for the project I'm working on?

Claude: Let me check my memory.
[Tool: recall_memories]
query: "project deadline"

Result: "Working on Acme project... Deadline: March 15th"

Claude: The Acme project deadline is March 15th. You're working on it with Sarah using React and PostgreSQL.
```

---

## Related

- [Python SDK](../guides/python-sdk.md)
- [JavaScript SDK](../guides/javascript-sdk.md)
- [Conversation Ingestion](../guides/conversation-ingestion.md)
- [REST API](../guides/rest-api.md)
