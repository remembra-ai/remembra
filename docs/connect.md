# Connect Remembra to any AI client

Remembra speaks the **Model Context Protocol (MCP)**, so it plugs into Cursor,
Windsurf, Claude Desktop, Claude Code, Cline, Continue, VS Code, Zed, and anything
else that speaks MCP. There are two ways to connect — pick one.

## Option A — Remote (recommended): one URL, no install

The hosted Remembra MCP runs on our infrastructure. You connect with a **URL + your
API key** — no binary to install, no PATH to configure, nothing to keep updated.

```jsonc
{
  "mcpServers": {
    "remembra": {
      "url": "https://mcp.remembra.dev/mcp",
      "headers": { "X-API-Key": "rem_YOUR_KEY" }
    }
  }
}
```

Optionally scope a connection to a single project by adding `?project=<id>` to the URL
(e.g. `https://mcp.remembra.dev/mcp?project=clawbot`).

Every request authenticates with **your** key, and the server scopes every operation
to your account — your memories are never visible to another caller.

### Client specifics

- **Cursor** — Settings → MCP → *Add new MCP server* → paste the JSON above (Cursor
  supports remote MCP via `url`).
- **Windsurf** — Settings → Cascade → MCP servers → add a server with the `url` + header.
- **Claude Desktop / Claude Code** — add the `remembra` entry to your MCP config
  (`claude_desktop_config.json` / `.mcp.json`).
- **Cline / Continue / VS Code / Zed** — add the same `remembra` server object to the
  client's MCP settings.

## Option B — Local stdio binary (self-host / offline)

For self-hosting or fully-local setups, run the stdio binary. Note: GUI apps don't
inherit your shell `PATH`, so use the **absolute path** to the binary.

```jsonc
{
  "mcpServers": {
    "remembra": {
      "command": "/full/path/to/remembra-mcp",
      "args": [],
      "env": {
        "REMEMBRA_URL": "https://api.remembra.dev",
        "REMEMBRA_API_KEY": "rem_YOUR_KEY",
        "REMEMBRA_PROJECT": "default"
      }
    }
  }
}
```

Find the absolute path with `which remembra-mcp` (or `command -v remembra-mcp`).

## What you get

Once connected, the assistant gains these tools: `store_memory`, `recall_memories`,
`forget_memories`, `ingest_conversation`, `health_check`, plus entity/timeline and
agent-inbox tools. Tell it to *"remember"* something and it persists; ask about past
context and it recalls — across sessions and across every client you connect.

## Running the remote MCP yourself (self-host)

The same binary serves the remote transport — point it at your Remembra API:

```bash
REMEMBRA_MCP_TRANSPORT=streamable-http \
REMEMBRA_URL=https://api.remembra.dev \
REMEMBRA_MCP_PORT=8765 \
remembra-mcp
```

It listens on `/mcp` and requires each caller to send their own `X-API-Key`
(no shared server key) — put it behind TLS at a hostname like `mcp.remembra.dev`.
