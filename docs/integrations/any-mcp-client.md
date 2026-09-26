# Any MCP Client

Connect Remembra to any MCP-compatible AI tool.

If your tool supports the [Model Context Protocol](https://modelcontextprotocol.io), you can add Remembra as a memory server. This guide covers the general setup for any MCP client.

!!! tip "Dedicated guides available"
    We have step-by-step guides for popular tools:

    - [Claude Code](claude-code.md)
    - [Claude Desktop](claude-desktop.md)
    - [Cursor](cursor.md)
    - [Windsurf](windsurf.md)
    - [Zed](zed.md)

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```

## Transport Modes

Remembra's MCP server supports three transport modes. Choose based on your client's capabilities:

### stdio (Default)

Standard I/O transport — the most common for local MCP clients. The client launches `remembra-mcp` as a subprocess and communicates via stdin/stdout.

```bash
remembra-mcp
```

**Config format** (most MCP clients use this):

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787"
      }
    }
  }
}
```

### SSE (Server-Sent Events)

For remote or networked MCP connections:

```bash
REMEMBRA_MCP_TRANSPORT=sse remembra-mcp
```

The server will start on a local port and stream events to connected clients.

### Streamable HTTP

For HTTP-based transports:

```bash
REMEMBRA_MCP_TRANSPORT=streamable-http remembra-mcp
```

## Environment Variables

Configure Remembra via environment variables in your MCP client config:

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_URL` | `http://localhost:8787` | Remembra server URL |
| `REMEMBRA_API_KEY` | — | API key (if auth is enabled) |
| `REMEMBRA_USER_ID` | `default` | User ID for memory isolation |
| `REMEMBRA_PROJECT` | `default` | Project namespace |
| `REMEMBRA_MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, or `streamable-http` |

## Available Tools

Once connected, your MCP client has 21 tools; the [MCP Tool Reference](mcp-server.md) lists them. The main ones:

| Tool | Description |
|------|-------------|
| `store_memory` | Store information in persistent memory |
| `recall_memories` | Search memory with natural language |
| `forget_memories` | Delete one memory by ID, or the memories about one entity or every memory in one project (both with a confirmation phrase) |
| `health_check` | Check server connection status |
| `ingest_conversation` | Auto-extract memories from conversations |

See the [MCP Tool Reference](mcp-server.md) for full parameter documentation.

## Available Resources

| Resource URI | Description |
|-------------|-------------|
| `memory://recent` | 10 most recently stored memories |
| `memory://status` | Server status and configuration |

## Docker Alternative

Run the MCP server in a container instead of installing via pip:

```bash
docker run --rm -i \
  -e REMEMBRA_URL=http://host.docker.internal:8787 \
  remembra/remembra:latest \
  remembra-mcp
```

!!! note
    Use `host.docker.internal` to connect to a Remembra server running on your host machine.

## MCP Registry

Remembra is published on the [MCP Registry](https://registry.modelcontextprotocol.io) as:

```
io.github.remembra-ai/remembra
```

Some MCP clients can install servers directly from the registry.

## Troubleshooting

### "remembra-mcp: command not found"

```bash
pip install remembra[mcp]
which remembra-mcp
```

If it's installed but not on PATH, use the full path in your client config.

### "Connection refused" to Remembra server

```bash
curl http://localhost:8787/health
```

If this fails, start the server:

```bash
docker compose up -d
```

### Client doesn't support stdio

Use SSE or streamable-http transport:

```bash
REMEMBRA_MCP_TRANSPORT=sse remembra-mcp
```

Then configure your client to connect to the SSE endpoint.

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full parameter docs for all tools
- [Python SDK](../guides/python-sdk.md) — Programmatic access
- [JavaScript SDK](../guides/javascript-sdk.md) — Node.js / TypeScript
- [REST API](../guides/rest-api.md) — Direct HTTP access
