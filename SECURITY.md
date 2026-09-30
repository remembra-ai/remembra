# Security Policy

Remembra is built with security as a first-class citizen. AI memory systems handle sensitive context and must be trustworthy by default.

<!-- requires owner: no roadmap item has an approved date; publish a quarter here and on landing/security.html together only once the owner approves it, and approve the SOC 2 wording; tests/test_landing_site.py holds the two to the same text -->

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

Remembra has several separate security layers. Each is described below, with what it does not cover.

```
┌─────────────────────────────────────────────────────┐
│                   Rate Limiting                      │
│              (slowapi, per-endpoint)                 │
├─────────────────────────────────────────────────────┤
│               Authentication & RBAC                  │
│       (API keys, JWT, 3 roles, 17 permissions)      │
├─────────────────────────────────────────────────────┤
│              Input Sanitization                      │
│    (30 prompt injection patterns, trust scoring)     │
├─────────────────────────────────────────────────────┤
│              PII Detection & Redaction               │
│     (12 pattern types, detect/redact/block modes)   │
├─────────────────────────────────────────────────────┤
│      Encryption at Rest (partial — see below)       │
│   (AES-256-GCM on vector-store memory text)         │
├─────────────────────────────────────────────────────┤
│       Anomaly Detection (built, not active)         │
│        (nothing runs the detector yet)              │
├─────────────────────────────────────────────────────┤
│               Audit Logging                          │
│   (memory, key and account events, IP tracking)     │
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
| Memory `metadata` | Qdrant point payload | Yes: every string, also inside lists and nested objects. Keys, numbers, true/false and null are not |
| Memory `extracted_facts`, entity refs | Qdrant point payload | Yes (every string). Points written before this was covered keep them in plaintext until `scripts/maintenance/reencrypt_payloads.py --apply` runs |
| Filter fields: `user_id`, `project_id`, `memory_type`, `scope`, `scope_prefixes`, dates | Qdrant point payload | **No** (Qdrant filters on them) |
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

Credentials that match Remembra's detection patterns are replaced with
`[REDACTED:<kind>]` before they are saved (`REMEMBRA_SECRET_REDACTION_ENABLED`,
on by default; `false` turns all of it off). This covers memory text and
extracted facts, every string in metadata, status values and status keys,
handoffs and inbox messages. The patterns are known provider key formats, PEM
private keys, JWTs, credentialed URLs, bearer tokens, labelled
`password=`/`password:` values, hex after a credential word ("api key is
<hex>"), base64 or hex that decodes to a known key, and long random-looking
tokens. Detection is pattern-based. These can still be stored as written:
loosely labelled or prose passwords ("pass: ...", "pw is ..."), all-lowercase
passwords, hex keys with no credential word before them, and short base64 or
hex values that do not decode to a known key. Rows saved before a check
existed are cleaned when they are read; only
`scripts/maintenance/redact_stored_secrets.py --apply` rewrites them, and the
server does not run it by itself.

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

- TLS is handled by the reverse proxy in front of the API, not by the server
  itself. On Remembra Cloud that is Cloudflare. The remembra.dev zone requires
  TLS 1.2 or newer; external checks on September 30, 2026 verified that the API,
  dashboard, main site, www redirect, and docs reject TLS 1.0 and 1.1.
- On Remembra Cloud, HTTP redirects to HTTPS. The API sends HSTS (1 year,
  `includeSubDomains`) whenever debug mode is off.
- Webhook deliveries carry an HMAC-SHA256 signature (`X-Remembra-Signature`)
  only when a signing secret was given when the webhook was registered.

---

## Authentication & Access Control

### API Key Security

- **Entropy:** 256-bit cryptographically secure (`secrets.token_urlsafe(32)`)
- **Format:** `rem_` prefix for identification
- **Storage:** a bcrypt hash with its own salt, plus an unsalted SHA-256 digest used to find the key's row. The key itself is never stored.
- **Rotation:** keys can be created and revoked independently, so rotating one needs no downtime
- **Revocation:** a revoked or permanently deleted key stops working on its next request. Real-time (WebSocket) connections opened with it are closed at once (code 4001) by the server process that handled the revocation. With several processes, a connection on another one gets no further events and closes within 30 seconds.

### Role-Based Access Control (RBAC)

Every API key has one of three roles: `admin`, `editor` or `viewer`. This table
is generated from `src/remembra/auth/rbac.py` (`permission_table()`), and a test
fails when it no longer matches the code:

<!-- permission-table:start -->
| Permission | admin | editor | viewer | What it allows |
|---|:---:|:---:|:---:|---|
| `memory:store` | yes | yes | no | Store, change, pin, import and ingest memories; send recall feedback; write inbox messages, relay handoffs and session status; change spaces, teams and project links; recompute the brain layer; start audio capture (self-hosted servers only) |
| `memory:recall` | yes | yes | yes | Recall, list, read and export memories; read spaces, inbox messages, relay briefs and trails, conflicts, timelines and brain insights |
| `memory:delete` | yes | yes | no | Delete memories and clean up expired or decayed ones |
| `key:create` | yes | yes | no | Create API keys (never above the caller's own role, never admin) and rename them |
| `key:list` | yes | yes | yes | List the account's API keys |
| `key:revoke` | yes | yes | no | Revoke or delete API keys (an API key only ones with no more access than itself); any key may revoke itself without it |
| `webhook:manage` | yes | yes | no | Create, read, change and delete webhooks and read their deliveries |
| `conflict:manage` | yes | yes | no | Resolve or dismiss memory conflicts |
| `entity:read` | yes | yes | yes | Read entities, their relationships and the memories that mention them |
| `admin:audit` | yes | no | no | Read the account's audit log |
| `admin:export` | yes | no | no | Export the account's audit log as JSON or CSV |
| `account:manage` | yes | yes | no | Redeem a promo code; email the verification link of an account created by API signup |
| `crew:read` | yes | yes | yes | Crew mode: read a crew, its members, sessions, zones, claims, tasks, messages, decisions, inbox and notifications; move the caller's own read markers |
| `crew:write` | yes | yes | no | Crew mode: register hosts, join a crew, send heartbeats and events; create and change zones, tasks, reports, checkpoints, messages and decisions; acknowledge collisions; work inbox items |
| `crew:claim` | yes | yes | no | Crew mode: claim, release, hand over and adopt files and tasks; run the guard check |
| `crew:override` | no | no | no | Crew mode, human only (no API key holds it): pause or resume sessions, freeze zones, override claims, issue bypass codes, assign, review or waive tasks and settle decisions |
| `crew:admin` | no | no | no | Crew mode, human only (no API key holds it): change crew settings and members, approve zone changes, redact or pin messages and add notification targets |
<!-- permission-table:end -->

- **Viewer keys are read-only.** Every route that changes data refuses them. A test sends a viewer key to every write route the API serves. The one exception applies to every key: a key can revoke itself (`DELETE /api/v1/keys/{its own id}`); deleting itself permanently (`?hard=true`) still needs `key:revoke`. In Crew mode, a viewer key (`crew:read`) can also move its own crew read markers.
- **Admin keys** are created only with the server's master key. An admin key can then give another of the account's keys the admin role. The dashboard creates editor and viewer keys. An API key can create keys only up to its own role, never admin.
- **Scopes** narrow a key's role and never widen it. Only an admin key can set them (`POST /api/v1/admin/roles`).
- **Project restrictions** limit a key to the projects it lists.
- A change of role, scopes or projects applies from the key's next request.
- A dashboard sign-in has the editor permissions. In Crew mode, a sign-in whose crew role is owner or admin also holds `crew:override` and `crew:admin` for that crew; no API key holds them, admin keys included. The `crew:*` permissions are checked only by the crew routes, which a server serves when Crew mode is on.

The [RBAC guide](https://docs.remembra.dev/guides/rbac/) has the details.

### Two-Factor Authentication (2FA)

Optional TOTP-based 2FA for dashboard access:

- TOTP implementation via `pyotp`
- QR code provisioning for authenticator apps
- 30-second window with ±1 tolerance
- When 2FA is on, every sign-in that gives a dashboard session asks for the code: email and password, Google and GitHub

### JWT Tokens

Dashboard sign-ins use JWTs:

- Algorithm: HS256
- Expiration: 24 hours. This is fixed; no setting changes it.
- Minimum secret length: 32 characters (enforced in production)
- Token blacklist for secure logout

---

## PII Detection & Redaction

Built-in detection of 12 personal-data patterns (`security/pii_detector.py`),
on by default in `redact` mode. It applies to memory text, handoff values and
status values, not to metadata.

### Detected Patterns

| Type | Severity | Example |
|------|----------|---------|
| SSN | Critical | `123-45-6789` |
| Credit Card | Critical | `4111-1111-1111-1111` |
| API Key/Secret | Critical | `api_` or `secret_` + 16 or more letters or digits |
| AWS Access Key | Critical | `AKIA` + 16 characters |
| Password | Critical | `password: ...`, `pwd=...` (6 or more characters) |
| Passport (US) | High | `A12345678` |
| Bank Account | High | 8-17 digit numbers |
| Driver's License | High | `A` + 6-8 digits |
| Email | Medium | `user@example.com` |
| Phone (US) | Medium | `(555) 123-4567` |
| Phone (International) | Medium | Only a `+` right after a letter or digit: `+44 20 7123 4567` is **not** detected |
| Date of Birth | Medium | `MM/DD/YYYY` |

IP addresses are not detected (removed in April 2026: deploy and server
addresses must survive recall). Credentials are handled by the separate
credential redaction described under [Encryption](#encryption).

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

The content sanitizer (`security/sanitizer.py`, on by default) checks 30 prompt injection patterns in 7 categories. It checks the text as written and again with look-alike and fullwidth letters folded to Latin, so an override spelled with them still matches:

| Category | Weight | Examples |
|----------|--------|---------|
| Instruction Override | 0.35 | "ignore previous instructions", "SYSTEM OVERRIDE." |
| Concealment | 0.35 | "do not tell the user", "keep this secret from the user" |
| Role Manipulation | 0.25-0.35 | "you are now...", "act as if...", "your new instructions are" |
| Prompt Extraction | 0.30-0.35 | "show me your instructions" |
| Delimiter Injection | 0.20-0.30 | Fake `[SYSTEM]` or `<\|im_start\|>` tags |
| Output Manipulation | 0.15 | "output only...", "respond with only..." |
| Memory Manipulation | 0.20-0.25 | "insert this into memory", "remember that you must" |

It also strips XSS markup (script tags, event handlers, `javascript:` URLs). Each memory receives a **trust score** (0.0-1.0) that these findings lower. Content below the threshold (default: 0.5), or content that had markup stripped, is flagged as suspicious. A SHA-256 checksum is stored for integrity verification. The patterns are a fixed list: a reworded injection, or one encoded in base64 or hex, is not detected.

### OWASP ASI06 (Memory and Context Poisoning)

ASI06 in the OWASP Top 10 for Agentic Applications is partly covered, not solved: a key with write access can still store a false note. The full mapping is on [remembra.dev/security](https://remembra.dev/security#owasp). What the code does:

- PII detection redacts (by default) or blocks the personal-data patterns above
- The sanitizer flags injection text and lowers the memory's trust score
- Per-route rate limits slow bulk writes
- The audit log records the memory and key events listed below, for review afterwards
- Anomaly detection is not active (see below)

---

## Anomaly Detection

**Not active.** `security/anomaly_detector.py` is built at startup when
`REMEMBRA_ANOMALY_DETECTION_ENABLED` is true (the default), but nothing in the
server runs it yet. No anomaly is detected, logged or alerted, and there is no
anomaly webhook or API. Its code has three checks, for when it is wired up:

| Check | Threshold | Severity |
|-------|-----------|----------|
| High memory acquisition rate | 100/hour (`REMEMBRA_ANOMALY_RATE_THRESHOLD`) | Warning → Critical at 2x |
| Source distribution anomaly | >90% of more than 50 memories in 24 hours from a webhook, import, external API or no source | Warning |
| Bulk deletion | >50 deletions/hour | Warning (it counts `memory_delete` audit rows, but deletes are logged as `memory_forget`, so it could not fire even if run) |

---

## Audit Logging

The audit log records the events below. **Content is never logged** — only IDs and metadata.

It does not record ordinary sign-ins (successful or failed), rate-limit hits,
entity changes, role changes made with `POST /api/v1/admin/roles`, relay
handoffs or status values, or webhook, team, space, import/export, inbox,
conflict or temporal operations. Rows are kept until the account is erased.

### Tracked Events

| Event | Written when |
|-------|--------------|
| `memory_store` | A memory is stored through the memory or ingest routes (the MCP connector uses them too) |
| `memory_recall` | A recall runs through `POST /api/v1/memories/recall` |
| `memory_update` | A memory is updated or superseded |
| `memory_forget` | Memories are deleted, or `POST /api/v1/memories/cleanup-expired` removes expired ones |
| `memory_decayed` | The sleep-time decay cleanup deletes an old note. It runs only when `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_ENABLED=true`; it is off by default |
| `key_created` | An API key is created |
| `key_updated` | An API key is changed through `PATCH /api/v1/keys/{key_id}` |
| `key_revoked`, `key_deleted_permanently` | An API key is revoked, or deleted with `?hard=true` |
| `relay_project_split`, `relay_project_split_undone` | A shared Relay project is split, or a split is undone |
| `identity_linked`, `identity_unlinked` | A Google or GitHub sign-in is linked or unlinked |
| `account_review_opened`, `account_review_updated`, `account_review_session`, `account_review_kept`, `account_review_revoked`, `account_review_deferred`, `account_review_completed` | An account check is opened or updated, a sign-in that may act on it happens, one of its items is kept or revoked, or it is put off or completed |
| `account_erased` | An account is erased (the receipt keeps no account content) |

### Stored Fields

- Event ID, timestamp (UTC), user ID
- Action type, resource ID
- API key ID (the key's id, never the key), client IP address
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

Rate limiting is on by default (`REMEMBRA_RATE_LIMIT_ENABLED=true`). Each route sets its own limit; there is no global default, and a few routes have none. Examples:

| Endpoint | Limit |
|----------|--------------|
| `POST /api/v1/memories` (store) | 30/minute |
| `POST /api/v1/memories/recall` | 60/minute |
| `DELETE /api/v1/memories` | 10/minute |

Limits are counted per account once a request is authenticated, and per client IP address otherwise. Storage is in memory or in Redis for several processes. Remembra Cloud adds per-plan recall and relay limits on top.

---

## Network Security

### Security Headers

Every API response carries these. The connector's sign-in and consent pages set their own, stricter referrer and content policies.

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
Strict-Transport-Security: max-age=31536000; includeSubDomains  (when debug is off)
```

