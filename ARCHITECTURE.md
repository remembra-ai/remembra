# Remembra - Technical Architecture

## Competitive Research (March 2026)

### Mem0 Analysis (Main Competitor)
Based on their arxiv paper (2504.19413) and production system:

| Metric | Mem0 vs Alternatives |
|--------|---------------------|
| Accuracy | **+26%** vs OpenAI Memory (LOCOMO benchmark) |
| Latency | **91% faster** than full-context (p95) |
| Token Cost | **90% cheaper** than full-context |
| Graph Memory | **+2%** accuracy boost over base |

**Their Architecture:**
1. **Multi-Level Memory** - User, Session, Agent state
2. **Graph Memory** - Entity relationships via graph DB
3. **Rerankers** - Post-retrieval relevance scoring
4. **Dynamic Extraction** - LLM extracts salient info automatically
5. **Memory Consolidation** - Deduplication and merging

**Query Types (LOCOMO Benchmark):**
- Single-hop (direct recall)
- Temporal (time-based queries)
- Multi-hop (reasoning across memories)
- Open-domain (general knowledge)

**Their Weaknesses (Our Opportunity):**
- Self-hosting docs are poor
- Pricing jumps from $19 → $249 (no middle tier)
- Enterprise-first, not developer-first
- Complex deployment requirements

