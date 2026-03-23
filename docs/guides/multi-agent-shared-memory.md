# Multi-Agent Shared Memory Setup

Connect all your AI agents to the same Remembra memory pool. What one agent stores, all agents can recall.

!!! success "New in v0.10.0: One-Command Setup"
    ```bash
    pip install remembra
    remembra-install --all --api-key rem_xxx --project my-project
    ```
    This auto-detects and configures ALL your installed agents. See [Agent Setup Guide](../getting-started/agent-setup.md) for details.

---

## Overview

This guide shows how to connect multiple AI tools to a single Remembra instance:

- **Claude Desktop** (Anthropic desktop app)
- **Claude Code** (CLI terminal)
- **Codex CLI** (OpenAI coding agent)
- **Gemini CLI** (Google AI)
- **Clawdbot** (Multi-channel AI assistant)

All agents share the same memory — no more siloed conversations.

---

## Prerequisites

1. **Remembra server running** — Self-hosted or cloud at `https://api.remembra.dev`
2. **API key** — Get from Remembra dashboard
3. **User ID and Project ID** — For memory isolation

### Install MCP Server

```bash
# Using uv (recommended)
uv tool install "remembra[mcp]"

# Or using pip
pip install "remembra[mcp]"

# Verify installation
which remembra-mcp
# Should return: ~/.local/bin/remembra-mcp
```

---

## Configuration

### Required Environment Variables

All agents need these same values to share memory:

| Variable | Description | Example |
|----------|-------------|---------|
| `REMEMBRA_URL` | Your Remembra server URL | `https://api.remembra.dev` |
| `REMEMBRA_API_KEY` | API key for authentication | `rem_abc123...` |
| `REMEMBRA_PROJECT` | Project namespace | `my-project` |
| `REMEMBRA_USER_ID` | User ID for memory isolation | `user_xyz789` |

⚠️ **Critical:** All agents MUST use the same `REMEMBRA_PROJECT` and `REMEMBRA_USER_ID` to share memory!

---

## Agent Configurations

### Claude Desktop

**Config file:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

```json
{
  "mcpServers": {
    "remembra": {
      "command": "/Users/YOUR_USERNAME/.local/bin/remembra-mcp",
      "env": {
        "REMEMBRA_URL": "https://api.remembra.dev",
        "REMEMBRA_API_KEY": "rem_YOUR_API_KEY",
        "REMEMBRA_PROJECT": "my-project",
        "REMEMBRA_USER_ID": "user_YOUR_USER_ID"
      }
    }
  }
}
```

**After editing:** Cmd+Q to quit, then reopen Claude Desktop.

---

### Claude Code (Terminal)

**Config file:** `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "remembra": {
      "command": "/Users/YOUR_USERNAME/.local/bin/remembra-mcp",
      "env": {
        "REMEMBRA_URL": "https://api.remembra.dev",
        "REMEMBRA_API_KEY": "rem_YOUR_API_KEY",
        "REMEMBRA_PROJECT": "my-project",
        "REMEMBRA_USER_ID": "user_YOUR_USER_ID"
      }
    }
  }
}
```

---

### Codex CLI (OpenAI)

**Config file:** `~/.codex/config.toml`

```toml
# MCP Servers - Shared Memory Layer
[mcp_servers.remembra]
command = "/Users/YOUR_USERNAME/.local/bin/remembra-mcp"

[mcp_servers.remembra.env]
REMEMBRA_URL = "https://api.remembra.dev"
REMEMBRA_API_KEY = "rem_YOUR_API_KEY"
REMEMBRA_PROJECT = "my-project"
REMEMBRA_USER_ID = "user_YOUR_USER_ID"
```

---

### Gemini CLI (Google)

**Config file:** `~/.gemini/settings.json`

```json
{
  "mcpServers": {
    "remembra": {
      "command": "/Users/YOUR_USERNAME/.local/bin/remembra-mcp",
      "env": {
        "REMEMBRA_URL": "https://api.remembra.dev",
        "REMEMBRA_API_KEY": "rem_YOUR_API_KEY",
        "REMEMBRA_PROJECT": "my-project",
        "REMEMBRA_USER_ID": "user_YOUR_USER_ID"
      }
    }
  }
}
```

