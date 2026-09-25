# Claude Code

Add persistent memory to Claude Code in 2 minutes.

Claude Code is Anthropic's CLI-based AI coding assistant. With Remembra, Claude remembers your preferences, project context, and decisions across sessions.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```

## Setup

Register Remembra as an MCP server:

```bash
claude mcp add remembra \
  -e REMEMBRA_URL=http://localhost:8787 \
  -- remembra-mcp
```

That's it. Claude Code will now have access to Remembra's memory tools.

### With Authentication

If your server has auth enabled:

```bash
claude mcp add remembra \
  -e REMEMBRA_URL=http://localhost:8787 \
  -e REMEMBRA_API_KEY=your_key_here \
  -e REMEMBRA_USER_ID=your_user_id \
  -- remembra-mcp
```

### Set the agent id and project

Other agents address Claude Code's inbox by its agent id. Give every agent the same project:

```bash
claude mcp add remembra \
  -e REMEMBRA_URL=https://api.remembra.dev \
  -e REMEMBRA_API_KEY=your_key_here \
  -e REMEMBRA_PROJECT=your-shared-project \
  -e REMEMBRA_AGENT_ID=claude-code \
  -- remembra-mcp
```

If `REMEMBRA_AGENT_ID` is missing, `health_check` returns a warning and the server prints a warning to stderr at startup.

### Load context automatically at session start (Remembra Relay)

`remembra-relay connect --apply` adds a SessionStart hook (`remembra-relay brief`) and a
SessionEnd hook (`remembra-relay close`) to `~/.claude/settings.json`. At session start
Claude Code gets the pickup brief: what the last agent did, what it did not finish, what
is failing and its suggested next step, plus your unread inbox and recent memories. At
session end the hook records a structured handoff from git and the session transcript.
The hooks read the key from the `env` block of the `remembra` MCP server in
`~/.claude.json`, so the API key stays in one place, and they never block a session.

`connect` replaces the older `integrations/claude-code/session_start.py` hook if it finds
it. The project comes from the repository; your `REMEMBRA_PROJECT` names a repository the
first time it is seen, so your existing memories stay in view. See the
[Relay guide](../guides/relay.md) for details.

### Keep the MCP server in step with the API

If you run from a checkout, install the MCP server in editable mode. Then the
`remembra-mcp` binary always runs the code you have checked out:

```bash
pipx install --force --editable "/path/to/remembra[mcp]"
# or: uv tool install --force --editable "/path/to/remembra[mcp]"
```

`health_check` reports `client_version`. It warns when that version differs from
the API's version.

## Verify

```bash
claude mcp list
# remembra: remembra-mcp — ✓ Connected
```

## Try It

Start a Claude Code session and test:

```
You: Remember that I prefer TypeScript, use Tailwind CSS, and deploy to Vercel.
Claude: I'll save that to memory.
       [Tool: store_memory] ✓ Stored — 3 facts extracted

You: What's my preferred stack?
Claude: Let me check my memory...
       [Tool: recall_memories]
       You prefer TypeScript, use Tailwind CSS, and deploy to Vercel.
```

## Project-Wide Config

Share Remembra config with your team by adding `.mcp.json` to your project root:

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

Commit this file — anyone who clones the repo gets Remembra automatically.

## Troubleshooting

### "remembra-mcp: command not found"

Make sure the MCP package is installed:

```bash
pip install remembra[mcp]
which remembra-mcp
```

### "Connection refused"

Ensure the Remembra server is running:

```bash
curl http://localhost:8787/health
```

### Tools not appearing

```bash
claude mcp list          # Check status
claude mcp logs remembra # View logs
```

If the status shows disconnected, remove and re-add:

```bash
claude mcp remove remembra
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 5 tools and 2 resources
- [Python SDK](../guides/python-sdk.md) — Programmatic access from Python
- [Conversation Ingestion](../guides/conversation-ingestion.md) — Auto-extract memories from conversations
