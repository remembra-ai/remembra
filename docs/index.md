# Remembra

**One agent stops. The next one already knows.**

## Remembra Relay

When an agent's session ends, Remembra Relay saves a **handoff**: what was done, what is not done, what is
failing and the next step, read from git and (for Claude Code) the session's test runs, not written by an LLM.
When the next session starts, in any tool, on any machine, that agent gets a **brief**: a short summary of
where the work stands, led by the last handoff. Every handoff stays on the **trail**.

```bash
pipx install 'remembra[mcp]'
remembra-install --all --api-key <your-key>
remembra-relay connect
```

Get a free key at [app.remembra.dev](https://app.remembra.dev/signup), or [host the server yourself](getting-started/docker.md).
`connect` is a dry run that shows what it would change; add `--apply` to write the hooks. remembra 0.16.0 is the
first release with `remembra-relay`.

- [Relay guide](guides/relay.md): how handoffs, briefs and the trail work, and which agents are verified.
- [Agent setup](getting-started/agent-setup.md): connect Claude Code, Codex, Cursor and other agents.
- [Remembra and other handoff tools](comparisons/handoff-tools.md): how it compares with claude-mem, agentmemory and local tools.

Claude Code's session hooks are verified. The hooks for Codex, Cursor, Gemini CLI, Qwen Code and Kimi follow
each tool's docs but have not been run against it yet; those agents use the MCP tools `session_brief` and
`close_session` until they are.

---

## The memory API underneath

Remembra Relay runs on Remembra's memory layer, which you can also call directly from Python, JavaScript or any MCP client.

=== "Python"

    ```python
    from remembra import Memory

    memory = Memory(user_id="user_123")

    # Store memories
    memory.store("User prefers dark mode and works at Acme Corp")

    # Recall with context
    result = memory.recall("What are user's preferences?")
    print(result.context)
    # → "User prefers dark mode. Works at Acme Corp."
    ```

=== "JavaScript"

    ```typescript
    import { Remembra } from 'remembra';

    const memory = new Remembra({ url: 'http://localhost:8787' });

    // Store memories
    await memory.store('User prefers dark mode and works at Acme Corp');

    // Recall with context
    const result = await memory.recall("What are user's preferences?");
    console.log(result.context);
    // → "User prefers dark mode. Works at Acme Corp."
    ```

=== "MCP (Claude Code)"

    ```bash
    # Install
    pip install remembra[mcp]

    # Add to Claude Code
    claude mcp add remembra \
      -e REMEMBRA_URL=http://localhost:8787 \
      -- remembra-mcp

    # Claude now has persistent memory across sessions!
    ```

## Why Remembra?

### The Problem
Every AI app needs memory. Developers hack together solutions using vector databases, embeddings, and custom retrieval logic. It's complex, fragmented, and everyone rebuilds the same thing.

### Our Approach
- **Self-host in minutes**: One Docker command, everything bundled
- **MCP-native**: Works with Claude Code and Cursor out of the box
- **Open source core**: MIT license, own your data
- **Built for production**: Entity resolution, temporal decay, hybrid search

## Core Features

<div class="grid cards" markdown>

-   :material-brain:{ .lg .middle } __Smart Extraction__

    ---

    LLM-powered fact extraction transforms messy conversations into clean, searchable memories.

-   :material-account-group:{ .lg .middle } __Entity Resolution__

    ---

    Knows that "Adam", "Mr. Smith", and "my husband" are the same person.

-   :material-clock-time-four:{ .lg .middle } __Temporal Memory__

    ---

    TTL support, memory decay, and historical queries with `as_of`.

-   :material-magnify:{ .lg .middle } __Hybrid Search__

    ---

    Semantic + keyword search with CrossEncoder reranking for accurate recall.

-   :material-graph:{ .lg .middle } __Entity Graph__

    ---

    Traverse relationships to find related memories across your knowledge graph.

-   :material-connection:{ .lg .middle } __MCP Server__

    ---

    Built-in Model Context Protocol server for Claude Code, Claude Desktop, and Cursor.

</div>

## Quick Start

### 1. Start the Server

=== "Docker (Recommended)"

    ```bash
    docker run -d -p 8787:8787 remembra/remembra
    ```

=== "Quick Start (One Command)"

    ```bash
    curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
    ```

=== "From Source"

    ```bash
    git clone https://github.com/remembra-ai/remembra
    cd remembra
    pip install -e ".[server]"
    remembra-server
    ```

### 2. Use an SDK

=== "Python"

    ```bash
    pip install remembra
    ```

    ```python
    from remembra import Memory

    memory = Memory(
        base_url="http://localhost:8787",
        user_id="user_123"
    )

    memory.store("User's name is John. He's a software engineer at Google.")
    result = memory.recall("Who is the user?")
    print(result.context)
    # → "John is a software engineer at Google."
    ```

=== "JavaScript"

    ```bash
    npm install remembra
    ```

    ```typescript
    import { Remembra } from 'remembra';

    const memory = new Remembra({
      url: 'http://localhost:8787',
      userId: 'user_123',
    });

    await memory.store("User's name is John. He's a software engineer at Google.");
    const result = await memory.recall('Who is the user?');
    console.log(result.context);
    // → "John is a software engineer at Google."
    ```

=== "MCP Server"

    ```bash
    pip install remembra[mcp]
    claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
    ```

    Claude Code now has persistent memory. It will automatically store and recall context across sessions.

    [MCP Setup Guide :material-arrow-right:](integrations/mcp-server.md){ .md-button }

=== "REST API"

    ```bash
    # Store
    curl -X POST http://localhost:8787/api/v1/memories \
      -H "Content-Type: application/json" \
      -d '{"content": "John is a software engineer at Google", "user_id": "user_123"}'

    # Recall
    curl -X POST http://localhost:8787/api/v1/memories/recall \
      -H "Content-Type: application/json" \
      -d '{"query": "Who is John?", "user_id": "user_123"}'
    ```

[Get Started :material-arrow-right:](getting-started/quickstart.md){ .md-button .md-button--primary }
[View on GitHub :material-github:](https://github.com/remembra-ai/remembra){ .md-button }

---

## What's New in v0.13.x

<div class="grid cards" markdown>

-   :material-view-dashboard:{ .lg .middle } __Dashboard v2__

    ---

    Teams, activity logs, entity browser, timeline fixes, retrieval settings, diagnostics, and admin surfaces for operating memory.

-   :material-archive:{ .lg .middle } __Cold Archive__

    ---

    Decayed memories can move to queryable archive storage and be restored back into active recall when needed.

-   :material-tune:{ .lg .middle } __Adaptive Thresholds__

    ---

    Cleanup and pruning can adjust by session mode, memory density, warm-up phase, and result quality.

-   :material-account-box:{ .lg .middle } __User Profiles API__

    ---

    `GET /api/v1/users/{user_id}/profile` returns aggregated facts, activity metrics, top topics, and memory count.

-   :material-clock-alert:{ .lg .middle } __Smart Auto-Forgetting__

    ---

    35+ temporal patterns automatically set TTL. "Meeting tomorrow" → 36h, "deadline in 2 hours" → 3h, "call next week" → 8 days. Zero configuration needed.

-   :material-calendar-clock:{ .lg .middle } __Event-Driven Expiry__

    ---

    `expires_at` accepts ISO 8601 timestamps for explicit expiration control. Perfect for event-driven workflows.

-   :material-alert-decagram:{ .lg .middle } __Strict Mode 410 GONE__

    ---

    Enable `REMEMBRA_STRICT_MODE=true` for explicit expiry handling. Expired memory requests return `410 GONE` instead of silent accept.

-   :material-lightning-bolt:{ .lg .middle } __Shadow TTLs__

    ---

    SDK maintains local expiry cache, skipping recall for known-expired memories. Reduces API calls by up to 40%.

</div>

---

## What's New in v0.10.x

<div class="grid cards" markdown>

-   :material-robot:{ .lg .middle } __Universal Agent Installer__

    ---

    `remembra-install --all` auto-detects and configures Claude, Codex, Cursor, Gemini, Windsurf in one command.

-   :material-stethoscope:{ .lg .middle } __Setup Diagnostics__

    ---

    `remembra-doctor <agent>` diagnoses connection issues with clear failure labels: `dns_failure`, `sandbox_blocked`, `auth_failure`.

-   :material-bridge:{ .lg .middle } __Local Bridge__

    ---

    `remembra-bridge` tunnels sandboxed agents (Codex, Claude Code) to your local/remote Remembra server.

-   :material-key:{ .lg .middle } __Centralized Credentials__

    ---

    API keys stored securely in `~/.remembra/credentials` (chmod 600). No more repeating `--api-key` on every command.

-   :material-lightning-bolt:{ .lg .middle } __Slim Recall Mode__

    ---

    `recall(query, slim=True)` returns 90% smaller payloads—just the context string, no metadata bloat.

-   :material-shield-check:{ .lg .middle } __Security Hardening__

    ---

    RBAC enforcement, error sanitization, SSRF protection, and safer defaults across the board.

</div>

---

## What's New in v0.9.0

<div class="grid cards" markdown>

-   :material-clock-time-four:{ .lg .middle } __Temporal Knowledge Graph__

    ---

    Bi-temporal relationship model with `valid_from`, `valid_to`, and `superseded_by`. Ask "Where did Alice work in January 2022?" and get accurate historical answers.

-   :material-tools:{ .lg .middle } __6 New MCP Tools__

    ---

    MCP server expanded from 5 → 11 tools: `update_memory`, `search_entities`, `list_memories`, `share_memory`, `timeline`, and `relationships_at`.

-   :material-graph:{ .lg .middle } __Entity Graph Visualization__

    ---

    Interactive force-directed graph with flowing particle effects on relationship edges. Click-to-explore entity neighborhoods.

-   :material-calendar-search:{ .lg .middle } __Point-in-Time Queries__

    ---

    Query entity relationships at any historical date. Perfect for tracking job changes, relationship history, and temporal facts.

-   :material-swap-horizontal:{ .lg .middle } __Contradiction Detection__

    ---

    New relationships automatically supersede old ones. "Alice works at Meta" correctly supersedes "Alice works at Google" with full history preserved.

-   :material-share-variant:{ .lg .middle } __Cross-Agent Memory Sharing__

    ---

    Share memories between agents via Spaces. New `share_memory` MCP tool enables collaborative agent workflows.

</div>

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Your Application / AI Assistant                 │
├──────────┬──────────────┬────────────────────────────────────┤
│ Python   │ JavaScript   │ MCP Server (Claude/Cursor)         │
│ SDK      │ SDK          │ remembra-mcp                       │
├──────────┴──────────────┴────────────────────────────────────┤
│                   Remembra REST API                          │
├──────────────┬──────────────┬───────────────┬────────────────┤
│  Extraction  │   Entities   │   Retrieval   │   Temporal     │
│  (LLM-based) │ (Resolution) │(Hybrid Search)│  (TTL/Decay)   │
├──────────────┼──────────────┼───────────────┼────────────────┤
│  Ingestion   │  Sleep-Time  │  PII Detect   │   Anomaly      │
│              │  Compute     │  (OWASP)      │   Detection    │
├──────────────┼──────────────┼───────────────┼────────────────┤
│  Plugins     │ Spaces (RBAC)│               │                │
├──────────────┴──────────────┴───────────────┴────────────────┤
│                      Storage Layer                           │
│         Qdrant (vectors) + SQLite (metadata/graph)           │
└─────────────────────────────────────────────────────────────┘
```

## License

Remembra is open source under the [MIT License](https://github.com/remembra-ai/remembra/blob/main/LICENSE).

Built with :heart: by [DolphyTech](https://dolphytech.com) | [remembra.dev](https://remembra.dev)
