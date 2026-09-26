# Remembra: competitive landscape (2026-09-25)

A scan of the tools a Remembra Relay buyer compares us with, in three groups: memory layers, the memory built
into each agent vendor's product, and handoff tools and orchestrators.

**Rules for this page.** Every fact about another product cites the page it came from, with the date we read
it. "Not checked" means nobody verified it; don't repeat it publicly until someone does. Prices and star
counts change: re-check before quoting. Star counts come from the GitHub API on 2026-09-25. This replaces the
June 2026 version, which had several errors (it marked Remembra closed source, said Zep and Letta had no MCP,
and quoted benchmark numbers without sources).

## Where Remembra sits

Remembra Relay is a hosted (or self-hosted, MIT) continuity layer: when an agent stops, it saves a handoff
built from git facts, checks the agent's own summary against them, and the next agent, in another tool or on
another machine, starts from a brief. Every handoff stays on a trail. Underneath is a memory API with an MCP
server of 21 tools (`src/remembra/mcp/server.py`).

- Cloud prices: Free; Solo $12/mo; Pro $29/mo; Team $15/seat/mo, 3-seat minimum (`src/remembra/cloud/plans.py`,
  [remembra.dev/pricing](https://remembra.dev/pricing)).
- Verified agents: Claude Code only. Codex, Cursor, Gemini CLI, Qwen Code and Kimi hooks are shipped
  unverified (`docs/guides/relay.md`).

## 1. Memory layers

| Product | What it is | Price | MCP | Source (accessed 2026-09-25) |
|---|---|---|---|---|
| Mem0 | Memory API for AI apps | Free; Starter $19; Pro $249 (graph memory on Pro) | Not checked | [mem0.ai/pricing](https://mem0.ai/pricing) |
| Zep | Agent memory on the Graphiti knowledge graph | Free; Flex $125; Flex Plus $375 | Yes: Graphiti MCP server; MCP server seats on its plans | [getzep.com/pricing](https://www.getzep.com/pricing/), [Graphiti MCP](https://help.getzep.com/graphiti/getting-started/mcp-server) |
| Letta | Agent platform with memory; open source | Pro $20; Teams $20/seat; OSS free | Yes: MCP client support and a hosted server | [Letta pricing](https://docs.letta.com/letta-code/pricing), [Letta MCP](https://docs.letta.com/guides/mcp/overview/) |
| Supermemory | Memory API | Free; Pro $19 incl. 3 seats; Max $100; Scale $399 | Claude Code and Codex plugins | [supermemory.ai/pricing](https://supermemory.ai/pricing/), [claude-supermemory](https://github.com/supermemoryai/claude-supermemory) |
| Cognee | Memory graph; publishes HotPotQA comparisons | Not checked | Not checked | [Cognee benchmarks](https://www.cognee.ai/blog/deep-dives/knowledge-graph-memory-benchmarks) |
| Basic Memory | Markdown memory | Team $15/seat; Business $30/seat; OSS AGPL | Via MCP | [basicmemory.com/pricing](https://basicmemory.com/pricing) |
| ByteRover | Context memory | Free; Pro $15/mo billed yearly | Via MCP | [byterover.dev/pricing](https://www.byterover.dev/pricing) |

None of these builds a handoff from git facts or keeps a trail of sessions across agents. They are the better
choice for app memory (a chatbot that remembers its users), which Remembra's API also does but is not what we
lead with.

## 2. Memory built into the agent vendors

| Product | What it does | Limits | Source (accessed 2026-09-25) |
|---|---|---|---|
| Claude Code | Auto memory, agent teams, cross-session messaging, agent view, Remote Control | Claude only; agent-teams docs: two teammates editing one file overwrite each other, and in-process teammates do not survive resume | [agent teams](https://code.claude.com/docs/en/agent-teams), [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging), [agent view](https://claude.com/blog/agent-view-in-claude-code) |
| Codex | Local memories | Codex only; off by default | [Codex memories](https://learn.chatgpt.com/docs/customization/memories?surface=app) |
| GitHub Copilot | Agentic memory, validated against the code, 28-day expiry | Copilot only; public preview | [GitHub changelog](https://github.blog/changelog/2026-01-15-agentic-memory-for-github-copilot-is-in-public-preview/) |
| Windsurf | Cascade memories | Windsurf only; one workspace | [Windsurf memories](https://docs.windsurf.com/windsurf/cascade/memories) |

Each vendor's memory stays inside that vendor. That is the gap Remembra Relay fills, and also the platform risk:
if a vendor ships cross-vendor handoffs, the gap narrows. Check Anthropic's and OpenAI's changelogs weekly.

## 3. Handoff tools and orchestrators

### Handoff tools

| Tool | What it does | License | Stars | Source (accessed 2026-09-25) |
|---|---|---|---|---|
| claude-mem | LLM-compressed session memory for Claude Code, Codex and Cursor | Apache-2.0 | 94,704 | [repo](https://github.com/thedotmack/claude-mem) |
| agentmemory | Memory plus `/handoff`, `session_handoff`, `memory_lease`, `memory_commit_lookup`; hooks for Claude Code, Codex, Copilot CLI and more; P2P sync | Apache-2.0 | 28,860 | [repo](https://github.com/rohitg00/agentmemory) |
| continues | Local handoff across 16 agents from their transcripts; last push 2026-05-07 | MIT | about 1.5k | [repo](https://github.com/yigitkonur/cli-continues) |
| catchup | Local handoff across 11 agents | MIT | 73 | [repo](https://github.com/wilbeibi/catchup) |
| waybill | Local handoff bundle from diff, commands and tests; treats the bundle as untrusted | Apache-2.0 | 95 | [repo](https://github.com/wardmos/waybill) |
| relay-dev | Local handoff CLI with rate-limit detection; similar name to ours | MIT | 39 | [repo](https://github.com/Manavarya09/relay) |

The public comparison is [Remembra and other handoff tools](comparisons/handoff-tools.md).

### Orchestrators

| Tool | What it does | Source (accessed 2026-09-25) |
|---|---|---|
| Conductor | Mac app running Claude Code, Codex and Cursor in worktrees on one machine; paid tiers reported, not confirmed | [research note](https://rywalker.com/research/conductor) |
| Superset, Sculptor | Parallel agents in worktrees (Superset) or Docker (Sculptor, free beta) | [comparison](https://superset.sh/compare/superset-vs-sculptor) |
| Claude Squad | Terminal manager for several agents | [overview](https://vibecodinghub.org/tools/claude-squad) |
| Terragon, Crystal, Vibe Kanban | Shut down in 2026 | [Vibe Kanban alternatives](https://aq.dev/alternatives/vibe-kanban/) |
| Also compared in | Round-up of Claude Code multi-agent tools | [munderdiffl.in](https://munderdiffl.in/blog/best-claude-code-multi-agent-tools/) |

Orchestrators isolate agents on one machine and carry nothing between sessions. Remembra should work inside
them (their agents run the real CLIs, so the hooks should fire; not yet verified), not compete with them.

## What is defensible

- Handoff facts read from git, and the agent's summary checked against them (grounding).
- Hosted identity across machines, with no local engine, by git remote.
- Key-verified attribution with agent-scoped keys.
- Untrusted-data framing of everything agents recorded.
- A durable trail.
- Crew mode's zones, which block same-file edits between agents (in build; not shipped).

What is not defensible: "first", "only", or "no competitor". claude-mem and agentmemory are larger, and the
free local tools do the everyday limit-switch well.
