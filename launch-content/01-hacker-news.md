# Hacker News — Show HN Post

## WHEN TO POST
- Best times: Tuesday-Thursday, 8-10am EST (when HN traffic peaks)
- March 11 (launch day) is a Tuesday — perfect
- But you can also post TODAY (Saturday) as a soft launch

## HOW TO POST
1. Go to https://news.ycombinator.com/submit
2. Title goes in "title" field
3. URL goes in "url" field (leave text blank when using URL)
4. After posting, immediately add the first comment (below)

---

## TITLE (pick one)

Option A (recommended):
```
Show HN: Remembra – Open-source memory layer for AI agents (100% on LoCoMo benchmark)
```

Option B:
```
Show HN: Remembra – Self-hosted AI memory with PII detection and encryption at rest
```

Option C:
```
Show HN: Remembra – Give Claude/Cursor/VS Code persistent memory across sessions
```

## URL
```
https://github.com/remembra-ai/remembra
```

## FIRST COMMENT (post immediately after submitting)

```
Hey HN — I built Remembra because I got tired of every AI coding session starting from scratch. Claude would forget my entire project context the moment I closed the terminal.

Remembra is a self-hosted memory server for AI agents. It works as an MCP server — one command and Claude Code, Cursor, VS Code, Windsurf, or any MCP client has persistent memory.

What makes it different from Mem0/Zep/Letta:

- **PII detection built-in** — 13 regex patterns detect SSNs, credit cards, API keys before they're stored. No competitor does this.
- **Entity extraction included free** — Mem0 charges $249/mo for theirs.
- **100% on LoCoMo benchmark** — Scored 100% across single-hop, multi-hop, temporal, and open-domain memory questions.
- **AES-256-GCM encryption at rest** — Field-level encryption with PBKDF2 key derivation.
- **Lightest infrastructure** — Just Qdrant + Ollama. No Neo4j, no Postgres, no Redis.
- **Self-host + monetize** — Full billing stack with Stripe if you want to offer it as a service.

Quick start:
```
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

MIT licensed. Python + TypeScript SDKs. Docker image at remembra/remembra.

Happy to answer any questions about the architecture, benchmarks, or security model.
```

## TIPS
- Respond to EVERY comment in the first 2 hours — HN rewards engagement
- Don't be defensive about criticism — acknowledge valid points
- If someone asks about competitors, be honest about tradeoffs
- Don't ask friends to upvote (HN detects and penalizes this)
