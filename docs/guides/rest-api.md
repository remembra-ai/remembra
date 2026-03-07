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
     http://localhost:8787/api/v1/store
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
  "version": "0.8.0",
  "qdrant": "connected",
  "database": "connected"
}
```

---

### Store Memory

Store a new memory with automatic extraction.

```http
POST /api/v1/store
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
  "ttl": "30d"
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
POST /api/v1/recall
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
POST /api/v1/recall
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
POST /api/v1/cleanup-expired
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
| `POST /store` | 30/minute |
| `POST /recall` | 60/minute |
| `DELETE /memories` | 10/minute |

Rate limit headers are included in responses:

```
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 25
X-RateLimit-Reset: 1709312400
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