### CORS

- Configurable allowed origins
- Localhost origins automatically removed in production
- Credentials support enabled

### Webhook Security

- HMAC-SHA256 signed deliveries when a signing secret was given at registration; without one, deliveries are unsigned
- Signature header: `X-Remembra-Signature: sha256={signature}`
- 10-second timeout and up to 3 attempts; redirects are never followed

---

## Docker Security

- **Non-root user:** Runs as `remembra:remembra`
- **Multi-stage build:** Minimal production image
- **Health checks:** Built-in `HEALTHCHECK` with 30s interval
- **No secrets in image:** All credentials via environment variables
- **Production validation:** with debug off, startup stops (`RuntimeError`) if the JWT secret is a known default or shorter than 32 characters

---

## Deletion and Export

### Deleting Data

On Remembra Cloud, deleting an account in the dashboard cancels any subscription at once and ends every session; 7 days later everything the account holds is erased automatically. Until then, email support@remembra.dev to undo it. Backups are not edited. When a new version of the server is deployed, it copies the database as it starts. Each copy is deleted once 3 newer ones exist, so erased data can stay in those copies until 3 more deploys have happened. There is no continuous backup yet. On a self-hosted server, an administrator can do it through the API.

Delete all of a user's memories:

```
DELETE /api/v1/memories?all_memories=true
```

