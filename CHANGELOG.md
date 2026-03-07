# Changelog

All notable changes to Remembra will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-03-07

### Added
- **One-Command Quick Start** — `curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash` sets up Remembra + Qdrant + Ollama with zero API keys required
- **Multi-Provider Entity Extraction** — Entity extraction now works with Anthropic Claude and Ollama, not just OpenAI. New `create_entity_extractor()` factory dispatches based on `REMEMBRA_LLM_PROVIDER`
- **Usage Warning Banners** — API responses include usage percentage headers (`X-Remembra-Usage-Percent`, `X-Remembra-Plan`) and `usage_warning` field at 60/80/95% thresholds
- **Docker Compose Quickstart** — New `docker-compose.quickstart.yml` with 3 services (Qdrant, Ollama, Remembra), health checks, zero config
- **125 New Tests** — Test coverage for embeddings (6 providers), entity extraction (3 providers), conflict resolution, memory spaces (RBAC), and plugin system (pipeline dispatch)
- **Shared Test Fixtures** — New `tests/conftest.py` with reusable fixtures for all test files

### Changed
- **httpx Connection Reuse** — All 6 embedding providers, webhook delivery, Python SDK client, and MCP server now use persistent HTTP clients. Reduces latency by 100-300ms per operation
- **MCP Server Ingestion** — `ingest_conversation` refactored to use SDK's `Memory.ingest_conversation()`
- **Python SDK** — `Memory` client now supports context manager and has explicit `close()` method
- **App Lifespan Cleanup** — Proper shutdown of all persistent HTTP clients on server stop

### Fixed
- **Connection Churn** — Eliminated 13 locations creating new TCP+TLS connections per request

## [0.7.2] - 2026-03-06

### Fixed
- **Dashboard: EntityGraph Performance** — Changed from N+1 API calls to single `/debug/entities/graph` endpoint
- **Dashboard: Error Display** — Fixed `[object Object]` showing instead of actual error messages
- **Dashboard: TypeScript** — Resolved strict mode compilation errors
- **API: Project Filtering** — Fixed recall defaulting to wrong project_id

### Added
- **Admin: rebuild-vectors endpoint** — `POST /admin/rebuild-vectors` to fix memories missing from Qdrant
- **Docs: Troubleshooting Guide** — Comprehensive diagnosis and fix guide for common issues
- **Docs: Setup Checklist** — 10-step verification checklist for self-hosters

## [0.7.1] - 2026-03-03

### Fixed
- **Security: CORS Configuration** — Removed `allow_origins=["*"]`, now configurable via `REMEMBRA_CORS_ORIGINS`
- **API: PATCH /memories/{id}** — Full implementation (was returning 501)
- **API: Batch Operations** — `/store/batch` and `/recall/batch` now functional
- **Streaming: SSE Endpoint** — `/ingest/stream` for conversation ingestion
- **Observability: OpenTelemetry** — Tracing module fully implemented
- **Production: CORS Origins** — Added `app.remembra.dev` and `remembra.dev` to allowed origins
- **Stripe: Environment Variables** — Accept both prefixed and non-prefixed Stripe env vars

### Changed
- Stub endpoints now return 503 Service Unavailable with helpful messages (was 501)
- Improved error messages throughout API

### Documentation
- Added QA Remediation Results report
- Updated MCP Server documentation for v0.7.0
- Added feature comparison chart
- Added Discord and Twitter links

## [0.7.0] - 2026-03-02

### Added
- **Enterprise Features**
  - **Webhook System** - Event-driven integrations
    - HMAC-SHA256 request signing for security
    - Automatic retry delivery with exponential backoff
    - Events: `memory.created`, `memory.updated`, `memory.deleted`, `entity.created`
    - Webhook management API: create, list, delete, test
  - **RBAC (Role-Based Access Control)**
    - Three roles: `admin`, `editor`, `viewer`
    - 12 granular permissions across memories, entities, webhooks, admin
    - Scoped API keys with role assignment
    - Permission middleware for all protected routes
  - **Memory Conflict Detection**
    - Detect contradictions in stored memories
    - Configurable strategies: `update`, `version`, `flag`
    - Conflict resolution API endpoints
  - **Audit Logging**
    - Complete audit trail of all operations
    - Export to JSON or CSV format
    - Role-protected admin endpoints

