# REST API

Complete REST API reference for Remembra.

## Base URL

```
http://localhost:8787/api/v1
```

## Authentication

When `REMEMBRA_AUTH_ENABLED=true` (the default), send your API key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: rem_your_api_key" \
     http://localhost:8787/api/v1/memories
```

A `rem_` key sent as `Authorization: Bearer rem_your_api_key` works too. The account comes from the key: a
`user_id` in a request body is ignored.

## Endpoints

### Health Check

```http
GET /health
```

**Response:**
```json
{
  "status": "ok",
  "version": "0.16.1",
  "dependencies": {"qdrant": {"status": "ok"}},
  "encryption": "AES-256-GCM"
}
```

`status` is `degraded` (HTTP 503) when Qdrant is down. `encryption` appears only when `REMEMBRA_ENCRYPTION_KEY`
is set, and `build_sha` when the server knows its build.

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
  "project_id": "default",
  "metadata": {
    "source": "chat",
    "session_id": "sess_abc"
  },
  "ttl": "30d"
}
```

Other fields: `expires_at` (an exact expiry time, instead of `ttl`), `skip_extraction` (store the content as one
memory), `memory_type`, `scope`, `supersedes`. A `ttl` the server cannot read is refused with `422` (see
[TTL formats](temporal.md#ttl-formats)). Credentials in `content` and `metadata` are replaced with
`[REDACTED:<kind>]` before anything is saved.

**Response:**
```json
{
  "id": "mem_abc123",
  "status": "stored",
  "extracted_facts": ["John works at Google as a senior engineer"],
  "entities": [],
  "entities_status": "pending",
  "consolidation": [
    {"fact": "John works at Google as a senior engineer", "action": "add", "memory_id": "mem_abc123", "decided_by": "llm"}
  ],
  "dropped_facts": [],
  "extraction": "llm",
  "expires_at": "2026-04-01T10:30:00Z"
}
```

`status` is `stored`, `duplicate` (every fact was already known; `duplicate_of` names the memory), `pending` or
`not_stored`. Entities are linked in the background (`entities_status: "pending"`).

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
  "project_id": "default",
  "limit": 5,
  "threshold": 0.4,
  "max_tokens": 4000,
  "enable_hybrid": true,
  "enable_rerank": false
}
```

Leave out `project_id` to recall across all your projects. Other fields: `filters` (exact-match metadata),
`scope`, `as_of`, `slim` (caps the context at 800 tokens), `include_superseded`, `include_decay_score`,
`include_low_trust`.

**Response:**
```json
{
  "context": "John works at Google as a senior engineer.",
  "memories": [
    {
      "id": "mem_abc123",
      "content": "John works at Google as a senior engineer",
      "relevance": 0.92,
      "created_at": "2026-03-01T10:30:00Z"
    }
  ],
  "entities": []
}
```

---

### Update Memory

Update an existing memory.

```http
PATCH /api/v1/memories/{memory_id}
```

**Request Body:**
```json
{
  "content": "John was promoted to Staff Engineer at Google",
  "metadata": {"source": "chat"}
}
```

**Response:**
```json
{
  "id": "mem_abc123",
  "updated_entities": []
}
```

---

### Delete Memory

Delete one memory, the memories about one entity, every memory in one project, or the whole account. The
parameters go in the query string, and only the caller's own memories are ever deleted.

```http
DELETE /api/v1/memories?memory_id=mem_abc123
DELETE /api/v1/memories?entity=John
DELETE /api/v1/memories?entity=John&project_id=work
DELETE /api/v1/memories?project_id=my-project
DELETE /api/v1/memories?all_memories=true
```

| Parameters | Deletes |
|------------|---------|
| `memory_id` | That one memory |
| `entity` | The memories that entity extraction linked to the entity with that exact canonical name or alias (any case; never a partial name or a pattern), in every project. Then each such entity no memory mentions any more, with its relationships |
| `entity` and `project_id` | The same, only in that project |
| `project_id` alone | Every memory in that project |
| `all_memories=true` | Every memory, entity, relationship and decision log in the account |

Give at most one of `memory_id`, `entity` and `all_memories=true`. A call with none of these and no
`project_id`, with more than one, or with a blank `entity` or `project_id` is rejected with `422` and deletes
nothing. Only `all_memories=true` deletes the whole account. A project-scoped key cannot use it, and its
`entity` deletes stay inside its projects: a key for several projects must pass `project_id`.

The response gives the counts deleted:

```json
{"deleted_memories": 2, "deleted_entities": 1, "deleted_relationships": 3}
```

!!! warning "Servers before 0.16.1"
    Before 0.16.1, `DELETE /api/v1/memories?entity=...` deleted every memory, entity, relationship and
    decision log in the account, and with `project_id` it deleted every memory in that project. Check that
    `GET /health` reports 0.16.1 or later before you delete by entity.

---

### List Memories

Your memories, newest first.

```http
GET /api/v1/memories?project_id=default&limit=20&offset=0
```

`limit` is 1-100 (default 20). Leave out `project_id` to list every project.

**Response:** a list of memories, each with `id`, `project_id`, `content`, `created_at`, `access_count`,
`memory_type`, `entities` and `metadata`.

### Get One Memory

```http
GET /api/v1/memories/{memory_id}
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
  "as_of": "2026-02-15T00:00:00Z"
}
```

---

### Cleanup Expired

Delete your expired memories.

```http
POST /api/v1/memories/cleanup-expired
```

**Response:**
```json
{
  "deleted_count": 15
}
```

For a preview first, use `POST /api/v1/temporal/cleanup` (below).

---

## User Endpoints

### Get User Profile

Aggregated facts, activity and topics for your account.

```http
GET /api/v1/users/me/profile
```

`GET /api/v1/users/{user_id}/profile` works only for your own `user_id`.

**Response:**
```json
{
  "user_id": "user_123",
  "project_id": null,
  "total_memories": 42,
  "total_entities": 15,
  "total_relationships": 9,
  "static_facts": {"facts": ["Works at Google as Staff Engineer"], "entities": [...]},
  "activity": {"memories_last_24h": 2, "memories_last_7d": 15, "memories_last_30d": 40},
  "top_topics": [{"topic": "work", "count": 12}],
  "created_at": "2026-02-15T08:00:00Z",
  "last_active": "2026-03-22T10:30:00Z"
}
```

---

## Entity Endpoints

### List Entities

```http
GET /api/v1/entities
```

**Response:**
```json
{
  "entities": [
    {
      "id": "ent_123",
      "canonical_name": "John Smith",
      "type": "PERSON",
      "aliases": ["John", "Mr. Smith"],
      "memory_count": 4
    }
  ],
  "total": 1,
  "by_type": {"PERSON": 1}
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
      "id": "rel_1",
      "from_entity_name": "John Smith",
      "to_entity_name": "Google",
      "type": "WORKS_AT",
      "valid_from": null,
      "valid_to": null
    }
  ],
  "total": 1
}
```

### Get Entity Memories

```http
GET /api/v1/entities/{entity_id}/memories
```

Entity routes need `entity:read`, which every role has.

---

## Temporal Endpoints

### Decay Report

Relevance scores and pruning candidates.

```http
GET /api/v1/temporal/decay/report?limit=50
```

**Response:**
```json
{
  "user_id": "user_123",
  "project_id": "default",
  "total_memories": 100,
  "prune_candidates": 5,
  "average_relevance": 0.62,
  "config": {"prune_threshold": 0.1},
  "memories": [
    {
      "id": "mem_123",
      "content_preview": "...",
      "relevance_score": 0.75,
      "days_since_access": 3.5,
      "access_count": 2,
      "should_prune": false,
      "is_expired": false
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
POST /api/v1/temporal/cleanup?dry_run=false&include_decayed=true
```

`dry_run` defaults to `true` (a preview). With `dry_run=false` it deletes expired memories, and with
`include_decayed=true` it also moves decayed ones to the cold archive.

---

## API Key Management

### Create API Key

```http
POST /api/v1/keys
```

**Auth:** a dashboard sign-in, an API key that holds `key:create` (it creates a
key for its own account, never above its own role), or the master key in
`X-API-Key` with a `user_id` in the body. Only the master key can create an
`admin` key. See [Roles and Permissions](rbac.md).

```bash
curl -X POST http://localhost:8787/api/v1/keys \
     -H "X-API-Key: $REMEMBRA_AUTH_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"user_id": "user_123", "name": "Production", "role": "editor"}'
```

**Response:**
```json
{
  "id": "key_xyz",
  "key": "rem_abc123...",
  "user_id": "user_123",
  "name": "Production",
  "rate_limit_tier": "standard",
  "role": "editor",
  "project_ids": [],
  "agent_id": null,
  "message": "Store this key securely. It cannot be retrieved again."
}
```

!!! warning
    The full API key is only shown once. Store it securely.

### List API Keys

```http
GET /api/v1/keys
```

Needs `key:list` (every role has it).

### Revoke API Key

```http
DELETE /api/v1/keys/{key_id}
```

Needs `key:revoke`. An API key can only revoke keys that hold no more access
than itself. Add `?hard=true` to delete the key instead.

---

## Rate Limits

Limits are per route, counted per account (per IP address without a key):

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

With `REMEMBRA_STRICT_MODE=true`, a `GET` or `PATCH` of an expired memory returns `410 GONE`. Without it, those
requests treat an expired memory like any other until cleanup deletes it. Recall never returns expired
memories either way.

**410 Response:**
```json
{
  "detail": {
    "error": "MEMORY_EXPIRED",
    "message": "Memory mem_abc123 has expired. Re-acquire context via recall.",
    "memory_id": "mem_abc123",
    "expires_at": "2026-03-21T14:00:00Z",
    "strict_mode": true
  }
}
```

---

## Error Responses

Errors use FastAPI's shape: a `detail` that is a string, or an object for some errors.

```json
{
  "detail": "Permission denied: memory:store required"
}
```

| HTTP Status | Meaning |
|-------------|---------|
| 400 | Bad request (for example PII in `block` mode) |
| 401 | Invalid or missing API key |
| 403 | The key lacks the permission, or the project or agent is not its own |
| 404 | Not found |
| 410 | Expired memory (strict mode) |
| 422 | Invalid request body (for example an unreadable `ttl`) |
| 429 | Rate limit or plan limit reached |
| 500 | Server error |
| 503 | A feature that is off (for example webhooks), or a dependency down |

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
