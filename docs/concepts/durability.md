# Durability & Recovery

Understanding Remembra's data persistence guarantees.

## Overview

Remembra uses **SQLite** for metadata and **Qdrant** for vector storage. Both provide strong durability guarantees, but understanding the nuances helps you design reliable systems.

## Recovery Contract

This section documents Remembra's **explicit guarantees** about data safety and recovery.

### The Contract

| Guarantee | Commitment |
|-----------|------------|
| **Committed writes are durable** | Once `POST /memories` returns 2xx, data survives crashes |
| **Bounded staleness** | Reads reflect writes within the snapshot window |
| **Crash recovery** | Server restarts recover all committed data |

### What We Don't Guarantee

| Non-Guarantee | Why |
|---------------|-----|
| **Sub-millisecond consistency** | Distributed systems have inherent propagation delay |
| **Infinite retention** | Memories with TTL expire; decayed memories rank lower |
| **Cross-region replication** | Not built-in (use Qdrant cluster for this) |
| **Atomicity across stores** | A store or delete writes SQLite, Qdrant and the keyword index without a shared transaction. A partial failure can leave a memory missing from search, or an orphan vector. The reconcile job reports it (`remembra_reconcile_drift` in /metrics), and `python -m remembra.storage.reconcile --repair` fixes what it can do safely. |

## Snapshot Window

The **snapshot window** is the maximum time between a write being committed and that write being visible to all readers.

### Current Behavior

| Component | Snapshot Window | Explanation |
|-----------|-----------------|-------------|
| **SQLite** | **0ms** (immediate) | Writes are synchronous, readers see instantly |
| **Qdrant** | **≤5 seconds** (default) | Flush interval before vectors are searchable |
| **Combined** | **≤5 seconds** | Bound by slowest component |

### What This Means

```
Time →   0ms        100ms       5000ms
         │           │           │
Write ───┼───────────┼───────────┼──────────────────
         │           │           │
         │  Metadata │  Vector   │
         │  visible  │  visible  │
         │  (SQLite) │  (Qdrant) │
         ▼           ▼           ▼
```

**Scenario:** You store a memory and immediately recall it.

- **Metadata queries** (list, get by ID): Immediate
- **Semantic search** (recall): May take up to 5 seconds

### Tuning the Snapshot Window

For tighter consistency (at cost of write performance):

```yaml
# qdrant config
storage:
  performance:
    flush_interval_sec: 1  # 1 second instead of 5
```

For maximum performance (relaxed consistency):

```yaml
storage:
  performance:
    flush_interval_sec: 30  # Batch more writes
```

### Exposure Surface

The snapshot window creates an **exposure surface** — a period where recent writes may not appear in search results.

**Impact by Use Case:**

| Use Case | Impact | Mitigation |
|----------|--------|------------|
| **Chat memory** | Low | Users rarely recall just-stored facts |
| **Multi-agent** | Medium | Agents may miss very recent writes |
| **Real-time sync** | High | Consider polling or webhooks |

**Mitigations:**

1. **Immediate metadata queries**: Use `GET /memories/{id}` right after store
2. **Webhook notifications**: Subscribe to `memory.stored` events
3. **Reduce flush interval**: Trade write throughput for faster visibility
4. **Application-level cache**: Cache recent writes client-side

## Durability vs Atomicity

These terms are often confused:

| Concept | Meaning | Remembra Behavior |
|---------|---------|-------------------|
| **Atomicity** | Operations complete fully or not at all | Not across stores: SQLite, Qdrant and the keyword index are written without a shared transaction |
| **Durability** | Committed data survives crashes | ✅ Data is fsync'd to disk |

### What This Means in Practice

When `POST /memories` returns success:

1. ✅ Memory is written to SQLite (metadata, content)
2. ✅ Vector is written to Qdrant
3. ✅ Both are durable on disk

If the server crashes mid-operation:

- **Before the SQLite commit**: The memory is not stored
- **Between the SQLite commit and the vector write**: The memory is stored but missing from semantic search; the reconcile job reports it, and `--repair` queues it to be embedded again
- **After both writes**: Data is fully persisted

