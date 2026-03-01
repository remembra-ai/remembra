# Temporal Memory

Time-aware features: TTL, decay, and historical queries.

## Time-to-Live (TTL)

Set automatic expiration on memories.

### Setting TTL

```python
# Expires in 30 days
memory.store("Meeting scheduled for next week", ttl="30d")

# Expires in 1 week
memory.store("Temporary API key: xyz123", ttl="1w")

# Expires in 24 hours
memory.store("User is currently browsing products", ttl="24h")

# Expires in 1 year
memory.store("Annual subscription renewed", ttl="1y")
```

### TTL Formats

| Format | Example | Description |
|--------|---------|-------------|
| `Nd` | `30d` | N days |
| `Nw` | `2w` | N weeks |
| `Nm` | `3m` | N months |
| `Ny` | `1y` | N years |
| `Nh` | `24h` | N hours |

### TTL Presets

```python
from remembra import TTLPresets

memory.store("Session context", ttl=TTLPresets.SESSION)       # 24h
memory.store("Conversation", ttl=TTLPresets.CONVERSATION)     # 7d
memory.store("Short term", ttl=TTLPresets.SHORT_TERM)         # 30d
memory.store("Long term", ttl=TTLPresets.LONG_TERM)           # 365d
memory.store("Permanent", ttl=TTLPresets.PERMANENT)           # Never expires
```

### Server Default TTL

Set a default TTL for all memories:

```bash
REMEMBRA_DEFAULT_TTL_DAYS=365  # All memories expire after 1 year
```

### Cleanup Expired

Expired memories are soft-deleted (marked expired) by default. Run cleanup to purge:

```python
# Preview what would be deleted
result = memory.cleanup_expired(dry_run=True)
print(f"Would delete {result['would_delete']} memories")

# Actually delete
memory.cleanup_expired(dry_run=False)
```

Or via API:

```bash
curl -X POST http://localhost:8787/api/v1/temporal/cleanup \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'
```

---

## Memory Decay

Older and unused memories rank lower in recall.

### How Decay Works

```
Decay Score = time_decay × access_boost × recency_boost

where:
- time_decay: Exponential decay based on age
- access_boost: Higher score for frequently accessed memories
- recency_boost: Higher score for recently accessed memories
```

### Decay Formula

Based on Ebbinghaus forgetting curve:

```python
retention = e^(-time / half_life)
```

With defaults:
- **Half-life**: 30 days (memory loses 50% score after 30 days)
- **Access boost**: Each access increases score by 20%
- **Minimum score**: 0.1 (never fully forgotten)

### Configuration

```bash
# Enable/disable decay
REMEMBRA_DECAY_ENABLED=true

# Days until 50% decay
REMEMBRA_DECAY_HALF_LIFE_DAYS=30

# Boost per access
REMEMBRA_ACCESS_BOOST_WEIGHT=0.2
```

### Viewing Decay

```python
# Get memories with decay scores
memories = memory.get_memories_with_decay()
for m in memories:
    print(f"{m['content'][:50]}... | decay: {m['decay_score']:.2f}")
```

Via API:

```bash
curl http://localhost:8787/api/v1/temporal/decay/report?user_id=user_123
```

### Decay Report

```json
{
  "total_memories": 100,
  "healthy": 85,      // decay_score > 0.7
  "decaying": 10,     // 0.3 < decay_score < 0.7
  "expired": 5,       // decay_score < 0.3
  "memories": [...]
}
```

---

## Historical Queries (as_of)

Time-travel to see memories as they existed at a point in time.

### Use Cases

- **Debugging**: "What did the system know last week?"
- **Auditing**: "What was stored before the incident?"
- **Analysis**: "How has user preference evolved?"

### Usage

```python
from datetime import datetime, timedelta

# What did we know about the user last month?
last_month = datetime.now() - timedelta(days=30)
context = memory.recall_as_of(
    query="User preferences",
    timestamp=last_month
)

# Or with recall()
context = memory.recall(
    query="User preferences",
    as_of=last_month
)
```

Via API:

```bash
curl -X POST http://localhost:8787/api/v1/recall \
  -H "Content-Type: application/json" \
  -d '{
    "query": "User preferences",
    "user_id": "user_123",
    "as_of": "2026-02-01T00:00:00Z"
  }'
```

### How It Works

The query filters memories to only include those that:

1. Were created **before** the `as_of` timestamp
2. Had not **expired** by the `as_of` timestamp

---

## Practical Patterns

### Pattern 1: Session Memory

Short-lived context for current session:

```python
# Store session context (expires in 24h)
memory.store(
    "User is comparing iPhone 15 and Galaxy S24",
    ttl="24h",
    metadata={"type": "session"}
)
```

### Pattern 2: Graduated TTL

Important facts get longer TTL:

```python
def store_with_importance(content: str, importance: str):
    ttl_map = {
        "low": "7d",
        "medium": "90d",
        "high": "365d",
        "permanent": None
    }
    memory.store(content, ttl=ttl_map.get(importance))

store_with_importance("User clicked on ad", "low")
store_with_importance("User purchased Premium", "permanent")
```

### Pattern 3: Audit Trail

Keep historical snapshots:

```python
# Store user state changes with timestamps
memory.store(
    f"User status changed to Premium at {datetime.now().isoformat()}",
    metadata={"event": "status_change", "new_status": "premium"}
)

# Later: audit what happened
history = memory.recall_as_of(
    "User status",
    timestamp=datetime(2026, 2, 15)
)
```

### Pattern 4: Memory Refresh

Re-store important facts to reset decay:

```python
# Refresh critical memories periodically
important_memories = memory.get_memories_with_decay()
for m in important_memories:
    if m['decay_score'] < 0.5 and m['metadata'].get('important'):
        # Re-store to reset decay
        memory.store(m['content'], metadata=m['metadata'])
```

---

## API Reference

### Temporal Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/temporal/decay/report` | GET | View decay scores |
| `/api/v1/temporal/memory/{id}/decay` | GET | Single memory decay |
| `/api/v1/temporal/cleanup` | POST | Run cleanup job |
| `/api/v1/cleanup-expired` | POST | Delete expired memories |

### Configuration Summary

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_TTL_DAYS` | None | Server-wide default TTL |
| `REMEMBRA_DECAY_ENABLED` | true | Enable decay scoring |
| `REMEMBRA_DECAY_HALF_LIFE_DAYS` | 30 | Days to 50% decay |
| `REMEMBRA_ACCESS_BOOST_WEIGHT` | 0.2 | Boost per access |
