# Temporal Memory

Time-aware features: TTL, decay, and historical queries.

## Time-to-Live (TTL)

Set automatic expiration on memories.

### Setting TTL

```python
# Expires in 30 days
memory.store("Meeting scheduled for next week", ttl="30d")

# Expires in 1 week
memory.store("Demo account is open this week", ttl="1w")

# Expires in 24 hours
memory.store("User is currently browsing products", ttl="24h")

# Expires in 1 year
memory.store("Annual subscription renewed", ttl="1y")
```

### TTL Formats

A TTL is a number and a unit. Decimals work: `1.5d` is 36 hours. Case does not matter, and a space is allowed
(`2 weeks`).

| Unit | Example | Means |
|------|---------|-------|
| `s` | `90s` | seconds |
| `min` or `m` | `90min` | minutes |
| `h` | `24h` | hours |
| `d` | `30d` | days |
| `w` | `2w` | weeks |
| `mo` | `3mo` | months (30 days) |
| `y` | `1y` | years (365 days) |

A TTL runs from 1 second to 100 years. The server refuses any other value with 422 and stores nothing.

!!! warning "`m` is minutes"
    Servers 0.16.1 and earlier read `m` as months, read only whole numbers (`1.5d` was ignored and the memory
    never expired), and did not know `min` or `mo`. For months, write `mo`. For a TTL that an older server must
    read, use whole `h`, `d`, `w` or `y` values.

### Common TTL values

There are no named presets in the SDK; pass the value itself.

| Use | TTL |
|-----|-----|
| Session context | `24h` |
| Conversation summary | `7d` |
| Short-term memory | `30d` |
| Long-term fact | `1y` |
| Permanent | no `ttl` |

### Server Default TTL

Set a default TTL for every memory stored without one:

```bash
REMEMBRA_DEFAULT_TTL_DAYS=365  # memories stored without a ttl expire after 1 year
```

Checkpoints (`memory_type="checkpoint"`) get `REMEMBRA_CHECKPOINT_DEFAULT_TTL` (default `7d`) instead.

### Cleanup Expired

An expired memory is hidden from recall at once, but it stays stored until a cleanup deletes it. The SDK has no
cleanup method; use the API:

```bash
# Preview (dry_run defaults to true)
curl -X POST "http://localhost:8787/api/v1/temporal/cleanup" \
  -H "X-API-Key: $REMEMBRA_API_KEY"

# Delete expired memories
curl -X POST "http://localhost:8787/api/v1/temporal/cleanup?dry_run=false" \
  -H "X-API-Key: $REMEMBRA_API_KEY"
```

With `dry_run=false` it deletes the expired memories (it needs `memory:delete`). Add `include_decayed=true` to
also move decayed memories (see below) to the cold archive. `POST /api/v1/memories/cleanup-expired` deletes
expired memories too, without a preview.

The server can also run cleanup on a timer (`REMEMBRA_TEMPORAL_CLEANUP_ENABLED=true`, every
`REMEMBRA_TEMPORAL_CLEANUP_INTERVAL_SECONDS`, default 3600). It is off by default, and it moves expired
memories to the cold archive rather than deleting them.

---

## Memory Decay

Older and unused memories rank lower in recall.

### The decay score in recall

Recall returns a `decay_score` for each memory when the request sets `include_decay_score=true`:

```
decay_score = 0.5 ^ (age_days / half_life) × (1 + 0.1 × ln(1 + accesses)) + recency_bonus
```

- **Half-life:** 30 days (`REMEMBRA_RANKING_RECENCY_DECAY_DAYS`): 0.5 after 30 days, with no accesses.
- **Accesses:** one access multiplies it by about 1.07.
- **Recency bonus:** up to 0.2 when the memory was accessed recently, fading on the same half-life.
- There is no minimum. Scores approach 0.

### The relevance score for pruning

The `/temporal` decay endpoints and pruning use a separate relevance score with a steeper curve (about 0.15
at 30 days for a memory at importance 0.5 that was never accessed). Memories below 0.1 are pruning
candidates. Cleanup prunes nothing unless you run it with `include_decayed=true`. The sleep-time worker's decay
cleanup is separate and off by default; see [Sleep-Time Compute](sleep-time-compute.md#decay-cleanup).

```bash
curl "http://localhost:8787/api/v1/temporal/decay/report?limit=50" \
  -H "X-API-Key: $REMEMBRA_API_KEY"
```

### Decay Report

```json
{
  "user_id": "user_123",
  "project_id": "default",
  "total_memories": 100,
  "prune_candidates": 5,
  "average_relevance": 0.62,
  "config": {"prune_threshold": 0.1, "...": "..."},
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
result = memory.recall(
    query="User preferences",
    as_of=last_month
)
print(result.context)
```

Via API:

```bash
curl -X POST http://localhost:8787/api/v1/memories/recall \
  -H "X-API-Key: $REMEMBRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "User preferences",
    "as_of": "2026-02-01T00:00:00Z"
  }'
```

### How It Works

The query only includes memories that:

1. Were created **before** the `as_of` timestamp
2. Were valid then: a memory superseded since then is included if it was still current at `as_of`
3. Have not expired

Memories deleted since then are gone and cannot be shown.

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
history = memory.recall(
    "User status",
    as_of=datetime(2026, 2, 15)
)
```

### Pattern 4: Memory Refresh

A memory that recall returns counts as accessed, which raises its decay score. To keep an important fact
from ranking lower with age, store it again when it is still true.

---

## API Reference

### Temporal Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/temporal/decay/report` | GET | Relevance scores and pruning candidates |
| `/api/v1/temporal/memory/{id}/decay` | GET | One memory's relevance score |
| `/api/v1/temporal/cleanup` | POST | Preview or run cleanup (`dry_run`, `include_decayed`) |
| `/api/v1/memories/cleanup-expired` | POST | Delete expired memories |

### Configuration Summary

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_TTL_DAYS` | None | Server-wide default TTL |
| `REMEMBRA_CHECKPOINT_DEFAULT_TTL` | `7d` | Default TTL for checkpoints |
| `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` | 30 | Half-life of the recall decay score and of ranking recency |
| `REMEMBRA_TEMPORAL_CLEANUP_ENABLED` | false | Run cleanup on a timer (moves expired memories to the cold archive) |
| `REMEMBRA_TEMPORAL_CLEANUP_INTERVAL_SECONDS` | 3600 | Seconds between timed cleanups |
