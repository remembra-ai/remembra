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
  -- remembra-mcp --client codex
```

### With Authentication

```bash
codex mcp add remembra \
  --env REMEMBRA_URL=http://localhost:8787 \
  --env REMEMBRA_API_KEY=your_key_here \
  --env REMEMBRA_USER_ID=your_user_id \
  -- remembra-mcp --client codex
```

## Setup (Config File)

Alternatively, edit your config directly.

=== "Global (~/.codex/config.toml)"

    ```toml
    [mcp_servers.remembra]
    command = "remembra-mcp"
    args = ["--client", "codex"]

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
args = ["--client", "codex"]

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

!!! note "Why --client codex"
    The Codex app can run a chat through its cloud connection. In that mode it starts MCP servers
    **without the `env` from your config**, so the server would not know your URL or key. With
    `--client codex`, `remembra-mcp` reads any missing setting from the `remembra` entry in
    `~/.codex/config.toml`; settings that do arrive in the environment still win. `remembra-install`
    writes this line for you. Without it, `remembra-mcp` 0.17.3+ still detects that Codex started it
    with no settings and reads the same file.

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

If the error says **"No Remembra settings reached this MCP server"**, Codex started the server
without its `env` (the Codex app does this in cloud-connected chats). Add
`args = ["--client", "codex"]` under `[mcp_servers.remembra]`, or rerun `remembra-install`, then
start a new chat.

Otherwise the Remembra server isn't running:

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

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 31 tools and 2 resources
- [Python SDK](../guides/python-sdk.md) — Programmatic access from Python
- [REST API](../guides/rest-api.md) — Direct HTTP access
