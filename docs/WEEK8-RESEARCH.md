# Week 8 Research: Temporal Features

**Date:** 2026-03-01
**Goal:** Implement TTL, memory decay, and historical queries for Remembra

---

## Executive Summary

Temporal features transform Remembra from a static memory store to a **living, evolving knowledge system** that mirrors human cognition. The key insight from research: **good memory systems need to forget effectively** — forgetting isn't a flaw, it's a feature.

---

## 1. TTL (Time-To-Live) Implementation

### Industry Approaches

**MongoDB/Document DBs:**
- TTL implemented via index on date field with `expireAfterSeconds`
- Automatic cleanup without manual intervention
- Example: `db.collection.createIndex({createdAt: 1}, {expireAfterSeconds: 86400})`

**Vector DB Hygiene (Best Practices):**
- Store: `expires_at`, `created_at`, `last_seen_at`, `last_accessed_at`
- Different TTL policies by content type:
  - Logs: days
  - Docs: months
  - User preferences: years (or never)
  - Session data: hours

**Mem0's Approach:**
- Decay mechanisms that remove irrelevant information over time
- Priority scoring determines what stays vs. goes
- Memory consolidation moves important info to long-term storage

### Recommended Implementation for Remembra

```python
# Memory schema additions
class Memory:
    id: str
    content: str
    embedding: List[float]
    
    # Temporal fields
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime
    access_count: int = 0
    
    # TTL config
    ttl_seconds: Optional[int] = None  # None = never expires
    expires_at: Optional[datetime] = None
    
    # Decay scoring
    importance_score: float = 0.5  # 0-1 scale
    decay_rate: float = 0.07  # Based on Ebbinghaus curve
```

### TTL Strategies

| Memory Type | Default TTL | Rationale |
|-------------|-------------|-----------|
| Session context | 24 hours | Temporary, high churn |
| Conversation summaries | 7 days | Medium retention |
| User preferences | Never | Core personalization |
| Facts/entities | 90 days | Refreshed on access |
| Episodic memories | 30 days | Event-specific |

---

## 2. Memory Decay Algorithm

### The Science: Ebbinghaus Forgetting Curve

**Exponential Decay Formula:**
```
R = e^(-t/S)

Where:
- R = probability of recall (retrievability)
- t = time elapsed
- S = memory stability (strength)
```

**Power Law Alternative (for mixed memories):**
```
P(recall) = m(1 + ht)^-f

Where:
- m = degree of initial learning (probability at t=0)
- h = scaling factor on time
- f = exponential decay factor
```

**SuperMemo's Formula:**
```
R = 0.9906 * power(interval, -0.07)

Where:
- 0.9906 = recall after one day
- -0.07 = decay constant
```

### Key Insight: Reinforcement Counteracts Decay

Each access/recall **strengthens** the memory:
- Access increases `stability` (S)
- Higher stability = slower decay
- Mimics human spaced repetition learning

### Recommended Decay Implementation

```python
import math
from datetime import datetime, timedelta

def calculate_relevance_score(memory: Memory) -> float:
    """
    Calculate current relevance based on decay + reinforcement.
    Returns 0.0-1.0 where higher = more relevant.
    """
    now = datetime.utcnow()
    
    # Time since last access (in days)
    time_elapsed = (now - memory.last_accessed_at).total_seconds() / 86400
    
    # Base decay (Ebbinghaus curve)
    # Stability increases with access_count
    stability = 1.0 + (memory.access_count * 0.5)
    decay_factor = math.exp(-time_elapsed / stability)
    
    # Combine with importance score
    relevance = memory.importance_score * decay_factor
    
    # Boost for recent creation (new memories get a grace period)
    days_since_creation = (now - memory.created_at).total_seconds() / 86400
    if days_since_creation < 7:
        newness_boost = 1.0 + (0.3 * (1 - days_since_creation / 7))
        relevance *= newness_boost
    
    return min(1.0, max(0.0, relevance))


def should_prune(memory: Memory, threshold: float = 0.1) -> bool:
    """
    Determine if memory should be pruned based on decay.
    """
    if memory.ttl_seconds is None:
        # No TTL = check decay score only
        return calculate_relevance_score(memory) < threshold
    
    # Has TTL = check expiration
    if memory.expires_at and datetime.utcnow() > memory.expires_at:
        return True
    
    return calculate_relevance_score(memory) < threshold
```

### Decay Tuning Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_decay_rate` | 0.07 | How fast memories fade (higher = faster) |
| `access_stability_bonus` | 0.5 | Stability increase per access |
| `importance_weight` | 0.6 | How much importance affects retention |
| `prune_threshold` | 0.1 | Relevance below this = candidate for removal |
| `newness_grace_days` | 7 | Days before decay fully kicks in |

---

## 3. Historical Queries (as_of)

### Use Cases

1. **Debugging:** "What did the agent know at time X?"
2. **Audit:** "What was the user's preference before they changed it?"
3. **Time travel:** "Answer this question as if it were last Tuesday"

### Implementation Approaches

**Option A: Soft Delete + Versioning**
- Never hard delete memories
- Mark as `superseded_at` when updated
- Query with `WHERE superseded_at IS NULL OR superseded_at > as_of_time`

**Option B: Event Sourcing**
- Store all memory events (CREATE, UPDATE, DELETE)
- Reconstruct state at any point in time
- More storage, but complete history

**Option C: Snapshots + Events (Recommended)**
- Periodic snapshots (daily/weekly)
- Events between snapshots
- Best balance of performance and completeness

