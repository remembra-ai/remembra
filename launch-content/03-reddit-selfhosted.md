# Reddit — r/selfhosted Post

## WHEN TO POST
- r/selfhosted loves Docker Compose stacks, privacy-first tools
- Active weekends — post TODAY

---

## TITLE
```
Remembra — self-hosted memory server for AI agents (Docker Compose, Ollama, no cloud dependency)
```

## BODY

```
I built a self-hosted memory layer for AI tools and I think the r/selfhosted crowd would appreciate the approach.

**The problem:** Every AI coding session starts from scratch. Claude, Copilot, Cursor — none of them remember what you told them yesterday.

**What Remembra does:**
- Persistent memory server that any MCP-compatible AI tool can use
- Stores and recalls facts, preferences, decisions across sessions
- Entity extraction, conflict detection, temporal memory
- Everything runs locally — no cloud, no API keys required

**Self-hosting stack:**
```yaml
# docker-compose.yml — that's it
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
  ollama:
    image: ollama/ollama:latest
    ports: ["11434:11434"]
  remembra:
    image: remembra/remembra:latest
    ports: ["8787:8787"]
    environment:
      - EMBEDDING_PROVIDER=ollama
```

One `docker compose up -d` and you're running. No external dependencies.

**Security (because your memories are yours):**
- AES-256-GCM encryption at rest
- PII detection — automatically catches SSNs, credit cards, API keys before storing
- RBAC with isolated memory spaces
- Audit logging
- MIT licensed — you own everything

**Works with:**
Claude Code, Claude Desktop, Cursor, VS Code + Copilot, Windsurf, JetBrains, Zed, OpenAI Codex

GitHub: https://github.com/remembra-ai/remembra
Docs: https://docs.remembra.dev
Docker Hub: remembra/remembra
```
