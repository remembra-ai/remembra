# Configuration Reference

All environment variables for Remembra.

## Required

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key | `sk-...` |

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HOST` | `0.0.0.0` | Server bind address |
| `REMEMBRA_PORT` | `8787` | Server port |
| `REMEMBRA_WORKERS` | `1` | Number of worker processes |
| `REMEMBRA_LOG_LEVEL` | `INFO` | Logging level |

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DATABASE_PATH` | `./remembra.db` | SQLite database path |
| `QDRANT_HOST` | `localhost` | Qdrant server host |
| `QDRANT_PORT` | `6333` | Qdrant server port |
| `QDRANT_API_KEY` | - | Qdrant API key (if secured) |
| `QDRANT_COLLECTION` | `remembra` | Qdrant collection name |

## Embeddings

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | Provider: `openai`, `ollama`, `cohere` |
| `REMEMBRA_EMBEDDING_MODEL` | `text-embedding-3-small` | Model name |
| `REMEMBRA_EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `COHERE_API_KEY` | - | Cohere API key |

## Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SMART_EXTRACTION_ENABLED` | `true` | Enable LLM extraction |
| `REMEMBRA_EXTRACTION_MODEL` | `gpt-4o-mini` | Model for extraction |
| `REMEMBRA_EXTRACTION_TEMPERATURE` | `0.0` | Extraction temperature |

## Entity Resolution

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENTITY_EXTRACTION_ENABLED` | `true` | Extract entities |
| `REMEMBRA_ENTITY_MATCHING_THRESHOLD` | `0.85` | Alias matching threshold |

## Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_THRESHOLD` | `0.40` | Default similarity threshold |
| `REMEMBRA_DEFAULT_LIMIT` | `10` | Default recall limit |
| `REMEMBRA_DEFAULT_MAX_TOKENS` | `4000` | Max context tokens |

### Hybrid Search

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HYBRID_SEARCH_ENABLED` | `true` | Enable hybrid search |
| `REMEMBRA_HYBRID_ALPHA` | `0.4` | Keyword weight (0-1) |
| `REMEMBRA_HYBRID_FUSION` | `weighted` | Fusion: `weighted` or `rrf` |

### Reranking

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RERANK_ENABLED` | `false` | Enable CrossEncoder reranking |
| `REMEMBRA_RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranker model |
| `REMEMBRA_RERANK_TOP_K` | `20` | Candidates to rerank |

### Ranking Weights

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RANKING_SEMANTIC_WEIGHT` | `0.6` | Semantic score weight |
| `REMEMBRA_RANKING_RECENCY_WEIGHT` | `0.15` | Recency boost weight |
| `REMEMBRA_RANKING_ENTITY_WEIGHT` | `0.15` | Entity match weight |
| `REMEMBRA_RANKING_KEYWORD_WEIGHT` | `0.1` | Keyword match weight |
| `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` | `30` | Recency half-life (days) |

### Graph Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_GRAPH_RETRIEVAL_ENABLED` | `true` | Enable graph traversal |
| `REMEMBRA_GRAPH_TRAVERSAL_DEPTH` | `2` | Max hop depth |

## Temporal

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_TTL_DAYS` | - | Default TTL (optional) |
| `REMEMBRA_DECAY_ENABLED` | `true` | Enable memory decay |
| `REMEMBRA_DECAY_HALF_LIFE_DAYS` | `30` | Decay half-life |
| `REMEMBRA_ACCESS_BOOST_WEIGHT` | `0.2` | Access count boost |

## Security

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTH_ENABLED` | `true` | Enable API key auth |
| `REMEMBRA_AUTH_MASTER_KEY` | - | Master admin key |

### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |
| `REMEMBRA_RATE_LIMIT_STORAGE` | `memory` | Backend: `memory` or `redis://...` |
| `REMEMBRA_RATE_LIMIT_STORE` | `30/minute` | Store endpoint limit |
| `REMEMBRA_RATE_LIMIT_RECALL` | `60/minute` | Recall endpoint limit |
| `REMEMBRA_RATE_LIMIT_FORGET` | `10/minute` | Forget endpoint limit |

### Sanitization

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SANITIZATION_ENABLED` | `true` | Enable input sanitization |
| `REMEMBRA_TRUST_SCORE_THRESHOLD` | `0.5` | Suspicious content threshold |

## Dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_STATIC_DIR` | - | Path to dashboard build |

## Auto-Forgetting (v0.12.0)

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTO_TTL_ENABLED` | `true` | Enable smart auto-forgetting |
| `REMEMBRA_STRICT_MODE` | `false` | Return 410 GONE for expired memories |

Smart auto-forgetting detects 35+ temporal patterns and sets appropriate TTLs:

- "meeting tomorrow" → 36 hours
- "call next week" → 8 days
- "deadline in 2 hours" → 3 hours
- "event next month" → 35 days

No configuration needed—just store memories naturally.

## Example .env File

```bash
# Required
OPENAI_API_KEY=sk-your-key-here

# Server
REMEMBRA_HOST=0.0.0.0
REMEMBRA_PORT=8787

# Database
REMEMBRA_DATABASE_PATH=/app/data/remembra.db
QDRANT_HOST=qdrant
QDRANT_PORT=6333

# Security (enable in production!)
REMEMBRA_AUTH_ENABLED=true
REMEMBRA_AUTH_MASTER_KEY=your-secure-master-key
REMEMBRA_RATE_LIMIT_ENABLED=true

# Extraction
REMEMBRA_SMART_EXTRACTION_ENABLED=true
REMEMBRA_EXTRACTION_MODEL=gpt-4o-mini

# Retrieval
REMEMBRA_HYBRID_SEARCH_ENABLED=true
REMEMBRA_RERANK_ENABLED=false
REMEMBRA_DEFAULT_MAX_TOKENS=4000

# Temporal
REMEMBRA_DEFAULT_TTL_DAYS=365
REMEMBRA_DECAY_ENABLED=true
```
