# AI Memory Competitor Analysis

**Date:** 2026-03-03
**Purpose:** Research before building conversation ingestion for Remembra

---

## Executive Summary

The AI memory space has 3 main architectural approaches:

| Approach | How It Works | Leaders |
|----------|--------------|---------|
| **Auto-Extract** | Send conversations → AI extracts facts automatically | Mem0, Zep |
| **Self-Edit** | Agent edits its own memory via tools | Letta/MemGPT |
| **Manual Store** | Developer explicitly calls store() | LangChain, raw vector DBs |

**Key Insight:** Mem0 is the current leader because they cracked **automatic extraction** - you send conversations, they figure out what to remember. This is exactly what we need to build.

---

## Detailed Competitor Breakdown

### 1. Mem0 (mem0.ai) ⭐ Main Competitor

**Funding:** Well-funded, aggressive marketing
**Stars:** 25K+ GitHub
**Approach:** Hybrid (vector + optional graph)

#### How They Handle Conversations

```python
# Mem0's API - accepts raw conversation
client.add(
    messages=[
        {"role": "user", "content": "My wife Suzan and I..."},
        {"role": "assistant", "content": "That sounds great!"},
    ],
    user_id="mani",
    infer=True  # DEFAULT - auto-extract facts
)
```

**The Magic:** `infer=True` (default) triggers their extraction pipeline:

1. **Extraction Phase:**
   - Takes message pair + conversation summary + recent messages
   - LLM extracts "candidate facts" (atomic, salient memories)
   - Uses GPT-4o-mini for extraction

2. **Update Phase:**
   - For each extracted fact, find similar existing memories
   - LLM decides: ADD | UPDATE | DELETE | NOOP
   - Prevents duplicates, handles contradictions

**Key Parameters:**
- `infer=True` → Auto-extract (default)
- `infer=False` → Store raw messages as-is
- `enable_graph=True` → Also build entity graph

**Performance (from their paper):**
- 26% accuracy improvement over baselines
- 91% lower latency vs full-context
- 90% token cost savings

**What They Extract:**
- User preferences
- Decisions made
- Goals/tasks
- Entities and relationships
- Feedback/clarifications

#### Their Extraction Prompt (inferred from docs)

They look for facts that are:
- Self-contained (understandable without context)
- Specific (names, dates, numbers)
- Long-term useful (not transient)

They skip:
- Greetings, filler
- Vague statements
- Temporary info

---

### 2. Zep (getzep.com)

**Approach:** Temporal Knowledge Graph
**Powered By:** Graphiti (their open-source graph framework)
**Focus:** Enterprise, how facts CHANGE over time

#### How They Handle Conversations

```python
# Zep - Add messages as context
zep.memory.add(
    session_id="session-123",
    messages=[...],  # Conversation turns
)

# Retrieve with relationship awareness
context = zep.memory.get(session_id="session-123")
```

**Key Differentiator:** Temporal awareness
- Each fact has `valid_at` and `invalid_at` timestamps
- Tracks how preferences/relationships evolve
- "User liked coffee in January, switched to tea in March"

**Architecture:**
- Graph DB (Neo4j-like) for relationships
- Automatic entity extraction
- Multi-hop reasoning across connected facts

**Performance:**
- 18.5% accuracy improvement
- ~90% latency reduction
- Good for complex relational queries

**Best For:** Enterprise apps needing audit trails, relationship modeling

---

### 3. Letta / MemGPT (letta.com)

**Approach:** Agent self-manages memory via tools
**Origin:** MemGPT research paper (viral)
**Philosophy:** Memory as an OS resource

#### How They Handle Conversations

```python
# Agent has memory tools it can call
tools = [
    "core_memory_replace",   # Edit in-context memory
    "core_memory_append",    
    "archival_memory_insert", # Store to external DB
    "archival_memory_search",
    "conversation_search",    # Search past conversations
]
```

**Memory Hierarchy:**
| Type | Analogy | Description |
|------|---------|-------------|
| Core Memory | RAM | Always in context, editable by agent |
| Archival Memory | Disk | External storage, searchable |
| Recall Memory | Cache | Conversation history |

**Key Differentiator:** The AGENT decides what to remember
- Not automatic extraction
- Agent explicitly calls memory tools
- "Sleep-time compute" - agent refines memory when idle

**Best For:** Maximum control, complex multi-agent systems

---

### 4. LangChain Memory

**Approach:** Manual, developer-controlled
**Types Available:**

| Memory Type | What It Does |
|-------------|--------------|
| ConversationBufferMemory | Stores raw messages |
| ConversationSummaryMemory | Compresses to rolling summary |
| ConversationBufferWindowMemory | Last K messages only |
| EntityMemory | Tracks entities mentioned |
| VectorStoreRetrieverMemory | Semantic search over history |

**How It Works:**
```python
# Developer manually adds to memory
memory = ConversationSummaryMemory(llm=llm)
memory.save_context(
    {"input": "My wife is Suzan"},
    {"output": "Nice to meet you!"}
)
```

