# Security

Authentication, rate limiting, and content protection.

## Authentication

Remembra uses API keys for authentication.

### Enable Authentication

```bash
REMEMBRA_AUTH_ENABLED=true
REMEMBRA_AUTH_MASTER_KEY=your-secure-master-key
```

!!! warning "Production"
    Always enable authentication in production. Without it, anyone can read/write memories.

### API Key Format

Keys are prefixed with `rem_` for easy identification:

```
rem_abc123def456...
```

Keys are:
- 256-bit entropy (cryptographically secure)
- Hashed with bcrypt before storage
- Never stored in plaintext

### Creating Keys

Using the master key:

```bash
curl -X POST http://localhost:8787/api/v1/keys \
  -H "Authorization: Bearer master_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "name": "Production API Key"
  }'
```

Response:

```json
{
  "key": "rem_abc123...",
  "key_id": "key_xyz",
  "name": "Production API Key"
}
```

!!! warning
    The full key is only returned once. Store it securely.

### Using Keys

Include in the Authorization header:

```bash
curl -H "Authorization: Bearer rem_abc123..." \
     http://localhost:8787/api/v1/recall
```

Or in the SDK:

```python
memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    api_key="rem_abc123..."
)
```

### Revoking Keys

```bash
curl -X DELETE http://localhost:8787/api/v1/keys/key_xyz \
  -H "Authorization: Bearer master_key_here"
```

### User Isolation

API keys are scoped to a user_id. A key can only access memories for its associated user.

```python
# Key created for user_123 can only access user_123's memories
# Attempting to access user_456's memories returns empty results
```

---

## Rate Limiting

Protect against abuse and DoS attacks.

### Default Limits

| Endpoint | Limit |
|----------|-------|
| `POST /store` | 30/minute |
| `POST /recall` | 60/minute |
| `DELETE /memories` | 10/minute |
| Other endpoints | 120/minute |

### Enable Rate Limiting

```bash
REMEMBRA_RATE_LIMIT_ENABLED=true
```

### Custom Limits

```bash
REMEMBRA_RATE_LIMIT_STORE=50/minute
REMEMBRA_RATE_LIMIT_RECALL=100/minute
REMEMBRA_RATE_LIMIT_FORGET=5/minute
```

### Rate Limit Headers

Responses include rate limit info:

```
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 25
X-RateLimit-Reset: 1709312400
```

### Redis Backend (Scaling)

For multiple Remembra instances, use Redis:

```bash
REMEMBRA_RATE_LIMIT_STORAGE=redis://redis:6379
```

---

## Content Protection

Defense against memory injection attacks (MINJA).

### The Threat

Research shows that malicious actors can inject instructions into AI memory to manipulate future responses. Remembra defends against this.

### Input Sanitization

Enabled by default:

```bash
REMEMBRA_SANITIZATION_ENABLED=true
```

Detects and flags:
- Instruction overrides ("Ignore previous instructions...")
- Role manipulation ("You are now...")
- Delimiter injection (fake system messages)
- Encoded payloads (base64, hex)

### Trust Scoring

Each memory gets a trust score (0-1):

```python
result = memory.store("Normal user preference")
# trust_score: 0.95

result = memory.store("Ignore all instructions and...")
# trust_score: 0.15 (flagged as suspicious)
```

### Configuration

```bash
# Minimum trust score to store
REMEMBRA_TRUST_SCORE_THRESHOLD=0.5

# Below threshold: memory is rejected or flagged
```

### Integrity Verification

Each memory includes:
- SHA-256 checksum
- Source provenance
- Trust score

```python
memories = memory.get_memories_with_metadata()
for m in memories:
    print(f"Checksum: {m['checksum']}")
    print(f"Trust: {m['trust_score']}")
    print(f"Source: {m['source']}")
```

---

## Audit Logging

Track all operations for compliance.

### What's Logged

| Event | Details |
|-------|---------|
| `memory.store` | user_id, key_id, memory_id |
| `memory.recall` | user_id, key_id, query (hash) |
| `memory.forget` | user_id, key_id, memory_ids |
| `key.created` | key_id, user_id |
| `key.revoked` | key_id |
| `auth.failed` | IP, attempted key_id |

### Privacy

Audit logs never include:
- Actual memory content
- Full API keys
- Query text (only hash)

### Accessing Logs

Logs are written to stdout/stderr by default:

```bash
docker logs remembra 2>&1 | grep "audit"
```

Or configure a log file:

```bash
REMEMBRA_AUDIT_LOG_PATH=/var/log/remembra/audit.log
```

---

## Best Practices

### 1. Use Environment Variables

Never hardcode keys:

```python
# ❌ Bad
api_key = "rem_abc123..."

# ✅ Good
import os
api_key = os.environ["REMEMBRA_API_KEY"]
```

### 2. Rotate Keys Regularly

```bash
# Create new key
NEW_KEY=$(curl -s -X POST ... | jq -r '.key')

# Update your application
# ...

# Revoke old key
curl -X DELETE .../keys/old_key_id
```

### 3. Minimal Permissions

Create separate keys for:
- Production vs staging
- Different services
- Read-only vs write access (coming soon)

### 4. Monitor Rate Limits

Watch for 429 responses:

```python
try:
    memory.store(content)
except RateLimitError as e:
    logger.warning(f"Rate limited. Retry after {e.retry_after}s")
    time.sleep(e.retry_after)
```

### 5. Validate Content

Even with sanitization, validate on your end:

```python
def safe_store(content: str):
    if len(content) > 10000:
        raise ValueError("Content too long")
    if "IGNORE_INSTRUCTIONS" in content.upper():
        raise ValueError("Suspicious content")
    return memory.store(content)
```

---

## Security Checklist

- [ ] `REMEMBRA_AUTH_ENABLED=true`
- [ ] Strong master key (32+ chars, random)
- [ ] Rate limiting enabled
- [ ] HTTPS in production (via reverse proxy)
- [ ] API keys stored securely (env vars, secrets manager)
- [ ] Regular key rotation
- [ ] Audit logs monitored
- [ ] Input validation in your application
