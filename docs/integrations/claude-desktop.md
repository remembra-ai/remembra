# Claude Desktop

Add persistent memory to Claude Desktop in 2 minutes.

Claude Desktop is Anthropic's desktop app for chatting with Claude. With Remembra, Claude can store what you tell it and recall it in later conversations, when it calls Remembra's memory tools.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```

## Setup

Open your Claude Desktop config file:

=== "macOS"

    ```
    ~/Library/Application Support/Claude/claude_desktop_config.json
    ```

    Open it with:
    ```bash
    open ~/Library/Application\ Support/Claude/claude_desktop_config.json
    ```

=== "Windows"

    ```
    %APPDATA%\Claude\claude_desktop_config.json
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
        "REMEMBRA_USER_ID": "your_user_id"
      }
    }
  }
}
```

!!! tip
    If you already have other MCP servers configured, add `remembra` alongside them inside the existing `mcpServers` object. Don't replace the whole file.

## Restart Claude Desktop

After saving the config, fully quit and reopen Claude Desktop:

- **macOS**: Cmd+Q → reopen from Applications
- **Windows**: Right-click tray icon → Quit → reopen

## Verify

In a new Claude conversation, you should see the MCP tools icon (hammer) in the input area. Click it to confirm `remembra` tools are listed:

- `store_memory`
- `recall_memories`
- `forget_memories`
- `health_check`
- `ingest_conversation`

## Try It

```
You: Remember that my daughter's birthday is June 15th
     and she loves dinosaurs.

Claude: I'll save that to memory.
        ✓ Stored — 2 facts extracted

--- (new conversation, days later) ---

You: I need a gift idea for my daughter.

Claude: Let me check what I remember...
        Your daughter loves dinosaurs and her birthday is June 15th.
        How about a dinosaur fossil dig kit or a trip to the
        natural history museum?
```

## Troubleshooting

### "remembra-mcp" not found

Claude Desktop needs the full path if `remembra-mcp` isn't on your system PATH:

```bash
which remembra-mcp
# /Users/you/.local/bin/remembra-mcp
```

Use the full path in your config:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "/Users/you/.local/bin/remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787"
      }
    }
  }
}
```

### Tools not appearing after restart

1. Check the config file is valid JSON (no trailing commas)
2. Verify the server is running: `curl http://localhost:8787/health`
3. Check Claude Desktop logs for MCP errors

### Memory not persisting between conversations

Make sure every conversation uses the same `REMEMBRA_API_KEY` (the server takes the account from the key) and the
same `REMEMBRA_PROJECT`. Claude also has to call the memory tools: ask it to recall, or tell it to remember.

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 33 tools and 2 resources
- [Python SDK](../guides/python-sdk.md) — Programmatic access from Python
- [Security Guide](../guides/security.md) — Encryption at rest, PII detection, RBAC
