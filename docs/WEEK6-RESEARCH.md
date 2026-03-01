# Week 6 Research: Advanced Retrieval

## Executive Summary

Based on industry research (March 2026), here's what top memory/RAG systems are doing:

---

## 1. Hybrid Search (Semantic + Keyword)

### The Standard Approach
- **BM25** for sparse/keyword matching + **Vector embeddings** for semantic
- Three-way retrieval (dense + sparse + full text) is optimal per InfinityFlow study
- Stack Overflow migrated to this approach with significant improvements

### Why Hybrid?
| Scenario | Vector Search | Keyword Search | Winner |
|----------|--------------|----------------|--------|
| Typos in query | ✅ Handles well | ❌ Fails | Vector |
| Names/abbreviations ("GAN", "Biden") | ❌ Lost in embeddings | ✅ Exact match | Keyword |
| Semantic relationships | ✅ Understands meaning | ❌ Literal only | Vector |
| Code snippets | ❌ Useless | ✅ Exact match | Keyword |

### Implementation Pattern
```python
# 1. Run both searches
sparse_results = bm25_search(query)  # keyword
dense_results = vector_search(query)  # semantic

# 2. Normalize scores (min-max scaling)
sparse_normalized = min_max_scale(sparse_results)
dense_normalized = min_max_scale(dense_results)

# 3. Combine with weights
final_score = (alpha * sparse_normalized) + ((1-alpha) * dense_normalized)
# alpha = 0.3-0.5 typical, tunable per use case
```

### BM25 Libraries for Python
- `rank_bm25` - Pure Python, simple
- `bm25s` - Fast, NumPy-based
- SQLite FTS5 - Built-in, we already use SQLite

**Recommendation:** Use SQLite FTS5 since we already have SQLite. Zero new dependencies.

---

## 2. Graph-Aware Retrieval

### Industry Leaders

**Zep (Graphiti)**
- Temporally-aware knowledge graph
- Three-tier architecture:
  1. Episode subgraph (conversation chunks)
  2. Semantic entity subgraph (people, places, things)
  3. Community subgraph (clusters of related entities)
- Incremental updates, no batch recomputation

**GraphRAG (Microsoft)**
- Explicit knowledge graph in retrieval
- Entity extraction → relationship mapping → graph traversal
- Community detection for summarization

### Our Approach (We Already Have Entities!)
Since v0.3.0 has entity extraction + relationships, we need:

1. **Entity-first retrieval**: Query → extract entities → find all memories linked to those entities
2. **Relationship traversal**: If asking about "David Kim", also surface memories about his company, colleagues
3. **Alias resolution**: "Mr. Kim" query finds "David Kim" memories (already done in v0.3.0)

```python
def graph_aware_recall(query):
    # 1. Extract entities from query
    query_entities = extract_entities(query)
    
    # 2. Expand via relationships
    related_entities = get_related_entities(query_entities, depth=1)
    
    # 3. Get memories linked to all entities
    entity_memories = get_memories_by_entities(query_entities + related_entities)
    
    # 4. Combine with semantic search
    semantic_memories = vector_search(query)
    
    # 5. Merge and rank
    return merge_results(entity_memories, semantic_memories)
```

---

## 3. Reranking

### Why Rerank?
- Cross-encoders examine full query-document pairs (deeper understanding)
- **35% reduction in hallucinations** (Databricks study)
- +28% NDCG@10 improvements over baseline

### Options
| Model | Type | Speed | Accuracy | Cost |
|-------|------|-------|----------|------|
| Cohere Rerank 3 | API | Fast | Excellent | $$ |
| Cohere Rerank 3 Nimble | API | Very Fast | Good | $ |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | Local | Medium | Good | Free |
| LLM-based (GPT-4/Claude) | API | Slow | Excellent | $$$ |

**Recommendation:** Start with local cross-encoder for self-hosted, add Cohere as optional premium.

### Implementation
```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')

def rerank(query, documents, top_k=5):
    pairs = [[query, doc['content']] for doc in documents]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(scores, documents), reverse=True)
    return [doc for _, doc in ranked[:top_k]]
```

---

## 4. Context Window Optimization

### Problem
LLMs have token limits. Need to fit best memories in available space.

### Strategies
1. **Chunking**: Store memories in smaller chunks, retrieve most relevant
2. **Summarization**: Compress old memories into summaries
3. **Relevance cutoff**: Only return above threshold
4. **Token budget**: `max_tokens` parameter, fill until budget exhausted

### Implementation
```python
def recall(query, max_tokens=4000):
    results = hybrid_search(query)
    results = rerank(query, results)
    
    selected = []
    token_count = 0
    for result in results:
        tokens = count_tokens(result['content'])
        if token_count + tokens <= max_tokens:
            selected.append(result)
            token_count += tokens
        else:
            break
    return selected
```

---

## 5. Implementation Plan

### Priority Order
1. **Hybrid Search** (biggest accuracy win)
   - Add FTS5 index to SQLite
   - Implement BM25 search
   - Score normalization + blending
   
2. **Graph-Aware Retrieval** (leverage v0.3.0 work)
   - Entity extraction from queries
   - Relationship traversal
   - Memory-entity join queries

3. **Reranking** (polish)
   - Add cross-encoder dependency
   - Optional rerank step
   - Configurable model

4. **Context Optimization** (QoL)
   - max_tokens parameter
   - Token counting
   - Smart truncation

### Config Variables
```
REMEMBRA_HYBRID_SEARCH_ENABLED=true
REMEMBRA_HYBRID_ALPHA=0.4  # keyword weight
REMEMBRA_RERANK_ENABLED=true
REMEMBRA_RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
REMEMBRA_DEFAULT_MAX_TOKENS=4000
```

---

## References
- [Elastic Hybrid Search Guide](https://www.elastic.co/what-is/hybrid-search)
- [Superlinked: Optimizing RAG with Hybrid Search](https://superlinked.com/vectorhub/articles/optimizing-rag-with-hybrid-search-reranking)
- [Zep: Knowledge Graph Memory](https://blog.getzep.com/graphiti/)
- [InfinityFlow: Best Hybrid Search Solution](https://infiniflow.org/blog/best-hybrid-search-solution)
- [ZeroEntropy: Reranking Guide 2026](https://www.zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025)
