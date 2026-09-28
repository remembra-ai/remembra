# Remembra

**One agent stops. The next one already knows.**

## Remembra Relay

When an agent's session ends, Remembra Relay saves a **handoff**: what was done, what is not done, what is
failing and the next step. With the `remembra-relay` hooks, it is read from git and (for Claude Code and Codex) the
session's test runs, not written by an LLM. Agents that hand off through the `close_session` MCP tool (Cursor and
other MCP agents) declare their own facts, and the handoff labels them that way. When the next session starts, in
another tool or on another machine, that agent gets a **brief**: a short summary of where the work stands, led by the
last handoff. Every handoff stays on the **trail**.

First get a free key at [app.remembra.dev](https://app.remembra.dev/signup), then run:

```bash
pipx install --force 'remembra[mcp]>=0.16'
remembra-install --all
remembra-relay connect --apply
```

`remembra-install` asks for the key at a hidden prompt, so it never goes on the command line or into your shell
history. `connect --apply` writes the hooks and keeps a backup of each file; run `remembra-relay connect` alone first
to see every change without writing. Codex runs the hooks only after you trust them: Settings > Hooks > Trust in
the Codex app, or `/hooks` in the Codex CLI (again whenever a hook's command changes).
[Hosting the server yourself](getting-started/docker.md)? Add `--url <your server>` to `remembra-install`. remembra 0.16.0 is the first
release with `remembra-relay`.

- [Relay guide](guides/relay.md): how handoffs, briefs and the trail work, and which agents are verified.
- [Agent setup](getting-started/agent-setup.md): connect Claude Code, Codex, Cursor and other agents.
- [Remembra and other handoff tools](comparisons/handoff-tools.md): how it compares with claude-mem, agentmemory and local tools.

Claude Code's and Codex's session hooks are verified (Codex with codex-cli 0.155.0-alpha.16.4, a prerelease, and
the same tests also pass on the stable 0.157.1; trust them once in Codex). The Gemini CLI, Qwen Code and Kimi Code hooks are verified too (Gemini CLI
0.61.0, Qwen Code 0.24.6 and Kimi Code 2.1.1, each run with a local stand-in for the model). The Cursor hooks are
unverified: Cursor's own hook runner ran them, but no logged-in Cursor session has yet; Cursor uses the MCP tools
`session_brief` and `close_session` until it has.

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

    const memory = new Remembra({ url: 'http://localhost:8787', userId: 'user_123' });

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

    # Claude Code can now store and recall memories across sessions through Remembra's MCP tools.
    # For automatic handoffs at session start and end, run remembra-relay connect.
    ```

## Why Remembra?

### The Problem
Every AI app needs memory. Developers hack together solutions using vector databases, embeddings, and custom retrieval logic. It's complex, fragmented, and everyone rebuilds the same thing.

### Our Approach
- **Self-host in minutes**: one command (`quickstart.sh`) starts Remembra, Qdrant and Ollama with Docker Compose
- **MCP-native**: an MCP server for Claude Code, Cursor and other MCP clients
- **Open source core**: MIT license, own your data
- **Memory features**: entity resolution, temporal decay, hybrid search

## Core Features

<div class="grid cards" markdown>

-   :material-brain:{ .lg .middle } __Smart Extraction__

    ---

    LLM-powered fact extraction transforms messy conversations into clean, searchable memories.

-   :material-account-group:{ .lg .middle } __Entity Resolution__

    ---

    An LLM matcher merges name variants that fit the context, such as "Mr. Smith" and "John Smith". Resolving a
    mention like "my husband" to a named person is best-effort and untested.

-   :material-clock-time-four:{ .lg .middle } __Temporal Memory__

    ---

    TTL support, memory decay, and historical queries with `as_of`.

-   :material-magnify:{ .lg .middle } __Hybrid Search__

    ---

    Semantic + keyword (BM25) search. CrossEncoder reranking with the optional `rerank` extra (on in Remembra
    Cloud).

-   :material-graph:{ .lg .middle } __Entity Graph__

    ---

    Traverse relationships to find related memories across your knowledge graph.

-   :material-connection:{ .lg .middle } __MCP Server__

    ---

    Built-in Model Context Protocol server for Claude Code, Claude Desktop, and Cursor.

</div>

## Quick Start

### 1. Start the Server

=== "Quick Start (One Command)"

    ```bash
    curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
    ```

    Starts Remembra, Qdrant and Ollama with Docker Compose, with auth off and local embeddings.

=== "Docker"

    ```bash
    docker run -d -p 8787:8787 \
      -e REMEMBRA_QDRANT_URL=http://your-qdrant:6333 \
      -e REMEMBRA_OPENAI_API_KEY=sk-your-key \
      -e REMEMBRA_JWT_SECRET=$(openssl rand -hex 32) \
      -v remembra-data:/data \
      remembra/remembra
    ```

    The image does not include Qdrant: run one next to it (see [Docker Deployment](getting-started/docker.md)).

=== "From Source"

    ```bash
    git clone https://github.com/remembra-ai/remembra
    cd remembra
    pip install -e ".[server]"
    remembra-server
    ```

    It needs a running Qdrant and a few settings first; see [Installation](getting-started/installation.md).

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

    Claude Code can now store and recall memories across sessions through Remembra's MCP tools. For automatic
    handoffs at session start and end, run `remembra-relay connect`.

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

    Python SDK, opt-in (`auto_expire_temporal=True`): 38 temporal patterns suggest a TTL, e.g. "Meeting tomorrow" → 36h, "deadline in 2 hours" → 3h, "call next week" → 10 days. SDK 0.16.1 sends some as values a 0.16.1 server cannot read (`1.5d`, `1mo`): with both, those memories get no expiry. Later SDKs send whole hours or days, and later servers read decimals and `mo`.

-   :material-calendar-clock:{ .lg .middle } __Event-Driven Expiry__

    ---

    `expires_at` accepts ISO 8601 timestamps for explicit expiration control. Perfect for event-driven workflows.

-   :material-alert-decagram:{ .lg .middle } __Strict Mode 410 GONE__

    ---

    With `REMEMBRA_STRICT_MODE=true`, a GET or PATCH of an expired memory returns `410 GONE`.

-   :material-lightning-bolt:{ .lg .middle } __Shadow TTLs__

    ---

    Python SDK, opt-in (`enable_shadow_ttl=True`): a local TTL cache you can check with `is_memory_valid()`. Recall does not use it.

</div>

---

## What's New in v0.10.x

<div class="grid cards" markdown>

-   :material-robot:{ .lg .middle } __Universal Agent Installer__

    ---

    `remembra-install --all` auto-detects and configures Claude Code, Codex, Cursor and Gemini CLI, and Claude Desktop on macOS, in one command.

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

    The `recall_memories` MCP tool with `slim=true` returns only the context string (capped at 800 tokens) and a
    count, without the memories or their metadata. In the Python SDK and REST API, `slim=True` only caps the
    context at 800 tokens; memories and entities are still returned.

-   :material-shield-check:{ .lg .middle } __Security Hardening__

    ---

    RBAC enforcement, error sanitization, SSRF protection, and safer defaults across the board.

</div>

---

## What's New in v0.9.0

<div class="grid cards" markdown>

-   :material-clock-time-four:{ .lg .middle } __Temporal Knowledge Graph__

    ---

    Bi-temporal relationship model with `valid_from`, `valid_to`, and `superseded_by`, and point-in-time queries. A relationship gets an end date only when the stored text states one.

-   :material-tools:{ .lg .middle } __6 New MCP Tools__

    ---

    MCP server expanded from 5 → 11 tools: `update_memory`, `search_entities`, `list_memories`, `share_memory`, `timeline`, and `relationships_at`. (It has 31 tools today.)

-   :material-graph:{ .lg .middle } __Entity Graph Visualization__

    ---

    Interactive force-directed graph with flowing particle effects on relationship edges. Click-to-explore entity neighborhoods. (v0.9.0; the current dashboard's map shows agents, projects, trail entries and entities.)

-   :material-calendar-search:{ .lg .middle } __Point-in-Time Queries__

    ---

    Query entity relationships at any historical date. Perfect for tracking job changes, relationship history, and temporal facts.

-   :material-swap-horizontal:{ .lg .middle } __Contradiction Detection__

    ---

    Planned in v0.9.0, not shipped for relationships: a new `WORKS_AT` does not end the old one. Outdated facts are superseded at the memory level instead (since v0.16.0).

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
│  Ingestion   │  Sleep-Time  │  PII Detect   │   Secret       │
│              │  Compute     │  (OWASP)      │   Redaction    │
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
