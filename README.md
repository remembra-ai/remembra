<p align="center">
  <img src="https://remembra.dev/logo.svg" alt="Remembra Logo" width="120">
</p>

<h1 align="center">Remembra</h1>

<p align="center">
  <strong>The memory layer for AI that actually works.</strong><br>
  Persistent memory with entity resolution, temporal decay, and graph-aware recall.<br>
  Self-host in 5 minutes. No vendor lock-in.
</p>

<p align="center">
  <a href="https://pypi.org/project/remembra/"><img src="https://img.shields.io/pypi/v/remembra?color=blue&label=PyPI" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/remembra"><img src="https://img.shields.io/npm/v/remembra?color=green&label=npm" alt="npm"></a>
  <a href="https://github.com/remembra-ai/remembra/stargazers"><img src="https://img.shields.io/github/stars/remembra-ai/remembra?style=social" alt="GitHub Stars"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://docs.remembra.dev"><img src="https://img.shields.io/badge/docs-remembra.dev-blue" alt="Documentation"></a>
</p>

<p align="center">
  <a href="https://docs.remembra.dev">Documentation</a> •
  <a href="https://remembra.dev">Website</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#why-remembra">Why Remembra?</a> •
  <a href="https://twitter.com/remembradev">Twitter</a> •
  <a href="https://discord.gg/Bzv3JshRa3">Discord</a>
</p>

---

## 🚀 What's New in v0.7.0

- **Conversation Ingestion** — Auto-extract memories from chat history
- **Sleep-Time Compute** — Background consolidation during idle time
- **PII Detection** — Automatic redaction of sensitive data
- **Anomaly Detection** — Protection against memory poisoning
- **TypeScript SDK** — First-class JavaScript support

---

## The Problem

Every AI app needs memory. Your chatbot forgets users between sessions. Your agent can't recall decisions from yesterday. Your assistant asks the same questions over and over.

**The current solutions suck:**
- Mem0: $249/mo for graph features, self-hosting docs are trash
- Zep: Academic, complex to deploy
- Letta: Research-grade, not production-ready
- LangChain Memory: Too basic, no persistence

## The Solution

```python
from remembra import Memory

memory = Memory(user_id="user_123")

# Store — entities and facts extracted automatically
memory.store("Had a meeting with Sarah from Acme Corp. She prefers email over Slack.")

# Recall — semantic search finds relevant memories
result = memory.recall("How should I contact Sarah?")
print(result.context)
# → "Sarah from Acme Corp prefers email over Slack."

# It knows "Sarah" and "Acme Corp" are entities. It builds relationships.
# It persists across sessions, reboots, context windows. Forever.
```

---

## ⚡ Quick Start (2 Minutes)

### One Command Install

```bash
curl -sSL https://get.remembra.dev/quickstart.sh | bash
```

That's it. Remembra + Qdrant + Ollama start locally. No API keys needed.

**Or with Docker Compose directly:**

```bash
git clone https://github.com/remembra-ai/remembra && cd remembra
docker compose -f docker-compose.quickstart.yml up -d
```

**Try it:**

```bash
# Store a memory
curl -X POST http://localhost:8787/api/v1/memories/store \
  -H "Content-Type: application/json" \
  -d '{"content": "Alice is CEO of Acme Corp", "user_id": "demo"}'

# Recall it
curl -X POST http://localhost:8787/api/v1/memories/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "Who runs Acme?", "user_id": "demo"}'
```

### Connect to Claude (MCP)

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787",
        "REMEMBRA_USER_ID": "default"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

**Cursor** — add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "remembra": {
      "command": "remembra-mcp",
      "env": {
        "REMEMBRA_URL": "http://localhost:8787"
      }
    }
  }
}
```

Now ask Claude: *"Remember that Alice is CEO of Acme Corp"* — then later: *"Who runs Acme?"*

### Python SDK

```bash
pip install remembra
```

```python
from remembra import Memory

memory = Memory(user_id="user_123")
memory.store("Had a meeting with Sarah from Acme Corp. She prefers email over Slack.")
result = memory.recall("How should I contact Sarah?")
print(result.context)  # "Sarah from Acme Corp prefers email over Slack."
```

### TypeScript SDK

```bash
npm install remembra
```

```typescript
import { Remembra } from 'remembra';

