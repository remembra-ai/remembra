# Security Policy

Remembra is built with security as a first-class citizen. AI memory systems handle sensitive context and must be trustworthy by default.

<!-- requires owner: approve the roadmap quarters and the SOC 2 wording here and on landing/security.html together; tests/test_landing_site.py holds the two to the same text -->

The security page at [remembra.dev/security](https://remembra.dev/security) is the source of truth for what Remembra Cloud does and doesn't do yet, the roadmap, and how to report a vulnerability. This file describes the code and matches that page.

## Reporting Vulnerabilities

Report security issues privately to [security@dolphytech.com](mailto:security@dolphytech.com). We aim to reply within 48 hours, keep you updated while we fix it, and credit you when it is fixed if you want. The same contact is in [security.txt](https://remembra.dev/.well-known/security.txt).

Please do **not** open public GitHub issues for security vulnerabilities.

**In scope:**

- remembra.dev, app.remembra.dev, api.remembra.dev and docs.remembra.dev
- The open-source code in this repository, including `remembra-relay`, the MCP server and the installers

**Out of scope:**

- Denial of service, load testing, spam and social engineering
- Services we use but don't run, such as Paddle, Cloudflare and GitHub: report those to them
- Findings with no security impact, such as missing headers on pages without sensitive content

**Safe harbor.** If you make a good-faith effort to follow this policy, we will not take legal action against you or ask anyone else to, and we will treat your research as authorized. In return: use only accounts you own or have permission to test, look at no more of anyone else's data than you need to show the problem, and delete it afterwards; don't degrade the service; and give us 90 days to fix an issue before you publish it, or tell us if you need to publish sooner. We don't run a paid bug bounty.

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

### OWASP ASI06 (Memory and Context Poisoning)

ASI06 in the OWASP Top 10 for Agentic Applications is partly covered, not solved: a key with write access can still store a false note. The full mapping is on [remembra.dev/security](https://remembra.dev/security#owasp). What the code does:

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

## Deletion and Export

### Deleting Data

On Remembra Cloud, deleting an account in the dashboard cancels any subscription at once and ends every session; 7 days later everything the account holds is erased automatically. Until then, email support@remembra.dev to undo it. Backups are not edited; their copies age out (24 hours of continuous backup history, and the pre-deploy database copy until 3 newer deploys replace it). On a self-hosted server, an administrator can do it through the API.

Delete all of a user's memories:

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

## What We Don't Do Yet

Most security questionnaires ask about these. The honest answers, the same as on [remembra.dev/security](https://remembra.dev/security#not-yet):

- **No SOC 2 report, and no outside penetration test.** Remembra has had internal security reviews only. Cloudflare and Paddle have their own audits; Hetzner's ISO 27001 certificate covers its European data centers, not the US site Remembra runs in. Remembra itself is not audited.
- **No EU data region.** Remembra Cloud is hosted in the United States only.
- **The metadata database is not field-encrypted.** Encryption at rest covers the vector store; the database that holds account records and a second copy of your notes does not have field-level encryption yet. One encryption key protects the vector store, and it is not rotated yet.
- **API keys don't expire.** You revoke them yourself. An agent-scoped key fixes who a handoff is from, but it can read everything the account can; limit reads with project-scoped keys.
- **No SSO or SAML** for the dashboard.
- **Backups are not edited when an account is erased.** A deleted account is erased automatically 7 days later; the copies of it in backups age out instead (see [retention](https://remembra.dev/security#retention)).

Earlier versions of this file gave target dates for SOC 2 Type I and Type II, a HIPAA BAA and ISO 27001. Those dates are withdrawn. Remembra has no certification and does not offer a HIPAA BAA today.

## Roadmap

| What | When |
|------|------|
| Account erasure that also reaches backups (today backup copies age out) | Q4 2026 |
| Key expiry, key rotation, and read limits for agent-scoped keys | Q1 2027 |
| Field-level encryption for the metadata database, with key rotation | Q1 2027 |
| SSO (SAML and OIDC) for Team plans | Q2 2027 |
| An outside penetration test, with a summary published here | Q2 2027 |
| EU data region; SOC 2 | When customers need them; no date yet |

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
- **General questions:** [support@remembra.dev](mailto:support@remembra.dev)
- **Security page:** [remembra.dev/security](https://remembra.dev/security)
- **Documentation:** [docs.remembra.dev](https://docs.remembra.dev)
