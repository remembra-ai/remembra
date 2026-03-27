# Durability & Recovery

Understanding Remembra's data persistence guarantees.

## Overview

Remembra uses **SQLite** for metadata and **Qdrant** for vector storage. Both provide strong durability guarantees, but understanding the nuances helps you design reliable systems.

## Durability vs Atomicity

These terms are often confused:

| Concept | Meaning | Remembra Behavior |
|---------|---------|-------------------|
| **Atomicity** | Operations complete fully or not at all | ✅ Each store/update is atomic |
| **Durability** | Committed data survives crashes | ✅ Data is fsync'd to disk |

### What This Means in Practice

When `POST /memories` returns success:

1. ✅ Memory is written to SQLite (metadata, content)
2. ✅ Vector is written to Qdrant
3. ✅ Both are durable on disk

If the server crashes mid-operation:

- **Before commit**: Nothing is written (atomic rollback)
- **After commit**: Data is fully persisted

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

**Mitigation:**
- Enable WAL mode for better read concurrency
- Use connection pooling
- Consider Qdrant cluster for write scaling

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
- **Before commit**: The memory is not stored (atomic)
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
