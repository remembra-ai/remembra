# Reddit — r/LocalLLaMA Post

## WHEN TO POST
- r/LocalLLaMA is active all day, peak around 10am-2pm EST
- This community loves self-hosted, privacy-first tools
- Post TODAY — this is your core audience

## HOW TO POST
1. Go to https://reddit.com/r/LocalLLaMA/submit
2. Select "Text" post type
3. Paste title and body below

---

## TITLE
```
I built an open-source memory layer for AI agents — self-hosted, runs with Ollama, 100% on LoCoMo benchmark
```

## BODY

```
After spending months watching my AI coding sessions lose all context every time I closed the terminal, I built Remembra — an open-source memory server that gives any AI tool persistent memory.

**What it does:**
- Stores facts, preferences, and context from your conversations
- Retrieves them automatically when relevant
- Extracts entities and relationships ("Sarah from Acme" → PERSON + ORGANIZATION)
- Works as an MCP server — plug into Claude Code, Cursor, VS Code, Windsurf, etc.

**Why I'm posting here — it runs 100% locally with Ollama:**
- Embeddings: Ollama (nomic-embed-text or any model you want)
- Vector store: Qdrant (self-hosted)
- Entity extraction: Works with Ollama models (llama3.1, etc.)
- No data leaves your machine. Ever.

**Benchmarks:**
Just ran the LoCoMo benchmark (Snap Research, ACL 2024):
- Single-hop recall: 100%
- Multi-hop reasoning: 100%
- Temporal queries: 100%
- Open-domain: 100%

**Quick start:**
```bash
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
```

This pulls Qdrant + Ollama + Remembra via Docker Compose. No API keys needed.

**Security features (because your memories are sensitive):**
- AES-256-GCM encryption at rest
- PII detection (13 patterns — SSNs, credit cards, API keys, etc.)
- RBAC with memory spaces
- Full audit logging

**Links:**
- GitHub: https://github.com/remembra-ai/remembra
- Docs: https://docs.remembra.dev
- Docker: remembra/remembra on Docker Hub

MIT licensed. Happy to answer questions.
```

## AFTER POSTING
- Reply to every comment
- If someone asks about Ollama model recommendations, suggest nomic-embed-text for embeddings and llama3.1 for entity extraction
- If someone asks how it compares to Mem0, key points: PII detection (Mem0 doesn't have it), entity extraction free (Mem0 charges $249/mo), runs fully local (Mem0 requires cloud)
