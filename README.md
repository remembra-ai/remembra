# Remembra - AI Memory Layer

> Persistent memory for AI applications. Self-host in 5 minutes.

## What Is This?

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

## Why We're Building This

### The Problem
Every AI app needs memory. Developers hack together solutions using vector databases, embeddings, and custom retrieval logic. It's complex, fragmented, and everyone rebuilds the same thing.

### Current Solutions Suck
- **Mem0**: $24M raised, but self-hosting docs are trash, pricing jumps from $19 to $249
- **Zep**: Academic, complex to deploy
- **Letta**: Not production-ready
- **LangChain Memory**: Too basic, no persistence

### Our Approach
- **Self-host in 5 minutes**: One Docker command, everything bundled
- **Fair pricing**: $0 → $29 → $99 (not $19 → $249)
- **Open source core**: MIT license, own your data
- **Actually works**: Built because we need it ourselves (Clawdbot)

## Core Features

### 1. Simple Memory Operations
- `store()` - Save memories with automatic extraction
- `recall()` - Semantic search with context
- `update()` - Intelligent merging
- `forget()` - GDPR-compliant deletion

### 2. Entity Resolution (Our Killer Feature)
Knows that "Adam", "Adam Smith", "Mr. Smith", and "my husband" are the same person.

### 3. Temporal Awareness
Memories have time context. TTL support. Historical queries.

### 4. Hybrid Storage
Vector (semantic) + Graph (relationships) + Relational (metadata) in one system.

### 5. Observability Dashboard
See what's stored, debug retrievals, visualize entity graphs.

## Quick Start

### Self-Hosted (Recommended)
```bash
docker run -d -p 8787:8787 remembra/remembra
```

### Python SDK
```bash
pip install remembra
```

```python
from remembra import Memory

# Connect to local instance
memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    project="my_app"
)

# Store
memory.store("User's name is John. He's a software engineer at Google.")

# Recall
context = memory.recall("Who is the user?")
print(context)
# "John is a software engineer at Google."
```

## Documentation

- [Product Spec](./PRODUCT-SPEC.md) - Full product specification
- [Build Plan](./BUILD-PLAN.md) - Week-by-week development plan
- [Architecture](./ARCHITECTURE.md) - Technical architecture details
- [API Reference](./API.md) - API documentation

## Project Status

🚧 **In Development** - MVP target: 12 weeks

## License

MIT License - Use it however you want.

---

Built by [DolphyTech](https://dolphytech.com)
