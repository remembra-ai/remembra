# Security Policy

Remembra is built with security as a first-class citizen. AI memory systems handle sensitive context and must be trustworthy by default.

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly:

- **Email:** [security@dolphytech.com](mailto:security@dolphytech.com)
- **Response time:** Within 48 hours
- **Disclosure:** We follow coordinated disclosure practices

Please do **not** open public GitHub issues for security vulnerabilities.

---

## Installing a release you can check

`remembra-relay` runs as a session hook on your machine, so treat an upgrade
like any other code you run. Install an exact version rather than "latest":

```bash
pipx install 'remembra==0.16.0'            # CLI + relay hooks
uvx 'remembra-mcp==0.16.0'                 # MCP server, as the MCP Registry entry runs it
```

How releases are built (`.github/workflows/release.yml`): one `v*` tag builds
the wheels once, a maintainer approves the `release` environment, and the same
files go to PyPI through trusted publishing (no stored PyPI token). Every
GitHub Action in the workflows is pinned to a commit SHA.

Releases published by that workflow carry PEP 740 attestations on PyPI. To
check that a wheel was built from this repository's release workflow:

```bash
pipx run pypi-attestations verify pypi \
  --repository https://github.com/remembra-ai/remembra \
  pypi:remembra-0.16.0-py3-none-any.whl
```

The Docker image is pushed with build provenance and an SBOM:

```bash
docker buildx imagetools inspect remembra/remembra:v0.16.0 --format '{{ json .Provenance }}'
```

Versions published before this workflow (0.13.2 and earlier) have no
attestations.

---

## Security Architecture

### Defense in Depth

Remembra implements multiple independent security layers. No single point of failure compromises the system.

```
┌─────────────────────────────────────────────────────┐
│                   Rate Limiting                      │
│              (slowapi, per-endpoint)                 │
├─────────────────────────────────────────────────────┤
│               Authentication & RBAC                  │
│       (API keys, JWT, 3 roles, 12 permissions)      │
├─────────────────────────────────────────────────────┤
│              Input Sanitization                      │
│    (26 prompt injection patterns, trust scoring)     │
├─────────────────────────────────────────────────────┤
│              PII Detection & Redaction               │
│     (13 pattern types, detect/redact/block modes)   │
├─────────────────────────────────────────────────────┤
│      Encryption at Rest (partial — see below)       │
│   (AES-256-GCM on vector-store content/metadata)    │
├─────────────────────────────────────────────────────┤
│              Anomaly Detection                       │
│    (rate anomalies, source anomalies, bulk ops)     │
├─────────────────────────────────────────────────────┤
│               Audit Logging                          │
│     (11 event types, IP tracking, user scoping)     │
├─────────────────────────────────────────────────────┤
│              Data Isolation                          │
│      (user_id + project_id scoping, IDOR checks)   │
└─────────────────────────────────────────────────────┘
```

---

## Encryption

### Encryption at Rest (AES-256-GCM) — current scope

When `REMEMBRA_ENCRYPTION_KEY` is set, Remembra applies AES-256-GCM
field-level encryption to a **limited set of fields**. Be precise about what
is and is not covered:

| Data | Where | Encrypted with `REMEMBRA_ENCRYPTION_KEY`? |
|------|-------|-------------------------------------------|
| Memory `content` | Qdrant point payload | Yes |
| Memory `metadata` | Qdrant point payload | Yes |
| Memory `extracted_facts`, entity refs | Qdrant point payload | **No** |
| Memory `content`, `extracted_facts`, `metadata` | SQLite `memories` / `archived_memories` | **No** (plaintext) |
| Keyword index | SQLite `memories_fts` (FTS5) | **No** (plaintext) |
| Entities, relationships, communities | SQLite | **No** (plaintext) |
| TOTP 2FA secrets | SQLite `users` | Yes (key: `REMEMBRA_ENCRYPTION_KEY`, else derived from the JWT secret) |
| API keys, passwords, reset tokens | SQLite | Hashed (bcrypt / SHA-256), not reversible |
| Embedding vectors | Qdrant | No |

