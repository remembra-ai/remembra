# MCP Server

Use Remembra as persistent memory for AI assistants via the [Model Context Protocol](https://modelcontextprotocol.io).

Works with **Claude Code**, **Claude Desktop**, **Cursor**, and any MCP-compatible client.

## Installation

```bash
pip install remembra[mcp]
```

This installs both the Remembra SDK and the MCP server binary (`remembra-mcp`).

## Setup

### Claude Code

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

### Claude Desktop

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

### Cursor

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

### Project-Level Config

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

Team members set their own env vars; the config stays in version control.

## Available Tools

### store_memory

Store information in persistent memory.

```
Tool: store_memory
Parameters:
  content (required): Text content to memorize
  metadata (optional): Key-value metadata to attach
  ttl (optional): Time-to-live ("24h", "7d", "30d", "1y")
```

The AI assistant will automatically call this when it encounters important information that should persist across sessions — facts, decisions, preferences, project context.

### recall_memories

Search persistent memory for relevant information.

```
Tool: recall_memories
Parameters:
  query (required): Natural language search query
  limit (optional): Max results, 1-50 (default: 5)
  threshold (optional): Min relevance 0.0-1.0 (default: 0.4)
```

The assistant should call this before answering questions about past decisions, context, people, or projects.

### forget_memories

Delete memories (GDPR-compliant).

```
Tool: forget_memories
Parameters:
  memory_id (optional): Delete specific memory by ID
  entity (optional): Delete all memories about an entity
  all_memories (optional): Delete ALL memories (use with caution)
```

### health_check

Verify server connection and health.

```
Tool: health_check
Parameters: none
```

## Resources

The MCP server also exposes two resources:

| URI | Description |
|-----|-------------|
| `memory://recent` | Last 10 stored memories |
| `memory://status` | Server status and configuration |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_URL` | `http://localhost:8787` | Server URL |
| `REMEMBRA_API_KEY` | — | API key for authentication |
| `REMEMBRA_USER_ID` | `default` | User ID for memory isolation |
| `REMEMBRA_PROJECT` | `default` | Project namespace |
| `REMEMBRA_MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, or `streamable-http` |

## Transport Modes

### stdio (default)

Standard input/output. Used by Claude Code, Claude Desktop, and Cursor.

```bash
remembra-mcp
```

### SSE (Server-Sent Events)

For remote/networked connections:

```bash
REMEMBRA_MCP_TRANSPORT=sse remembra-mcp
# Starts SSE server on http://127.0.0.1:8000/sse
```

### Streamable HTTP

For modern HTTP-based connections:

```bash
REMEMBRA_MCP_TRANSPORT=streamable-http remembra-mcp
# Starts on http://127.0.0.1:8000/mcp
```

## How It Works

```
┌─────────────────────┐       ┌──────────────┐       ┌──────────────────┐
│  Claude Code /      │ stdio │  remembra-   │ HTTP  │  Remembra        │
│  Claude Desktop /   │◄─────►│  mcp         │◄─────►│  Server          │
│  Cursor             │       │  (MCP Server)│       │  (localhost:8787) │
└─────────────────────┘       └──────────────┘       └──────────────────┘
```

The MCP server is a thin wrapper around the Remembra Python SDK. It translates MCP tool calls into SDK method calls and returns structured JSON results.

## Tips

!!! tip "Let the AI decide when to store"
    You don't need to explicitly tell Claude to store memories. Well-configured assistants will naturally call `store_memory` when they encounter important information.

!!! tip "Recall before answering"
    Prompt your assistant to call `recall_memories` before answering questions about past context. Include instructions like "Check memory before answering questions about past decisions."

!!! tip "Use projects for isolation"
    Set different `REMEMBRA_PROJECT` values per workspace to keep memories isolated between projects.
