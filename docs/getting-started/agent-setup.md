# Agent Setup

Configure all your AI agents to share memory with one command.

## Quick Setup (Recommended)

```bash
# Install Remembra with its MCP server
pip install "remembra[mcp]"

# Configure all detected agents
remembra-install --all
```

This command:

1. **Detects** all installed AI agents (Claude, Codex, Cursor, Gemini, Windsurf)
2. **Configures** MCP settings for each agent
3. **Stores** credentials securely in `~/.remembra/credentials` (chmod 600)
4. **Saves your API key** so future installs don't need `--api-key`

!!! success "Zero manual config"
    No JSON editing. No copy-pasting. Just run and restart your agents.

!!! tip "Verify setup with doctor"
    After installation, run `remembra-doctor all` to verify everything is working.

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
remembra-install --all
```

### Specific Agent
```bash
remembra-install --agent claude-code
remembra-install --agent codex
remembra-install --agent cursor
remembra-install --agent gemini
remembra-install --agent windsurf
```

### Detect Without Installing
```bash
remembra-install --detect
```

### First-Time Setup (With API Key)
```bash
remembra-install --all --api-key rem_your_key_here
```

After first setup, the API key is saved to `~/.remembra/credentials` and auto-loaded for future commands.

### With Custom Project
```bash
remembra-install --all --project my-project
```

### With User ID
```bash
remembra-install --all --user-id user_123
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
remembra-install --all --url http://localhost:8787
```

Or with a custom API key:

```bash
remembra-install --all --url http://localhost:8787 --api-key your-key
```

## Sandboxed Agents (Codex, Claude Code)

Some agents run in sandboxes that block network access. Use the **local bridge**:

```bash
# Terminal 1: Start the bridge (keeps running)
remembra-bridge --url https://api.remembra.dev --api-key your-key

# Terminal 2: Install agents pointing to bridge
remembra-install --all --url http://localhost:8766
```

The bridge tunnels requests from the sandbox to your Remembra server.

**Bridge commands:**
```bash
remembra-bridge --status   # Check if bridge is running
remembra-bridge --stop     # Stop the bridge
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

### Run Diagnostics First

```bash
remembra-doctor all
```

This checks config files, command resolution, server health, and actual recall functionality.

### Agent not detected

The installer only configures agents it finds. If an agent isn't detected:

1. Make sure the agent is installed
2. Run the agent at least once (creates config directories)
3. Re-run `remembra-install --all`

### MCP not working

1. Verify `remembra-mcp` is in your PATH: `which remembra-mcp`
2. If missing, install: `pip install "remembra[mcp]"`
3. Restart the AI agent completely (not just the window)
4. Run `remembra-doctor <agent>` for specific diagnostics

### Connection errors

1. Run `remembra-doctor <agent>` to identify the issue
2. Check your API key: `curl -H "Authorization: Bearer your-key" https://api.remembra.dev/health`
3. For sandboxed agents: use `remembra-bridge`
4. For self-hosted: verify your Remembra server is running

### Sandbox blocked

If `remembra-doctor` shows `sandbox_blocked`:

1. Start the bridge: `remembra-bridge --url https://api.remembra.dev --api-key your-key`
2. Reconfigure agent: `remembra-install --agent <name> --url http://localhost:8766`
3. Restart the agent