In other words, **the SQLite database (and any backup or Litestream replica
of it) contains memory text in plaintext** even when the encryption key is
set. Protect it with volume/disk-level encryption and restricted access to
the host, the data volume, and backup storage.

Credentials are additionally never persisted in memory text: API keys, tokens,
private keys and passwords are replaced with `[REDACTED:<kind>]` on every write
and read path (`REMEMBRA_SECRET_REDACTION_ENABLED`, default on). Existing rows
can be backfilled with `scripts/maintenance/redact_stored_secrets.py`.

Field encryption details (where applied):

- **Algorithm:** AES-256-GCM (Galois/Counter Mode)
- **Key derivation:** PBKDF2-HMAC-SHA256 with 480,000 iterations
- **Nonce:** 96-bit random nonce per encryption operation
- **Authentication:** GCM tag detects tampering

**Configuration:**

```bash
# Enable field encryption for vector-store payloads and TOTP secrets
REMEMBRA_ENCRYPTION_KEY=your-256-bit-key-here

# Generate a secure key:
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Extending field-level encryption to SQLite (content, facts, FTS) is an open
follow-up; until then volume-level encryption is the control for SQLite data.

### Encryption in Transit

- All API communications support TLS 1.2+ (enforced via reverse proxy)
- HSTS header with 1-year max-age in production
- Webhook deliveries use HMAC-SHA256 signatures (`X-Remembra-Signature`)

---

## Authentication & Access Control

### API Key Security

- **Entropy:** 256-bit cryptographically secure (`secrets.token_urlsafe(32)`)
- **Format:** `rem_` prefix for identification
- **Storage:** bcrypt-hashed with unique salt — never stored in plaintext
- **Rotation:** Keys can be revoked and regenerated without downtime

### Role-Based Access Control (RBAC)

Three built-in roles with 12 granular permissions:

| Role | Permissions | Use Case |
|------|-------------|----------|
| **Admin** | All 12 permissions | System administrators |
| **Editor** | store, recall, delete, entity read, webhook manage | Application backends |
| **Viewer** | recall, entity read, key list | Analytics, dashboards |

Permissions can be customized per API key with scope overrides and project-level restrictions.

### Two-Factor Authentication (2FA)

Optional TOTP-based 2FA for dashboard access:

- TOTP implementation via `pyotp`
- QR code provisioning for authenticator apps
- 30-second window with ±1 tolerance

### JWT Tokens

- Algorithm: HS256
- Expiration: 7 days (configurable)
- Minimum secret length: 32 characters (enforced in production)
- Token blacklist for secure logout

---

## PII Detection & Redaction

Built-in detection of 13 sensitive data patterns with configurable handling modes.

### Detected Patterns

| Type | Severity | Example |
|------|----------|---------|
| SSN | Critical | `123-45-6789` |
| Credit Card | Critical | `4111-1111-1111-1111` |
| API Key/Secret | Critical | `sk_live_abc123...` |
| AWS Access Key | Critical | `AKIA...` (16 chars) |
| Passport (US) | High | `A12345678` |
| Bank Account | High | 8-17 digit numbers |
| Driver's License | High | `A` + 6-8 digits |
| Email | Medium | `user@example.com` |
| Phone (US) | Medium | `(555) 123-4567` |
| Phone (International) | Medium | `+44 20 7123 4567` |
| Date of Birth | Medium | `MM/DD/YYYY` |
| IPv4 Address | Low | `192.168.1.1` |

### Handling Modes

| Mode | Behavior | Recommended For |
|------|----------|-----------------|
| `detect` | Log warning, allow storage | Development, auditing |
| `redact` | Replace with `[REDACTED_TYPE]` | **Production (recommended)** |
| `block` | Reject the request entirely | High-security / regulated |

```bash
REMEMBRA_PII_DETECTION_ENABLED=true
REMEMBRA_PII_MODE=redact
```

---

## Content Protection

### Prompt Injection Defense

The content sanitizer detects 26 prompt injection patterns across 6 attack categories:

| Category | Weight | Examples |
|----------|--------|---------|
| Instruction Override | 0.35 | "ignore previous instructions" |
| Role Manipulation | 0.25-0.30 | "you are now...", "act as if..." |
| Prompt Extraction | 0.30 | "show me your instructions" |
| Delimiter Injection | 0.20-0.30 | Fake `[SYSTEM]` or `<\|im_start\|>` tags |
| Output Manipulation | 0.15 | "output only...", "respond with only..." |
| Memory Manipulation | 0.20-0.25 | "insert into memory", "remember that you must" |

Each memory receives a **trust score** (0.0-1.0). Content below the threshold (default: 0.5) is flagged as suspicious. A SHA-256 checksum is stored for integrity verification.

### OWASP ASI06 Compliance

Remembra addresses the OWASP AI Security Initiative guideline ASI06 (Memory Poisoning):

- PII detection prevents sensitive data exfiltration via memory
- Anomaly detection identifies injection/poisoning attempts
- Rate limiting prevents bulk poisoning
- Trust scoring flags manipulative content
- Audit logging enables forensic analysis

---

## Anomaly Detection

Real-time monitoring for abuse patterns:

| Check | Threshold | Severity |
|-------|-----------|----------|
| High memory acquisition rate | 100/hour (configurable) | Warning → Critical at 2x |
| Source distribution anomaly | >90% from single suspicious source | Warning |
| Bulk deletion | >50 deletions/hour | Warning |

Anomalies are logged and can trigger webhook alerts for external monitoring.

---

## Audit Logging

Complete audit trail of all operations. **Content is never logged** — only IDs and metadata.

### Tracked Events

| Event | Description |
|-------|-------------|
| `MEMORY_STORE` | Memory created |
| `MEMORY_RECALL` | Search query executed |
| `MEMORY_FORGET` | Memory deleted |
| `MEMORY_GET` | Memory retrieved by ID |
| `KEY_CREATED` | API key generated |
| `KEY_UPDATED` | API key modified |
| `KEY_REVOKED` | API key deactivated |
| `AUTH_SUCCESS` | Successful authentication |
| `AUTH_FAILED` | Failed authentication attempt |
| `AUTH_RATE_LIMITED` | Rate limit triggered |

### Stored Fields

- Event ID, timestamp (UTC), user ID
- Action type, resource ID
- API key ID (masked), client IP address
- Success/failure status, error message

---

## Data Isolation & Multi-Tenancy

### Strict Scoping

Every query is filtered by `user_id` at the database level:

- **Memories:** `WHERE user_id = ? AND project_id = ?`
- **Entities:** Scoped to user and project
- **Audit logs:** Per-user indexed queries
- **API keys:** Bound to user, can be restricted to specific projects

### IDOR Prevention

Before any mutation (update, delete), ownership is verified:

```python
memory = await self.db.get_memory(memory_id)
if memory.get("user_id") != current_user.user_id:
    return ForgetResponse(deleted_memories=0)  # Silent deny
