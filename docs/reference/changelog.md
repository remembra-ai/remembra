# Changelog

See [CHANGELOG.md](https://github.com/remembra/remembra/blob/main/CHANGELOG.md) for the full version history.

## Latest: v0.6.3 (March 2026)

### Added
- **Docker Support** (Week 11)
  - Production-ready `Dockerfile` with multi-stage build
  - `docker-compose.yml` for complete stack
  - Static file serving for dashboard UI
  - Health checks for container orchestration

## v0.6.2

### Added
- **Entity API Endpoints** (Week 10)
  - List entities, get relationships, get entity memories
- **Dashboard Improvements**
  - Entity graph visualization
  - Memory editing support

## v0.6.1

### Added
- **Temporal API Endpoints**
  - Decay reports, cleanup jobs, single memory decay info
- **Decay Module**
  - Ebbinghaus forgetting curve implementation
- **TTL Module**
  - Parse TTL strings, presets

## v0.6.0

### Added
- **Temporal Features (Week 8)**
  - TTL (Time-to-Live) with `memory.store(..., ttl="30d")`
  - Memory decay algorithm
  - Historical queries with `as_of`
- **Changelog Ingestion**
  - Auto-import project changelogs as memories

### Fixed
- FK constraint bug in consolidation

## v0.5.0

### Added
- **API Key Authentication**
- **Rate Limiting**
- **Memory Protection Layer** (MINJA defense)
- **Audit Logging**

## v0.4.0

### Added
- **Hybrid Search** (semantic + BM25)
- **CrossEncoder Reranking**
- **Graph-Aware Retrieval**
- **Context Window Optimization**
- **Advanced Relevance Ranking**

## v0.3.0

### Added
- **Entity Extraction**
- **Entity Matching/Resolution**
- **Relationship Storage**
- **Memory-Entity Links**

## v0.2.0

### Added
- **LLM-powered fact extraction**
- **Memory consolidation** (ADD/UPDATE/DELETE/NOOP)
- **Smart merging**

## v0.1.0

### Added
- Initial release
- Python SDK with `Memory` client
- REST API with FastAPI
- `store()`, `recall()`, `forget()`
- Qdrant + SQLite storage
- OpenAI/Ollama/Cohere embeddings
