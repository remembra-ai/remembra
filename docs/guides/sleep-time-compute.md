# Sleep-Time Compute

**NEW in v0.7.0** — Background memory consolidation that runs during idle time.

Inspired by Letta/MemGPT's approach — your AI "thinks" between conversations to improve memory quality.

## Overview

Sleep-Time Compute is a background worker that processes memories when the system is idle:

- **Cross-session deduplication** — Merge duplicate memories across sessions
- **Entity alias resolution** — Link "my wife" = "Suzan" = "Mrs. Johnson"
- **Relationship discovery** — Find patterns and connections
- **Importance rescoring** — Adjust based on actual access patterns
- **Memory decay cleanup** — Remove stale, unused memories

## Why This Matters

Real-time memory operations need to be fast. Complex consolidation takes time.

**Without Sleep-Time Compute:**
- Duplicates accumulate across sessions
- Entity aliases remain unlinked
- Memory quality degrades over time

**With Sleep-Time Compute:**
- Memories consolidate automatically
- Entity graph stays connected
- Quality improves over time

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                    Real-Time Operations                      │
│  store() → quick insert, basic dedup                        │
│  recall() → fast retrieval                                  │
└─────────────────────────────────────────────────────────────┘
                           ↓
                    (when idle)
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                   Sleep-Time Worker                          │
│  1. Deep deduplication across all sessions                  │
│  2. Entity alias resolution                                 │
│  3. Relationship graph updates                              │
│  4. Importance rescoring                                    │
│  5. Decay cleanup                                           │
└─────────────────────────────────────────────────────────────┘
```

## Configuration

Enable and configure in your environment:

```bash
# Enable sleep-time compute
REMEMBRA_SLEEP_TIME_ENABLED=true

# Run every 6 hours (in seconds)
REMEMBRA_SLEEP_TIME_INTERVAL=21600

# Minimum idle time before running (seconds)
REMEMBRA_SLEEP_TIME_IDLE_THRESHOLD=300

# Consolidation similarity threshold (0.0-1.0)
REMEMBRA_CONSOLIDATION_THRESHOLD=0.85
```

## API Reference

### Trigger Manually

```http
POST /api/v1/admin/consolidate
```

```json
{
  "user_id": "user_123"  // Optional - all users if omitted
}
```

### Response

```json
{
  "success": true,
  "report": {
    "user_id": "user_123",
    "started_at": "2026-03-03T12:00:00Z",
    "completed_at": "2026-03-03T12:00:45Z",
    "stats": {
      "memories_scanned": 1250,
      "duplicates_merged": 23,
      "entities_linked": 8,
      "relationships_discovered": 5,
      "memories_decayed": 12,
      "importance_updates": 45
    }
  }
}
```

## Python SDK

```python
from remembra import Memory
from remembra.services.sleep_time import SleepTimeWorker

memory = Memory(user_id="user_123")

# Trigger consolidation manually
report = await memory.consolidate()

print(report.stats)
# ConsolidationStats(
#     duplicates_merged=23,
#     entities_linked=8,
#     relationships_discovered=5,
#     ...
# )
```

## Consolidation Tasks

### 1. Duplicate Detection

Finds memories with high semantic similarity that should be merged:

```
Before:
  - "John works at Acme Corp" (session A)
  - "John is employed by Acme Corporation" (session B)

After:
  - "John works at Acme Corp" (merged, higher confidence)
```

### 2. Entity Alias Resolution

Links entity references across memories:

```
Before:
  - Entity: "my wife" (unlinked)
  - Entity: "Suzan" (unlinked)
  - Memory: "my wife's birthday is March 15"
  - Memory: "Suzan works at Google"

After:
  - Entity: "Suzan" (aliases: ["my wife", "Mrs. Johnson"])
  - All memories linked to same entity
```

### 3. Relationship Discovery

Infers relationships from patterns:

```
Found pattern:
  - "John works at Acme"
  - "Sarah works at Acme"
  - "John and Sarah had a meeting"

Discovered:
  - Relationship: John → COLLEAGUE_OF → Sarah
```

### 4. Importance Rescoring

Adjusts importance based on actual usage:

```
Memory: "User prefers dark mode"
  - Stored importance: 0.5
  - Recalled 15 times
  - New importance: 0.8 (frequently accessed = more important)
```

### 5. Decay Cleanup

Removes memories below threshold:

```
Memory: "Weather was nice yesterday"
  - Initial importance: 0.3
  - TTL: 24h
  - After decay: 0.05
  - Action: DELETED (below 0.1 threshold)
```

## Monitoring

### Check Status

```http
GET /api/v1/admin/consolidate/status
```

```json
{
  "enabled": true,
  "last_run": "2026-03-03T06:00:00Z",
  "next_scheduled": "2026-03-03T12:00:00Z",
  "running": false,
  "last_report": { ... }
}
```

### Webhook Events

Subscribe to consolidation events:

- `consolidation.started` — Worker began
- `consolidation.completed` — Worker finished
- `consolidation.error` — Worker failed

## Best Practices

### Production Settings

```bash
# Conservative settings for production
REMEMBRA_SLEEP_TIME_INTERVAL=21600      # Every 6 hours
REMEMBRA_CONSOLIDATION_THRESHOLD=0.90   # High similarity required
REMEMBRA_SLEEP_TIME_IDLE_THRESHOLD=600  # 10 min idle
```

### Development Settings

```bash
# Aggressive for testing
REMEMBRA_SLEEP_TIME_INTERVAL=300        # Every 5 minutes
REMEMBRA_CONSOLIDATION_THRESHOLD=0.80   # Lower threshold
REMEMBRA_SLEEP_TIME_IDLE_THRESHOLD=60   # 1 min idle
```

## Related

- [Conversation Ingestion](./conversation-ingestion.md) — Real-time extraction
- [Entity Resolution](./entity-resolution.md) — How entities are linked
- [Security](./security.md) — PII detection and anomaly monitoring