### Recommended Schema

```python
class MemoryVersion:
    memory_id: str
    version: int
    content: str
    embedding: List[float]
    valid_from: datetime
    valid_until: Optional[datetime]  # None = current version
    change_type: str  # 'create', 'update', 'delete'
    
class MemoryEvent:
    event_id: str
    memory_id: str
    event_type: str  # 'access', 'update', 'delete', 'decay_prune'
    timestamp: datetime
    metadata: dict  # What changed
```

### Query API

```python
# Current state (default)
memories = remembra.recall("user preferences")

# Historical state
memories = remembra.recall(
    "user preferences",
    as_of=datetime(2026, 2, 15, 12, 0, 0)
)

# With decay simulation at that time
memories = remembra.recall(
    "user preferences",
    as_of=datetime(2026, 2, 15),
    apply_decay_at_time=True  # Calculates relevance as if queried then
)
```

---

## 4. Competitor Analysis: Mem0

### What Mem0 Does Well

1. **Dynamic Forgetting:** Decays low-relevance entries over time
2. **Memory Consolidation:** Moves info between short/long-term based on usage
3. **Priority Scoring:** Not all memories equally important
4. **Cross-Session Continuity:** Maintains context across sessions

### Gaps We Can Exploit

1. **No explicit TTL configuration** — Users can't set custom expiry
2. **Limited historical queries** — Can't query "as of" a specific time
3. **Opaque decay algorithm** — Users don't control decay parameters
4. **No decay visualization** — Can't see why memories were pruned

### Remembra Differentiation

| Feature | Mem0 | Remembra (Planned) |
|---------|------|-------------------|
| TTL Support | Implicit | Explicit per-memory |
| Decay Algorithm | Proprietary | Configurable, transparent |
| Historical Queries | ❌ | ✅ (as_of parameter) |
| Decay Dashboard | ❌ | ✅ (visualize decay over time) |
| Access Patterns | Limited | Full analytics |

---

## 5. Architecture Recommendations

### Database Layer

**Qdrant (Vector Store):**
- Use payload fields for temporal metadata
- Filter by `expires_at` during search
- No native TTL, implement via background job

**SQLite (Metadata + Versioning):**
- Store `memory_versions` table for history
- Store `memory_events` for audit trail
- TTL cleanup via scheduled task

### Background Jobs

```python
# Scheduled tasks (run every hour)
async def temporal_maintenance():
    """
    1. Prune expired memories (hard TTL)
    2. Mark decayed memories (soft decay)
    3. Consolidate short-term → long-term
    4. Update access statistics
    """
    
    # 1. Hard TTL expiration
    expired = await db.query(
        "SELECT id FROM memories WHERE expires_at < NOW()"
    )
    for memory_id in expired:
        await archive_memory(memory_id)  # Move to cold storage
        await delete_from_qdrant(memory_id)
    
    # 2. Soft decay marking
    all_memories = await get_all_memories()
    for memory in all_memories:
        score = calculate_relevance_score(memory)
        if score < PRUNE_THRESHOLD:
            await mark_for_review(memory)  # Don't auto-delete, flag for review
    
    # 3. Consolidation (move frequently accessed short-term to long-term)
    # ... implementation
```

---

## 6. Implementation Plan

### Phase 1: Core Temporal Fields (Day 1-2)
- [ ] Add temporal columns to memory schema
- [ ] Update store() to set timestamps
- [ ] Update recall() to track access
- [ ] Write migrations

### Phase 2: TTL Support (Day 2-3)
- [ ] Add TTL parameter to store()
- [ ] Implement expiration filtering in recall()
- [ ] Create background cleanup job
- [ ] Add TTL to dashboard

### Phase 3: Decay Algorithm (Day 3-4)
- [ ] Implement relevance scoring
- [ ] Add decay to search ranking
- [ ] Create decay configuration endpoints
- [ ] Add decay visualization to dashboard

### Phase 4: Historical Queries (Day 4-5)
- [ ] Create memory_versions table
- [ ] Implement versioning on update
- [ ] Add as_of parameter to recall()
- [ ] Test historical reconstruction

### Phase 5: Testing & Polish (Day 5)
- [ ] Unit tests for all temporal functions
- [ ] Integration tests
- [ ] Performance benchmarks
- [ ] Documentation

---

## 7. Key Formulas Reference

### Relevance Score
```python
relevance = importance * e^(-time_days / stability) * newness_boost
```

### Stability Increase (per access)
```python
new_stability = old_stability + (access_bonus * sqrt(access_count))
```

### Decay Threshold for Pruning
```python
should_prune = relevance < threshold AND days_since_access > grace_period
```

---

## 8. Open Questions

1. **Hard vs Soft Delete:** Archive decayed memories or truly delete?
2. **User Override:** Let users "pin" memories to prevent decay?
3. **Decay Notification:** Alert users before auto-pruning important memories?
4. **Batch vs Realtime:** Calculate decay on-query or pre-compute?

---

## References

1. Ebbinghaus, H. (1885). Memory: A Contribution to Experimental Psychology
2. SuperMemo Algorithm SM-17 - supermemo.guru
3. Wixted & Carpenter (2007). Forgetting Curve Formula
4. Mem0 Documentation - mem0.ai/docs
5. MongoDB TTL Indexes - docs.mongodb.com
6. "Beyond Vector Databases" - Medium Article (2025)

---

*Research compiled by General 🎖️ for Remembra v0.6.0*