---

### Clawdbot

**Config file:** `~/.clawdbot/clawdbot.json`

```json
{
  "plugins": {
    "entries": {
      "remembra": {
        "enabled": true,
        "config": {
          "apiUrl": "https://api.remembra.dev",
          "apiKey": "rem_YOUR_API_KEY",
          "projectId": "my-project",
          "userId": "user_YOUR_USER_ID",
          "autoSync": true
        }
      }
    }
  }
}
```

---

## Auto-Recall Instruction Files (Optional)

Create instruction files so agents automatically recall memory at session start.

### Claude Code: `~/.claude/CLAUDE.md`

```markdown
# Shared Memory Protocol

You are connected to Remembra, a shared memory system.

## On Session Start
BEFORE doing anything else, recall recent context:
- Use `recall_memories` with query: "what was I just working on"

## During Session
Store important context continuously:
- Use `store_memory` after significant actions
- Format: "[AGENT] [TASK] - Did X. Next: Y."

## Connected Agents
All share the same memory: Claude Desktop, Claude Code, Codex, Gemini, Clawdbot.
```

### Codex CLI: `~/.codex/instructions.md`

Same content, adapted for Codex.

### Gemini CLI: `~/.gemini/GEMINI.md`

Same content, adapted for Gemini.

---

## Verification

### 1. Test MCP Server

```bash
# Should start without errors
REMEMBRA_URL="https://api.remembra.dev" \
REMEMBRA_API_KEY="rem_YOUR_KEY" \
remembra-mcp &
sleep 2
kill %1
echo "MCP server works!"
```

### 2. Test API Connection

```bash
curl https://api.remembra.dev/health
# Should return: {"status":"ok","version":"0.9.0",...}
```

### 3. Cross-Agent Test

1. **In Claude Desktop:** "Remember that my favorite color is blue"
2. **In Codex CLI:** "What's my favorite color?"
3. If Codex returns "blue" → **Shared memory is working!**

---

## Troubleshooting

### "Server disconnected" in Claude Desktop

1. Check MCP server is installed: `which remembra-mcp`
2. Use full path in config (not just `remembra-mcp`)
3. Restart Claude Desktop after config changes

### Different agents seeing different memories

- Verify ALL agents use the **same** `REMEMBRA_PROJECT` and `REMEMBRA_USER_ID`
- Different values = different memory spaces

### Tools not appearing

1. Restart the agent after config changes
2. Check JSON syntax (no trailing commas)
3. Verify remembra-mcp is executable: `chmod +x ~/.local/bin/remembra-mcp`

### macOS PATH issues

Create a wrapper script at `~/.local/bin/remembra-mcp-wrapper.sh`:

```bash
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec ~/.local/bin/remembra-mcp "$@"
```

Then use `/bin/bash` with args in config:

```json
{
  "command": "/bin/bash",
  "args": ["~/.local/bin/remembra-mcp-wrapper.sh"],
  "env": { ... }
}
```

---

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Claude Desktop │     │    Codex CLI    │     │   Gemini CLI    │
│   (MCP Client)  │     │   (MCP Client)  │     │   (MCP Client)  │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         │    MCP Protocol       │                       │
         │   (stdio transport)   │                       │
         ▼                       ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                      remembra-mcp                                │
│                   (MCP Server Binary)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │  HTTPS
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    api.remembra.dev                              │
│                  (Remembra API Server)                           │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │   Qdrant    │  │  Postgres   │  │   Redis     │              │
│  │  (Vectors)  │  │   (Data)    │  │  (Cache)    │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Summary

| Setting | Must Match Across All Agents |
|---------|------------------------------|
| `REMEMBRA_URL` | ✅ Same server |
| `REMEMBRA_API_KEY` | ✅ Same key |
| `REMEMBRA_PROJECT` | ✅ Same project |
| `REMEMBRA_USER_ID` | ✅ Same user |

**Result:** One brain, many agents. What one stores, all can recall.

---

## Related Docs

- [Claude Desktop Setup](../integrations/claude-desktop.md)
- [Codex CLI Setup](../integrations/codex.md)
- [MCP Server Reference](../integrations/mcp-server.md)
