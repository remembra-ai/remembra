# Remembra Security Audit & Hardening — June 2026

**Audited version:** 0.13.2 (live at `api.remembra.dev`) → **shipped as 0.14.0**
**Scope:** Authentication, API-key lifecycle, encryption at rest, endpoint authorization,
SQL safety, the dashboard SPA, and the deployment posture.
**Method:** Manual review of the auth/keys/encryption/config/app-wiring core, three
breadth reviews (endpoint auth map, SQL-injection sweep, dashboard review), and live
verification against the production `/health` endpoint. Every load-bearing finding was
confirmed against the actual code or running service rather than inferred.

---

## Summary

Remembra is a mature, well-built system: parameterized SQL throughout, AES-256-GCM
encryption **confirmed live in production**, superadmin endpoints that fail *closed*
via an `owner_emails` allowlist, sanitized error responses, good security headers, and
a hard production boot-guard against a default/short JWT secret. The findings below are
hardening and correctness fixes, not a system in crisis.

Three scanner over-flags were investigated and **downgraded** after verification:
- "Default JWT secret → forgery" — mitigated: `main.py` refuses to boot in production
  with a default or <32-char secret.
- "Anyone can mint keys for any user via master key" — not real: that code path read a
  non-existent attribute and simply errored (now fixed).
- "Critical dashboard XSS via `dangerouslySetInnerHTML`" — not exploitable: the sink
  renders a *static* onboarding snippet, not user/memory content (flagged as a latent
  landmine instead).

---

## Findings & fixes (shipped in 0.14.0)

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | **High** | API-key validation was O(n)-bcrypt on cache miss → CPU-exhaustion DoS (invalid keys never cache, so each forced a full scan) | ✅ Fixed |
| 2 | **High** | `POST /api/v1/audio/start` & `/stop` unauthenticated (confirmed mounted in prod) | ✅ Fixed |
| 3 | Medium | `require_master_key` failed **open** when no master key configured | ✅ Fixed (fail closed) |
| 4 | Medium | Master-key compared with `!=` (timing-attackable) | ✅ Fixed (`hmac.compare_digest`) |
| 5 | Medium | `POST /api/v1/keys` master-key branch broken (`app.state.settings` / `master_api_key` don't exist) | ✅ Fixed |
| 6 | Low | `get_optional_user` never validated a supplied key (dead code, latent landmine) | ✅ Fixed |
| 7 | Low | No prod warning when master/encryption keys unset | ✅ Fixed (startup warnings) |

### 1. O(1) API-key validation (`auth/keys.py`, `storage/database.py`)
`validate_key` loaded all active keys and bcrypt-checked the candidate against each on a
cache miss. Fix: added an indexed `api_keys.key_lookup` column (`sha256(raw_key)` — a
256-bit-entropy key makes the hash non-brute-forceable, so it is safe to store and
index). Validation is now a single indexed read + one bcrypt verify. Legacy keys
backfill lazily on first successful validation; once `get_unmigrated_active_api_keys()`
is empty, unknown keys are rejected in O(1) with no bcrypt scan, closing the DoS vector.

### 2. Audio endpoint authentication (`api/v1/audio.py`)
Both endpoints now require `CurrentUser`, and each capture session is bound to its
owner in `_session_owners`; `/stop` returns 404 for sessions the caller doesn't own
(no session-id enumeration).

### 3–5. Master-key hardening (`auth/middleware.py`, `api/v1/keys.py`)
`require_master_key` now denies in production when unconfigured (debug may bypass) and
uses a constant-time comparison. The broken key-creation branch now reads the real
`auth_master_key` via `get_settings()`, also constant-time.

### 6–7. Optional-auth repair + posture warnings
`get_optional_user` validates the key and returns the user. Production boot logs
warnings when `auth_master_key` or `encryption_key` are unset.

All fixes covered by 19 new tests; full suite **644 passed, 6 skipped**.

---

## Verified-safe (no change needed)

- **SQL injection:** parameterized queries throughout. The f-string spots
  (`reindex.py` WHERE-builder, `database.py` `IN (...)` placeholder strings,
  `teams/manager.py` UPDATE) interpolate only hardcoded clause fragments / `?`
  placeholders, never user values. FTS5 `MATCH` escapes quotes and is `user_id`-scoped.
- **Authorization:** superadmin endpoints (delete user, reset password, tier changes)
  gate on `owner_emails` and fail closed if the list is empty.
- **Encryption at rest:** AES-256-GCM, confirmed enabled in prod via `/health`.
- **JWT:** explicit `algorithms=[...]` (no alg-confusion), `type`/expiry checks, hard
  boot-guard against default secrets.

---

## Remaining recommendations (not yet implemented)

These are deliberately deferred — each deserves its own focused change with tests:

- **P2 — Dashboard token storage:** JWT + API key live in `localStorage` and are placed
  in the WebSocket URL query string (`hooks/useWebSocket.ts`), where they reach logs and
  history. Move to httpOnly cookies, or add a strict CSP + move WS auth out of the URL +
  remove debug `console.log` of auth data.
- **P2 — JWT logout doesn't truly invalidate:** `verify_jwt_token` doesn't consult the
  `token_blacklist` table, so a logged-out token remains valid until expiry (24h cap).
- **P2 — Encryption key model:** single global key with a deterministic
  passphrase-derived salt and no rotation path. Per-tenant envelope encryption + a
  rotation tool would make it enterprise-grade (see roadmap: "blind indexing").
- **P2 — Don't reuse `highlightCode`** (`dashboard/.../EmptyState.tsx`) on any
  user/memory content — it's a `dangerouslySetInnerHTML` sink that is only safe today
  because its input is a static code sample.
- **P3 — Debug/observability endpoints** (`/api/v1/debug/*`) are auth-gated but not
  plan-gated despite docstrings implying `has_observability`; wire the tier check.

---

*Audit performed by Claude (Opus 4.8). Fixes are in `0.14.0`; see `CHANGELOG.md`.*
