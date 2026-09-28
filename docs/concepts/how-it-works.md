# How It Works

Understanding Remembra's architecture.

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Your Application                         │
│                                                              │
│  memory.store("User likes dark mode")                        │
│  context = memory.recall("What are user preferences?")       │
├─────────────────────────────────────────────────────────────┤
│                   Remembra SDK / REST API                    │
├──────────────┬──────────────┬───────────────┬───────────────┤
│  Extraction  │   Entities   │   Retrieval   │   Temporal    │
│              │              │               │               │
│  LLM-based   │  Resolution  │ Hybrid Search │  TTL/Decay    │
│  fact parse  │  + Matching  │ + Reranking   │  + History    │
├──────────────┴──────────────┴───────────────┴───────────────┤
│                      Storage Layer                           │
│                                                              │
│     Qdrant (vectors)  +  SQLite (metadata, graph)           │
└─────────────────────────────────────────────────────────────┘
```

## The Store Pipeline

When you call `memory.store()`:

### 1. Smart Extraction

Raw text is transformed into clean, atomic facts.

```
Input: "Had coffee with John at Starbucks. He mentioned 
        he got promoted to VP. Great news!"

Extracted Facts:
- "John was promoted to VP"
- "Met John at Starbucks"
```

The extraction model (GPT-4o-mini by default) handles:
- Noise removal (filler words, emotions)
- Fact atomization (one fact per statement)
- Normalization (consistent formatting)

### 2. Consolidation

Before storing, each new fact is compared with up to 5 similar memories:

| Action | When | Result |
|--------|------|--------|
| `ADD` | New fact | Store as new memory |
| `NOOP` | Already exists | Skip, don't duplicate |
| `SUPERSEDE` | Replaces an outdated memory | Store the new fact; mark the old one superseded (it is kept, not deleted) |

Before v0.16.0 the actions were ADD, UPDATE, DELETE and NOOP.

### 3. Entity Extraction

Identify entities in the facts:

```
Fact: "John was promoted to VP"

Entities:
- John (PERSON)
- VP (ROLE) → linked to John
```

### 4. Entity Resolution

Match to existing entities or create new ones:

```
Existing: "John Smith" with aliases ["John", "Mr. Smith"]

New mention: "John" → Matched to "John Smith"
```

### 5. Relationship Extraction

Map connections between entities:

```
"John works at Google"

Relationship: John → WORKS_AT → Google
```

### 6. Embedding

Convert facts to vectors for semantic search:

```python
embedding = embed("John was promoted to VP")
# Returns: [0.023, -0.451, 0.812, ...] (1536 dims)
```

### 7. Storage

- **Qdrant**: the vector, plus the memory text and metadata (text fields encrypted when
  `REMEMBRA_ENCRYPTION_KEY` is set)
- **SQLite**: the memory text and metadata, entities, relationships and the keyword index (not field-encrypted)

---

## The Recall Pipeline

When you call `memory.recall()`:

### 1. Query Embedding

```python
query_vector = embed("What do I know about John?")
```

### 2. Vector Search (Semantic)

Find memories with similar meaning:

```
Query: "What do I know about John?"
Match: "John was promoted to VP" (score: 0.89)
Match: "John works at Google" (score: 0.85)
```

### 3. Keyword Search (BM25)

Find exact keyword matches:

```
Query contains: "John"
Match: All memories mentioning "John"
```

### 4. Hybrid Fusion

Combine semantic + keyword scores:

```
final_score = (1 - α) × semantic + α × keyword
```

Default α = 0.4 (40% keyword, 60% semantic)

### 5. Graph Expansion

If enabled, expand via entity graph:

```
Query mentions: "John"
Graph finds: John → WORKS_AT → Google
Expand to: Also include Google-related memories
```

### 6. Relevance Ranking

Multi-signal scoring:

```
score = semantic_weight × semantic_score
      + recency_weight × recency_boost
      + entity_weight × entity_match
      + keyword_weight × keyword_score
```

### 7. CrossEncoder Reranking (Optional)

With the optional `rerank` extra installed (`pip install "remembra[rerank]"`; the default Docker image does not
include it, Remembra Cloud does), the top candidates are reranked:

```python
# Before: Ranked by embedding similarity
# After: Ranked by query-memory relevance (more accurate)
```

### 8. Context Optimization

Fit results into LLM context window:

```
max_tokens = 4000
# Truncate at sentence boundaries
# Prioritize high-scoring memories
```

### 9. Return

```python
return context  # Ready for LLM injection
```

---

## Storage Architecture

### Qdrant (Vectors)

- Memory embeddings
- Optimized for semantic search
- Horizontal scaling support

### SQLite (Everything Else)

- Memory text and metadata (id, created_at, user_id, project)
- Entity graph (nodes, edges, aliases)
- Relationships (typed connections)
- Full-text search index (FTS5)
- Audit logs
- API keys

### Why This Split?

| Qdrant | SQLite |
|--------|--------|
| Optimized for ANN search | Simple, embedded, portable |
| Handles high-dimensional vectors | Handles relational queries |
| Requires separate service | Bundled in app |

In Docker, Qdrant runs as a separate container. SQLite is a file in the data volume.

---

## Configuration Impact

### Extraction Quality

```bash
# Model choice affects extraction accuracy
REMEMBRA_EXTRACTION_MODEL=gpt-4o-mini  # Fast, cheap
REMEMBRA_EXTRACTION_MODEL=gpt-4o       # Best quality
```

### Retrieval Accuracy

```bash
# Hybrid search improves recall
REMEMBRA_ENABLE_HYBRID_SEARCH=true

# Reranking improves precision (needs the rerank extra)
REMEMBRA_ENABLE_RERANKING=true
```

### Performance

```bash
# Lower token limit = faster but less context
REMEMBRA_CONTEXT_MAX_TOKENS=2000

# Shallower graph = faster but less expansion
REMEMBRA_GRAPH_MAX_DEPTH=1
```

---

## Comparison to Alternatives

Mem0 and Zep figures are from their own docs and pricing pages as read in September 2026. Check them before
you rely on them.

| Feature | Remembra | Mem0 | Zep |
|---------|----------|------|-----|
| Graph storage | SQLite (a Neo4j backend is planned, not supported) | Graph memory (entity linking) on the $249 Pro plan | Yes |
| Hybrid search | Semantic + keyword (BM25) | Semantic + BM25 + entity | Yes |
| Reranking | CrossEncoder, with the optional `rerank` extra | Pluggable rerankers | Cross-encoder and graph-distance reranking (Graphiti) |
| Pricing | Self-host free (MIT); Cloud Free, $12 and $29 a month | Free, $19 and $249 a month | Free tier, then $125 or $375 a month; Enterprise custom |

Remembra also has TTLs, decay scores and `as_of` queries, and `quickstart.sh` starts a self-hosted server with one
command.
