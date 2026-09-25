# Security Features

**NEW in v0.7.1** — PII detection and anomaly monitoring for safer AI memory operations.

Remembra includes production-grade security features to protect your AI memory system from attacks and data leaks.

## Overview

| Feature | Purpose | OWASP Reference |
|---------|---------|-----------------|
| PII Detection | Prevent sensitive data storage | ASI06 (Memory Poisoning) |
| Anomaly Detection | Detect abuse patterns | ASI06 (Memory Poisoning) |
| Audit Logging | Complete operation trail | General Security |

## PII Detection

Automatically scan content for Personally Identifiable Information before storage.

### Supported PII Types

| Type | Pattern | Example |
|------|---------|---------|
| `ssn` | Social Security Number | `123-45-6789` |
| `credit_card` | Credit Card Numbers | `4111-1111-1111-1111` |
| `email` | Email Addresses | `john@example.com` |
| `phone_us` | US Phone Numbers | `(555) 123-4567` |
| `phone_intl` | International Phone | `+44 20 7123 4567` |
| `api_key` | API Keys/Secrets | `sk_...` |
| `aws_key` | AWS Access Keys | `AKIA...` |
| `ip_address` | IP Addresses | `192.168.1.1` |

### Modes

Configure how Remembra handles detected PII:

```bash
# Mode options: detect | redact | block
REMEMBRA_PII_MODE=redact
```

| Mode | Behavior | Use Case |
|------|----------|----------|
| `detect` | Log warning, allow storage | Development, auditing |
| `redact` | Replace with `[REDACTED_TYPE]` | Production (recommended) |
| `block` | Reject the request | High-security environments |

### Configuration

```bash
# Enable PII detection
REMEMBRA_PII_ENABLED=true

# Set mode
REMEMBRA_PII_MODE=redact

# Exclude specific types (comma-separated)
REMEMBRA_PII_EXCLUDE=email,ip_address
```

### API

```http
POST /api/v1/memories
Content-Type: application/json

{
  "content": "Call me at 555-123-4567, my SSN is 123-45-6789",
  "user_id": "user_123"
}
```

**With `redact` mode:**
```json
{
  "id": "mem_abc123",
  "content": "Call me at [REDACTED_PHONE_US], my SSN is [REDACTED_SSN]",
  "pii_detected": ["phone_us", "ssn"],
  "pii_redacted": true
}
```

**With `block` mode:**
```json
{
  "error": "PII_DETECTED",
  "message": "Content contains sensitive information",
  "types": ["phone_us", "ssn"]
}
```

### Python SDK

```python
from remembra import Memory

memory = Memory(user_id="user_123")

# PII is automatically handled based on server config
result = await memory.store("My SSN is 123-45-6789")

# Check if PII was detected
if result.pii_detected:
    print(f"PII types found: {result.pii_types}")
    print(f"Stored content: {result.content}")
    # → "My SSN is [REDACTED_SSN]"
```

---

## Anomaly Detection

Monitor for unusual patterns that could indicate attacks or abuse.

### Detected Anomalies

| Type | Description | Severity |
|------|-------------|----------|
| `high_acquisition_rate` | Too many memories stored per hour | Warning |
| `source_anomaly` | Unusual source distribution | Warning |
| `topic_shift` | Sudden topic changes | Info |
| `suspicious_pattern` | Known attack patterns | Critical |
| `bulk_extraction` | Rapid recall requests | Warning |

### Configuration

```bash
# Enable anomaly detection
REMEMBRA_ANOMALY_DETECTION=true

# Memories per hour threshold
REMEMBRA_ANOMALY_RATE_LIMIT=100

# Action on critical anomaly: log | alert | block
REMEMBRA_ANOMALY_ACTION=alert
```

### Thresholds

| Metric | Default Threshold | Severity |
|--------|-------------------|----------|
| Store rate | 100/hour | Warning at 80%, Critical at 150% |
| Recall rate | 500/hour | Warning at 80%, Critical at 200% |
| Unique sources | 20/hour | Warning if >50 new sources |
| Topic entropy | 0.8 | Warning if sudden shift |

