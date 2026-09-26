# Cursor

Add persistent memory to Cursor in 2 minutes.

Cursor is an AI-powered code editor. With Remembra, Cursor's AI remembers your coding preferences, architecture decisions, and project context across sessions.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```

## Setup

Create `.cursor/mcp.json` in your project root:

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

### With Authentication

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787",
        "REMEMBRA_API_KEY": "your_key_here",
        "REMEMBRA_USER_ID": "your_user_id",
        "REMEMBRA_PROJECT": "my-project"
      }
    }
  }
}
```

!!! tip "Per-project memory"
    Set `REMEMBRA_PROJECT` to your project name to keep memories isolated per codebase. Your React project memories won't bleed into your Python project.

## Restart Cursor

After saving the config, reload the window:

- **Cmd+Shift+P** (macOS) or **Ctrl+Shift+P** (Windows/Linux)
- Type "Reload Window" and select it

## Verify

Open Cursor's Composer (Cmd+I / Ctrl+I) and check that MCP tools are available. You should see Remembra's tools listed when the AI considers using them.

## Try It

In Composer or Chat:

```
You: Remember that this project uses PostgreSQL 16,
     Prisma ORM, and we deploy to Railway.

Cursor: I'll save that to memory.
        ✓ Stored — 3 facts extracted

--- (later, or new session) ---

You: Write a database migration to add a users table.

Cursor: Let me check the project context...
        [recalls: PostgreSQL 16, Prisma ORM, Railway]

        Here's the Prisma migration for PostgreSQL...
```

## Team Sharing

Commit `.cursor/mcp.json` to your repo. Everyone who clones the project gets Remembra configured automatically — they just need the server running and `remembra[mcp]` installed.

For team setups, use environment variable interpolation:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "${REMEMBRA_URL:-http://localhost:8787}",
        "REMEMBRA_API_KEY": "${REMEMBRA_API_KEY}",
        "REMEMBRA_USER_ID": "${REMEMBRA_USER_ID:-default}",
        "REMEMBRA_PROJECT": "${REMEMBRA_PROJECT:-my-project}"
      }
    }
  }
}
```

Each developer sets their own env vars; the shared config just wires it up.

## Troubleshooting

### Tools not showing in Composer

1. Make sure `.cursor/mcp.json` is in the **project root** (not a subdirectory)
2. Reload the window (Cmd+Shift+P → "Reload Window")
3. Check that `remembra-mcp` is on your PATH: `which remembra-mcp`

### "Connection refused"

The Remembra server isn't running:

```bash
curl http://localhost:8787/health
# If this fails, start the server:
docker compose up -d
```

### Full path workaround

If `remembra-mcp` isn't found, use the full path:

```bash
which remembra-mcp
# /Users/you/.local/bin/remembra-mcp
```

Update your config to use the absolute path as the `command`.

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 24 tools and 2 resources
- [JavaScript SDK](../guides/javascript-sdk.md) — Use Remembra in your Node.js code
- [REST API](../guides/rest-api.md) — Direct HTTP access
