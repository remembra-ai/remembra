# Remembra

**Persistent memory for AI applications. Self-host in 5 minutes.**

<div class="admonition tip" markdown>
<p class="admonition-title">🚀 v0.8.0 Released!</p>
<p>Now with <strong>One-Command Quick Start</strong>, <strong>Multi-Provider Entity Extraction</strong>, <strong>Performance Boost (httpx connection reuse)</strong>, and <strong>Usage Warnings</strong>. <a href="#whats-new-in-v080">See what's new →</a></p>
</div>

---

## What is Remembra?

Remembra is a universal memory layer for LLMs. It solves the fundamental problem that every AI forgets everything between sessions.

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
    import { Remembra } from '@remembra/client';

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
- **Self-host in 5 minutes**: One Docker command, everything bundled
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

=== "Quick Start (One Command)"

    ```bash
    curl -sSL https://get.remembra.dev/quickstart.sh | bash
    ```

=== "Docker (Recommended)"

    ```bash
    docker run -d -p 8787:8787 remembra/remembra
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
    npm install @remembra/client
    ```

    ```typescript
    import { Remembra } from '@remembra/client';

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

## What's New in v0.8.0

<div class="grid cards" markdown>

-   :material-console:{ .lg .middle } __One-Command Install__

    ---

    `curl -sSL https://get.remembra.dev/quickstart.sh | bash` — Zero-config setup with Ollama embeddings.

-   :material-robot-outline:{ .lg .middle } __Multi-Provider Extraction__

    ---

    Entity extraction now works with OpenAI, Anthropic Claude, and Ollama. Choose the provider that fits your stack.

-   :material-lightning-bolt:{ .lg .middle } __Performance Boost__

    ---

    Persistent HTTP connections via httpx connection reuse reduce latency by 100-300ms per operation.

-   :material-bell-alert:{ .lg .middle } __Usage Warnings__

    ---

    API responses include usage thresholds at 60/80/95% to drive Pro upgrades. Stay informed before hitting limits.

-   :material-docker:{ .lg .middle } __Docker Compose Quickstart__

    ---

    New zero-config compose with Qdrant + Ollama + Remembra. One file, all services, ready to go.

-   :material-test-tube:{ .lg .middle } __125 New Tests__

    ---

    Comprehensive test coverage for embeddings, entities, conflicts, spaces, and plugins.

</div>

### Previous Release (v0.7.2)

<div class="grid cards" markdown>

-   :material-lightning-bolt:{ .lg .middle } __EntityGraph Performance__

    ---

    Eliminated N+1 API calls. Single `/debug/entities/graph` endpoint replaces 50+ individual calls for blazing fast graph loading.

-   :material-database-refresh:{ .lg .middle } __Admin: rebuild-vectors__

    ---

    New `POST /admin/rebuild-vectors` endpoint to fix memories missing from Qdrant. Self-heal vector sync issues without data loss.

-   :material-book-open-page-variant:{ .lg .middle } __Troubleshooting Guide__

    ---

    Comprehensive diagnosis and fix guide for common issues. Step-by-step instructions for self-hosters.

    [View Guide →](TROUBLESHOOTING.md)

-   :material-clipboard-check:{ .lg .middle } __Setup Checklist__

    ---

    10-step verification checklist ensures your Remembra instance is production-ready.

    [View Checklist →](SETUP-CHECKLIST.md)

-   :material-bug-check:{ .lg .middle } __Error Display Fix__

    ---

    Dashboard now shows actual error messages instead of `[object Object]`. Pydantic validation errors properly parsed.

-   :material-filter:{ .lg .middle } __Project Filtering Fix__

    ---

    Fixed recall defaulting to wrong project_id. Memories now correctly filtered by project scope.

</div>

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Your Application / AI Assistant                   │
├──────────┬──────────────┬─────────────────────────────────────┤
│ Python   │ JavaScript   │ MCP Server (Claude/Cursor)          │
│ SDK      │ SDK          │ remembra-mcp                        │
├──────────┴──────────────┴─────────────────────────────────────┤
│                   Remembra REST API                           │
├──────────────┬──────────────┬───────────────┬───────────────┤
│  Extraction  │   Entities   │    Retrieval  │   Temporal    │
│  (LLM-based) │ (Resolution) │(Hybrid Search)│  (TTL/Decay)  │
├──────────────┼──────────────┼───────────────┼───────────────┤
│  Ingestion   │  Sleep-Time  │  PII Detect   │   Anomaly     │
│              │  Compute     │  (OWASP)      │   Detection   │
├──────────────┼──────────────┼───────────────┼───────────────┤
│  Plugins     │ Spaces (RBAC)│               │               │
├──────────────┴──────────────┴───────────────┴───────────────┤
│                      Storage Layer                           │
│         Qdrant (vectors) + SQLite (metadata/graph)          │
└─────────────────────────────────────────────────────────────┘
```

## License

Remembra is open source under the [MIT License](https://github.com/remembra-ai/remembra/blob/main/LICENSE).

Built with :heart: by [DolphyTech](https://dolphytech.com) | [remembra.dev](https://remembra.dev)