```

---

## Rate Limiting

Protection against abuse and denial-of-service:

| Endpoint | Default Limit |
|----------|--------------|
| `POST /memories` (store) | 30/minute |
| `POST /memories/recall` | 60/minute |
| `DELETE /memories` | 10/minute |
| Health / other endpoints | 120/minute |

Rate limits are keyed by API key (preferred) or client IP address. Supports in-memory or Redis-backed storage for distributed deployments.

---

## Network Security

### Security Headers (All Responses)

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
Strict-Transport-Security: max-age=31536000; includeSubDomains  (production)
```

### CORS

- Configurable allowed origins
- Localhost origins automatically removed in production
- Credentials support enabled

### Webhook Security

- HMAC-SHA256 signed deliveries
- Signature header: `X-Remembra-Signature: sha256={signature}`
- Configurable timeout (10s) and retries (3 attempts)

---

## Docker Security

- **Non-root user:** Runs as `remembra:remembra`
- **Multi-stage build:** Minimal production image
- **Health checks:** Built-in `HEALTHCHECK` with 30s interval
- **No secrets in image:** All credentials via environment variables
- **Production validation:** Raises `RuntimeError` if default JWT secret is used

---

## GDPR Compliance

### Right to Be Forgotten

Complete user data deletion via API:

```
DELETE /api/v1/memories?all_memories=true
```