- **Import/Export System**
  - **Import from**:
    - ChatGPT conversation exports
    - Claude conversation exports
    - Plain text files
    - JSON, JSONL, CSV formats
  - **Export to**:
    - JSON (full fidelity)
    - JSONL (streaming-friendly)
    - CSV (spreadsheet-compatible)
  - Bulk import API with progress tracking

- **Cloud & Revenue (Phase 2)**
  - **Stripe Billing Integration**
    - Subscription management
    - Usage-based metering
    - Customer portal integration
    - Webhook handlers for billing events
  - **Plan Limits**
    - Configurable limits per plan (memories, API calls, storage)
    - Automatic enforcement with graceful degradation
    - Usage dashboards and alerts
  - **Spaces (Multi-tenancy)**
    - Isolated memory spaces per organization
    - Space-level settings and quotas
    - Cross-space queries for admins

- **Plugin System**
  - Extensible plugin architecture
  - Built-in plugins:
    - `auto_tagger` - Automatic memory tagging
    - `recall_logger` - Query analytics
    - `slack_notifier` - Slack integration for events
  - Custom plugin development guide

- **API Expansion**
  - 52 total API routes across 11 route groups
  - New endpoints: `/admin/*`, `/webhooks/*`, `/transfer/*`, `/conflicts/*`
  - OpenAPI schema updated

### Changed
- Embeddings API refactored for multi-provider support
- Memory service expanded with conflict detection
- Config updated with cloud/billing settings

### Fixed
- TypeScript type reference in dashboard api.ts

## [0.6.3] - 2026-03-01

### Added
- **Docker Support** (Week 11)
  - Production-ready `Dockerfile` with multi-stage build
  - `docker-compose.yml` for complete stack (API + Qdrant)
  - `.env.example` with all configuration options
  - `DOCKER.md` deployment guide
  - Static file serving for dashboard UI
  - Health checks for container orchestration
- **Configuration**
  - `REMEMBRA_STATIC_DIR` for serving dashboard

### Changed
- Dashboard UI now served by API server when `static_dir` is set

## [0.6.2] - 2026-03-01

### Added
- **Entity API Endpoints** (Week 10)
  - `GET /api/v1/entities` - List all entities with type counts
  - `GET /api/v1/entities/{id}` - Get entity by ID
  - `GET /api/v1/entities/{id}/relationships` - Get entity relationships
  - `GET /api/v1/entities/{id}/memories` - Get memories linked to entity
- **Dashboard Improvements**
  - Entity graph visualization (force-directed layout)
  - Memory editing support
  - Graph tab with interactive canvas
  - Entity detail modal with relationships and memories

### Fixed
- TypeScript strict mode compatibility in dashboard components

## [0.6.1] - 2026-03-01

### Added
- **Temporal API Endpoints** - REST API for decay management
  - `GET /api/v1/temporal/decay/report` - View memory health and decay scores
  - `POST /api/v1/temporal/cleanup` - Run cleanup with dry-run support
  - `GET /api/v1/temporal/memory/{id}/decay` - Single memory decay info
- **Decay Module** (`remembra.temporal.decay`)
  - Ebbinghaus forgetting curve implementation
  - Configurable decay parameters (DecayConfig)
  - `calculate_relevance_score()`, `should_prune()` functions
- **TTL Module** (`remembra.temporal.ttl`)
  - Parse TTL strings: "30d", "1y", "2w", "24h"
  - TTL presets: session, conversation, short_term, long_term, permanent
- **Cleanup Job** (`remembra.temporal.cleanup`)
  - Background cleanup for expired/decayed memories
  - Archive mode (soft delete) vs hard delete

### Fixed
- Temporal module properly exported from package

## [0.6.0] - 2026-03-01

### Added
- **Temporal Features (Week 8)** - Time-aware memory operations
  - **TTL (Time-to-Live)** - Memories can now expire automatically
    - Set TTL on store: `memory.store("...", ttl="30d")` (supports d/w/m/y)
    - `cleanup_expired()` method and `/cleanup-expired` endpoint
    - Server-side default TTL configurable via `REMEMBRA_DEFAULT_TTL_DAYS`
  
  - **Memory Decay Algorithm** - Older/unused memories rank lower
    - Exponential time decay with configurable half-life
    - Access count boost (frequently accessed = higher score)
    - Recency of access boost (recently accessed = higher score)
    - `get_memories_with_decay()` for decay score visibility
  
  - **Historical Queries (as_of)** - Time-travel memory recall
    - `recall_as_of()` method to see memories at a point in time
    - Useful for auditing, debugging, historical analysis
    - Respects both creation time and expiration time
  
