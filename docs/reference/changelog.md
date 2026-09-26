# Changelog

See [CHANGELOG.md](https://github.com/remembra-ai/remembra/blob/main/CHANGELOG.md) for the full version history.

## Latest: v0.16.0, Remembra Relay

When an agent's session ends, `remembra-relay close` saves a handoff built from git facts (and, for Claude Code,
the session's test runs), and checks the agent's own summary against them. The next agent, in any tool or on any
machine, starts from a short brief; every handoff stays on the trail. Agent-scoped keys mark handoffs
key-verified. Claude Code's and Codex's hooks are verified (Codex with codex-cli 0.155.0-alpha.16.4, a prerelease);
Cursor, Gemini CLI, Qwen Code and Kimi hooks ship unverified. Get a free key at
[app.remembra.dev](https://app.remembra.dev/signup), then run (`remembra-install` asks for it at a hidden prompt):

```bash
pipx install --force 'remembra[mcp]>=0.16'
remembra-install --all
remembra-relay connect --apply
```

Setup: [Relay guide](../guides/relay.md). The short version of every release: [remembra.dev/changelog](https://remembra.dev/changelog).
Releases v0.11 to v0.15 are in [CHANGELOG.md](https://github.com/remembra-ai/remembra/blob/main/CHANGELOG.md).

---

## v0.10.1 (March 15, 2026)

### Added
- **Centralized Credentials** — API keys stored securely in `~/.remembra/credentials` (chmod 600). No more repeating `--api-key` on every command.
- **Slim Recall Mode** — `recall(query, slim=True)` returns 90% smaller payloads for token-constrained agents.
- **Bridge Lifecycle Management** — `remembra-bridge --stop` and `--status` commands for better control.

### Fixed
- Version sync between pyproject.toml and `__version__`

---

## v0.10.0 (March 15, 2026)

### Added
- **Universal Agent Installer** — `remembra-install --all` auto-detects and configures Claude, Codex, Cursor, Gemini, Windsurf
- **Setup Diagnostics** — `remembra-doctor <agent>` diagnoses connection issues with clear failure labels
- **Local Bridge** — `remembra-bridge` tunnels sandboxed agents to your Remembra server
- **Security Hardening** — RBAC enforcement, error sanitization, SSRF protection

### Changed
- MCP server now supports more agents out of the box
- Improved error messages for common setup issues

---

## v0.9.0 (March 2026)

### Added
- **Temporal Knowledge Graph** — Bi-temporal relationship model with `valid_from`, `valid_to`, and `superseded_by`. Enables point-in-time queries like "Where did Alice work in January 2022?"
- **6 New MCP Tools** — `update_memory`, `search_entities`, `list_memories`, `share_memory`, `timeline`, `relationships_at` (5 → 11 tools total)
- **Entity Graph Visualization** — Interactive force-directed graph with flowing particle effects on relationship edges
- **Contradiction Detection** — New relationships automatically supersede old ones with full history preserved
- **SDK Client Methods** — `memory.update()` and `memory.list_entities()` in Python SDK

### Changed
- MCP server instructions updated to reflect 11 available tools
- Entity graph retrieval now supports temporal filtering

---

## v0.8.4 (March 2026)

### Fixed
- **Maintainer Name** — Corrected maintainer name to "Damany Dolphy" in all metadata
- **README Cleanup** — Removed unprofessional language from documentation

### Added
- **Pre-commit Hook** — Automated check to block unprofessional content before commits

---

## v0.8.3 (March 2026)

### Changed
- **Accurate Messaging** — Updated all marketing copy to be accurate and not overpromise
- **NPM SDK Sync** — TypeScript SDK synced to v0.8.3
- **Documentation** — Replaced "5 minutes" claims with accurate "minutes" throughout

---

## v0.8.2 (March 2026)

### Added
- **Security Features Release** — Production hardening with AES-256-GCM encryption
- **16 New Encryption Tests** — 272 total tests now passing

### Changed
- **Documentation** — Updated all version references to v0.8.2

---

## v0.8.1 (March 2026)

### Added
- **Encryption at Rest (AES-256-GCM)** — Field-level encryption for memory content and metadata with PBKDF2 key derivation (480K iterations). Set `REMEMBRA_ENCRYPTION_KEY` to enable. Backwards-compatible with unencrypted data.
- **SECURITY.md** — Comprehensive security policy with architecture overview, compliance roadmap (SOC 2, HIPAA), and self-hosted hardening checklist

### Changed
- `cryptography` added as optional dependency (`pip install "remembra[encryption]"`)
- Qdrant store now transparently encrypts/decrypts content payloads

---

## v0.8.0 (March 2026)

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