### Our Differentiators
1. **Self-host with one command** - `quickstart.sh` starts Remembra, Qdrant and Ollama with Docker Compose
2. **Fair pricing** - Free → Solo $12 → Pro $29 → Team $15/seat (not $19 → $249); relay is free on every plan, AI enrichment is metered in smart credits
3. **Developer-first docs** - Actually usable
4. **MIT license** - True open source
5. **Lightweight** - one API container with SQLite, plus Qdrant (no minimum server size has been measured)

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ Python SDK  │  │   JS SDK    │  │      REST API           │  │
│  │ pip install │  │ npm install │  │   /api/v1/*             │  │
│  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
└─────────┼────────────────┼─────────────────────┼────────────────┘
          │                │                     │
          ▼                ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                        API LAYER (FastAPI)                       │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Authentication  │  Rate Limiting  │  Request Logging       ││
│  └─────────────────────────────────────────────────────────────┘│
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  /store  │  /recall  │  /update  │  /forget  │  /search     ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    INTELLIGENCE LAYER                            │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │   Extraction    │  │ Entity Resolution│  │   Retrieval     │  │
│  │   Engine        │  │    Engine        │  │   Engine        │  │
│  │                 │  │                  │  │                 │  │
│  │ • LLM parsing   │  │ • Extract entities│ │ • Semantic search│ │
│  │ • Fact extract  │  │ • Match/link     │  │ • Graph traversal│ │
│  │ • Categorize    │  │ • Confidence     │  │ • Reranking ★   │  │
│  └────────┬────────┘  └────────┬─────────┘  └────────┬────────┘  │
└───────────┼────────────────────┼─────────────────────┼──────────┘
            │                    │                     │
            ▼                    ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                      STORAGE LAYER                               │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │   VECTOR STORE  │  │   GRAPH STORE   │  │ RELATIONAL STORE│  │
│  │     (Qdrant)    │  │    (SQLite)     │  │   (SQLite)      │  │
│  │                 │  │                 │  │                 │  │
│  │ • Embeddings    │  │ • Entities      │  │ • Users         │  │
│  │ • Semantic idx  │  │ • Relationships │  │ • Projects      │  │
│  │ • Similarity    │  │ • Graph queries │  │ • Metadata      │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     EMBEDDING LAYER                              │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  OpenAI         │  Cohere Embed    │  Ollama (Local)        ││
│  │  text-embed-3   │  (Alternative)   │  nomic-embed-text      ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Status

> **CHANGELOG.md is the ground truth for what's shipped** — this table is the
> high-level map (updated 2026-09-27, after v0.16.1).

| Area | Where | Status |
|------|-------|--------|
| Core store/recall (hybrid vector+BM25+graph+rerank+decay) | `services/memory.py`, `retrieval/` | ✅ Shipped |
| **Lossless memory** (verbatim source records + receipts + fact verification) | `services/memory.py` | ✅ Shipped (0.16.0) |
| LLM extraction + consolidation | `extraction/` | ✅ Shipped |
| Entity resolution + bitemporal graph | `extraction/`, `retrieval/graph.py` | ✅ Shipped |
| Brain layer (GraphRAG communities, dependency-free Louvain) | `brain/` | ✅ Shipped |
| Temporal: TTL, Ebbinghaus decay, archive, as-of queries | `temporal/` | ✅ Shipped |
| MCP server: stdio, SSE and streamable HTTP, 24 tools (the hosted remote endpoint is not live yet) | `mcp/server.py` | ✅ Shipped |
| Auth: API keys (O(1) lookup), JWT + 2FA, RBAC scopes | `auth/` | ✅ Shipped |
| Tenancy: users, teams, spaces, projects | `teams/`, `spaces/` | ✅ Shipped |
| Cloud: Paddle billing, plan limits, metering | `cloud/` | ✅ Shipped |
| Dashboard (React SPA, 2D/3D knowledge graph, brain insights) | `dashboard/` | ✅ Shipped |
| TypeScript SDK / Python client / Chrome extension | `sdk/`, `client/`, `extension/` | ✅ Shipped |
| Observability: request IDs, structured logs | `main.py`, `core/` | ✅ Shipped (0.16.0) |
| Backups: a database copy when a new build starts (the newest 3 are kept); Litestream only when `LITESTREAM_REPLICA_URL` is set (not set on Remembra Cloud) | `storage/backup.py`, `Dockerfile.cloud`, `scripts/cloud-entrypoint.sh` | ✅ Shipped (0.16.0) |
| Async enrichment (fast writes) | `services/memory.py` | ✅ Behind `REMEMBRA_ASYNC_ENRICHMENT` flag |
| SQLite → Postgres migration | — | 📋 Planned |
| Recall-quality regression gate in CI (LoCoMo runner exists) | `benchmarks/` | 📋 Planned |

Operations: see `docs/OPERATIONS.md` (production builds `Dockerfile.cloud`; the hosted service's own runbook is kept outside this repository).

---

## Data Models

### Memory Object
```python
class Memory:
    id: str                    # ULID
    user_id: str               # Owner
    project_id: str            # Project scope (default: "default")
    content: str               # Original text
    extracted_facts: List[str] # Parsed facts
    entities: List[EntityRef]  # Linked entities
    embedding: List[float]     # Vector representation (excluded from API)
    metadata: dict             # Custom metadata
    created_at: datetime
    updated_at: datetime
    expires_at: datetime       # Optional TTL
    access_count: int          # For decay algorithm
    last_accessed: datetime
```

### Entity Object
```python
class Entity:
    id: str                    # ULID
    canonical_name: str        # "Adam Smith"
    aliases: List[str]         # ["Adam", "Mr. Smith", "husband"]
    type: str                  # "person", "company", "place", "concept"
    attributes: dict           # {"role": "CTO", "company": "Acme"}
    relationships: List[Rel]   # Links to other entities
    confidence: float          # 0.0 - 1.0
    created_at: datetime
    updated_at: datetime
```

### Relationship Object
```python
class Relationship:
    id: str
    from_entity_id: str        # Entity ID
    to_entity_id: str          # Entity ID
    type: str                  # "works_at", "knows", "married_to"
    properties: dict
    confidence: float
    source_memory_id: str      # Which memory created this
```

---

## API Endpoints

### Store Memory
```http
POST /api/v1/memories
Content-Type: application/json

{
  "user_id": "user_123",
  "content": "Had a meeting with John from Acme Corp. He's interested in our product.",
  "project_id": "my_app",
  "metadata": {"source": "meeting_notes"},
  "ttl": "30d"
}

Response (201 Created):
{
  "id": "01HQXYZ...",
  "extracted_facts": [
    "User had a meeting with John.",
    "John works at Acme Corp.",
    "John is interested in our product."
  ],
  "entities": [
    {"id": "...", "canonical_name": "John", "type": "person", "confidence": 0.95}
  ]
}
```

### Recall Memories
```http
POST /api/v1/memories/recall
Content-Type: application/json

{
  "user_id": "user_123",
  "query": "What do I know about John?",
  "project_id": "my_app",
  "limit": 5,
  "threshold": 0.7
}

Response (200 OK):
{
  "context": "John works at Acme Corp and is interested in your product.",
  "memories": [
    {"id": "01HQXYZ...", "relevance": 0.92, "content": "...", "created_at": "..."}
  ],
  "entities": [
    {"id": "...", "canonical_name": "John", "type": "person", "confidence": 0.95}
  ]
}
```

### Get Memory by ID
```http
GET /api/v1/memories/{memory_id}

Response (200 OK):
{
  "id": "01HQXYZ...",
  "content": "...",
  "user_id": "user_123",
  "created_at": "..."
}
```

### Forget (Delete)
Give exactly one of `memory_id`, `entity` or `all_memories=true`, or only `project_id`:
```http
DELETE /api/v1/memories?memory_id=01HQXYZ...
DELETE /api/v1/memories?entity=John            (since 0.16.1; add project_id to limit it to one project)
DELETE /api/v1/memories?project_id=my_app      (every memory in that project)
DELETE /api/v1/memories?all_memories=true      (the whole account)

Response (200 OK):
{
  "deleted_memories": 5,
  "deleted_entities": 1,
  "deleted_relationships": 3
}
```

---

## Python SDK

```python
from remembra import Memory

# Initialize (self-hosted)
memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    project="my_app"
)

# Initialize (Remembra Cloud)
memory = Memory(
    base_url="https://api.remembra.dev",
    api_key="rem_xxx",
    user_id="user_123"
)

# Store
result = memory.store("John is the CTO at Acme Corp")
print(result.id)  # "01HQXYZ..."
print(result.extracted_facts)  # ["John is the CTO at Acme Corp."]

# Recall
result = memory.recall("Who is John?")
print(result.context)  # "John is the CTO at Acme Corp."
print(result.memories)  # [Memory(...)]

# Forget
memory.forget(memory_id="01HQXYZ...")  # Delete specific
memory.forget(entity="John")  # Delete the memories linked to an entity
memory.forget(all_memories=True)  # Delete everything in the account (explicit only)
```

---

## Self-Hosting

### Quickstart (one command)
```bash
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
```
It starts Remembra, Qdrant and Ollama with Docker Compose, with auth off and local embeddings. The
`remembra/remembra` image alone is not enough: it needs a Qdrant server (`REMEMBRA_QDRANT_URL`) and keeps its
SQLite database in `/data`. See `docs/getting-started/docker.md` for the other compose files and settings.

### Development (with Qdrant)
```bash
docker-compose up -d
```

---

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HOST` | `0.0.0.0` | Bind address |
| `REMEMBRA_PORT` | `8787` | API port |
| `REMEMBRA_DEBUG` | `false` | Debug mode |
| `REMEMBRA_LOG_LEVEL` | `info` | Log level |
| `REMEMBRA_QDRANT_URL` | `http://qdrant:6333` | Qdrant address |
| `REMEMBRA_DATABASE_URL` | `sqlite+aiosqlite:///remembra.db` (the Docker image sets `/data/remembra.db`) | Metadata DB |
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | openai/ollama/cohere |
| `REMEMBRA_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `REMEMBRA_OPENAI_API_KEY` | - | OpenAI API key |
| `REMEMBRA_OLLAMA_URL` | `http://localhost:11434` | Ollama address |
| `REMEMBRA_EXTRACTION_MODEL` | `gpt-4o-mini` | OpenAI model for fact extraction, consolidation, entity matching and conversation ingest |
| `REMEMBRA_LLM_PROVIDER` | `openai` | Entity-extraction backend only (`openai`, `anthropic`, `ollama`) |
| `REMEMBRA_LLM_MODEL` | `gpt-4o-mini` | Entity-extraction fallback model, used only when `REMEMBRA_EXTRACTION_MODEL` does not fit the provider |

---

## Performance Targets

| Metric | Target | Current |
|--------|--------|---------|
| Store latency | <500ms | TBD |
| Recall latency | <200ms | TBD |
| Throughput | 100 req/s | TBD |
| Memory limit | 1M memories/instance | TBD |

**Benchmark vs Mem0 (Target):**
- Match or exceed their 91% latency improvement
- Match or exceed their 90% token savings
- Competitive accuracy on LOCOMO benchmark

---

## Security

1. **API keys**: admin, editor and viewer roles; a key can be limited to some projects or to one agent
2. **Data Isolation**: User data strictly scoped by user_id + project_id
3. **Deletion**: `forget()` deletes the memories, their vectors, keyword entries, entity links and the
   relationships pulled from them at once. Conflict records that quoted the text stay until the account is
   erased, and a database copy taken when a new build starts keeps deleted data until 3 newer copies exist.
4. **Self-hosted**: memories stay on your servers, except the text sent to the embedding and LLM providers you
   configure (OpenAI by default)

---

## File Structure

```
remembra/
├── src/remembra/
│   ├── __init__.py
│   ├── main.py              # FastAPI app
│   ├── config.py            # Settings
│   ├── api/
│   │   ├── router.py        # API router
│   │   └── v1/
│   │       └── memories.py  # Memory endpoints
│   ├── core/
│   │   ├── health.py        # Health checks
│   │   └── logging.py       # Structured logging
│   ├── models/
│   │   └── memory.py        # Pydantic models
│   ├── services/
│   │   └── memory.py        # Business logic
│   └── storage/
│       ├── qdrant.py        # Vector store
│       ├── database.py      # SQLite metadata
│       └── embeddings.py    # Embedding providers
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```
