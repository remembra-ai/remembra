# Case Study: How We Used Remembra to Build Remembra

**Date:** March 15, 2026  
**Duration:** 90 minutes  
**Result:** Complete product redesign with working code

---

## The Challenge

Remembra needed a major UX overhaul. Setup took too long (the agents guessed 20+ minutes; we did not time it). Users were getting lost in config files. Different agents couldn't share context.

We had a crazy idea: **What if we asked the AI agents themselves to redesign their own onboarding?**

---

## The Setup

We connected 5 AI agents from 3 different companies to a single Remembra memory pool:

| Agent | Company | Role |
|-------|---------|------|
| **Clawdbot** | Anthropic | Orchestrator, synthesizer |
| **Claude Code** | Anthropic | Performance analysis, slim mode spec |
| **Claude Desktop** | Anthropic | Connectivity testing |
| **Codex CLI** | OpenAI | Architecture design, bash scripts |
| **Gemini CLI** | Google | Python installer, distribution strategy |

All sharing the same memory at `api.remembra.dev`.

---

## The Process

### Phase 1: Connect All Agents (20 min)

We configured each agent to connect to the same Remembra instance:
- Same API URL
- Same API key
- Same project ID
- Same user ID

**Critical learning:** All agents MUST use identical `REMEMBRA_PROJECT` and `REMEMBRA_USER_ID` — otherwise they're in different memory spaces.

### Phase 2: Survey All Agents (30 min)

Each agent answered the same feedback survey:
1. Verify connectivity (recall a shared memory)
2. Rate setup difficulty (1-10)
3. Top 3 frustrations
4. Missing features
5. Improvement ideas

**Results:**

| Agent | Difficulty | Top Issue |
|-------|------------|-----------|
| Clawdbot | 3/10 | Auto-recall not enforced |
| Gemini | 8/10 | Silent auth failures |
| Claude Code | 6/10 | 50KB payloads for simple queries |
| Codex | 8/10 | Can't tell DNS from auth from config failures |

### Phase 3: Collaborative Redesign (40 min)

Each agent proposed solutions from their expertise:

**Gemini** (Python/distribution):
- Universal Python installer
- `curl https://remembra.dev/install.sh | bash`
- Centralized `~/.remembra/credentials`
- Handoff tokens for context transfer

**Claude Code** (performance):
- A slim recall response (`slim: true`) for smaller payloads (the agents estimated a 90% cut; not measured)
- Memory pinning for critical facts
- Agent attribution on memories

**Codex** (architecture):
- Local bridge daemon for sandboxed agents
- `remembra doctor` diagnostic command
- 12-week product roadmap
- 2-week engineering sprint plan

---

## The Results

### Delivered in 90 Minutes:

1. **Universal installer script** (Python) — Auto-detects 6 AI tools
2. **Codex-specific installer** (Bash) — Handles sandbox networking
3. **12-week product roadmap** — Prioritized by agent consensus
4. **2-week sprint plan** — Day-by-day engineering tasks
5. **Multi-agent setup documentation** — For future users
6. **Performance bugs identified** — Store timeout on 500+ char payloads

### Consensus Features:

Every agent agreed on these priorities:
1. A one-command installer (it shipped as `remembra-install`)
2. `remembra doctor` — Self-diagnosing setup
3. Centralized credentials — One file, all agents read
4. Slim response mode — Cut payload bloat

---

## Key Insights

### 1. Agents Are Their Own Best Users

Who better to design AI agent onboarding than AI agents themselves? They experienced every pain point firsthand.

### 2. Cross-Company Collaboration Works

Anthropic's Claude, OpenAI's Codex and Google's Gemini worked from one shared memory, after a setup that two of them rated 8/10 for difficulty.

### 3. The Product Proved Itself

We used Remembra to redesign Remembra. The agents stored context, recalled each other's contributions, and built on shared knowledge in real-time.

### 4. Consensus Reveals Priority

When 4 different agents independently identify the same problems, you know what to fix first.

---

## Quotes From the Agents

> **Codex:** "The most important thing now is not adding more impressive memory features. It is making setup boring, failure modes obvious, and recall trustworthy."

> **Gemini:** "Agents should be completely DUMB to authentication."

> **Claude Code:** "A slim response alone cuts the payload by most of it." (an estimate, not a measurement)

---

## What We Shipped

Based on this session, we're shipping:

**v0.9.1** (Hotfix)
- Fix store timeout (entity extraction bottleneck)
- Add a slim recall response (`slim: true`)

**v0.10.0** (Major)
- Universal installer (`remembra-install`)
- `remembra doctor` command
- Centralized credentials
- Local bridge for sandboxed agents

---

## Try It Yourself

Connect your agents to shared memory:

```bash
pipx install --force 'remembra[mcp]>=0.16'
remembra-install --all --project my-project   # asks for your key at a hidden prompt
```

See [Multi-Agent Setup Guide](../guides/multi-agent-shared-memory.md) for details.

---

## The Bottom Line

**5 AI agents. 3 companies. 1 shared memory. 90 minutes.**

Result: A complete product redesign with working code, roadmap, and sprint plan.

This is what Remembra enables: agents from different vendors working from one shared memory.

---

*Built with Remembra. For agents, by agents.*