## SQLite Configuration

Remembra uses SQLite with these settings:

```
PRAGMA foreign_keys = ON
PRAGMA journal_mode = DELETE (default)
```

### Journal Modes

| Mode | Behavior | Trade-off |
|------|----------|-----------|
| `DELETE` (default) | Journal file deleted after commit | Safest, slightly slower |
| `WAL` | Write-ahead log, concurrent reads | Faster, requires cleanup |
| `MEMORY` | Journal in RAM only | ⚠️ Not durable |

To enable WAL mode for better concurrency:

```python
# In your config or startup
await conn.execute("PRAGMA journal_mode = WAL")
```

## Qdrant Durability

Qdrant provides:

- **Write-ahead logging**: All writes logged before applied
- **Snapshots**: Periodic full snapshots for recovery
- **Segment flush**: Configurable flush intervals

Default behavior ensures durability but you can tune for performance:

```yaml
# qdrant config
storage:
  on_disk_payload: true
  performance:
    flush_interval_sec: 5  # Flush every 5 seconds
```

## Recovery Scenarios

### Scenario 1: Server Crash

**What happens:**
- SQLite recovers from journal on restart
- Qdrant replays WAL and recovers

**Data loss:** None (committed transactions are safe)

### Scenario 2: Partial Write (Torn Write)

**Detection:** Remembra validates data integrity on read:
- SQLite uses checksums on pages
- Qdrant validates segment integrity

**Recovery:** Corrupt partial writes are discarded

### Scenario 3: Concurrent Writes Under Load

**Behavior under 50+ concurrent agents:**
- SQLite serializes writes (single-writer)
- Qdrant handles concurrent writes internally
- p99 latency may increase under extreme load

**Current Performance (measured):**
- Under 50 concurrent agents: ~2.1s p99 stale window
- Target: <500ms p99 (tracked in [#4](https://github.com/remembra-ai/remembra/issues/4))

**Mitigation:**
- Enable WAL mode for better read concurrency
- Use connection pooling
- Consider Qdrant cluster for write scaling
- Reduce Qdrant flush interval for tighter consistency

## Best Practices

### For Production Deployments

1. **Enable WAL mode** for better concurrency:
   ```bash
   export REMEMBRA_SQLITE_WAL=true
   ```

2. **Regular backups**:
   ```bash
   # SQLite backup (safe during operation)
   sqlite3 remembra.db ".backup backup.db"
   
   # Qdrant snapshot
   curl -X POST http://localhost:6333/collections/remembra/snapshots
   ```

3. **Monitor disk space**: Both SQLite WAL and Qdrant segments grow

### For High-Availability

1. Use Qdrant cluster mode for vector storage redundancy
2. Replicate SQLite using Litestream or similar
3. Deploy multiple Remembra instances behind a load balancer

## Metrics to Monitor

| Metric | What It Indicates |
|--------|-------------------|
| `store_latency_p99` | Write performance under load |
| `recall_latency_p99` | Read performance |
| `sqlite_wal_size` | WAL file growth (if using WAL) |
| `qdrant_segments` | Vector storage fragmentation |

## FAQ

### "Lose at most the last memory" — What does this mean?

If the server crashes during a `store` operation:
- **Before the SQLite commit**: The memory is not stored
- **After commit**: The memory is fully durable

You cannot lose previously committed memories.

### How often are checkpoints run?

- **SQLite WAL**: Checkpoints when WAL reaches ~1000 pages
- **Qdrant**: Configurable flush interval (default: 5 seconds)

### Can I tune snapshot frequency?

For Qdrant, yes:
```yaml
storage:
  performance:
    flush_interval_sec: 1  # More frequent (slower writes)
```

For SQLite WAL checkpoints:
```sql
PRAGMA wal_checkpoint(TRUNCATE);  -- Force checkpoint
```

---

*Questions? Open an issue on [GitHub](https://github.com/remembra-ai/remembra/issues).*