Or full account deletion, now, by the server's superadmin (an account listed in the server's owner emails):

```
DELETE /api/v1/admin/users/{user_id}?confirm=true
```

Full deletion covers: memories, entities, relationships, API keys, audit logs, cloud records, usage data, tokens, and the user account itself. It first cancels any paid subscription, and it keeps one `account_erased` audit entry that holds no account content. Billing records with no account column (Paddle transaction and subscription ids, net amounts) are kept.

### Data Portability

Export memories as JSON, JSONL or CSV with `GET /api/v1/transfer/export`: one project per request, up to 100,000 memories, with stored credentials redacted. Any key with `memory:recall` can do it. Entities, relationships and inbox messages are not in the export; read them through their own API routes. The dashboard has no export button yet.

### Data Minimization

- TTL-based automatic expiration
- Decay cleanup of old notes nobody recalled is off by default (`REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_ENABLED`). When it is on, it never deletes handoffs, checkpoints, status values, pinned memories or source records.
- Remembra does not train models on stored data

---

## What We Don't Do Yet

Most security questionnaires ask about these. The honest answers, the same as on [remembra.dev/security](https://remembra.dev/security#not-yet):

- **No SOC 2 report, and no outside penetration test.** Remembra has had internal security reviews only. Cloudflare and Paddle have their own audits; Hetzner's ISO 27001 certificate covers its European data centers, not the US site Remembra runs in. Remembra itself is not audited.
- **No EU data region.** Remembra Cloud is hosted in the United States only.
- **The metadata database is not field-encrypted.** Encryption at rest covers the vector store. The database that holds account records and the second copy of your notes is not field-encrypted, and neither are its keyword index and entity graph. The only field encrypted there is the two-factor secret. One encryption key protects the vector store, and it is not rotated yet.
- **API keys don't expire.** You revoke them yourself. An agent-scoped key fixes who a handoff, memory or inbox message is from, and sees only its own agent's inbox. Otherwise it can read everything its account, or its project scope, can read, and with a write role it can change or delete memories other agents wrote. Limit reads with project-scoped keys.
- **No SAML or enterprise SSO.** The dashboard offers Sign in with Google or GitHub.
- **Backups are not edited when an account is erased.** A deleted account is erased automatically 7 days later; its copies in the backups taken at each deploy are deleted after 3 more deploys (see [retention](https://remembra.dev/security#retention)).

Earlier versions of this file gave target dates for SOC 2 Type I and Type II, a HIPAA BAA and ISO 27001. Those dates are withdrawn. Remembra has no certification and does not offer a HIPAA BAA today.

## Roadmap

| What | When |
|------|------|
| Account erasure that also reaches backups (today a backup copy is deleted after 3 more deploys) | Planned; no date yet |
| Key expiry, key rotation, and read limits for agent-scoped keys | Planned; no date yet |
| Field-level encryption for the metadata database, with key rotation | Planned; no date yet |
| SSO (SAML and OIDC) for Team plans | Planned; no date yet |
| An outside penetration test, with a summary published here | Planned; no date yet |
| EU data region; SOC 2 | When customers need them; no date yet |

---

## Security Checklist for Self-Hosted Deployments

```
[x] Set REMEMBRA_AUTH_ENABLED=true
[x] Generate strong master key (32+ chars, random)
[x] Set unique REMEMBRA_JWT_SECRET (32+ chars)
[x] Set REMEMBRA_ENCRYPTION_KEY (encrypts the text fields of Qdrant payloads + TOTP secrets)
[x] Encrypt the data volume holding the SQLite database and its backups (memory text is plaintext in SQLite)
[x] Enable rate limiting (REMEMBRA_RATE_LIMIT_ENABLED=true)
[x] Set PII mode to redact or block (REMEMBRA_PII_MODE=redact)
[x] Configure HTTPS via reverse proxy (nginx, Caddy, etc.), with TLS 1.2 as the minimum
[x] Use non-default ports if exposed to internet
[x] Restrict CORS origins to your domains
[x] Regular key rotation schedule
```

---

## Architecture Decisions

### Field-Level Encryption Scope

Field-level encryption currently covers the text fields of the vector-store
payload (content, the strings in metadata, extracted facts and entity refs)
and TOTP secrets only. It travels with Qdrant snapshots, so anyone with raw
Qdrant access sees ciphertext in those fields; the filter fields (ids, dates,
memory type, scope) and the embedding vectors stay readable. It does **not**
cover the SQLite database (which keeps its own copy of content, facts and
metadata), the FTS index or the entity graph, so it is not a substitute for
disk/volume encryption of the SQLite data and its backups.

### Why bcrypt for API Keys

API keys are stored as a bcrypt hash, plus an unsalted SHA-256 digest that
finds the key's row in one indexed read instead of a bcrypt check of every key.
The keys are 256-bit random values, so neither can be brute-forced. bcrypt adds:

1. **Slow hashing:** each check is deliberately expensive
2. **Salt uniqueness:** each key gets its own random salt
3. **Adaptive cost:** the work factor can be raised over time

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
