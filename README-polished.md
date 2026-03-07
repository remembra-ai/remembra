<p align="center">
  <img src="https://remembra.dev/logo.svg" alt="Remembra Logo" width="140">
</p>

<h1 align="center">Remembra</h1>

<p align="center">
  <strong>AI memory that actually works.</strong><br>
  Your AI forgets everything. Remembra fixes that.<br>
  Self-host in minutes. No vendor lock-in. MIT licensed.
</p>

<p align="center">
  <a href="https://pypi.org/project/remembra/"><img src="https://img.shields.io/pypi/v/remembra?color=blue&label=PyPI" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/remembra"><img src="https://img.shields.io/npm/v/remembra?color=green&label=npm" alt="npm"></a>
  <a href="https://github.com/remembra-ai/remembra/stargazers"><img src="https://img.shields.io/github/stars/remembra-ai/remembra?style=social" alt="GitHub Stars"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://docs.remembra.dev"><img src="https://img.shields.io/badge/docs-live-blue" alt="Documentation"></a>
  <a href="https://discord.gg/Bzv3JshRa3"><img src="https://img.shields.io/discord/1234567890?color=7289da&label=Discord&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="https://docs.remembra.dev">📚 Docs</a> •
  <a href="https://remembra.dev">🌐 Website</a> •
  <a href="#-quick-start">⚡ Quick Start</a> •
  <a href="https://twitter.com/remembradev">𝕏 Twitter</a> •
  <a href="https://discord.gg/Bzv3JshRa3">💬 Discord</a>
</p>

---

## 💡 Why Remembra Exists

> *"Dad, why doesn't the AI remember what I told it yesterday?"*  
> — My 10-year-old daughter, asking the question that started everything

Every AI app has the same problem: **amnesia**. Your chatbot forgets users between sessions. Your agent can't recall decisions from yesterday. Your assistant asks the same questions over and over.

We built Remembra because existing solutions either cost a fortune ($249/mo for basic features) or require a PhD to deploy. 

**Remembra is different:** One command to start. Free tier that actually works. Entity graphs and temporal memory included.

---

## 🚀 What's New in v0.7.0

✨ **Conversation Ingestion** — Auto-extract memories from chat history  
⏰ **Sleep-Time Compute** — Background consolidation during idle time  
🔒 **PII Detection** — Automatic redaction of sensitive data  
🛡️ **Anomaly Detection** — Protection against memory poisoning  
📦 **TypeScript SDK** — First-class JavaScript support  

---

## ⚡ Quick Start

### Docker (Recommended)

```bash
# Start the server
docker run -d -p 8787:8787 remembra/remembra

# Install SDK
pip install remembra

# Done. That's it.
```

### Python

```python
from remembra import Memory

memory = Memory(user_id="user_123")

# Store — entities and facts extracted automatically
memory.store("Had coffee with Sarah from Acme Corp. She prefers email over Slack.")

# Recall — semantic search finds relevant memories  
result = memory.recall("How should I contact Sarah?")
print(result.context)
# → "Sarah from Acme Corp prefers email over Slack."

# It knows "Sarah" and "Acme Corp" are entities.
# It builds relationships. It persists forever.
```

### MCP Server (Claude Code / Cursor)

```bash
pip install remembra[mcp]
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp

# Now Claude has persistent memory across all sessions! 🧠
```

### TypeScript / JavaScript

```typescript
import { Remembra } from 'remembra';

const memory = new Remembra({ url: 'http://localhost:8787' });
await memory.store('User prefers dark mode');
const result = await memory.recall('preferences');
```

---

## 🔥 How We Compare

| Feature | Remembra | Mem0 | Zep | Letta |
|---------|:--------:|:----:|:---:|:-----:|
| **Self-host in 5 min** | ✅ | ❌ | ⚠️ | ❌ |
| **Entity Resolution** | ✅ Free | 💰 $249/mo | ✅ | ❌ |
| **Temporal Features** | ✅ | ❌ | ✅ | ✅ |
| **Conversation Ingestion** | ✅ | ✅ | ❌ | ❌ |
| **Sleep-Time Compute** | ✅ | ❌ | ❌ | ✅ |
| **PII Detection** | ✅ | ❌ | ❌ | ❌ |
| **MCP Server** | ✅ | ✅ | ✅ | ❌ |
| **TypeScript SDK** | ✅ | ✅ | ✅ | ❌ |
| **Free Tier** | ✅ Generous | ⚠️ Limited | ❌ | ✅ |

**TL;DR:** We're the only solution with entity graphs, temporal memory, PII detection, AND easy self-hosting in the free tier.

---

## 🧠 Core Features

| Feature | What It Does |
|---------|--------------|
| **🔍 Smart Extraction** | LLM-powered fact extraction from raw text |
| **👥 Entity Resolution** | "Adam", "Mr. Smith", "my husband" → same person |
| **⏱️ Temporal Memory** | TTL, decay curves, historical queries |
| **🎯 Hybrid Search** | Semantic + keyword for accurate recall |
| **🔒 Security** | PII detection, anomaly monitoring, audit logs |
| **📊 Dashboard** | Visual memory browser, entity graphs, analytics |
| **🔌 MCP Integration** | Native Claude Code / Cursor support |
| **🌐 Multi-language** | Python, TypeScript, REST API |

---

## 📖 Documentation

| Guide | Description |
|-------|-------------|
| [🚀 Quick Start](https://docs.remembra.dev/getting-started/quickstart/) | Running in minutes |
| [🐍 Python SDK](https://docs.remembra.dev/guides/python-sdk/) | Full Python reference |
| [📘 TypeScript SDK](https://docs.remembra.dev/guides/javascript-sdk/) | JavaScript/TypeScript guide |
| [🤖 MCP Server](https://docs.remembra.dev/integrations/mcp-server/) | Claude Code / Cursor setup |
| [🔌 REST API](https://docs.remembra.dev/guides/rest-api/) | API reference |
| [🐳 Docker](https://docs.remembra.dev/getting-started/docker/) | Self-hosting guide |

---

## 💰 Pricing

| Plan | Price | Memories | API Calls |
|------|-------|----------|-----------|
| **Free** | $0 | 1,000 | 10K/mo |
| **Pro** | $29/mo | 50,000 | 500K/mo |
| **Team** | $99/mo | 500,000 | 5M/mo |
| **Enterprise** | Custom | Unlimited | Unlimited |

Self-hosted is always **free and unlimited**.

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

We love contributions! See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
git clone https://github.com/remembra-ai/remembra
cd remembra
pip install -e ".[dev]"
pytest
```

---

## ⭐ Star Us!

If Remembra helps you build better AI, please star the repo! It helps others discover the project.

[![Star History Chart](https://api.star-history.com/svg?repos=remembra-ai/remembra&type=Date)](https://star-history.com/#remembra-ai/remembra&Date)

---

<p align="center">
  <strong>Built with ❤️ by <a href="https://dolphytech.com">DolphyTech</a></strong><br>
  <a href="https://remembra.dev">Website</a> • 
  <a href="https://docs.remembra.dev">Docs</a> • 
  <a href="https://twitter.com/remembradev">Twitter</a> • 
  <a href="https://discord.gg/Bzv3JshRa3">Discord</a>
</p>

<p align="center">
  <sub>MIT License — Use it however you want.</sub>
</p>
