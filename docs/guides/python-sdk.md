# Python SDK

Complete reference for the Remembra Python SDK.

## Installation

```bash
pip install remembra
```

## Quick Start

```python
from remembra import Memory

memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    project="my_app"  # Optional namespace
)

# Store a memory
memory.store("User prefers dark mode")

# Recall memories
context = memory.recall("What are user preferences?")
```

## Memory Class

### Constructor

```python
Memory(
    base_url: str = "http://localhost:8787",
    user_id: str = None,
    project: str = "default",
    api_key: str = None,
    timeout: float = 30.0
)
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `base_url` | Remembra server URL | `http://localhost:8787` |
| `user_id` | Unique user identifier | Required |
| `project` | Project namespace | `"default"` |
| `api_key` | API key (if auth enabled) | `None` |
| `timeout` | Request timeout in seconds | `30.0` |

## Core Methods

### store()

Store memories with automatic fact extraction.

memory.store(
    content: str,
    metadata: dict = None,
    ttl: str = None,
    expires_at: datetime = None,  # NEW in v0.12.0
    source: str = None
) -> dict
memory.store(
    content: str,
    metadata: dict = None,
    ttl: str = None,
    source: str = None
) -> dict
```

**Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `content` | `str` | Text to store (can be messy conversation) |
| `metadata` | `dict` | Custom metadata (tags, source, etc.) |
| `ttl` | `str` | Time-to-live: "30d", "1w", "24h", "1y" |
| `source` | `str` | Content provenance (e.g., "chat", "email") |
| `expires_at` | `datetime` | Explicit expiry timestamp (ISO 8601) |

**Example:**

```python
# Basic store
memory.store("User's name is John")

# With metadata
memory.store(
    "User prefers morning meetings",
    metadata={"category": "preferences", "confidence": "high"}
)

# With TTL (expires in 30 days)

# With explicit expiry (v0.12.0)
from datetime import datetime, timedelta
memory.store(
    "Conference call tomorrow at 3pm",
    expires_at=datetime.now() + timedelta(hours=36)
)
# With TTL (expires in 30 days)
memory.store(
    "Meeting scheduled for March 15",
    ttl="30d"
)
```

**What Happens:**

1. Content is sent to the extraction model (GPT-4o-mini)
2. Facts are extracted and cleaned
3. Entities are identified (PERSON, ORG, LOCATION)
4. Duplicates are detected and merged
5. Vectors are stored in Qdrant
6. Relationships are mapped in SQLite

### recall()

Retrieve relevant memories using semantic search.

```python
memory.recall(
    query: str,
    limit: int = 10,
    threshold: float = 0.4,
    max_tokens: int = None,
    enable_hybrid: bool = True,
    enable_rerank: bool = False,
    as_of: datetime = None
    slim: bool = False  # NEW in v0.12.0
) -> str
```

**Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `query` | `str` | Natural language query |
| `limit` | `int` | Max memories to return | 
| `threshold` | `float` | Minimum similarity (0-1) |
| `max_tokens` | `int` | Truncate to fit context window |
| `enable_hybrid` | `bool` | Use semantic + keyword search |
| `enable_rerank` | `bool` | Apply CrossEncoder reranking |
| `as_of` | `datetime` | Historical query (time travel) |
| `slim` | `bool` | Return only context string (90% smaller) |

**Example:**

```python
# Basic recall
context = memory.recall("What do I know about the user?")

# With options
context = memory.recall(
    "What projects is John working on?",
    limit=5,
    threshold=0.5,
    max_tokens=2000
)

# Historical query (see memories as of last week)
from datetime import datetime, timedelta
last_week = datetime.now() - timedelta(days=7)
context = memory.recall("User status", as_of=last_week)
```

**Returns:**

Formatted string of relevant memories, ready for LLM context injection.

### update()

Update existing memories intelligently.

```python
memory.update(
    memory_id: str,
    content: str
) -> dict
```

**Example:**

```python
# Get memory ID from store response
result = memory.store("John is a software engineer")
memory_id = result["memories"][0]["id"]

# Update it
memory.update(memory_id, "John is a senior software engineer at Google")
```

### forget()

Delete memories (GDPR-compliant).

```python
memory.forget(
    memory_ids: list[str] = None,
    user_id: str = None,
    all: bool = False
) -> dict
```

**Example:**

```python
# Forget specific memory
memory.forget(memory_ids=["mem_abc123"])

