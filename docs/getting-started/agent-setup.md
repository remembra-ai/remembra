# Agent Setup

Configure all your AI agents to share memory with one command.

## Quick Setup (Recommended)

```bash
npx remembra setup --all
```

This command:

1. **Detects** all installed AI agents (Claude, Codex, Cursor, etc.)
2. **Configures** MCP settings for each agent
3. **Stores** credentials securely in `~/.remembra/credentials`

!!! success "Zero manual config"
    No JSON editing. No copy-pasting. Just run and restart your agents.

---

## Supported Agents

| Agent | Config Location | Status |
|-------|----------------|--------|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` | ✅ Auto-configured |
| Claude Code | `~/.claude/settings.json` | ✅ Auto-configured |
| Codex CLI | `~/.codex/config.toml` | ✅ Auto-configured |
| Gemini | `~/.gemini/settings.json` | ✅ Auto-configured |
| Cursor | `~/.cursor/mcp.json` | ✅ Auto-configured |
| Windsurf | `~/.windsurf/mcp_config.json` | ✅ Auto-configured |

---

## Setup Options

### All Agents (Default)
```bash
npx remembra setup --all
```

### Specific Agent
```bash
npx remembra setup --agent claude-code
npx remembra setup --agent codex
npx remembra setup --agent cursor
```

### With Custom Project
```bash
npx remembra setup --all --project my-project
```

### With User ID
```bash
npx remembra setup --all --user-id user_123
```

---

## What Gets Configured

The installer adds this MCP block to each agent's config:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "https://api.remembra.dev",
        "REMEMBRA_API_KEY": "your-api-key",
        "REMEMBRA_PROJECT": "default",
        "REMEMBRA_USER_ID": "default"
      }
    }
  }
}
```

---

## Credentials Storage

Credentials are stored in `~/.remembra/credentials`:

```toml
[credentials]
api_key = "rem_xxx"
url = "https://api.remembra.dev"

[defaults]
project_id = "default"
user_id = "default"
```

This file is created with `600` permissions (readable only by you).

---

## Self-Hosted Setup

For self-hosted Remembra instances:

```bash
npx remembra setup --all --url http://localhost:8787
```

Or with a custom API key:

```bash
npx remembra setup --all --url http://localhost:8787 --api-key your-key
```

---

## Manual Setup

If you prefer to configure manually, add this to your agent's MCP config:

=== "Claude Desktop"

    Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

    ```json
    {
      "mcpServers": {
        "remembra": {
          "command": "remembra-mcp",
          "env": {
            "REMEMBRA_URL": "https://api.remembra.dev",
            "REMEMBRA_API_KEY": "your-api-key"
          }
        }
      }
    }
    ```

=== "Claude Code"

    Edit `~/.claude/settings.json`:

    ```json
    {
      "mcpServers": {
        "remembra": {
          "command": "remembra-mcp",
          "env": {
            "REMEMBRA_URL": "https://api.remembra.dev",
            "REMEMBRA_API_KEY": "your-api-key"
          }
        }
      }
    }
    ```

=== "Codex CLI"

    Edit `~/.codex/config.toml`:

    ```toml
    [mcp_servers.remembra]
    command = "remembra-mcp"

    [mcp_servers.remembra.env]
    REMEMBRA_URL = "https://api.remembra.dev"
    REMEMBRA_API_KEY = "your-api-key"
    ```

---

## Verify Setup

After setup, restart your AI agents and test:

1. **Store a memory:** "Remember that my favorite color is blue"
2. **Switch agents:** Open a different AI tool
3. **Recall:** "What's my favorite color?"

If the second agent knows your color, shared memory is working! 🎉

---

## Troubleshooting

### Agent not detected

The installer only configures agents it finds. If an agent isn't detected:

1. Make sure the agent is installed
2. Run the agent at least once (creates config directories)
3. Re-run `npx remembra setup --all`

### MCP not working

1. Verify `remembra-mcp` is in your PATH: `which remembra-mcp`
2. If missing, install: `pip install remembra`
3. Restart the AI agent completely (not just the window)

### Connection errors

1. Check your API key is valid: `curl -H "Authorization: Bearer your-key" https://api.remembra.dev/health`
2. For self-hosted: verify your Remembra server is running
