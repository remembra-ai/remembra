# JetBrains IDEs

Add persistent memory to IntelliJ IDEA, PyCharm, WebStorm, and other JetBrains IDEs in 2 minutes.

JetBrains IDEs (IntelliJ IDEA, PyCharm, WebStorm, GoLand, PhpStorm, Rider, CLion, RubyMine) support MCP through the built-in AI Assistant. With Remembra, the AI Assistant can store your project context and preferences and recall them in later sessions, when it calls Remembra's memory tools.

## Prerequisites

1. **Remembra server running** — [Quick start](../getting-started/quickstart.md) or `docker compose up -d`
2. **MCP package installed:**
   ```bash
   pip install remembra[mcp]
   ```
3. **JetBrains IDE 2025.2+** with AI Assistant enabled

## Setup

1. Open **Settings** (**Cmd+,** on macOS / **Ctrl+Alt+S** on Windows/Linux)
2. Navigate to **Tools → AI Assistant → Model Context Protocol (MCP)**
3. Click **Add** (+)
4. Select **STDIO** as the connection type
5. Paste this configuration:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "args": [],
      "env": {
        "REMEMBRA_URL": "http://localhost:8787"
      }
    }
  }
}
```

6. Set **Level** to "Global" (all projects) or "Project" (current only)
7. Click **OK**, then **Apply**

### With Authentication

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "args": [],
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

## Verify

After adding the server:

1. Go back to **Settings → Tools → AI Assistant → MCP**
2. Check that `remembra` shows a **green** status indicator
3. If red, click the server name to view error logs

## Try It

Open AI Assistant Chat and test:

```
You: Remember that this project uses Spring Boot 3,
     Kotlin, and we deploy to Kubernetes on GCP.

AI Assistant: I'll save that to memory.
              [Tool: store_memory] ✓ Stored — 3 facts extracted

--- (later, or new session) ---

You: Help me add a REST controller for user management.

AI Assistant: Let me check the project context...
              [Tool: recall_memories]
              [recalls: Spring Boot 3, Kotlin, Kubernetes, GCP]

              Here's the Kotlin controller with Spring Boot 3...
```

## Scope: Global vs Project

| Level | Scope | Use Case |
|-------|-------|----------|
| **Global** | All projects in this IDE | Personal preferences, general context |
| **Project** | Current project only | Project-specific architecture, tech stack |

!!! tip
    Use **Global** for personal preferences (language, style) and **Project** for project-specific context (stack, architecture, team conventions).

## Works With All JetBrains IDEs

The same setup works across the entire JetBrains family:

| IDE | Primary Language |
|-----|-----------------|
| IntelliJ IDEA | Java, Kotlin |
| PyCharm | Python |
| WebStorm | JavaScript, TypeScript |
| GoLand | Go |
| PhpStorm | PHP |
| Rider | C#, .NET |
| CLion | C, C++ |
| RubyMine | Ruby |
| DataGrip | SQL, Databases |

## Troubleshooting

### Server shows red status

1. Click the server name in MCP settings to view error output
2. Check that `remembra-mcp` is on your PATH:
   ```bash
   which remembra-mcp
   ```
3. If not found, use the full path in the `command` field

### "Connection refused"

The Remembra server isn't running:

```bash
curl http://localhost:8787/health
# If this fails:
docker compose up -d
```

### MCP option not visible in settings

- Ensure you're on **JetBrains IDE 2025.2 or later**
- Ensure **AI Assistant** is enabled (requires JetBrains AI subscription)
- Go to **Settings → Plugins** and verify AI Assistant is installed and active

### Tools not being used by AI Assistant

In the AI Assistant Chat, type `/` to see available commands. MCP tools should appear in the list. If not, try restarting the IDE after applying the MCP configuration.

## Next Steps

- [MCP Tool Reference](mcp-server.md) — Full documentation of all 32 tools and 2 resources
- [Python SDK](../guides/python-sdk.md) — Programmatic access from Python
- [REST API](../guides/rest-api.md) — Direct HTTP access