- **Changelog Ingestion** - Auto-import project history
  - New endpoint: `POST /api/v1/ingest/changelog`
  - Parses Keep a Changelog format (and similar markdown formats)
  - Each release becomes a searchable memory with version/date metadata
  - SDK method: `memory.ingest_changelog(content_or_path, project_name="...")`
  - Supports both raw content and file path input
  
- Database temporal query methods:
  - `get_expired_memories()` - Find memories past their TTL
  - `get_memories_as_of()` - Query historical memory state
  - `get_memories_with_decay_info()` - Get access/decay metadata
  - `cleanup_expired_memories()` - Batch delete expired memories
  - `migrate_memory_relationships()` - Preserve links during UPDATE

### Fixed
- **Critical: FK Constraint Bug in Consolidation** - Memory UPDATE/DELETE operations
  no longer fail with foreign key constraint errors. The fix:
  - Relationships are now properly migrated to new memory before old is deleted
  - `delete_memory()` cleans up relationships and entity links first
  - Entity links preserved during consolidation merges
  
- Fixed duplicate `max_tokens` field in `RecallRequest` model

### Changed
- Memory deletion now explicitly handles FK constraints (relationships, entity links)
- Consolidation UPDATE path migrates relationships to preserve entity graph integrity
- `RecallRequest` now includes `as_of` and `include_decay_score` parameters

### Configuration
- `REMEMBRA_DEFAULT_TTL_DAYS` - Default TTL for all memories (optional)

## [0.5.0] - 2026-03-01

### Added
- **API Key Authentication** - Secure access control for all memory operations
  - Generate API keys with `rem_` prefix and 256-bit entropy
  - Keys hashed with bcrypt before storage (never stored in plaintext)
  - Per-user memory isolation enforced via API key
  - Master key support for admin operations
  - Key management endpoints: POST/GET/DELETE /api/v1/keys
  
- **Rate Limiting** - Protection against abuse and DoS
  - Per-endpoint limits (store: 30/min, recall: 60/min, forget: 10/min)
  - Rate limit by API key (not just IP)
  - Uses `slowapi` with in-memory or Redis backend
  - Configurable via environment variables
  
- **Memory Protection Layer** - Defense against prompt injection (MINJA)
  - Input sanitization before storage
  - Trust scoring based on suspicious pattern detection
  - Patterns detected: instruction override, role manipulation, delimiter injection
  - SHA-256 checksums for integrity verification
  - Content provenance tracking (source, trust_score, checksum)
  
- **Audit Logging** - Security monitoring and compliance
  - Logs all memory operations (store, recall, forget)
  - Logs authentication events (key created, revoked, failed attempts)
  - Includes: timestamp, user_id, key_id, action, resource_id, IP, success
  - Never logs actual memory content or full API keys
  
- New `auth/` module with:
  - `keys.py` - API key generation, hashing, validation
  - `middleware.py` - FastAPI dependencies for authentication
  
- New `security/` module with:
  - `sanitizer.py` - Content sanitization and trust scoring
  - `audit.py` - Security audit logging
  
- Database schema updates:
  - `api_keys` table for key storage
  - `audit_log` table for security events
  - Memory provenance columns: source, trust_score, checksum

### Configuration
- `REMEMBRA_AUTH_ENABLED` - Enable API key authentication (default: true)
- `REMEMBRA_AUTH_MASTER_KEY` - Master key for admin operations
- `REMEMBRA_RATE_LIMIT_ENABLED` - Enable rate limiting (default: true)
- `REMEMBRA_RATE_LIMIT_STORAGE` - Rate limit backend: "memory" or "redis://..."
- `REMEMBRA_SANITIZATION_ENABLED` - Enable input sanitization (default: true)
- `REMEMBRA_TRUST_SCORE_THRESHOLD` - Suspicious content threshold (default: 0.5)

### Security
- OWASP API Security Top 10 addressed
- Defense-in-depth against memory injection attacks (MINJA - 95% success rate in research)
- Cross-user memory access blocked via API key scoping
- user_id in requests overridden by authenticated user (prevents spoofing)

