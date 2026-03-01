# Remembra - Technical Architecture

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
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Authentication  │  Rate Limiting  │  Request Logging   │    │
│  └─────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  /store  │  /recall  │  /update  │  /forget  │  /search │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    INTELLIGENCE LAYER                            │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │   Extraction    │  │ Entity Resolution│  │   Retrieval     │  │
│  │   Engine        │  │    Engine        │  │   Engine        │  │
│  │                 │  │                  │  │                 │  │
│  │ • Parse text    │  │ • Extract entities│ │ • Semantic search│ │
│  │ • Extract facts │  │ • Match/link     │  │ • Graph traversal│ │
│  │ • Categorize    │  │ • Confidence score│ │ • Ranking        │  │
│  └────────┬────────┘  └────────┬─────────┘  └────────┬────────┘  │
└───────────┼────────────────────┼─────────────────────┼──────────┘
            │                    │                     │
            ▼                    ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                      STORAGE LAYER                               │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │   VECTOR STORE  │  │   GRAPH STORE   │  │ RELATIONAL STORE│  │
│  │     (Qdrant)    │  │ (SQLite/Neo4j)  │  │   (PostgreSQL)  │  │
│  │                 │  │                 │  │                 │  │
│  │ • Embeddings    │  │ • Entities      │  │ • Users         │  │
│  │ • Semantic idx  │  │ • Relationships │  │ • Projects      │  │
│  │ • Similarity    │  │ • Graph queries │  │ • API Keys      │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     EMBEDDING LAYER                              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  OpenAI Ada-002  │  Cohere Embed  │  Ollama (Local)     │    │
│  │  (Default cloud) │  (Alternative) │  (Self-host)        │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Models

### Memory Object
```python
class Memory:
    id: str                    # UUID
    user_id: str               # Owner
    project_id: str            # Project scope
    content: str               # Original text
    extracted_facts: List[str] # Parsed facts
    entities: List[Entity]     # Linked entities
    embedding: List[float]     # Vector representation
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
    id: str                    # UUID
    canonical_name: str        # "Adam Smith"
    aliases: List[str]         # ["Adam", "Mr. Smith", "husband"]
    type: str                  # "person", "company", "place"
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
    from_entity: str           # Entity ID
    to_entity: str             # Entity ID
    type: str                  # "works_at", "knows", "married_to"
    properties: dict
    confidence: float
    source_memory: str         # Which memory created this
```

---

## API Endpoints

### Store Memory
```http
POST /api/v1/memories
Authorization: Bearer <api_key>
Content-Type: application/json

{
  "user_id": "user_123",
  "content": "Had a meeting with John from Acme Corp. He's interested in our product.",
  "metadata": {"source": "meeting_notes"},
  "ttl": "30d"
}

Response:
{
  "id": "mem_abc123",
  "extracted_facts": [
    "User had a meeting with John",
    "John works at Acme Corp",
    "John is interested in our product"
  ],
  "entities": [
    {"name": "John", "type": "person", "confidence": 0.95},
    {"name": "Acme Corp", "type": "company", "confidence": 0.98}
  ]
}
```

### Recall Memories
```http
POST /api/v1/memories/recall
Authorization: Bearer <api_key>
Content-Type: application/json

{
  "user_id": "user_123",
  "query": "What do I know about John?",
  "limit": 5,
  "threshold": 0.7
}

Response:
{
  "context": "John works at Acme Corp and is interested in your product. You had a meeting with him recently.",
  "memories": [
    {"id": "mem_abc123", "relevance": 0.92, "content": "..."}
  ],
  "entities": [
    {"name": "John", "type": "person", "facts": ["works at Acme Corp", "interested in product"]}
  ]
}
```

### Update Memory
```http
PATCH /api/v1/memories/{memory_id}

{
  "content": "John got promoted to CEO of Acme Corp"
}

Response:
{
  "id": "mem_abc123",
  "updated_entities": [
    {"name": "John", "attribute_changed": "role", "old": "unknown", "new": "CEO"}
  ]
}
```