**Limitation:** No automatic fact extraction - just stores what you give it.

---

### 5. Claude-Mem (Open Source Plugin)

**Approach:** Session persistence for coding agents
**Target:** Claude Code, Codex CLI users

#### How It Works:
1. **Capture:** Records prompts, tool usage, observations
2. **Compress:** AI creates compact memory units
3. **Retrieve:** Injects relevant context at session start

**Performance:** Up to 95% token reduction

**Best For:** Coding workflows, not general conversation

---

## Comparison Matrix

| Feature | Mem0 | Zep | Letta | LangChain | Remembra (Current) |
|---------|------|-----|-------|-----------|-------------------|
| Auto-extract from conversation | ✅ | ✅ | ❌ (agent-driven) | ❌ | ❌ |
| Entity extraction | ✅ | ✅ | ✅ | ✅ (basic) | ✅ |
| Knowledge graph | ✅ (optional) | ✅ (core) | ❌ | ❌ | 🟡 (entities only) |
| Temporal tracking | ❌ | ✅ | ❌ | ❌ | ❌ |
| Conflict resolution | ✅ | ✅ | ❌ | ❌ | ✅ |
| Self-hosted option | ✅ | ❌ (cloud only) | ✅ | ✅ | ✅ |
| MCP support | ❌ | ❌ | ✅ | ❌ | ✅ |

---

## What We Need to Build

### The Gap
Remembra currently requires manual `store()` calls. Users must:
1. Decide what's worth remembering
2. Format the content
3. Call the API

This fails because AI agents forget to store, or context gets wiped before they can.

### The Solution: Automatic Conversation Ingestion

**Like Mem0's approach, but with our advantages:**

1. **Accept raw conversations** (messages array)
2. **Auto-extract facts** (we already have `FactExtractor`)
3. **Extract entities** (we already have `EntityExtractor`)
4. **Dedupe/consolidate** (we already have `MemoryConsolidator`)
5. **Handle conflicts** (we already have `ConflictManager`)

### Our Differentiators vs Mem0

| Mem0 | Remembra (Opportunity) |
|------|------------------------|
| Cloud-first, OSS secondary | OSS-first, cloud option |
| Vendor lock-in | Universal/portable |
| Closed extraction logic | Transparent, customizable |
| No MCP | MCP server for Claude |
| Generic extraction | Configurable extraction rules |

---

## Architecture Questions to Resolve

### 1. Extraction Granularity
- **Per-message pair** (like Mem0) - process as messages arrive
- **Per-session batch** - process entire conversation at end
- **Hybrid** - real-time + periodic consolidation

**Recommendation:** Start with batch (simpler), add streaming later.

### 2. What to Extract
Mem0 extracts:
- Preferences
- Decisions
- Facts about entities
- Goals/tasks
- Relationships

**Question:** Should extraction be configurable per-user/project?

### 3. Importance Scoring
How to filter low-value facts?
- LLM-assigned score (0.0-1.0)
- Heuristics (length, entity presence, etc.)
- User-defined rules

### 4. Deduplication Strategy
- Exact match → skip
- High semantic similarity (>0.9) → merge or update
- Contradiction → use ConflictManager

### 5. Context Window for Extraction
Mem0 uses:
- Conversation summary (global context)
- Last 10 messages (recent context)
- Current message pair

**Question:** How much context do we provide to extractor?

---

## Research Links for Deep Dive

### Papers
- [Mem0 Paper (ArXiv)](https://arxiv.org/html/2504.19413v1) - Full technical details
- [MemGPT Paper](https://research.memgpt.ai/) - OS-inspired memory

### Docs
- [Mem0 Add Memory API](https://docs.mem0.ai/api-reference/memory/add-memories)
- [Mem0 Core Concepts](https://docs.mem0.ai/core-concepts/memory-operations/add)
- [Zep Docs](https://help.getzep.com)
- [Letta/MemGPT Concepts](https://docs.letta.com/concepts/letta/)
- [Letta Agent Memory Blog](https://www.letta.com/blog/agent-memory)

### Comparisons
- [2026 AI Memory Comparison](https://serenitiesai.com/articles/ai-agent-memory-why-2026-is-the-year-of-persistent-context) - Mem0 vs Zep vs others

---

## Next Steps

1. **Deep dive on Mem0's extraction prompt** - What exactly makes a good fact?
2. **Study their update phase logic** - How do they decide ADD vs UPDATE vs DELETE?
3. **Test Mem0's API ourselves** - Hands-on understanding
4. **Draft Remembra's extraction prompt** - Tailored to our strengths
5. **Design the ingestion endpoint** - Request/response schema
6. **Build incrementally** - Start simple, add complexity

---

## Questions for Mani

1. Should we match Mem0's API format exactly (easier migration) or design our own?
2. Real-time processing or batch-only to start?
3. Should extraction rules be user-configurable?
4. Priority: speed-to-market or feature completeness?
