# Dev.to Article

## HOW TO POST
1. Go to https://dev.to/new
2. Paste the title and body below
3. Add tags: ai, opensource, mcp, python
4. Add a cover image (use a screenshot of the terminal or the landing page)
5. Publish

---

## TITLE
```
I Built an Open-Source Memory Layer for AI Agents — Here's How It Scored 100% on an Academic Benchmark
```

## BODY

```markdown
Every AI coding session starts the same way: explaining your tech stack, your preferences, your project structure — again. Claude forgets. Copilot forgets. Cursor forgets.

I got tired of it, so I built **Remembra** — an open-source memory server that gives any AI tool persistent memory across sessions.

## What It Does

Remembra runs as an MCP (Model Context Protocol) server. One command, and your AI assistant can store and recall memories:

```bash
pip install remembra[mcp]
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

Now when you tell Claude "I prefer TypeScript and deploy to Vercel," it actually remembers that tomorrow. And next week.

## The Benchmark

We ran the [LoCoMo benchmark](https://github.com/snap-research/locomo) (Snap Research, ACL 2024) — the standard academic test for AI memory systems:

| Category | Accuracy |
|----------|----------|
| Single-hop (direct recall) | **100%** |
| Multi-hop (cross-session reasoning) | **100%** |
| Temporal (time-based queries) | **100%** |
| Open-domain (memory + world knowledge) | **100%** |

The benchmark runner is open-source — you can reproduce these results yourself.

## What Makes It Different

I looked at every competitor before building this. Here's what Remembra has that others don't:

### 1. PII Detection (No Competitor Has This)

Remembra scans every memory for sensitive data before storing it — SSNs, credit card numbers, API keys, AWS credentials. 13 regex patterns with detect/redact/block modes.

Your AI's memory shouldn't accidentally store your production database password.

### 2. Encryption at Rest

AES-256-GCM field-level encryption with PBKDF2-HMAC-SHA256 key derivation (480K iterations). Your memories are encrypted in the vector store. Even if someone gets access to the database, they can't read your data.

### 3. Entity Resolution

Tell it "Sarah from Acme called about the project." Remembra extracts:
- **Sarah** → PERSON
- **Acme** → ORGANIZATION
- **Sarah works at Acme** → RELATIONSHIP

Later, ask "What did Sarah say?" and it connects the dots.

### 4. Runs 100% Local

Qdrant (vector store) + Ollama (embeddings) + Remembra. All in Docker Compose. No API keys needed. No data leaves your machine.

### 5. Works With Everything

9 AI tools supported with copy-paste setup guides:

- Claude Code, Claude Desktop
- Cursor
- VS Code + GitHub Copilot
- Windsurf
- JetBrains (IntelliJ, PyCharm, WebStorm...)
- Zed
- OpenAI Codex
- Any MCP client

## Quick Start

```bash
# One command — pulls Qdrant + Ollama + Remembra
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash

# Add to Claude Code
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

## The Stack

- **Python** backend (FastAPI)
- **Qdrant** for vector storage
- **Ollama** for local embeddings
- Hybrid search: BM25 + semantic vectors
- Multi-signal ranking (semantic + recency + entity + keyword + access frequency)
- TypeScript + Python SDKs
- REST API
- MIT licensed

## Links

- **GitHub**: [github.com/remembra-ai/remembra](https://github.com/remembra-ai/remembra)
- **Docs**: [docs.remembra.dev](https://docs.remembra.dev)
- **Docker Hub**: [remembra/remembra](https://hub.docker.com/r/remembra/remembra)

Would love to hear your thoughts. What features would you want in an AI memory system?
```

## TAGS
```
ai, opensource, mcp, python, selfhosted
```
