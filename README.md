# Remembra - AI Memory Layer

> Persistent memory for AI applications. Self-host in 5 minutes.

<!-- mcp-name: io.github.remembra-ai/remembra -->

[![PyPI](https://img.shields.io/pypi/v/remembra)](https://pypi.org/project/remembra/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

## What Is This?

Remembra is a universal memory layer for LLMs. It solves the fundamental problem that every AI forgets everything between sessions.

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

## MCP Server (Claude Code / Cursor)

Remembra ships with a built-in [Model Context Protocol](https://modelcontextprotocol.io) server. Any MCP-compatible AI assistant (Claude Code, Claude Desktop, Cursor, etc.) can use it as persistent memory.

### Setup

```bash
pip install remembra[mcp]
```

Add to your Claude Code config:

```bash
claude mcp add remembra \
  -e REMEMBRA_URL=http://localhost:8787 \
  -e REMEMBRA_API_KEY=your_key \
  -- remembra-mcp
```

Or add manually to `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787",
        "REMEMBRA_API_KEY": "your_key"
      }
    }
  }
}
```

### MCP Tools

| Tool | Description |
|------|-------------|
| `store_memory` | Save facts, decisions, context to persistent memory |
| `recall_memories` | Hybrid search (semantic + keyword) across all memories |
| `forget_memories` | GDPR-compliant deletion by ID, entity, or all |
| `health_check` | Verify server connection and health |

### MCP Resources

| Resource | Description |
|----------|-------------|
| `memory://recent` | Last 10 stored memories |
| `memory://status` | Server status and config |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_URL` | `http://localhost:8787` | Remembra server URL |
| `REMEMBRA_API_KEY` | — | API key for authentication |
| `REMEMBRA_USER_ID` | `default` | User ID for memory isolation |
| `REMEMBRA_PROJECT` | `default` | Project namespace |
| `REMEMBRA_MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, or `streamable-http` |

## Why We're Building This

### The Problem
Every AI app needs memory. Developers hack together solutions using vector databases, embeddings, and custom retrieval logic. It's complex, fragmented, and everyone rebuilds the same thing.

### Current Solutions Fall Short
- **Mem0**: Pricing jumps from $19 to $249, self-hosting is complex
- **Zep**: Academic, complex to deploy
- **Letta**: Not production-ready
- **LangChain Memory**: Too basic, no persistence

### Our Approach
- **Self-host in 5 minutes**: One Docker command, everything bundled
- **MCP-native**: Works with Claude Code and Cursor out of the box
- **Open source core**: MIT license, own your data
- **Built for production**: Entity resolution, temporal decay, hybrid search

## Core Features

### Hybrid Search
Vector (semantic) + BM25 (keyword) search combined. Finds memories even when the query doesn't match exact words.

### Entity Resolution
Knows that "Adam", "Adam Smith", "Mr. Smith", and "my husband" are the same person. Automatically extracts and links people, organizations, locations, and concepts.

### Temporal Awareness
Memories have time context. TTL support. Ebbinghaus-inspired decay curves. Historical ("as of") queries.

### Hybrid Storage
Vector (Qdrant) + Graph (relationships) + Relational (SQLite metadata) in one system.

### Observability Dashboard
See what's stored, debug retrievals, visualize entity graphs.

## Quick Start

### 1. Start the Server

```bash
docker run -d -p 8787:8787 remembra/remembra
```

### 2. Install the SDK

**Python:**
```bash
pip install remembra
```

```python
from remembra import Memory

memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    project="my_app"
)

# Store
result = memory.store("User's name is John. He's a software engineer at Google.")
print(result.extracted_facts)
# → ["John is a software engineer at Google."]

# Recall
result = memory.recall("Who is the user?")
print(result.context)
# → "John is a software engineer at Google."

# Forget
memory.forget(memory_id=result.memories[0].id)
```

**JavaScript/TypeScript:**
```bash
npm install @remembra/client
```

```typescript
import { Remembra } from '@remembra/client';

const memory = new Remembra({
  url: 'http://localhost:8787',
  apiKey: 'rem_xxx',
});

// Store
const stored = await memory.store('Alice is the CTO of Acme Corp');
console.log(stored.extracted_facts);

// Recall
const result = await memory.recall('Who leads Acme?');
console.log(result.context);
// → "Alice is the CTO of Acme Corp."

// Entities
const entities = await memory.listEntities({ type: 'person' });
```

## API Reference

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/memories` | Store a memory |
| `POST` | `/api/v1/memories/recall` | Search memories |
| `GET` | `/api/v1/memories/{id}` | Get specific memory |
| `DELETE` | `/api/v1/memories` | Delete memories |
| `GET` | `/api/v1/entities` | List entities |
| `GET` | `/api/v1/entities/{id}/relationships` | Get entity relationships |
| `GET` | `/api/v1/temporal/decay/report` | Memory decay report |
| `POST` | `/api/v1/ingest/changelog` | Ingest changelog |
| `GET` | `/health` | Server health check |

Full API docs available at `http://localhost:8787/docs` (Swagger UI).

## Documentation

- [Architecture](./ARCHITECTURE.md) - Technical architecture details
- [API Reference](./API.md) - Full API documentation
- [Product Spec](./PRODUCT-SPEC.md) - Product specification

## License

MIT License - Use it however you want.

---

Built by [DolphyTech](https://dolphytech.com) | [remembra.dev](https://remembra.dev)
