# OpenAI Codex

Add persistent memory to OpenAI Codex in 2 minutes.

Codex is OpenAI's coding agent (CLI and IDE extension). With Remembra, Codex can store your project context, architecture decisions and preferences and recall them in later sessions, when it calls Remembra's memory tools.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```

## Setup (CLI)

The fastest way — one command:

```bash
codex mcp add remembra \
  --env REMEMBRA_URL=http://localhost:8787 \
  -- remembra-mcp
```

### With Authentication

```bash
codex mcp add remembra \
  --env REMEMBRA_URL=http://localhost:8787 \
  --env REMEMBRA_API_KEY=your_key_here \
  --env REMEMBRA_USER_ID=your_user_id \
  -- remembra-mcp
```

## Setup (Config File)

Alternatively, edit your config directly.

=== "Global (~/.codex/config.toml)"

    ```toml
    [mcp_servers.remembra]
    command = "remembra-mcp"

    [mcp_servers.remembra.env]
    REMEMBRA_URL = "http://localhost:8787"
    ```

=== "Project (.codex/config.toml)"

    ```toml
    [mcp_servers.remembra]
    command = "remembra-mcp"

    [mcp_servers.remembra.env]
    REMEMBRA_URL = "http://localhost:8787"
    REMEMBRA_PROJECT = "my-project"
    ```

### With Authentication

```toml
[mcp_servers.remembra]
command = "remembra-mcp"

[mcp_servers.remembra.env]
REMEMBRA_URL = "http://localhost:8787"
REMEMBRA_API_KEY = "your_key_here"
REMEMBRA_USER_ID = "your_user_id"
```

### Optional Tuning

```toml
[mcp_servers.remembra]
command = "remembra-mcp"
startup_timeout_sec = 15
tool_timeout_sec = 30
```

!!! note "TOML, not JSON"
    Codex uses TOML configuration, unlike Claude Desktop and Cursor which use JSON. Make sure you're editing a `.toml` file.

## Verify

```bash
codex mcp list
# remembra — ✓ Connected
```

## Try It

Start a Codex session and test:

```
You: Remember that this project uses FastAPI, SQLAlchemy,
     and deploys to AWS Lambda via SAM.

Codex: I'll save that to memory.
       [Tool: store_memory] ✓ Stored — 3 facts extracted

You: Help me add a new endpoint for user profiles.

Codex: Let me check the project context...
       [Tool: recall_memories]
       [recalls: FastAPI, SQLAlchemy, AWS Lambda, SAM]

       Here's the FastAPI endpoint with SQLAlchemy model...
```

## Project-Scoped Config

Add `.codex/config.toml` to your project root to share Remembra config with your team:

```toml
[mcp_servers.remembra]
command = "remembra-mcp"

[mcp_servers.remembra.env]
REMEMBRA_URL = "http://localhost:8787"
REMEMBRA_PROJECT = "my-project"
```

!!! warning
    Project-scoped configs only work in trusted projects. Codex will prompt you to trust the project on first use.

## Troubleshooting

### "remembra-mcp: command not found"

Make sure the MCP package is installed and on your PATH:

```bash
pip install remembra[mcp]
which remembra-mcp
```

If it's installed but not found, use the full path:

```toml
[mcp_servers.remembra]
command = "/Users/you/.local/bin/remembra-mcp"
```

### "Connection refused"

The Remembra server isn't running:

```bash
curl http://localhost:8787/health
# If this fails:
docker compose up -d
```

### Server not connecting

Check startup timeout — if your server takes a while to boot:

```toml
[mcp_servers.remembra]
command = "remembra-mcp"
startup_timeout_sec = 30
```

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 32 tools and 2 resources
- [Python SDK](../guides/python-sdk.md) — Programmatic access from Python
- [REST API](../guides/rest-api.md) — Direct HTTP access
