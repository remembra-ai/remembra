# Discord — AI/Dev Community Posts

## WHERE TO POST

### High-Priority Discord Servers (post TODAY)

1. **Ollama Discord** — https://discord.gg/ollama
   - Channel: #showcase or #projects
   - Angle: "Memory server that runs 100% local with Ollama"

2. **LangChain Discord** — https://discord.gg/langchain
   - Channel: #showcase or #projects
   - Angle: "Persistent memory layer for AI agents"

3. **Cursor Discord** — https://discord.gg/cursor (if they have one)
   - Channel: #extensions or #tools
   - Angle: "Give Cursor persistent memory with one config file"

4. **Claude/Anthropic Discord** — Check for community servers
   - Angle: "MCP memory server for Claude Code and Claude Desktop"

5. **r/LocalLLaMA Discord** — linked from subreddit
   - Angle: Same as Reddit post

6. **MLOps Community** — https://discord.gg/mlops
   - Channel: #tools or #showcase
   - Angle: "Self-hosted AI memory with benchmarks"

---

## SHORT POST (for Discord — keep it concise)

```
🧠 **Remembra** — Open-source memory layer for AI agents

Just shipped this: a self-hosted memory server that gives Claude, Cursor, VS Code, or any MCP tool persistent memory across sessions.

**Why I built it:** Every AI session starts from scratch. Remembra fixes that.

**Key features:**
• 100% on LoCoMo benchmark (ACL 2024)
• Runs 100% local with Ollama — no API keys needed
• PII detection (13 patterns — catches SSNs, API keys, etc.)
• AES-256-GCM encryption at rest
• Works with 9 AI tools (Claude Code, Cursor, VS Code, Windsurf, JetBrains, Zed, Codex...)
• MIT licensed

**Quick start:**
```
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
```

GitHub: https://github.com/remembra-ai/remembra
Docs: https://docs.remembra.dev

Would love feedback!
```

## OLLAMA-SPECIFIC VERSION

```
🧠 **Remembra** — AI memory server that runs 100% local with Ollama

Built a self-hosted memory layer for AI agents. Uses Ollama for embeddings (nomic-embed-text) and entity extraction (llama3.1). No external API calls. Everything stays on your machine.

**What it does:**
Your AI remembers your preferences, project context, and decisions across sessions. Works as an MCP server — plug it into Claude Code, Cursor, VS Code, etc.

**Stack:** Docker Compose → Qdrant + Ollama + Remembra. That's it.

**Benchmarks:** 100% on LoCoMo (ACL 2024) across all memory categories.

```
docker compose up -d  # starts Qdrant + Ollama + Remembra
claude mcp add remembra -e REMEMBRA_URL=http://localhost:8787 -- remembra-mcp
```

GitHub: https://github.com/remembra-ai/remembra
MIT licensed. Feedback welcome!
```
