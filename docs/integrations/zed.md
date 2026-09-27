# Zed

Add persistent memory to Zed in 2 minutes.

Zed is a fast, collaborative code editor with built-in AI. With Remembra, Zed's AI assistant can store your project context and preferences and recall them in later sessions, when it calls Remembra's memory tools.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```

## Setup

Open Zed settings (**Cmd+,** on macOS) and add a `context_servers` section:

```json
{
  "context_servers": {
    "remembra": {
      "command": {
        "path": "remembra-mcp",
        "args": [],
        "env": {
          "REMEMBRA_URL": "http://localhost:8787"
        }
      }
    }
  }
}
```

!!! warning "Different key name"
    Zed uses `context_servers`, not `mcpServers`. This is different from Claude Desktop, Cursor, and Windsurf.

### With Authentication

```json
{
  "context_servers": {
    "remembra": {
      "command": {
        "path": "remembra-mcp",
        "args": [],
        "env": {
          "REMEMBRA_URL": "http://localhost:8787",
          "REMEMBRA_API_KEY": "your_key_here",
          "REMEMBRA_USER_ID": "your_user_id",
          "REMEMBRA_PROJECT": "my-project"
        }
      }
    }
  }
}
```

### Full Path

If `remembra-mcp` isn't on your PATH, use the full path:

```bash
which remembra-mcp
# /Users/you/.local/bin/remembra-mcp
```

```json
{
  "context_servers": {
    "remembra": {
      "command": {
        "path": "/Users/you/.local/bin/remembra-mcp",
        "args": [],
        "env": {
          "REMEMBRA_URL": "http://localhost:8787"
        }
      }
    }
  }
}
```

## Verify

1. Open the **Agent Panel** in Zed
2. Go to the Agent Panel's **Settings** view
3. Look for `remembra` — the indicator dot should be **green** with tooltip "Server is active"

## Try It

In the Agent Panel:

```
You: Remember that this project uses Rust with Axum
     for the backend and SvelteKit for the frontend.

Zed: I'll save that to memory.
     ✓ Stored — 3 facts extracted

--- (later, or new session) ---

You: How should I structure a new API endpoint?

Zed: Let me check the project context...
     [recalls: Rust, Axum, SvelteKit]

     Here's an Axum handler with the routing pattern...
```

## Troubleshooting

### Red indicator or "Server not active"

1. Check that `remembra-mcp` is installed: `which remembra-mcp`
2. Verify the Remembra server is running: `curl http://localhost:8787/health`
3. Make sure the settings JSON is valid (no trailing commas, correct nesting)
4. Try using the full path to `remembra-mcp`

### Tools not appearing

Zed currently supports MCP **Tools** and **Prompts**. Make sure you're using the Agent Panel (not just inline completions) to access MCP tools.

### Config not taking effect

Save the settings file and Zed should pick up changes automatically. If not, restart Zed.

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 24 tools and 2 resources
- [Python SDK](../guides/python-sdk.md) — Programmatic access from Python
- [REST API](../guides/rest-api.md) — Direct HTTP access
