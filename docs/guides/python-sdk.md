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

```python
memory.store(
    content: str,
    metadata: dict | None = None,
    ttl: str | None = None,
    auto_expire: bool | None = None,
    skip_extraction: bool = False,
    memory_type: str | None = None,
    project_id: str | None = None,
) -> StoreResult
```

**Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `content` | `str` | Text to store (can be messy conversation) |
| `metadata` | `dict` | Custom metadata (tags, source, etc.) |
| `ttl` | `str` | Time-to-live: a number and a unit, e.g. `"24h"`, `"30d"`, `"2w"`, `"1y"`. Every unit is in [TTL formats](temporal.md#ttl-formats) |
| `auto_expire` | `bool` | Turn the temporal-phrase TTL on or off for this call (see Smart Auto-Forgetting below) |
| `skip_extraction` | `bool` | Store the content as one memory, with no fact extraction or merging |
| `memory_type` | `str` | For example `"checkpoint"` (gets the server's default TTL) or `"handoff"` |
| `project_id` | `str` | Store into this project instead of the client's |

The Python SDK has no `expires_at` argument. The REST API takes one: `POST /api/v1/memories` with
`"expires_at": "2026-03-25T14:00:00Z"`.

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
| `slim` | `bool` | Return only the context string, without metadata (a smaller payload) |

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

Delete memories. They leave the database and the vector store at once; copies in backups age out (see Retention on [remembra.dev/security](https://remembra.dev/security#retention)).

```python
memory.forget(
    memory_id: str | None = None,
    *,
    entity: str | None = None,
    project_id: str | None = None,
    all_memories: bool = False,
) -> ForgetResult
```

Give exactly one of `memory_id`, `entity` or `all_memories=True`. Any other call raises `MemoryError` and sends
nothing: no call deletes everything unless it says `all_memories=True`. The server deletes only the memories of
the account your API key belongs to.

- `memory_id` deletes that one memory.
- `entity` deletes the memories that entity extraction linked to the entity with that exact name or alias
  (any case; never a partial name), in every project, or only in `project_id` when you pass it. The entity and
  its relationships go too once no memory mentions it. A memory that names the entity but was never linked to it
  (entity extraction off or still pending) stays.
- `all_memories=True` deletes every memory, entity and relationship in the account. A project-scoped API key
  cannot use it.

!!! warning "Servers before 0.16.1"
    A server before 0.16.1 deleted every memory in the account for a delete by `entity`. `forget(entity=...)`
    reads the server version from `/health` first and raises `MemoryError` without deleting anything when the
    server is older than 0.16.1 or does not report a version.

To delete every memory in one project, use `forget_project(project_id)`.

**Example:**

```python
# Forget one memory
memory.forget(memory_id="mem_abc123")

# Forget what was stored about John in one project
result = memory.forget(entity="John", project_id="work")
print(result.deleted_memories, result.deleted_entities)

# Forget every memory in one project
memory.forget_project("my-project")
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

Off by default. Turn it on when you create the client, and a store with no `ttl` gets one from a temporal
phrase in the text (38 patterns):

```python
memory = Memory(auto_expire_temporal=True)

memory.store("Meeting tomorrow at 3pm")  # sends ttl="36h"
memory.store("Deadline in 2 hours")      # sends ttl="3h"
memory.store("Call next week")           # sends ttl="10d"
memory.store("Ping me in 10 minutes")    # sends ttl="1h"
```

The patterns cover phrases such as "tomorrow", "tonight", "next week", "in 3 days", "remember this for 2
hours", "until Friday", "next month" and "annually". `store(..., auto_expire=False)` turns it off for one call, and an explicit
`ttl` always wins.

The SDK sends whole hours, rounded up, or whole days. SDK 0.16.1 and earlier sent many phrases as values that
a server at 0.16.1 or earlier cannot read, such as `1.5d` for "tomorrow", `1.4w` for "next week" and `1mo` for
"this month": with both at those versions, the memory gets no expiry. They sent `40m` for "in 10 minutes",
which those servers read as 40 months.