### Dependencies
- Added `bcrypt>=4.0.0` for key hashing
- Added `slowapi>=0.1.9` for rate limiting

## [0.4.0] - 2026-03-01

### Added
- **Hybrid Search** - Combines semantic (vector) and keyword (BM25) matching
  - **SQLite FTS5** integration for persistent full-text indexing
  - In-memory BM25 fallback when FTS5 unavailable
  - Score normalization with min-max scaling
  - Configurable alpha weight for keyword/semantic balance
  - Reciprocal Rank Fusion (RRF) option for rank-based fusion
  
- **CrossEncoder Reranking** - Optional post-retrieval reranking (NEW)
  - Uses `sentence-transformers` CrossEncoder models
  - Reduces hallucinations by ~35% (per Databricks research)
  - Default model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (local, free)
  - Graceful degradation when model unavailable
  - Blends rerank scores with original scores
  
- **Graph-Aware Retrieval** - Uses entity relationships for smarter recall
  - Traverses entity graph to find related memories
  - Alias matching ("Mr. Kim" → "David Kim")
  - Configurable traversal depth (default: 2 hops)
  - Entity neighborhood expansion
  
- **Context Window Optimization** - Smart truncation for LLM context limits
  - **tiktoken integration** for accurate token counting (NEW)
  - Character-based fallback estimation
  - `max_tokens` parameter on `recall()` endpoint
  - Relevance-aware truncation at sentence boundaries
  
- **Advanced Relevance Ranking** - Multi-signal scoring
  - Recency boost (newer memories score higher)
  - Entity match boost (entities in query)
  - Keyword match boost (from BM25)
  - Diversity-aware reranking (MMR) to reduce redundancy
  - Configurable weights via environment variables
  
- New `retrieval/` module with:
  - `hybrid.py` - BM25Index, HybridSearcher
  - `graph.py` - GraphRetriever for entity traversal
  - `context.py` - ContextOptimizer with tiktoken
  - `ranking.py` - RelevanceRanker with configurable boosts
  - `reranker.py` - CrossEncoderReranker for quality improvement (NEW)
  
- FTS5 full-text search table in SQLite (`memories_fts`)
- Comprehensive tests for all retrieval features

### Configuration
- `REMEMBRA_HYBRID_SEARCH_ENABLED` - Toggle hybrid search (default: true)
- `REMEMBRA_HYBRID_ALPHA` - Keyword weight 0-1 (default: 0.4)
- `REMEMBRA_RERANK_ENABLED` - Toggle CrossEncoder reranking (default: false)
- `REMEMBRA_RERANK_MODEL` - CrossEncoder model name
- `REMEMBRA_DEFAULT_MAX_TOKENS` - Max context tokens (default: 4000)
- `REMEMBRA_GRAPH_RETRIEVAL_ENABLED` - Toggle graph traversal (default: true)
- `REMEMBRA_GRAPH_TRAVERSAL_DEPTH` - Entity graph depth (default: 2)
- `REMEMBRA_RANKING_SEMANTIC_WEIGHT` - Ranking semantic weight (default: 0.6)
- `REMEMBRA_RANKING_RECENCY_WEIGHT` - Ranking recency weight (default: 0.15)
- `REMEMBRA_RANKING_ENTITY_WEIGHT` - Ranking entity weight (default: 0.15)
- `REMEMBRA_RANKING_KEYWORD_WEIGHT` - Ranking keyword weight (default: 0.1)
- `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` - Recency half-life (default: 30)

### Changed
- `recall()` now uses advanced retrieval pipeline by default
- `RecallRequest` accepts `max_tokens`, `enable_hybrid`, `enable_rerank` params
- `store()` now indexes memories in FTS5 for keyword search
- Improved relevance scoring considers multiple signals
- Context output optimized for LLM consumption

### Dependencies
- Added `tiktoken>=0.7.0` to server extras
- Added `sentence-transformers>=2.5.0` as optional `rerank` extra

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

## [0.4.1] - 2026-03-01

### Fixed
- API recall endpoint signature (removed duplicate max_tokens argument)
- Hybrid search fallback path (correct method signature for fusion)
- Test compatibility with HybridSearchConfig API

### Added
- RELEASE-CHECKLIST.md - mandatory pre-deploy verification
