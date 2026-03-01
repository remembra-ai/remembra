# Memory Model

Understanding how Remembra stores and retrieves information.

## What is a Memory?

A memory is an atomic unit of information:

```json
{
  "id": "mem_abc123",
  "content": "John works at Google as a senior engineer",
  "user_id": "user_123",
  "project": "default",
  "created_at": "2026-03-01T10:30:00Z",
  "updated_at": "2026-03-01T10:30:00Z",
  "expires_at": null,
  "metadata": {
    "source": "chat",
    "session_id": "sess_xyz"
  },
  "entities": ["John", "Google"],
  "trust_score": 0.95
}
```

## Memory Lifecycle

```
Input Text → Extraction → Consolidation → Storage → Retrieval → Decay/Expiry
```

### 1. Input

Raw text from your application:

```python
memory.store("""
    Just finished a call with Sarah from Acme.
    They need the proposal by Friday.
    Budget: $50k-75k range.
""")
```

### 2. Extraction

LLM transforms to clean facts:

- "Sarah from Acme needs proposal by Friday"
- "Acme's budget is $50k-75k"

### 3. Consolidation

Check against existing memories:

| Scenario | Action |
|----------|--------|
| New information | ADD |
| Updated fact | UPDATE (merge) |
| Same fact exists | NOOP (skip) |
| Contradiction | DELETE old + ADD new |

### 4. Storage

- Vector embedding → Qdrant
- Metadata → SQLite
- Entity links → SQLite graph

### 5. Retrieval

Recalled via semantic search + ranking.

### 6. Lifecycle End

- **TTL expires** → Soft delete
- **Decay drops** → Lower ranking
- **Manual forget** → Hard delete

## Memory Types

### Explicit Facts

Direct statements extracted from input:

```
Input: "User's name is John Smith"
Memory: "User's name is John Smith"
```

### Inferred Facts

Derived from context:

```
Input: "Got an email from john@acme.com about the project"
Memories:
- "John has email john@acme.com"
- "John is associated with Acme" (domain inference)
```

### Relationship Facts

Connections between entities:

```
Input: "Sarah is John's manager"
Memory: "Sarah is John's manager"
Relationship: Sarah → MANAGES → John
```

## Metadata

Attach arbitrary metadata to memories:

```python
memory.store(
    "User prefers email over phone",
    metadata={
        "category": "preferences",
        "confidence": "high",
        "source": "user_settings",
        "tags": ["communication", "contact"]
    }
)
```

### Reserved Metadata Keys

| Key | Purpose |
|-----|---------|
| `source` | Content provenance |
| `session_id` | Session association |
| `trust_score` | Security trust level |
| `checksum` | Integrity verification |

## Projects (Namespaces)

Isolate memories by application:

```python
# Personal assistant memories
personal = Memory(user_id="user_1", project="personal")

# Work assistant memories (same user, different context)
work = Memory(user_id="user_1", project="work")

# These are completely separate
personal.store("My wife's birthday is March 15")  # Not visible to work
work.store("Q4 targets: $1M revenue")  # Not visible to personal
```

## Memory Scoring

When recalled, memories get scored:

```
final_score = weighted_sum(
    semantic_similarity,  # How well it matches the query
    recency_boost,        # Newer = higher
    access_boost,         # Frequently recalled = higher
    entity_match,         # Entities in query match memory
    keyword_match         # Exact word matches
)
```

## Memory States

| State | Description | Recall | Visible |
|-------|-------------|--------|---------|
| Active | Normal memory | Yes | Yes |
| Decayed | Low decay score | Yes (low rank) | Yes |
| Expired | Past TTL | No | No |
| Deleted | User forgot | No | No |
| Archived | Soft deleted | Via as_of only | No |

## Best Practices

### 1. Atomic Facts

Store one fact per memory:

```python
# ✅ Good
memory.store("John's role is VP of Sales")
memory.store("John started at Acme in 2024")

# ❌ Avoid
memory.store("John is VP of Sales at Acme since 2024 and reports to the CEO")
```

### 2. Clear Context

Include context when relevant:

```python
# ✅ Good
memory.store("User prefers dark mode in the mobile app")

# ❌ Vague
memory.store("User prefers dark mode")  # Which app?
```

### 3. Use TTL Appropriately

```python
# Permanent facts
memory.store("User's email is john@example.com")

# Temporary context
memory.store("User is comparing iPhone models", ttl="7d")
```

### 4. Leverage Metadata

```python
# Filterable categories
memory.store(
    content="...",
    metadata={"category": "billing", "priority": "high"}
)
```
