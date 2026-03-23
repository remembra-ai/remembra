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

Download the OpenAPI 3.0 specification:

```
http://localhost:8787/openapi.json
```

## Endpoints Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/store` | Store memories |
| POST | `/recall` | Query memories |
| GET | `/memories` | List memories |
| PUT | `/memories/{id}` | Update memory |
| DELETE | `/memories` | Delete memories |
| GET | `/users/{user_id}/profile` | Get user profile (v0.12.0+) |
| GET | `/entities` | List entities |
| GET | `/entities/{id}` | Get entity |
| GET | `/entities/{id}/relationships` | Entity relationships |
| GET | `/entities/{id}/memories` | Entity memories |
| POST | `/keys` | Create API key |
| GET | `/keys` | List API keys |
| DELETE | `/keys/{id}` | Revoke API key |
| GET | `/temporal/decay/report` | Decay report |
| POST | `/temporal/cleanup` | Run cleanup |
| POST | `/cleanup-expired` | Remove expired |

## Authentication

Include API key in Authorization header:

```
Authorization: Bearer rem_your_api_key
```

## Request/Response Format

All requests and responses use JSON:

```http
Content-Type: application/json
```

## Error Format

```json
{
  "error": {
    "code": "error_code",
    "message": "Human readable message",
    "details": {}
  }
}
```

## Full Endpoint Documentation

See [REST API Guide](../guides/rest-api.md) for detailed endpoint documentation with examples.

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| POST /store | 30/minute |
| POST /recall | 60/minute |
| DELETE /memories | 10/minute |
| Others | 120/minute |

## SDKs

### Python

```bash
pip install remembra
```

```python
from remembra import Memory
memory = Memory(base_url="...", user_id="...")
```

### REST (Any Language)

Use the REST API directly with any HTTP client.

### JavaScript (Coming Soon)

```bash
npm install remembra
```
