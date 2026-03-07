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
1. **Self-host in minutes** - Single `docker run` command
2. **Fair pricing** - $0 → $29 → $99 (not $19 → $249)
3. **Developer-first docs** - Actually usable
4. **MIT license** - True open source
5. **Lightweight** - Runs on a $5/mo VPS

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

### ✅ Completed (Week 1-2)

| Component | File | Status |
|-----------|------|--------|
| FastAPI skeleton | `main.py` | ✅ Done |
| Config management | `config.py` | ✅ Done |
| Health checks | `core/health.py` | ✅ Done |
| Memory models | `models/memory.py` | ✅ Done |
| Qdrant integration | `storage/qdrant.py` | ✅ Done |
| SQLite metadata | `storage/database.py` | ✅ Done |
| Embedding service | `storage/embeddings.py` | ✅ Done |
| Memory service | `services/memory.py` | ✅ Done |
| API endpoints | `api/v1/memories.py` | ✅ Done |
| Docker setup | `docker-compose.yml` | ✅ Done |
| CI/CD pipeline | `.github/workflows/` | ✅ Done |
| Test suite | `tests/` | ✅ Passing |

### 🔄 In Progress (Week 3)

| Component | File | Status |
|-----------|------|--------|
| Python SDK | `sdk/` | 🔄 Building |
| PyPI packaging | `pyproject.toml` | 🔄 Next |

### 📋 Planned (Week 4+)

| Component | Priority | Notes |
|-----------|----------|-------|
| LLM-powered extraction | HIGH | Replace rule-based |
| Entity resolution | HIGH | Graph memory (+2% accuracy) |
| Reranking | MEDIUM | Post-retrieval scoring |
| Memory consolidation | MEDIUM | Dedup/merge |
| Temporal queries | MEDIUM | Time-aware recall |
| Context synthesis | MEDIUM | LLM-powered summarization |

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
```http
DELETE /api/v1/memories?user_id=user_123
DELETE /api/v1/memories?memory_id=01HQXYZ...
DELETE /api/v1/memories?entity=John  (coming Week 5)

Response (200 OK):
{
  "deleted_memories": 5,
  "deleted_entities": 1,
  "deleted_relationships": 3
}
```

---

## Python SDK (Week 3)

```python
from remembra import Memory

# Initialize (self-hosted)
memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    project="my_app"
)

# Initialize (cloud - future)
memory = Memory(
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
memory.forget(user_id="user_123")  # Delete all
memory.forget(memory_id="01HQXYZ...")  # Delete specific
```

---

## Self-Hosting

### Minimal (One Command)
```bash
docker run -d -p 8787:8787 \
  -e REMEMBRA_OPENAI_API_KEY=sk-xxx \
  remembra/remembra:latest
```

### With Persistent Storage
```bash
docker run -d -p 8787:8787 \
  -v remembra_data:/app/data \
  -e REMEMBRA_OPENAI_API_KEY=sk-xxx \
  remembra/remembra:latest
```

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
| `REMEMBRA_DATABASE_URL` | `sqlite:///remembra.db` | Metadata DB |
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | openai/ollama/cohere |
| `REMEMBRA_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `REMEMBRA_OPENAI_API_KEY` | - | OpenAI API key |
| `REMEMBRA_OLLAMA_URL` | `http://localhost:11434` | Ollama address |
| `REMEMBRA_LLM_MODEL` | `gpt-4o-mini` | LLM for extraction |

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

1. **API Keys**: Planned for cloud tier
2. **Data Isolation**: User data strictly scoped by user_id + project_id
3. **GDPR**: Complete deletion via forget() endpoint
4. **Self-hosted**: Your data never leaves your infrastructure

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
