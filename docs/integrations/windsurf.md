# Windsurf

Add persistent memory to Windsurf in 2 minutes.

Windsurf is an AI-powered IDE by Codeium with the Cascade AI agent. With Remembra, Cascade remembers your project context, coding patterns, and architecture decisions across sessions.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```
3. **MCP enabled in Windsurf** — see below

## Enable MCP in Windsurf

If you haven't enabled MCP yet:

1. Open Command Palette: **Cmd+Shift+P** (macOS) or **Ctrl+Shift+P** (Windows/Linux)
2. Search for **"Cascade"**
3. In Advanced Settings, find **"Model Context Protocol (MCP)"**
4. Enable it

## Setup

!!! warning "Unverified: check where your Windsurf reads MCP servers"
    Windsurf's docs, now served from [docs.devin.ai](https://docs.devin.ai/windsurf/plugins/cascade/mcp), name two
    files. The Windsurf Editor reads `~/.codeium/windsurf/mcp_config.json` (`~/.codeium/windsurf-next/` for
    Windsurf Next) through its MCP discovery: enable the `windsurf` source under `chat.mcp.discovery.enabled` in
    Settings. Cascade's **Open MCP config file** action opens `~/.config/devin/mcp_config.json`
    (`%AppData%\devin\mcp_config.json` on Windows). This guide and `remembra-install --agent windsurf` use the
    first; neither has been run against Windsurf by us. If Cascade does not list `remembra` after a restart, add the
    same block to the second file. `remembra-install --all` does not write Windsurf.

Open your Windsurf MCP config file:

=== "macOS"

    ```
    ~/.codeium/windsurf/mcp_config.json
    ```

=== "Windows"

    ```
    %USERPROFILE%\.codeium\windsurf\mcp_config.json
    ```

Add the Remembra server:

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

!!! tip
    If the config file doesn't exist, create it. If it already has other servers, add `remembra` inside the existing `mcpServers` object.

## Restart Windsurf

After saving, reload Windsurf:

- **Cmd+Shift+P** → "Reload Window"

## Verify

Open the Cascade panel and check the MCPs icon (top right). You should see `remembra` listed with its tools:

- `store_memory`
- `recall_memories`
- `forget_memories`
- `health_check`
- `ingest_conversation`

## Try It

In Cascade:

```
You: Remember that this project uses Next.js 15,
     tRPC, and Drizzle ORM with Neon Postgres.

Cascade: I'll save that to memory.
         ✓ Stored — 4 facts extracted

--- (later, or new session) ---

You: Help me add a new API endpoint for user profiles.

Cascade: Let me check the project context...
         [recalls: Next.js 15, tRPC, Drizzle ORM, Neon Postgres]

         Here's the tRPC router with Drizzle query...
```

## Good to Know

!!! warning "100-tool limit"
    Windsurf has a limit of 100 total MCP tools across all servers. Remembra uses 5 tools, so this shouldn't be an issue unless you have many other MCP servers configured.

## Troubleshooting

### Tools not appearing in Cascade

1. Make sure MCP is enabled (see "Enable MCP" above)
2. Verify the config file path is correct for your OS
3. Reload the window after config changes
4. Check that `remembra-mcp` is on your PATH: `which remembra-mcp`

### "Connection refused"

The Remembra server isn't running:

```bash
curl http://localhost:8787/health
# If this fails:
docker compose up -d
```

### Config file not found

Create it manually:

=== "macOS"

    ```bash
    mkdir -p ~/.codeium/windsurf
    touch ~/.codeium/windsurf/mcp_config.json
    ```

=== "Windows"

    ```powershell
    mkdir %USERPROFILE%\.codeium\windsurf
    echo {} > %USERPROFILE%\.codeium\windsurf\mcp_config.json
    ```

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 5 tools and 2 resources
- [JavaScript SDK](../guides/javascript-sdk.md) — Use Remembra in your Node.js code
- [REST API](../guides/rest-api.md) — Direct HTTP access
