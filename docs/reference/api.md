# API Reference

OpenAPI specification and endpoint details.

## Base URL

```
http://localhost:8787/api/v1
```

## Interactive Docs

Swagger UI available at:

```
http://localhost:8787/docs
```

ReDoc available at:

```
http://localhost:8787/redoc
```

## OpenAPI Spec

Download the OpenAPI 3.1 specification:

```
http://localhost:8787/openapi.json
```

## Endpoints Overview

Paths below are under `/api/v1`, except `/health`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/memories` | Store a memory |
| POST | `/memories/recall` | Query memories |
| GET | `/memories` | List memories |
| GET | `/memories/{id}` | Get one memory |
| PATCH | `/memories/{id}` | Update a memory |
| DELETE | `/memories` | Delete memories (`memory_id`, `entity` or `all_memories`) |
| POST | `/memories/batch` | Store several memories in one call |
| POST | `/memories/batch/recall` | Run several recalls in one call |
| GET | `/users/me/profile` | Your profile (v0.12.0+) |
| GET | `/entities` | List entities |
| GET | `/entities/{id}` | Get entity |
| GET | `/entities/{id}/relationships` | Entity relationships |
| GET | `/entities/{id}/memories` | Entity memories |
| POST | `/keys` | Create API key |
| GET | `/keys` | List API keys |
| PATCH | `/keys/{id}` | Rename a key, or change its role or projects |
| DELETE | `/keys/{id}` | Revoke API key (`?hard=true` deletes it) |
| GET | `/temporal/decay/report` | Decay report |
| POST | `/temporal/cleanup` | Preview or run cleanup |
| POST | `/memories/cleanup-expired` | Remove expired memories |

The full list is in the OpenAPI spec.

## Authentication

Send the API key in the `X-API-Key` header:

```
X-API-Key: rem_your_api_key
```

A `rem_` key sent as `Authorization: Bearer rem_your_api_key` works too. Dashboard sign-ins use
`Authorization: Bearer <JWT>`; they expire after a fixed 24 hours.

## Request/Response Format

All requests and responses use JSON:

```http
Content-Type: application/json
```

## Error Format

Errors use FastAPI's shape: a `detail` that is a string, or an object for some errors.

```json
{
  "detail": "Permission denied: memory:store required"
}
```

## Full Endpoint Documentation

See [REST API Guide](../guides/rest-api.md) for detailed endpoint documentation with examples.

## Rate Limits

Limits are per route, per account (per IP address without a key). The main ones:

| Endpoint | Limit |
|----------|-------|
| `POST /api/v1/memories` | 30/minute |
| `POST /api/v1/memories/recall` | 60/minute |
| `DELETE /api/v1/memories` | 10/minute |

Other routes have their own limits, and a few have none. See [Security](../guides/security.md#limits).

## SDKs

### Python

```bash
pip install remembra
```

```python
from remembra import Memory
memory = Memory(base_url="...", api_key="rem_...")
```

### REST (Any Language)

Use the REST API directly with any HTTP client.

### JavaScript / TypeScript

```bash
npm install remembra
```

See the [JavaScript SDK guide](../guides/javascript-sdk.md). The package on npm is 0.12.1; the repository's SDK is
0.13.2 and not published yet.