### Forget (Delete)
```http
DELETE /api/v1/memories?entity=John

Response:
{
  "deleted_memories": 5,
  "deleted_entities": 1,
  "deleted_relationships": 3
}
```

---

## SDK Usage Examples

### Python SDK
```python
from remembra import Memory

# Initialize
memory = Memory(
    api_key="rem_xxx",           # For cloud
    base_url="http://localhost:8787",  # For self-host
    user_id="user_123",
    project="my_app"
)

# Store with automatic extraction
memory.store("""
    Just finished call with Sarah. She's the new VP of Engineering 
    at TechCorp. Used to work with John at StartupXYZ. Interested 
    in enterprise plan, budget around $50k/year. Follow up next Tuesday.
""")

# Recall with natural query
context = memory.recall("Who should I follow up with?")
# Returns: "Sarah from TechCorp. She's VP of Engineering, interested 
#           in enterprise plan (~$50k budget). Follow up Tuesday."

# Get specific entity
john = memory.get_entity("John")
print(john.relationships)
# [{"type": "worked_with", "entity": "Sarah", "context": "at StartupXYZ"}]

# Forget everything about a person (GDPR)
memory.forget(entity="Sarah")
```

### With LangChain
```python
from remembra.integrations import RemembraMemory
from langchain.chat_models import ChatOpenAI
from langchain.chains import ConversationChain

memory = RemembraMemory(user_id="user_123")
llm = ChatOpenAI()

chain = ConversationChain(llm=llm, memory=memory)
chain.run("My name is John and I work at Acme Corp")
# Memory automatically stored

chain.run("What's my name?")
# Memory automatically recalled
# Response: "Your name is John and you work at Acme Corp"
```

---

## Self-Hosting

### Minimal (SQLite, Local Embeddings)
```bash
docker run -d \
  -p 8787:8787 \
  -v remembra_data:/data \
  remembra/remembra:latest
```

### Production (PostgreSQL, Qdrant)
```yaml
# docker-compose.yml
version: '3.8'
services:
  remembra:
    image: remembra/remembra:latest
    ports:
      - "8787:8787"
    environment:
      - DATABASE_URL=postgresql://user:pass@postgres:5432/remembra
      - QDRANT_URL=http://qdrant:6333
      - OPENAI_API_KEY=sk-xxx  # Or use local embeddings
    depends_on:
      - postgres
      - qdrant
  
  postgres:
    image: postgres:15
    volumes:
      - pg_data:/var/lib/postgresql/data
    environment:
      - POSTGRES_PASSWORD=pass
      - POSTGRES_DB=remembra
  
  qdrant:
    image: qdrant/qdrant
    volumes:
      - qdrant_data:/qdrant/storage

volumes:
  pg_data:
  qdrant_data:
```

---

## Configuration

```python
# remembra.config.py (or environment variables)

REMEMBRA_CONFIG = {
    # Storage
    "database_url": "sqlite:///remembra.db",  # or postgresql://...
    "vector_store": "qdrant",  # or "memory" for testing
    "qdrant_url": "http://localhost:6333",
    
    # Embeddings
    "embedding_provider": "openai",  # or "ollama", "cohere"
    "embedding_model": "text-embedding-ada-002",
    "ollama_url": "http://localhost:11434",  # for local
    
    # LLM (for extraction)
    "llm_provider": "openai",
    "llm_model": "gpt-4o-mini",
    
    # Features
    "enable_entity_resolution": True,
    "enable_temporal_decay": True,
    "default_ttl": None,  # or "30d", "1y"
    
    # Performance
    "max_memories_per_recall": 10,
    "embedding_batch_size": 100,
    "cache_embeddings": True,
}
```

---

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| Store latency | <500ms | Including embedding |
| Recall latency | <200ms | Semantic search |
| Throughput | 100 req/s | Single instance |
| Memory limit | 1M memories | Per instance |
| Embedding size | 1536 dims | Ada-002 |

---

## Security Considerations

1. **API Keys**: Hashed storage, never logged
2. **Data Isolation**: User data strictly separated
3. **Encryption**: At-rest encryption option
4. **GDPR**: Complete deletion via forget()
5. **Audit Log**: Track all access (enterprise)