### API

Check anomaly status:

```http
GET /api/v1/admin/anomalies?user_id=user_123
```

```json
{
  "user_id": "user_123",
  "checked_at": "2026-03-03T12:00:00Z",
  "has_anomalies": true,
  "anomalies": [
    {
      "detected": true,
      "type": "high_acquisition_rate",
      "severity": "warning",
      "message": "User stored 85 memories in the last hour (threshold: 100)",
      "details": {
        "current_rate": 85,
        "threshold": 100,
        "window": "1h"
      }
    }
  ],
  "critical_count": 0,
  "warning_count": 1
}
```

### Webhook Alerts

Subscribe to anomaly events:

```json
{
  "event": "anomaly.detected",
  "data": {
    "user_id": "user_123",
    "type": "high_acquisition_rate",
    "severity": "critical",
    "timestamp": "2026-03-03T12:00:00Z"
  }
}
```

### Python SDK

```python
from remembra import Memory

memory = Memory(user_id="user_123")

# Check for anomalies
report = await memory.check_anomalies()

if report.has_anomalies:
    for anomaly in report.anomalies:
        if anomaly.severity == "critical":
            print(f"CRITICAL: {anomaly.message}")
```

---

## Audit Logging

Complete audit trail of all memory operations.

### Logged Events

- Memory created/updated/deleted
- Recall queries
- Entity operations
- Webhook deliveries
- Admin actions
- Authentication events

### Configuration

```bash
# Enable audit logging
REMEMBRA_AUDIT_ENABLED=true

# Log level: minimal | standard | verbose
REMEMBRA_AUDIT_LEVEL=standard

# Retention (days)
REMEMBRA_AUDIT_RETENTION=90
```

### API

```http
GET /api/v1/admin/audit?user_id=user_123&limit=100
```

```json
{
  "entries": [
    {
      "id": "audit_abc123",
      "timestamp": "2026-03-03T12:00:00Z",
      "action": "memory.created",
      "user_id": "user_123",
      "resource_id": "mem_xyz789",
      "ip_address": "192.168.1.1",
      "details": { ... }
    }
  ],
  "total": 1250,
  "has_more": true
}
```

---

## MCP / Tool Security

Model Context Protocol (MCP) servers are *tools* — treat them like running code.

**Recommendations:**

- Only connect to MCP servers you trust (avoid “random marketplace” servers in production).
- Run MCP servers with least privilege (no shell access, minimal filesystem access, least network access).
- Prefer allowlists over blocklists for any server that can execute commands or reach internal systems.
- Assume prompt injection can trigger tool use; require explicit user confirmation for risky tools/actions.

If you expose Remembra via MCP (`remembra-mcp`), keep the Remembra API key scoped (RBAC + project scoping) and avoid sharing keys across unrelated projects.

---

## MCP Hardening (Supply Chain + Prompt Injection)

If you use Remembra via MCP clients (IDE agents, desktop chat apps, etc.), treat MCP server installation/configuration as **high risk**. Recent ecosystem advisories have shown that attacker-controlled content can sometimes lead to **malicious MCP STDIO server registration** and then local command execution, depending on the client and its configuration flow.

References:

