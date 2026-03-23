# REST API

Complete REST API reference for Remembra.

## Base URL

```
http://localhost:8787/api/v1
```

## Authentication

When `REMEMBRA_AUTH_ENABLED=true`, include your API key in the header:

```bash
curl -H "Authorization: Bearer rem_your_api_key" \
     http://localhost:8787/api/v1/memories
```

## Endpoints

### Health Check

```http
GET /health
```

**Response:**
```json
{
  "status": "healthy",
  "version": "0.9.0",
  "qdrant": "connected",
  "database": "connected"
}
```

---

### Store Memory

Store a new memory with automatic extraction.

```http
POST /api/v1/memories
```

**Request Body:**
```json
{
  "content": "User's name is John. He works at Google as a senior engineer.",
  "user_id": "user_123",
  "project": "default",
  "metadata": {
    "source": "chat",
    "session_id": "sess_abc"
  },
  "ttl": "30d",
  "expires_at": "2026-03-25T14:00:00Z"
}
```

**Response:**
```json
{
  "status": "success",
  "memories": [
    {
      "id": "mem_abc123",
      "content": "John works at Google as a senior engineer",
      "action": "ADD",
      "entities": ["John", "Google"]
    }
  ],
  "entities_extracted": 2,
  "relationships_created": 1
}
```

---

### Recall Memories

Query memories semantically.

```http
POST /api/v1/memories/recall
```

**Request Body:**
```json
{
  "query": "What do I know about John?",
  "user_id": "user_123",
  "project": "default",
  "limit": 10,
  "threshold": 0.4,
  "max_tokens": 4000,
  "enable_hybrid": true,
  "enable_rerank": false
}
```

**Response:**
```json
{
  "memories": [
    {
      "id": "mem_abc123",
      "content": "John works at Google as a senior engineer",
      "score": 0.92,
      "created_at": "2026-03-01T10:30:00Z"
    }
  ],
  "context": "John works at Google as a senior engineer.",
  "total": 1
}
```

---

### Update Memory

Update an existing memory.

```http
PUT /api/v1/memories/{memory_id}
```

**Request Body:**
```json
{
  "content": "John was promoted to Staff Engineer at Google"
}
```

**Response:**
```json
{
  "status": "success",
  "memory": {
    "id": "mem_abc123",
    "content": "John is a Staff Engineer at Google (promoted from Senior)"
  }
}
```

---

### Delete Memory

Delete specific memories.

```http
DELETE /api/v1/memories
```

**Request Body:**
```json
{
  "memory_ids": ["mem_abc123", "mem_def456"]
}
```

Or delete all for a user:

```json
{
  "user_id": "user_123",
  "all": true
}
```

---

### List Memories

Get all memories for a user.

```http
GET /api/v1/memories?user_id=user_123&project=default&limit=100
```

**Response:**
```json
{
  "memories": [...],
  "total": 42,
  "page": 1,
  "limit": 100
}
```

---

### Historical Query (as_of)

Time-travel query.

```http
POST /api/v1/memories/recall
```

**Request Body:**
```json
{
  "query": "User status",
  "user_id": "user_123",
  "as_of": "2026-02-15T00:00:00Z"
}
```

---

### Cleanup Expired

Remove expired memories.

```http
POST /api/v1/memories/cleanup-expired
```

**Request Body:**
```json
{
  "dry_run": true
}
```

**Response:**
```json
{
  "deleted": 0,
  "would_delete": 15,
  "dry_run": true
}
```

---

## User Endpoints

### Get User Profile

Get aggregated user intelligence including facts, activity, and topics.

```http
GET /api/v1/users/{user_id}/profile
```

**Response:**
```json
{
  "user_id": "user_123",
  "memory_count": 42,
  "entity_count": 15,
  "last_active": "2026-03-22T10:30:00Z",
  "first_memory": "2026-02-15T08:00:00Z",
  "top_topics": ["work", "travel", "family"],
  "top_entities": [
    {"name": "Google", "type": "ORG", "mentions": 12},
    {"name": "John Smith", "type": "PERSON", "mentions": 8}
  ],
  "activity": {
    "stores_last_7d": 15,
    "recalls_last_7d": 45,
    "avg_memories_per_day": 2.1
  },
  "aggregated_facts": [
    "Works at Google as Staff Engineer",
    "Lives in San Francisco",
    "Prefers dark mode interfaces"
  ]
}
```

