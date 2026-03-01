# Remembra

**Persistent memory for AI applications. Self-host in 5 minutes.**

---

## What is Remembra?

Remembra is a universal memory layer for LLMs. It solves the fundamental problem that every AI forgets everything between sessions.

```python
from remembra import Memory

memory = Memory(user_id="user_123")

# Store memories
memory.store("User prefers dark mode and works at Acme Corp")

# Recall with context
context = memory.recall("What are user's preferences?")
# Returns: "User prefers dark mode. Works at Acme Corp."
```

## Why Remembra?

### The Problem
Every AI app needs memory. Developers hack together solutions using vector databases, embeddings, and custom retrieval logic. It's complex, fragmented, and everyone rebuilds the same thing.

### Current Solutions
- **Mem0**: $24M raised, but self-hosting docs are trash, pricing jumps from $19 to $249
- **Zep**: Academic, complex to deploy
- **Letta**: Not production-ready
- **LangChain Memory**: Too basic, no persistence

### Our Approach
- **Self-host in 5 minutes**: One Docker command, everything bundled
- **Fair pricing**: $0 → $29 → $99 (not $19 → $249)
- **Open source core**: MIT license, own your data
- **Actually works**: Built because we need it ourselves

## Core Features

<div class="grid cards" markdown>

-   :material-brain:{ .lg .middle } __Smart Extraction__

    ---

    LLM-powered fact extraction transforms messy conversations into clean, searchable memories.

-   :material-account-group:{ .lg .middle } __Entity Resolution__

    ---

    Knows that "Adam", "Mr. Smith", and "my husband" are the same person.

-   :material-clock-time-four:{ .lg .middle } __Temporal Memory__

    ---

    TTL support, memory decay, and historical queries with `as_of`.

-   :material-magnify:{ .lg .middle } __Hybrid Search__

    ---

    Semantic + keyword search with CrossEncoder reranking for accurate recall.

-   :material-graph:{ .lg .middle } __Entity Graph__

    ---

    Traverse relationships to find related memories across your knowledge graph.

-   :material-docker:{ .lg .middle } __Self-Host First__

    ---

    One Docker command. All dependencies bundled. Your data stays yours.

</div>

## Quick Start

=== "Docker (Recommended)"

    ```bash
    docker run -d -p 8787:8787 remembra/remembra
    ```

=== "Python Package"

    ```bash
    pip install remembra
    ```

=== "From Source"

    ```bash
    git clone https://github.com/remembra/remembra
    cd remembra
    docker-compose up -d
    ```

Then use the Python SDK:

```python
from remembra import Memory

memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123"
)

# Store a memory
memory.store("User's name is John. He's a software engineer at Google.")

# Recall memories
context = memory.recall("Who is the user?")
print(context)  # "John is a software engineer at Google."
```

[Get Started :material-arrow-right:](getting-started/quickstart.md){ .md-button .md-button--primary }
[View on GitHub :material-github:](https://github.com/remembra/remembra){ .md-button }

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Your Application                         │
├─────────────────────────────────────────────────────────────┤
│                   Remembra SDK / API                         │
├──────────────┬──────────────┬───────────────┬───────────────┤
│  Extraction  │   Entities   │    Retrieval  │   Temporal    │
│  (LLM-based) │ (Resolution) │(Hybrid Search)│  (TTL/Decay)  │
├──────────────┴──────────────┴───────────────┴───────────────┤
│                      Storage Layer                           │
│         Qdrant (vectors) + SQLite (metadata/graph)          │
└─────────────────────────────────────────────────────────────┘
```

## License

Remembra is open source under the [MIT License](https://github.com/remembra/remembra/blob/main/LICENSE).

Built with :heart: by [DolphyTech](https://dolphytech.com)