# Forget all memories for a user
memory.forget(user_id="user_123", all=True)
```

## Advanced Methods

### recall_as_of()

Time-travel queries for historical state.

```python
from datetime import datetime

# See memories as they existed on a specific date
context = memory.recall_as_of(
    query="User preferences",
    timestamp=datetime(2026, 2, 15)
)
```

### get_memories_with_decay()

Get memories with decay score visibility.

```python
memories = memory.get_memories_with_decay()
for m in memories:
    print(f"{m['content']} - decay: {m['decay_score']}")
```

### cleanup_expired()

Remove expired memories (manual trigger).

```python
result = memory.cleanup_expired(dry_run=True)
print(f"Would delete {result['count']} memories")

# Actually delete
memory.cleanup_expired(dry_run=False)
```

### ingest_changelog()

Import project changelogs as searchable memories.

```python
memory.ingest_changelog(
    content_or_path="CHANGELOG.md",
    project_name="my-project"
)
```

## Entity Methods

### get_entities()

List all entities in the memory graph.

```python
entities = memory.get_entities()
for entity in entities:
    print(f"{entity['name']} ({entity['type']})")
```

### get_entity_relationships()

Get relationships for an entity.

```python
relationships = memory.get_entity_relationships(entity_id="ent_123")
for rel in relationships:
    print(f"{rel['source']} --{rel['type']}--> {rel['target']}")
```

## Async Support

All methods have async equivalents:

```python
from remembra import AsyncMemory

memory = AsyncMemory(
    base_url="http://localhost:8787",
    user_id="user_123"
)

async def main():
    await memory.store("Async memory!")
    context = await memory.recall("async")
    print(context)
```

## Error Handling

```python
from remembra.exceptions import (
    RemembraError,
    AuthenticationError,
    RateLimitError,
    ValidationError
)

try:
    memory.store("content")
except AuthenticationError:
    print("Invalid API key")
except RateLimitError as e:
    print(f"Rate limited. Retry after {e.retry_after}s")
except RemembraError as e:
    print(f"Error: {e}")
```

## Best Practices

### 1. Store Facts, Not Conversations

```python
# ❌ Don't store raw conversation
memory.store("User: Hi! Bot: Hello! User: What's the weather?")

# ✅ Store extracted facts
memory.store("User asked about weather on March 1, 2026")
```

### 2. Use Projects for Isolation

```python
# Separate memories by application
personal = Memory(user_id="user_1", project="personal_assistant")
work = Memory(user_id="user_1", project="work_assistant")
```

### 3. Set Appropriate TTL

```python
# Session context (delete after 24h)
memory.store("Currently browsing electronics", ttl="24h")

# Long-term facts (1 year)
memory.store("User birthday is March 15", ttl="1y")

# Permanent (no TTL)
memory.store("User's name is John")
```

### 4. Use Metadata for Filtering

```python
memory.store(
    "User purchased Premium plan",
    metadata={
        "category": "billing",
        "importance": "high",
        "timestamp": "2026-03-01"
    }
)
```

---

## User Profiles API (v0.12.0)

Get aggregated user intelligence including facts, metrics, and topics.

```python
profile = memory.get_user_profile()
```

**Returns:**

```python
{
    "user_id": "user_123",
    "memory_count": 47,
    "entity_breakdown": {
        "PERSON": 12,
        "ORG": 8,
        "LOCATION": 5
    },
    "top_topics": ["AI", "meetings", "projects"],
    "last_active": "2026-03-22T15:30:00Z",
    "aggregated_facts": [
        "Works at Acme Corp as senior engineer",
        "Prefers morning meetings",
        "Uses dark mode"
    ]
}
```

**Use Cases:**

- Personalization dashboards
- User insights and analytics
- Context pre-loading for AI assistants

---

## Smart Auto-Forgetting (v0.12.0)

Memories with temporal phrases automatically get appropriate TTLs:

```python
# No explicit TTL needed - auto-detected
memory.store("Meeting tomorrow at 3pm")  # → 36h TTL
memory.store("Deadline in 2 hours")      # → 3h TTL
memory.store("Call next week")           # → 8 days TTL
```

Supports 35+ patterns including:
- Relative dates: "tomorrow", "next week", "in 3 days"
- Specific times: "at 3pm", "this afternoon"
- Duration phrases: "for 2 hours", "until Friday"

