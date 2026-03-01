# Changelog

All notable changes to Remembra will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-03-01

### Added
- **Hybrid Search** - Combines semantic (vector) and keyword (BM25) matching
  - BM25 keyword index for exact term matching
  - Score fusion with configurable weights (linear or RRF)
  - Test: "David Kim merger" now finds "Mr. Kim mentioned acquisition"
- **Graph-Aware Retrieval** - Uses entity relationships for smarter recall
  - Traverses entity graph to find related memories
  - Alias matching ("Mr. Kim" → "David Kim")
  - Configurable traversal depth (default: 2 hops)
- **Context Window Optimization** - Smart truncation for LLM context limits
  - Token-aware truncation to fit context windows
  - Relevance-based prioritization
  - `max_tokens` parameter on `recall()` endpoint
- **Advanced Relevance Ranking** - Multi-signal scoring
  - Recency boost (newer memories score higher)
  - Entity match boost (entities in query)
  - Keyword match boost (from BM25)
  - Configurable weights via environment variables
- New `retrieval/` module with:
  - `hybrid.py` - BM25Index, HybridSearcher
  - `graph.py` - GraphRetriever for entity traversal
  - `context.py` - ContextOptimizer for LLM output
  - `ranking.py` - RelevanceRanker with configurable boosts
- Comprehensive tests for all retrieval features

### Configuration
- `REMEMBRA_ENABLE_HYBRID_SEARCH` - Toggle hybrid search (default: true)
- `REMEMBRA_HYBRID_SEMANTIC_WEIGHT` - Semantic weight in fusion (default: 0.7)
- `REMEMBRA_HYBRID_KEYWORD_WEIGHT` - Keyword weight in fusion (default: 0.3)
- `REMEMBRA_ENABLE_GRAPH_RETRIEVAL` - Toggle graph traversal (default: true)
- `REMEMBRA_GRAPH_MAX_DEPTH` - Entity graph depth (default: 2)
- `REMEMBRA_CONTEXT_MAX_TOKENS` - Max context tokens (default: 4000)
- `REMEMBRA_RANKING_SEMANTIC_WEIGHT` - Ranking semantic weight (default: 0.6)
- `REMEMBRA_RANKING_RECENCY_WEIGHT` - Ranking recency weight (default: 0.15)
- `REMEMBRA_RANKING_ENTITY_WEIGHT` - Ranking entity weight (default: 0.15)
- `REMEMBRA_RANKING_KEYWORD_WEIGHT` - Ranking keyword weight (default: 0.1)
- `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` - Recency half-life (default: 30)

### Changed
- `recall()` now uses advanced retrieval pipeline by default
- Improved relevance scoring considers multiple signals
- Context output optimized for LLM consumption

## [0.3.0] - 2026-03-01

### Added
- **Entity Extraction** - LLM extracts PERSON, ORG, LOCATION entities from memories
- **Entity Matching** - Resolves aliases ("Mr. Kim" → "David Kim", "NYC" → "New York City")
- **Alias Management** - Automatic alias tracking and resolution
- **Relationship Storage** - Stores entity relationships (WORKS_AT, SPOUSE_OF, KNOWS, etc.)
- **Memory-Entity Links** - Bidirectional links between memories and entities
- **Entity-Aware Recall** - Find memories via entity graph traversal
- New `entities.py` module for entity extraction
- New `matcher.py` module for entity resolution
- Entity resolution documentation

### Changed
- Memory storage now extracts and links entities automatically
- Recall considers entity relationships for improved relevance

## [0.2.0] - 2026-03-01

### Added
- **LLM-powered fact extraction** - Transforms messy text into clean atomic facts
- **Memory consolidation** - ADD/UPDATE/DELETE/NOOP logic prevents duplicates
- **Smart merging** - Updates preserve history (e.g., "VP of Sales (promoted from Director)")
- New extraction module with configurable LLM backend
- New consolidation module for memory conflict resolution

### Changed
- `store()` now uses intelligent extraction by default
- Improved recall relevance with semantic understanding
- Default threshold lowered to 0.40 for better recall

### Configuration
- `REMEMBRA_SMART_EXTRACTION_ENABLED` - Toggle LLM extraction (default: true)
- `REMEMBRA_EXTRACTION_MODEL` - Model for extraction (default: gpt-4o-mini)
- `REMEMBRA_CONSOLIDATION_THRESHOLD` - Similarity threshold for consolidation

## [0.1.0] - 2026-03-01

### Added
- Initial release of Remembra
- Python SDK with `Memory` client class
- REST API with FastAPI
- `store()` - Store memories with automatic fact extraction
- `recall()` - Semantic search across memories
- `forget()` - GDPR-compliant deletion
- Qdrant vector store integration
- SQLite metadata storage
- Embedding support for OpenAI, Ollama, and Cohere
- Docker and docker-compose setup
- Comprehensive test suite

### Notes
- This is an alpha release - API may change
- Entity resolution coming in v0.2.0
- LLM-powered extraction coming in v0.2.0
