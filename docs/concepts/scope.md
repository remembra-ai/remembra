# Scope & Boundaries

What Remembra does — and what it intentionally doesn't do.

## Remembra is a Memory Layer

Remembra is **persistent memory** for AI agents. It stores facts, recalls them semantically, and manages their lifecycle. That's it.

```
┌─────────────────────────────────────────────────────────┐
│                    YOUR APPLICATION                      │
│                                                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ Orchestrator│  │   Agents    │  │   Tools/APIs    │ │
│  │ (LangGraph, │  │ (Claude,    │  │   (Searches,    │ │
│  │  CrewAI,    │  │  GPT, etc)  │  │    DB calls)    │ │
│  │  AutoGen)   │  │             │  │                 │ │
│  └──────┬──────┘  └──────┬──────┘  └────────┬────────┘ │
│         │                │                   │          │
│         └────────────────┼───────────────────┘          │
│                          │                              │
│                          ▼                              │
│  ┌──────────────────────────────────────────────────┐  │
│  │              REMEMBRA (Memory Layer)              │  │
│  │                                                    │  │
│  │  • Store facts                                     │  │
│  │  • Recall semantically                            │  │
│  │  • Extract entities                               │  │
│  │  • Build knowledge graph                          │  │
│  │  • Manage decay/expiry                            │  │
│  │  • Handle conflicts                               │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## What Remembra Does ✅

| Capability | Description |
|------------|-------------|
| **Persistent Storage** | Facts survive restarts, sessions, context windows |
| **Semantic Recall** | Find memories by meaning, not just keywords |
| **Entity Extraction** | Automatically identify people, places, orgs |
| **Knowledge Graph** | Track relationships between entities |
| **Temporal Decay** | Recent memories rank higher than stale ones |
| **Conflict Detection** | Flag contradictory facts |
| **Multi-tenancy** | One account per API key, project namespaces |
| **TTL/Expiry** | Expired memories are hidden from recall; cleanup deletes them |

## What Remembra Doesn't Do ❌

| Not Our Job | Use Instead |
|-------------|-------------|
| **Orchestration** | LangGraph, CrewAI, AutoGen, custom code |
| **Agent Execution** | Your LLM provider (Claude, GPT, etc) |
| **Task Planning** | Orchestration frameworks |
| **Tool Calling** | Agent frameworks |
| **Conversation Management** | Your application layer |
| **Session State** | Redis, your app's session store |
| **RAG over Documents** | Vector DBs (Pinecone, Weaviate, etc) |
| **Real-time Streaming** | Your websocket/SSE layer |

## The Boundary in Practice

### Orchestration vs Memory

**Orchestration** decides *what to do*:
- "Should I search the web or ask the user?"
- "Which tool should I call?"
- "How do I break this task into steps?"

**Memory** provides *context for decisions*:
- "What do I know about this user?"
- "What happened in previous conversations?"
- "What facts are relevant to this query?"

### Example: Multi-Agent System

```python
# Orchestrator handles coordination
orchestrator = LangGraph(...)

# Both agents use the same project
research_agent = Agent(memory=Memory(project="company-x"))
writer_agent = Agent(memory=Memory(project="company-x"))

# Agents share facts through memory, not orchestration
research_agent.store("Company X revenue: $10M")
# Later...
writer_agent.recall("Company X financials")  # Gets the fact (a different project would not)
```

### Example: RAG vs Memory

**RAG (Document Retrieval):**
```python
# Retrieve chunks from a corpus
chunks = vector_db.search("quarterly earnings")
# Use for single conversation
```

**Memory (Persistent Facts):**
```python
# Extract and persist key facts
memory.store("Acme reported $10M Q4 revenue")
# Kept until deleted or expired; ranks lower as it ages
```

Use RAG for large document search. Use Memory for facts worth remembering.

## Integration Patterns

### Pattern 1: Memory-Augmented Agent

```python
def agent_turn(user_message):
    # 1. Recall relevant context
    context = memory.recall(user_message).context
    
    # 2. Agent generates response (YOUR orchestration)
    response = agent.generate(
        system=f"Context:\n{context}",
        user=user_message
    )
    
    # 3. Store new facts (YOUR extraction logic)
    memory.store(extract_facts(response))
    
    return response
```

### Pattern 2: Multi-Agent Shared Memory

```python
# Shared project for collaboration
memory = Memory(project="team-alpha")

# Research agent finds facts
research.store("Competitor launched new product", metadata={"agent": "research"})

# Strategy agent uses them
context = strategy.recall("competitor activity")
```

### Pattern 3: Session + Long-term Memory

```python
# Session state (ephemeral) - YOUR responsibility
session = {"current_task": "booking", "step": 3}

# Long-term memory (persistent) - Remembra
memory.store("User prefers aisle seats")
memory.store("User's frequent flyer: Delta Gold")
```

## FAQ

### "Should Remembra handle my agent's state machine?"

No. Use an orchestration framework (LangGraph, CrewAI) or custom code for agent state. Remembra stores *facts*, not *execution state*.

### "Should I store conversation history in Remembra?"

Store **extracted facts**, not raw messages. Conversation history belongs in your app layer. Memory is for distilled knowledge.

```python
# ❌ Don't do this
memory.store("User: Hi! Bot: Hello! User: What's the weather?")

# ✅ Do this
memory.store("User asked about weather in San Francisco")
memory.store("User prefers Celsius for temperature")
```

### "Can Remembra replace my vector database?"

Only for **memory** use cases. If you need:
- Document chunking → Use a vector DB
- Large corpus search → Use a vector DB  
- Persistent facts about users/entities → Use Remembra

### "Should I use Remembra for caching?"

No. Use Redis or similar. Memory is for *knowledge*, not *cache*.

---

*Still unsure where the boundary is? [Ask on Discord](https://discord.gg/mPYQRKzXz5) or [open an issue](https://github.com/remembra-ai/remembra/issues).*
