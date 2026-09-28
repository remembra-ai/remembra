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
    api_key="rem_...",       # when the server has auth on
    project="my_app",        # optional namespace
)

# Store a memory
memory.store("User prefers dark mode")

# Recall memories
result = memory.recall("What are user preferences?")
print(result.context)
```

The client is synchronous. There is no async client.

## Memory Class

### Constructor

```python
Memory(
    base_url: str = "http://localhost:8787",
    api_key: str | None = None,
    user_id: str = "default",
    project: str = "default",
    timeout: float = 30.0,
    auto_expire_temporal: bool = False,
    temporal_min_confidence: float = 0.6,
    enable_shadow_ttl: bool = False,
    shadow_ttl_max_entries: int = 10000,
    agent_id: str | None = None,
    session_id: str | None = None,
    provenance: bool = True,
    provenance_source: str = "sdk",
    project_aliases: Mapping[str, str] | None = None,
)
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `base_url` | Remembra server URL | `http://localhost:8787` |
| `api_key` | API key (when the server has auth on) | `None` |
| `user_id` | Sent with requests; with auth on, the server takes the user from the API key instead | `"default"` |
| `project` | Project namespace | `"default"` |
| `timeout` | Request timeout in seconds | `30.0` |
| `auto_expire_temporal` | Give stores a TTL from temporal phrases (see Smart Auto-Forgetting) | `False` |
| `enable_shadow_ttl` | Keep a local TTL cache you can check with `is_memory_valid()` | `False` |
| `agent_id`, `session_id` | Provenance stamped on each store's metadata | `None` |

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

Retrieve relevant memories using semantic and keyword search.

```python
memory.recall(
    query: str | None = None,
    limit: int = 5,
    threshold: float = 0.4,
    filters: dict[str, str] | None = None,
    retrieval_mode: str | None = None,
    scope: str | None = None,
    as_of: str | datetime | None = None,
    max_tokens: int | None = None,
    slim: bool = False,
    include_superseded: bool = False,
    project_id: str | None = None,
) -> RecallResult
```

**Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `query` | `str` | Natural language query (optional when `filters` is given) |
| `limit` | `int` | Max memories to return (1-50) |
| `threshold` | `float` | Minimum cosine similarity (0-1) for vector hits; keyword and entity-graph hits are not subject to it |
| `filters` | `dict` | Exact-match metadata filters, combined with AND |
| `retrieval_mode` | `str` | `balanced`, `debug` (recent first), `operational`, `strategic` or `auto` |
| `scope` | `str` | Only memories whose scope starts with this label |
| `as_of` | `datetime` or ISO string | Historical query (time travel) |
| `max_tokens` | `int` | Cap on the context string |
| `slim` | `bool` | Caps the context at 800 tokens. Memories and entities are still returned |
| `include_superseded` | `bool` | Also return memories replaced by newer ones |
| `project_id` | `str` | Recall from this project instead of the client's |

**Example:**

```python
# Basic recall
result = memory.recall("What do I know about the user?")
print(result.context)

# With options
result = memory.recall(
    "What projects is John working on?",
    limit=5,
    threshold=0.5,
    max_tokens=2000,
)
for m in result.memories:
    print(m.id, m.relevance, m.content)

# Historical query (see memories as of last week)
from datetime import datetime, timedelta
last_week = datetime.now() - timedelta(days=7)
result = memory.recall("User status", as_of=last_week)
```

**Returns:** a `RecallResult` with `context` (a string ready for an LLM prompt), `memories` (each with `id`,
`content`, `relevance`, `created_at`, `metadata`, `memory_type`), `entities`, and `degraded` when the server
answered in a degraded mode.

### update()

Change a memory's content (and, optionally, its metadata).

```python
memory.update(
    memory_id: str,
    content: str,
    metadata: dict | None = None,
) -> dict
```

**Example:**

```python
result = memory.store("John is a software engineer")

memory.update(result.id, "John is a senior software engineer at Google")
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

## Other Methods

| Method | What it does |
|--------|--------------|
| `get(memory_id)` | One memory by id |
| `list(limit=20, offset=0, project_id=None)` | Memories in the project, newest first |
| `timeline(start=None, end=None, entity=None, ...)` | Memories in a time range, oldest first by default |
| `list_entities(entity_type=None, limit=100)` | Entities in the account's graph |
| `ingest_conversation(messages, session_id=None, ...)` | Extract memories from a chat transcript |
| `ingest_changelog(content=None, file_path=None, project_name=None)` | Store each release of a changelog as a memory |
| `health()` | The server's `/health` response |
| `is_memory_valid(memory_id)` | With `enable_shadow_ttl=True`: whether the local TTL cache says the memory has not expired |

Relay and inbox methods (`session_brief`, `close_session`, `store_status`, `list_status`, `trail`,
`send_to_inbox`, `get_inbox`, `ack_inbox`) are covered in the [Relay guide](relay.md).

There are no SDK methods for expired-memory cleanup or decay scores. Use the REST API for those
(`POST /api/v1/temporal/cleanup`, `GET /api/v1/temporal/decay/report`; see [Temporal](temporal.md)).

**Example:**

```python
memory.ingest_changelog(file_path="CHANGELOG.md", project_name="my-project")
```

## Error Handling

Every failed request raises `remembra.MemoryError`. Its `status_code` holds the HTTP status (`None` when the
request never reached the server).

```python
from remembra import MemoryError

try:
    memory.store("content")
except MemoryError as e:
    if e.status_code == 401:
        print("Invalid API key")
    elif e.status_code == 429:
        print("Rate limited or over a plan limit")
    else:
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

Aggregated facts, entities, activity and top topics for your account. The SDK has no method for it; call the
REST API:

```http
GET /api/v1/users/me/profile
```

It returns `total_memories`, `total_entities`, `total_relationships`, `static_facts` (facts and top entities),
`activity` (memories in the last 24 hours, 7 days and 30 days), `top_topics` and `last_active`. A key can read
only its own account's profile.

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