- OX Security advisory: [MCP Supply Chain Advisory](https://www.ox.security/blog/mcp-supply-chain-advisory-rce-vulnerabilities-across-the-ai-ecosystem/)
- NVD example CVE: [CVE-2026-30615](https://nvd.nist.gov/vuln/detail/CVE-2026-30615)
- MCP spec (Security & Trust & Safety): [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25)

Recommended mitigations:

1. **Only install trusted MCP servers** (treat registry entries like packages: verify publisher, pin versions, review changes).
2. **Prefer direct binaries over shell wrappers** (avoid `bash -lc ...` style commands where possible).
3. **Pin commands to known paths** and avoid dynamic command strings or environment interpolation that changes behavior unexpectedly.
4. **Run MCP servers with least privilege** (separate user, minimal filesystem access, no unnecessary secrets).
5. **Use a local bridge/proxy** when working in sandboxed clients so API keys don’t need to live inside restricted tools.


### Export

```http
POST /api/v1/admin/audit/export
```

```json
{
  "user_id": "user_123",
  "start_date": "2026-03-01",
  "end_date": "2026-03-03",
  "format": "json"  // json | csv
}
```

---

## OWASP ASI Alignment

Remembra includes controls that align with the following OWASP AI Security Initiative guidance:

### ASI06: Memory Poisoning

**Threat:** Malicious data injection to corrupt AI memory.

**Remembra Mitigations:**
- ✓ PII detection prevents sensitive data storage
- ✓ Anomaly detection identifies injection attempts
- ✓ Rate limiting prevents bulk poisoning
- ✓ Audit logging enables forensic analysis

### Implementation Checklist

```markdown
[x] PII detection enabled (mode: redact)
[x] Anomaly detection enabled (action: alert)
[x] Rate limiting configured
[x] Audit logging enabled
[x] Webhook alerts configured
[x] Regular security reviews scheduled
```

---

## Encryption at Rest

AES-256-GCM field-level encryption is available, **with a limited scope**.

### What is encrypted

With `REMEMBRA_ENCRYPTION_KEY` set:

- Memory `content` and `metadata` in the **Qdrant** point payload
- TOTP 2FA secrets in SQLite

### What is not encrypted

- Memory `content`, `extracted_facts` and `metadata` in **SQLite**
  (`memories`, `archived_memories`) — stored in plaintext
- The SQLite FTS5 keyword index (`memories_fts`)
- Entities, relationships and communities
- `extracted_facts` and entity refs in the Qdrant payload
- Embedding vectors

The SQLite database and its backups (e.g. Litestream replicas) therefore hold
memory text in plaintext. Use volume/disk encryption for the data volume and
backup storage, and restrict host access.

Credentials are never stored in memory text regardless of encryption: API
keys, tokens, private keys and passwords are replaced with
`[REDACTED:<kind>]` on write and on read. See
`scripts/maintenance/redact_stored_secrets.py` to backfill existing data.

### Enable field encryption

```bash
# Generate a secure key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Set in environment
REMEMBRA_ENCRYPTION_KEY=your-generated-key-here
```

- **Algorithm:** AES-256-GCM (authenticated encryption)
- **Key derivation:** PBKDF2-HMAC-SHA256 with 480,000 iterations
- **Nonce:** 96-bit random nonce per operation

Enabling it is backwards-compatible: new Qdrant payloads are encrypted and
existing plaintext payloads are still read (auto-detected).

Encryption requires the `cryptography` package:

```bash
pip install "remembra[encryption]"
```

!!! warning "Production"
    Set `REMEMBRA_ENCRYPTION_KEY` **and** encrypt the volume that holds the
    SQLite database. The key alone does not protect SQLite data.

---

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
rem_...
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

### Using Keys

Include in the Authorization header:

```bash
curl -H "Authorization: Bearer $REMEMBRA_API_KEY" \
     http://localhost:8787/api/v1/recall
```

Or in the SDK:

```python
import os

memory = Memory(
    base_url="http://localhost:8787",
    user_id="user_123",
    api_key=os.environ["REMEMBRA_API_KEY"]
)
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

---

## Content Protection

Defense against memory injection attacks (MINJA).

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

---

## Security Checklist

```markdown
[x] REMEMBRA_AUTH_ENABLED=true
[x] Strong master key (32+ chars, random)
[x] Rate limiting enabled
[x] HTTPS in production (via reverse proxy)
[x] PII detection enabled (mode: redact)
[x] Anomaly detection enabled (action: alert)
[x] Audit logging enabled
[x] Webhook alerts configured
```

## Related

- [RBAC](./rbac.md) — Role-based access control
- [Webhooks](./webhooks.md) — Event notifications