---

## Entity Endpoints

### List Entities

```http
GET /api/v1/entities?user_id=user_123
```

**Response:**
```json
{
  "entities": [
    {
      "id": "ent_123",
      "name": "John Smith",
      "type": "PERSON",
      "aliases": ["John", "Mr. Smith"]
    },
    {
      "id": "ent_456", 
      "name": "Google",
      "type": "ORG",
      "aliases": ["Alphabet", "GOOG"]
    }
  ]
}
```

### Get Entity

```http
GET /api/v1/entities/{entity_id}
```

### Get Entity Relationships

```http
GET /api/v1/entities/{entity_id}/relationships
```

**Response:**
```json
{
  "relationships": [
    {
      "source": "John Smith",
      "target": "Google",
      "type": "WORKS_AT",
      "properties": {"role": "Staff Engineer"}
    }
  ]
}
```

### Get Entity Memories

```http
GET /api/v1/entities/{entity_id}/memories
```

---

## Temporal Endpoints

### Decay Report

View memory health and decay scores.

```http
GET /api/v1/temporal/decay/report?user_id=user_123
```

**Response:**
```json
{
  "total_memories": 100,
  "healthy": 85,
  "decaying": 10,
  "expired": 5,
  "memories": [
    {
      "id": "mem_123",
      "content": "...",
      "decay_score": 0.75,
      "last_accessed": "2026-02-28T10:00:00Z"
    }
  ]
}
```

### Single Memory Decay

```http
GET /api/v1/temporal/memory/{memory_id}/decay
```

### Run Cleanup

```http
POST /api/v1/temporal/cleanup
```

**Request Body:**
```json
{
  "dry_run": false,
  "archive": true
}
```

---

## API Key Management

### Create API Key

```http
POST /api/v1/keys
```

**Headers:** Master key required

```bash
curl -H "Authorization: Bearer master_key_here" \
     -X POST http://localhost:8787/api/v1/keys \
     -d '{"user_id": "user_123", "name": "Production"}'
```

**Response:**
```json
{
  "key": "rem_abc123...",
  "key_id": "key_xyz",
  "name": "Production"
}
```

!!! warning
    The full API key is only shown once. Store it securely.

### List API Keys

```http
GET /api/v1/keys
```

### Revoke API Key

```http
DELETE /api/v1/keys/{key_id}
```

---

## Rate Limits

Default rate limits (per API key):

| Endpoint | Limit |
|----------|-------|
| `POST /api/v1/memories` | 30/minute |
| `POST /api/v1/memories/recall` | 60/minute |
| `DELETE /api/v1/memories` | 10/minute |

Rate limit headers are included in responses:

```
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 25
X-RateLimit-Reset: 1709312400
```

---

## Configuration Options

### Strict Mode (410 GONE)

When `strict_mode` is enabled, requests for expired memories return `410 GONE` instead of silently accepting the request.

**Enable via environment variable:**
```bash
REMEMBRA_STRICT_MODE=true
```

**Or via config:**
```json
{
  "strict_mode": true
}
```

**Behavior:**
- Without strict mode: Expired memory requests succeed silently (memory just not returned)
- With strict mode: Expired memory requests return `410 GONE` with details

**410 Response:**
```json
{
  "error": {
    "code": "memory_expired",
    "message": "Memory has expired",
    "details": {
      "memory_id": "mem_abc123",
      "expired_at": "2026-03-21T14:00:00Z"
    }
  }
}
```

---

## Error Responses

```json
{
  "error": {
    "code": "validation_error",
    "message": "user_id is required",
    "details": {...}
  }
}
```

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `validation_error` | 400 | Invalid request body |
| `authentication_error` | 401 | Invalid or missing API key |
| `not_found` | 404 | Resource not found |
| `rate_limit_exceeded` | 429 | Too many requests |
| `internal_error` | 500 | Server error |

---

## OpenAPI Spec

Interactive API documentation available at:

```
http://localhost:8787/docs
```

Download OpenAPI spec:

```
http://localhost:8787/openapi.json
```