Or full account deletion (admin):

```
DELETE /api/v1/admin/users/{user_id}?confirm=true
```

Full deletion covers: memories, entities, relationships, API keys, audit logs, cloud records, usage data, tokens, and the user account itself.

### Data Portability

Export all memories in JSON, CSV, or JSONL format via the admin API.

### Data Minimization

- TTL-based automatic expiration
- Temporal decay removes stale memories
- No training on user data — ever

---

## Compliance Roadmap

### Current (v0.8.0)

- [x] PII detection and redaction (13 patterns, 3 modes)
- [x] RBAC with 3 roles and 12 granular permissions
- [x] Full audit logging (11 event types)
- [x] Anomaly detection (3 check types)
- [x] Content sanitization (26 injection patterns)
- [~] Encryption at rest — partial: AES-256-GCM on Qdrant content/metadata payloads and TOTP secrets; SQLite memory text is plaintext (use volume encryption)
- [x] Encryption in transit (TLS 1.2+)
- [x] GDPR-compliant deletion
- [x] OWASP ASI06 (Memory Poisoning) compliance
- [x] Security headers (HSTS, CSP, X-Frame-Options)
- [x] Rate limiting per endpoint
- [x] 2FA/TOTP support

### Planned (2026)

| Certification | Target | Status |
|--------------|--------|--------|
| SOC 2 Type I | Q3 2026 | Preparing controls documentation |
| SOC 2 Type II | Q4 2026 | Audit period begins after Type I |
| HIPAA BAA | Q4 2026 | Available for healthcare customers |
| ISO 27001 | 2027 | Scoping phase |

### In Progress

- [ ] SOC 2 readiness assessment
- [ ] Formal security policy documentation
- [ ] Penetration testing (annual)
- [ ] Bug bounty program
- [ ] SBOM (Software Bill of Materials) generation
- [ ] Dependency vulnerability scanning (CI/CD)

---

## Security Checklist for Self-Hosted Deployments

```
[x] Set REMEMBRA_AUTH_ENABLED=true
[x] Generate strong master key (32+ chars, random)
[x] Set unique REMEMBRA_JWT_SECRET (32+ chars)
[x] Set REMEMBRA_ENCRYPTION_KEY (encrypts Qdrant content/metadata payloads + TOTP secrets)
[x] Encrypt the data volume holding the SQLite database and its backups (memory text is plaintext in SQLite)
[x] Enable rate limiting (REMEMBRA_RATE_LIMIT_ENABLED=true)
[x] Set PII mode to redact or block (REMEMBRA_PII_MODE=redact)
[x] Enable anomaly detection
[x] Configure HTTPS via reverse proxy (nginx, Caddy, etc.)
[x] Use non-default ports if exposed to internet
[x] Restrict CORS origins to your domains
[x] Enable audit logging
[x] Set up webhook alerts for anomalies
[x] Regular key rotation schedule
```

---

## Architecture Decisions

### Field-Level Encryption Scope

Field-level encryption currently covers the vector-store payload (content and
metadata) and TOTP secrets only. It travels with Qdrant snapshots and keeps
those payloads opaque to anyone with raw Qdrant access. It does **not** cover
the SQLite database, FTS index, extracted facts or entity graph, so it is not
a substitute for disk/volume encryption of the SQLite data and its backups.

### Why bcrypt for API Keys

API keys are hashed with bcrypt (not SHA-256) because:

1. **Brute-force resistance:** bcrypt is intentionally slow (~100ms per hash)
2. **Salt uniqueness:** Each key gets its own random salt
3. **Adaptive cost:** Can increase work factor over time

### Why Separate Trust Scoring

Content trust scores are stored alongside memories (not used as a gate) because:

1. **Auditability:** Administrators can review flagged content
2. **Tuning:** Threshold can be adjusted without re-processing
3. **False positives:** Legitimate content that triggers patterns isn't lost

---

## Contact

- **Security reports:** [security@dolphytech.com](mailto:security@dolphytech.com)
- **General questions:** [support@dolphytech.com](mailto:support@dolphytech.com)
- **Documentation:** [remembra.dev/docs](https://remembra.dev/docs)
