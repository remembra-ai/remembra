# Changelog

See [CHANGELOG.md](https://github.com/remembra-ai/remembra/blob/main/CHANGELOG.md) for the full version history.

## Latest: v0.8.0 (March 2026)

### Added
- **One-Command Quick Start** — `curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash` zero-config setup with Ollama embeddings
- **Multi-Provider Entity Extraction** — OpenAI, Anthropic Claude, and Ollama support for entity extraction
- **Usage Warning Banners** — API responses include usage thresholds at 60/80/95% to drive Pro upgrades
- **Docker Compose Quickstart** — Zero-config compose with Qdrant + Ollama + Remembra in one file
- **125 New Tests** — Comprehensive test coverage for embeddings, entities, conflicts, spaces, and plugins

### Changed
- **httpx Connection Reuse** — Persistent connections across all 6 embedding providers for reduced latency
- **App Lifespan Cleanup** — Proper resource management during application startup and shutdown

### Fixed
- **Connection Churn** — Fixed 13 locations creating per-request httpx clients, now using shared persistent connections

## v0.7.2 (March 2026)

### Fixed
- **Security: CORS Configuration** — Removed `allow_origins=["*"]`, now configurable via `REMEMBRA_CORS_ORIGINS`
- **API: PATCH /memories/{id}** — Full implementation (was returning 501)
- **API: Batch Operations** — `/store/batch` and `/recall/batch` now functional
- **Streaming: SSE Endpoint** — `/ingest/stream` for conversation ingestion
- **Observability: OpenTelemetry** — Tracing module fully implemented
- **Production: CORS Origins** — Added `app.remembra.dev` and `remembra.dev` to allowed origins

### Changed
- Stub endpoints now return 503 Service Unavailable with helpful messages (was 501)
- Improved error messages throughout API

## v0.7.1 (March 2026)

### Fixed
- Minor bug fixes and stability improvements

## v0.7.0 (March 2026)

### Added
- **Enterprise Features**
  - Webhook System with HMAC-SHA256 signing and automatic retry
  - RBAC (Role-Based Access Control) with 3 roles and 12 permissions
  - Memory Conflict Detection with configurable resolution strategies
  - Audit Logging with export to JSON/CSV

- **Import/Export System**
  - Import from ChatGPT, Claude, plain text, JSON, JSONL, CSV
  - Export to JSON, JSONL, CSV formats
  - Bulk import API with progress tracking

- **Cloud & Revenue**
  - Stripe Billing Integration (subscriptions, metering, portal)
  - Plan Limits with automatic enforcement
  - Spaces (Multi-tenancy) for organizations

- **Plugin System**
  - Extensible plugin architecture
  - Built-in: `auto_tagger`, `recall_logger`, `slack_notifier`

- **API Expansion**
  - 52 total API routes across 11 route groups
  - New endpoints: `/admin/*`, `/webhooks/*`, `/transfer/*`, `/conflicts/*`

## v0.6.3

### Added
- **Docker Support**
  - Production-ready `Dockerfile` with multi-stage build
  - `docker-compose.yml` for complete stack
  - Static file serving for dashboard UI
  - Health checks for container orchestration

## v0.6.2

### Added
- **Entity API Endpoints**
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

## v0.6.0

### Added
- Initial public release
- Core memory storage and recall
- Entity extraction and resolution
- Hybrid search (vector + keyword)
- Python SDK
- REST API
- Dashboard UI
