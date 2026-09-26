# VS Code + GitHub Copilot

Add persistent memory to VS Code with GitHub Copilot in 2 minutes.

VS Code is the most popular code editor in the world. With Remembra, GitHub Copilot remembers your project context, coding preferences, and architecture decisions across sessions.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```
3. **VS Code 1.96+** with GitHub Copilot extension

## Setup

Create `.vscode/mcp.json` in your project root:

```json
{
  "servers": {
    "remembra": {
      "type": "stdio",
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787"
      }
    }
  }
}
```

!!! note "\"servers\", not \"mcpServers\""
    VS Code uses `"servers"` as the top-level key, unlike Claude Desktop and Cursor which use `"mcpServers"`.

### With Authentication

```json
{
  "servers": {
    "remembra": {
      "type": "stdio",
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

### User-Level (All Projects)

To make Remembra available across all your VS Code projects:

1. Open Command Palette: **Cmd+Shift+P** (macOS) or **Ctrl+Shift+P** (Windows/Linux)
2. Run **"MCP: Open User Configuration"**
3. Add the same config as above

## Verify

1. Open Command Palette → **"MCP: List Servers"**
2. Confirm `remembra` shows as connected
3. If issues, select **"Show Output"** to see diagnostic logs

You can also check in Copilot Chat:

1. Open Copilot Chat (**Ctrl+Alt+I** / **Cmd+Alt+I**)
2. Click the **Configure Tools** button
3. Confirm Remembra's tools are listed and enabled

## Try It

Open Copilot Chat and test:

```
You: Remember that this project uses Express.js,
     Prisma, and PostgreSQL. We deploy to Fly.io.

Copilot: I'll save that to memory.
         [Tool: store_memory] ✓ Stored — 4 facts extracted

--- (later, or new session) ---

You: Help me add a database migration for a posts table.

Copilot: Let me check the project context...
         [Tool: recall_memories]
         [recalls: Express.js, Prisma, PostgreSQL, Fly.io]

         Here's the Prisma schema and migration...
```

## Team Sharing

Commit `.vscode/mcp.json` to your repo — your whole team gets Remembra automatically. Each developer just needs:

1. Remembra server running locally
2. `pip install remembra[mcp]`

!!! tip "Use environment variables for secrets"
    VS Code supports input variables in MCP config. Avoid hardcoding API keys — use environment variables or VS Code's input variable syntax instead.

## Troubleshooting

### Tools not appearing in Copilot

1. Ensure MCP is enabled: **Settings → Extensions → GitHub Copilot → MCP**
2. Run **"MCP: List Servers"** from Command Palette — check for errors
3. Reload window: **Cmd+Shift+P** → "Reload Window"

### "remembra-mcp: command not found"

```bash
pip install remembra[mcp]
which remembra-mcp
```

If installed but not found, use the full path in your config:

```json
{
  "servers": {
    "remembra": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787"
      }
    }
  }
}
```

### "Connection refused"

```bash
curl http://localhost:8787/health
# If this fails:
docker compose up -d
```

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 21 tools and 2 resources
- [JavaScript SDK](../guides/javascript-sdk.md) — Use Remembra in your Node.js code
- [REST API](../guides/rest-api.md) — Direct HTTP access
