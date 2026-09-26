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

1. **Detects** the installed AI agents (Claude Desktop, Claude Code, Codex, Cursor, Gemini CLI)
2. **Configures** MCP settings for each agent
3. **Stores** credentials securely in `~/.remembra/credentials` (chmod 600)
4. **Saves your API key and server** so a later run keeps them (it never switches a self-hosted server to Remembra Cloud unless you pass `--url`)

!!! success "Zero manual config"
    No JSON editing. No copy-pasting. Just run and restart your agents.

!!! tip "Verify setup with doctor"
    After installation, run `remembra-doctor all` to verify everything is working.

---

## Supported Agents

| Agent | Config Location | Status |
|-------|----------------|--------|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` | ✅ Auto-configured |
| Claude Code | `~/.claude.json` (user scope, as `claude mcp add --scope user` writes it) | ✅ Auto-configured |
| Codex CLI | `~/.codex/config.toml` | ✅ Auto-configured |
| Gemini | `~/.gemini/settings.json` | ✅ Auto-configured |
| Cursor | `~/.cursor/mcp.json` | ✅ Auto-configured |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | ⚠️ Unverified: not written by `--all`; `--agent windsurf` writes it (see below) |

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
remembra-install --agent windsurf   # unverified, see the note below
```

!!! warning "Windsurf is unverified"
    Windsurf's docs (now at [docs.devin.ai](https://docs.devin.ai/windsurf/plugins/cascade/mcp)) name two files:
    `~/.codeium/windsurf/mcp_config.json`, which the Windsurf Editor reads through its MCP discovery (enable
    the `windsurf` source under `chat.mcp.discovery.enabled` in Settings), and `~/.config/devin/mcp_config.json`,
    which Cascade's **Open MCP config file** opens. `remembra-install --agent windsurf` writes the first; it has
    not been run against Windsurf. If Cascade does not list `remembra`, add the same block to the second file.

### Detect Without Installing
```bash
remembra-install --detect
```

### First-Time Setup (With API Key)
```bash
remembra-install --all            # asks for the key at a hidden prompt
remembra-install --all --apply    # or: write without asking (key from REMEMBRA_API_KEY or ~/.remembra/credentials)
```

The key is never needed on the command line, where shell history would keep it: the installer reads
`REMEMBRA_API_KEY`, asks at a hidden prompt, or takes it piped with `--api-key-stdin`. After first
setup it is saved to `~/.remembra/credentials` (owner-only) and used by later runs. Without `--apply`
(or a "y" at its question) the installer only shows what it would change. `--remove` takes the
Remembra entries out again.

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
remembra-install --all --url http://localhost:8787
```

## Sandboxed Agents (Codex, Claude Code)

Some agents run in sandboxes that block network access. Use the **local bridge**:

```bash
# Terminal 1: start the bridge (keeps running). It sends the key from REMEMBRA_API_KEY upstream.
read -rs REMEMBRA_API_KEY && export REMEMBRA_API_KEY   # paste the key: not shown, not in shell history
remembra-bridge --upstream https://api.remembra.dev

# Terminal 2: point the agents at the bridge (it listens on 127.0.0.1:9819)
remembra-install --all --url http://127.0.0.1:9819
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

    Claude Code reads user-scope MCP servers from `~/.claude.json`, not from
    `~/.claude/settings.json`. Add this under the top-level `mcpServers` of
    `~/.claude.json` (Claude Code keeps its own state in that file: edit it with
    Claude Code closed), then check it with `claude mcp get remembra`:

    ```json
    {
      "mcpServers": {
        "remembra": {
          "type": "stdio",
          "command": "remembra-mcp",
          "args": [],
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

1. Start the bridge with the key in `REMEMBRA_API_KEY`: `remembra-bridge --upstream https://api.remembra.dev`
2. Reconfigure agent: `remembra-install --agent <name> --url http://127.0.0.1:9819`
3. Restart the agent