const memory = new Remembra({ url: 'http://localhost:8787' });
await memory.store('User prefers dark mode');
const result = await memory.recall('preferences');
```

---

## 🔥 Why Remembra?

### Feature Comparison

| Feature | Remembra | Mem0 | Zep/Graphiti | Letta | Engram |
|---------|----------|------|-------------|-------|--------|
| **One-Command Install** | ✅ `curl \| bash` | ✅ pip | ✅ pip | ⚠️ Complex | ✅ brew |
| **Entity Resolution** | ✅ Free | 💰 $249/mo | ✅ | ❌ | ❌ |
| **Conflict Detection** | ✅ Unique | ❌ | ❌ | ❌ | ❌ |
| **PII Detection** | ✅ Built-in | ❌ | ❌ | ❌ | ❌ |
| **Hybrid Search** | ✅ BM25+Vector | ❌ | ✅ | ❌ | ❌ |
| **6 Embedding Providers** | ✅ Hot-swap | ❌ (1-2) | ❌ (1) | ❌ | ❌ |
| **Plugin System** | ✅ | ❌ | ❌ | ✅ | ❌ |
| **Sleep-Time Compute** | ✅ | ❌ | ❌ | ✅ | ❌ |
| **Self-Host + Billing** | ✅ Stripe | ❌ | ❌ | ❌ | ❌ |
| **Memory Spaces** | ✅ Multi-tenant | ❌ | ❌ | ❌ | ❌ |
| **MCP Server** | ✅ Native | ✅ | ❌ | ❌ | ✅ |
| **Pricing** | Free / $49 / $99 | $19 → $249 | $25+ | Free | Free |
| **License** | MIT | Apache 2.0 | Apache 2.0 | Apache 2.0 | MIT |

### Core Features

🧠 **Smart Extraction** — LLM-powered fact extraction from raw text

👥 **Entity Resolution** — "Adam", "Mr. Smith", "my husband" → same person

⏱️ **Temporal Memory** — TTL, decay curves, historical queries

🔍 **Hybrid Search** — Semantic + keyword for accurate recall

🔒 **Security** — PII detection, anomaly monitoring, audit logs

📊 **Dashboard** — Visual memory browser, entity graphs, analytics

---

## 📖 Documentation

| Resource | Description |
|----------|-------------|
| [Quick Start](https://docs.remembra.dev/getting-started/quickstart/) | Get running in 5 minutes |
| [Python SDK](https://docs.remembra.dev/guides/python-sdk/) | Full Python reference |
| [TypeScript SDK](https://docs.remembra.dev/guides/javascript-sdk/) | JavaScript/TypeScript guide |
| [MCP Server](https://docs.remembra.dev/integrations/mcp-server/) | Claude Code / Cursor setup |
| [REST API](https://docs.remembra.dev/guides/rest-api/) | API reference |
| [Self-Hosting](https://docs.remembra.dev/getting-started/docker/) | Docker deployment guide |

---

## 🛠️ MCP Server

Give Claude Code or Cursor persistent memory with one command:

```bash
pip install remembra[mcp]
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

**Available Tools:**

| Tool | Description |
|------|-------------|
| `store_memory` | Save facts, decisions, context |
| `recall_memories` | Semantic search across memories |
| `forget_memories` | GDPR-compliant deletion |
| `ingest_conversation` | Auto-extract from chat history |
| `health_check` | Verify connection |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Your Application                          │
├──────────┬──────────────┬───────────────────────────────────┤
│ Python   │ TypeScript   │ MCP Server (Claude/Cursor)        │
│ SDK      │ SDK          │ remembra-mcp                      │
├──────────┴──────────────┴───────────────────────────────────┤
│                   Remembra REST API                          │
├──────────────┬──────────────┬───────────────┬───────────────┤
│  Extraction  │   Entities   │   Retrieval   │   Security    │
│  (LLM)       │  (Graph)     │ (Hybrid)      │  (PII/Audit)  │
├──────────────┴──────────────┴───────────────┴───────────────┤
│                    Storage Layer                             │
│         Qdrant (vectors) + SQLite (metadata/graph)          │
└─────────────────────────────────────────────────────────────┘
```

---

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
# Clone
git clone https://github.com/remembra-ai/remembra
cd remembra

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Start dev server
remembra-server --reload
```

---

## 📄 License

MIT License — Use it however you want.

---

## ⭐ Star History

If Remembra helps you, please star the repo! It helps others discover the project.

[![Star History Chart](https://api.star-history.com/svg?repos=remembra-ai/remembra&type=Date)](https://star-history.com/#remembra-ai/remembra&Date)

---

<p align="center">
  Built with ❤️ by <a href="https://dolphytech.com">DolphyTech</a><br>
  <a href="https://remembra.dev">remembra.dev</a> • <a href="https://docs.remembra.dev">docs</a> • <a href="https://twitter.com/remembradev">twitter</a> • <a href="https://discord.gg/Bzv3JshRa3">discord</a>
</p>
