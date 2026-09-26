# Changelog

All notable changes to Remembra will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Sign in with GitHub and Google.** "Continue with Google" / "Continue with GitHub" on the dashboard's
  Sign in and Sign up pages (authorization code + PKCE; Google ID tokens verified against the JWKS with
  nonce). Only verified provider emails are accepted, and one account backs each verified email and provider
  account (`user_identities`). Google links into an existing account only when that account's email is
  verified; GitHub never links by email and is connected from **Settings → Security → Sign-in methods**
  (`POST /api/v1/auth/oauth/{provider}/link`, `GET`/`DELETE /api/v1/auth/identities`). The login code is bound
  to the browser by an `HttpOnly` cookie, so a code cannot be replayed from another browser (login CSRF). The
  owner is emailed when a provider is added. Providers without credentials are hidden and 404. Setup:
  `docs/guides/sign-in-providers.md`.
- `GET /api/v1/auth/providers`: enabled sign-in providers and the Turnstile site key; the Sign up page renders
  Cloudflare Turnstile when a site key is published, and checks the same password rules as the server.
- Email verification for `/cloud/signup` tenants (`/api/v1/cloud/verify-email/request` and `/confirm`); they
  are now held at the unverified-email credit cap until verified. Password signups get their verification
  link by email, completing a password reset verifies the email, and the dashboard has a `/verify-email` page
  and a **Resend verification email** button (Settings → Profile). One free account per verified email is
  enforced on every path (dashboard verify, API-signup verify, password reset, social sign-up).
- **Remembra Relay: session continuity across agents.** Every agent leaves a structured handoff
  when it stops, and any agent picks it up at session start, whatever the tool, machine or checkout location.
  - Location-independent project identity: `POST /api/v1/projects/resolve` maps a normalized git
    remote (then root commit, then path) to a per-user project id. The same repo on any machine,
    drive or worktree gets the same id. Adds project links (`/api/v1/projects/links`); a brief shows
    linked projects' latest handoff headlines.
  - `POST /api/v1/session/close`: session facts become ONE deterministic handoff
    (Done / Not done / Failing / Next step). The optional agent summary is grounding-checked against the facts.
    Idempotent per (agent, session). Upserts `last_agent:<project>` and `branch:<project>`.
  - `GET /api/v1/session/brief` accepts a location, leads with a "Last session: …" line and returns a
    compact `rendered` text (~1500 tokens). `GET /api/v1/trail` lists handoffs and checkpoints.
  - `remembra-relay` CLI (`brief`, `close`, `trail`, `resolve`, `connect`) gathers facts from git and
    Claude Code transcripts without uploading them. It is hook-safe (≤10 s, always exits 0). `connect` wires
    agent hooks through an adapter registry (Claude Code verified; Codex, Cursor, Gemini, Qwen and Kimi
    shipped unverified and dry-run only).
  - MCP: new `close_session` and `resolve_project` tools. `session_brief` is compact by default
    (`verbose=True` for the full JSON). The server instructions tell every MCP agent to brief at start and close before finishing.
  - Agent-scoped API keys (`agent_id` on key creation). Relay attribution comes from the key, not the request body.
- Migration 4: `project_fingerprints`, `project_links`, `api_keys.agent_id`.
- **Relay dashboard.** The signed-in dashboard is now mission control for Relay: Home (what changed since
  your last visit, the last handoff with a copyable continue command, unread messages, weekly recap, plan
  usage, a connect checklist), Trail (every handoff on a dashed rail, filterable by project and agent),
  Agents (activity per agent with a 14-day sparkline) and Inbox (write to an agent; the note leads its next
  brief). New read endpoints back it: `GET /api/v1/trail/summary`, `GET /api/v1/inbox/messages`,
  `GET /api/v1/inbox/summary`; `GET /api/v1/trail` gains `agent_id` and a per-item `detail`.
  Existing pages are restyled with the new light and dark tokens and work at phone width.

### Security
- A password reset on an account whose email was never verified now clears everything set up before it: API
  keys, sessions, 2FA, connector grants, webhooks and provider links. It protects a mailbox owner who takes
  back an address someone else pre-registered.

### Changed (breaking)
- **`remembra-relay` / MCP location briefs: which project a git repository uses.** A repository the
  server has not seen joins the configured project (`REMEMBRA_RELAY_PROJECT`, else `REMEMBRA_PROJECT`
  from the environment, MCP env or credentials, unless it is `default`); only with nothing configured
  does it get its own per-repository project. Existing users keep one namespace; bind a repository
  elsewhere with `remembra-relay resolve --project <id> --bind`. `GET /session/brief` and `GET /trail`
  no longer record bindings (only close and `POST /projects/resolve` do), and a brief warns when the
  repository resolves to a project other than the configured one.
- Project-restricted API keys can no longer bind (`403`) or record new location bindings.
- The rendered brief wraps everything recorded by agents in `<remembra-data untrusted="true">…</remembra-data>`,
  marks agents as key-verified or self-declared, labels an agent's next step as an unverified
  suggestion, withholds low-trust text and flags a handoff from another branch/commit as possibly stale.
- `POST /memories` (and batch, bulk, PATCH, supersede) drop the relay-only metadata keys `relay` and
  `relay_key`; agent-scoped keys stamp their own `agent_id` on memories and inbox messages.
- MCP `session_brief` returns the pre-relay fields again by default, plus `brief`, `handoff_id` and
  `inbox_unread`; `compact=true` returns only the text brief (the unreleased `verbose` flag is gone).
  `close_session` and `store_memory` default to the project the last `session_brief` resolved.
- The SDK sends `X-Remembra-Agent-Id` only for ids the relay accepts (ASCII letters, digits and
  `._:@/+-`), so ids such as "Claude Desktop" no longer break requests; `GET /session/brief` accepts
  any agent id again.

**New plans and cost protection for Remembra Cloud.**

### Added
- **New plan catalog:** Free $0, Solo $12/mo or $120/yr, Pro $29/mo or $290/yr,
  Team $15/seat/mo or $150/seat/yr (3-seat minimum), Enterprise custom. Founding 100:
  Solo at $108/yr, annual only, first 100 accounts. Existing $49 Pro and $199 Team
  subscribers move to grandfathered `legacy_pro_49` / `legacy_team_199` tiers with
  $30 / $150 monthly AI ceilings; reduced memory caps wait for
  `REMEMBRA_MEMORY_CAP_NOTICE_EFFECTIVE_AT`.
- **Smart credits.** AI enrichment (extraction, consolidation, entity resolution) is
  metered in credits: `max(ceil(chars / 8000), actual LLM $ / 0.0025)` per store. A
  chunk-aware reservation (16 credits per 8K chunk) is taken before any LLM call on
  every write path, then settled from real OpenAI `usage` after the background work
  finishes and the rest refunded. Annual plans get the whole year's credits up front
  (configurable: `REMEMBRA_ANNUAL_CREDIT_UPFRONT_MONTHS`).
- **Degrade, never reject.** Out of credits (or with the global free-tier breaker
  open), writes are stored atomically without enrichment. Responses carry
  `X-Remembra-Enrichment: full|degraded|atomic` and `X-Remembra-Credits-Remaining`.
  A store is rejected (429) only at the memory cap or, on Free, past the daily
  unenriched-write cap.
- **Relay is free:** handoffs, checkpoints, status values, inbox messages, pickup
  briefs, trail reads and recalls never use credits (relay has a per-plan burst limit
  and a reported soft cap; recalls have monthly and burst limits).
- **Global free-tier circuit breaker:** once a month's free AI spend reaches
  max($50, 20% of last month's net paid revenue), all free enrichment degrades until
  month end.
- **Bounded enrichment queue** with per-tenant concurrency (Free 2, Solo 4, Pro/Team 8)
  and a global cap.
- **Signup hardening:** 3 signups/hour per /24, 20/day per email domain, optional
  Cloudflare Turnstile (`REMEMBRA_TURNSTILE_SECRET`), and (once enabled) new Free
  accounts hold 25 credits until the email is verified. Rate-limit storage can be Redis
  (`REMEMBRA_RATE_LIMIT_STORAGE=redis://...`; `redis` added to the `cloud` extra).
- `GET /api/v1/cloud/usage/summary` for the dashboard billing panel.

### Fixed
- Entity resolution (a ~3K-token LLM call) no longer runs for atomic stores:
  handoff, checkpoint, status, `skip_extraction` and degraded writes.
- **The credit reservation is a hard AI budget.** Every OpenAI, Anthropic and
  TypeSafe call is checked against the write's reservation before it is made; once
  it is used up the rest of the write stores verbatim facts and queued entity
  linking is skipped. Settlement never charges more than was reserved (excess spend
  is logged as platform loss), so `credits_used` cannot pass the plan ceiling.
  Sleep-time work is budgeted by the credits left.
- Stale-reservation expiry never releases a hold whose work is still running in
  this process; startup releases only holds opened before the process started, and
  expired-but-unsettled free holds keep counting toward the free breaker.
- A credit settle released after the task registry shut down is no longer lost:
  shutdown drains enrichment, then the registry, then every pending settle.
- **Paddle webhooks map the plan from the price ID only.** Unknown prices are
  ignored and logged instead of trusting browser-set `custom_data` (which could
  grant Enterprise, legacy tiers or Founding). Team grants exactly the seats paid
  for (a quantity below 3 is flagged, not rounded up). The Founding 100 cap is
  enforced atomically in the webhook (past the cap: plain Solo annual, flagged for
  refund). Client-side checkout lists only single-quantity plans; Team and
  Founding go through server checkout. Approved refunds and chargebacks
  (`adjustment.*`) end the plan and are subtracted from revenue.
- **Team members are billed to the owner's pooled account** (ledger, limits,
  memory cap), up to the seats paid for; new teams get the owner's paid seats.
- **Unverified-email credit hold is opt-in** (`REMEMBRA_UNVERIFIED_CREDIT_CAP_EFFECTIVE_AT`)
  and grandfathers accounts created before it; master-key `/cloud/signup` tenants
  (no user record) are exempt.
- TypeSafe (Jev) spend is metered: billed to the write on stores, skipped for Free
  recalls, recorded in paid AI spend for paid recalls.
- Free accounts: at most 300 stores without enrichment per UTC day (atomic, relay,
  degraded), and their embedding cost feeds the free breaker.
- Signup limits are charged only after Turnstile passes (a looser attempt cap runs
  first), so token-less requests cannot lock out a network or a company domain.
- Behind Cloudflare the client IP comes from `CF-Connecting-IP` when the forwarded
  chain reaches a Cloudflare edge range (`REMEMBRA_TRUST_CLOUDFLARE_PROXIES`).

### Added (relay launch)
- **Claude Code handoffs at a usage limit.** `remembra-relay connect` also runs `close` on Claude Code's
  StopFailure (`rate_limit`, `billing_error`, `account_on_hold`, `cloud_credential_error`) and PreCompact, so
  the handoff is written when work stops, not when the user later quits. The reason is read from `error`
  (what Claude Code 2.1.168 sends) or `error_type`; the brief says `stopped: rate_limit` and the trail
  headline starts with it. A later close of the same session supersedes it. `connect --apply` upgrades an
  existing SessionStart/SessionEnd install in place, with a backup.
- **No silent loss of a handoff.** A `close` that cannot be delivered is queued in
  `~/.remembra/relay/outbox/` (0600, atomic, bounded to 50 entries and 14 days, secrets redacted, never the
  key) and logged to `~/.remembra/relay/relay.log`; the next `brief` or `close` sends it, oldest first
  (a `close` sends it before its own handoff when time allows). A close carries the time its session ended
  (`closed_at`, clamped to the server clock and 15 days); the brief's "Last session" and the `last_agent`
  status follow that time, so a handoff delivered late never replaces a newer one, and the brief says when
  it arrived. The brief names queued handoffs and a rejected key in its first lines. New `remembra-relay status`: queue, last result
  per agent, and whether the server accepts the key.
- **`remembra-relay disconnect`** (dry run unless `--apply`, backups kept) removes the relay hooks, and
  **`remembra-install --remove`** the MCP entries. The relay guide and the dashboard (Settings → Account,
  also in the delete-account dialog) list the uninstall steps.

### Changed (relay launch)
- **`remembra-install` is a dry run by default**: it prints each change as a diff and writes with `--apply`
  (or a "y" on a terminal). The diff masks the Remembra key and hides every other secret in the file (other
  MCP servers' `env`/`headers` values, token/key/password settings, `--token`-style arguments, known
  credential formats); JSON is compared in the layout it is written in, so only the real change shows.
  `remembra-relay connect` / `disconnect` print their diffs the same way. A run that writes nothing exits 3, so the dashboard's
  `remembra-install ... && remembra-relay connect --apply` stops when you answer no. The key is saved to
  `~/.remembra/credentials` even when no agent config is found. It reads the key from `REMEMBRA_API_KEY`, a hidden prompt,
  `--api-key-stdin` or `~/.remembra/credentials`; `--api-key` still works but warns (shell history). Writes
  are atomic, keep a backup and leave the file 0600; each agent's entry gets its own `REMEMBRA_AGENT_ID`.
  `remembra-install-codex` no longer requires `--api-key`. `remembra-doctor` warns about a key file other
  users can read. The landing page and dashboard install lines no longer carry a key.
- The dashboard's install command now installs `remembra[mcp]>=0.16`, like the landing page and the guide
  (without the extra, `remembra-mcp` could not start).
- `remembra`, `remembra-server` and `remembra-mcp` name the missing extra and exit 1 instead of failing
  with `ModuleNotFoundError`; `remembra-mcp --help` and `--version` answer once its imports load.

## [0.16.0] - 2026-07-16

**Lossless memory + production reliability.** The theme of this release: what you
store is exactly what you can get back, and when something fails you can see why.
(Also promotes the previously-unreleased brain layer, 3D graph, and remote MCP —
all live on Remembra Cloud as of this release.)

### Added
- **Lossless memory (provenance-grade fidelity).** Until now, storing content ran it
  through LLM fact-extraction and kept only the derived facts — the verbatim original
  was discarded, and a drifted or hallucinated "fact" was indistinguishable from a real
  one. Now, whenever extraction derives facts, the **exact original text is preserved
  as an immutable source record** (`memory_type="source"`, keyword-searchable, never
  LLM-merged, no vector so it can't pollute semantic recall). Every derived fact
  carries a **receipt** — `metadata.source_id` pointing back to its source record —
  and is **lexically verified against the source**: facts whose content words don't
  appear in the original are stored flagged `verified=false` instead of silently
  trusted. Store responses now include `source_id`. Config: `enable_source_records`
  (default on), `fact_verification_threshold` (default 0.5).
- **Async enrichment mode (opt-in fast writes).** With `REMEMBRA_ASYNC_ENRICHMENT=true`,
  `store` persists the verbatim source and returns immediately (`enrichment: "pending"`);
  extraction/consolidation run in the background. Cuts store latency from seconds
  (full LLM pipeline in-request) to a single write. Off by default — the store
  response contract changes (derived facts land after the response).
- **Request IDs everywhere.** Every request gets a server-generated `X-Request-ID`,
  bound into all structlog lines and used as the `error_id` in 500 responses
  (previously always `"unknown"`). Prod failures are now correlatable end-to-end.
- **Litestream in the cloud image (opt-in).** `Dockerfile.cloud` now ships litestream
  with a new entrypoint: set `LITESTREAM_REPLICA_URL` and the SQLite database is
  continuously replicated to object storage (S3/R2/Tigris) and auto-restored onto an
  empty volume. Without the env var, behavior is unchanged.
- **Honest upstream error mapping.** Embedding-provider failures during store/recall
  now return `429` (with `Retry-After`) on rate limits and `502` on provider outages,
  via a typed `EmbeddingProviderError` carrying the upstream status — instead of
  collapsing everything into `500 "Failed to store memory"`.

### Fixed
- **Opaque store 500s (intermittent MCP `store_memory` failures).** Root cause:
  unbounded content reached the embedding provider; anything past the model's token
  limit made OpenAI return 400, surfaced as a generic 500. All embedding input is now
  clamped to a provider-safe cap (24K chars) — oversized agent payloads store
  gracefully instead of failing whole.
- **Wrong-prefix API calls no longer return the dashboard.** `GET /v1/...` (the real
  prefix is `/api/v1/...`) and `/metrics` used to fall through the SPA catch-all and
  return **HTTP 200 + index.html**, breaking JSON clients with `Unexpected token '<'`.
  They now return a clean `404 {"detail": "Not found"}`.
- **`rem_` API keys sent as `Authorization: Bearer` now authenticate.** Many HTTP
  clients default to Bearer; a `rem_` key is unmistakably an API key, so it now routes
  to API-key validation instead of failing JWT verification with a confusing 401.
  `X-API-Key` still takes precedence; real JWTs are unaffected.

### Performance
- **Embedding cache actually wired.** Identical single-text embeds (recall queries
  repeat heavily) are served from the in-memory cache, keyed by
  `provider|model|dimensions|text` so provider/model switches can never serve stale
  vectors. Measured on production: repeat recalls **1.1s → 0.13s (~8×)**, and every
  hit is an embedding API call not paid for.

### Added (promoted from Unreleased)
- **Brain layer — themed understanding of your memory (GraphRAG-style).** Remembra now
  clusters the entity graph into **communities (themes)** using a dependency-free,
  deterministic Louvain modularity engine (`remembra/brain/`), then labels and
  summarizes each theme. A new **Brain** tab in the dashboard surfaces the themes
  (with summaries), the most **central entities** ("god nodes"), and **surprising
  cross-theme links**, and the 2D/3D knowledge graph now **colors nodes by theme**.
  New API: `GET /v1/brain/communities`, `GET /v1/brain/insights`, `POST /v1/brain/analyze`.
  Communities recompute automatically in the sleep-time consolidation worker. This is
  the higher-level "what is my memory about" layer the leading systems (Microsoft
  GraphRAG, LightRAG) converge on — built natively, no heavy graph dependency.
- **Knowledge graph "Neural Universe" (3D).** A new immersive 3D view of the memory
  graph — glowing nodes sized by memory count, firing-synapse particles along
  connections, cinematic bloom, a starfield, and a slow orbital drift, so the graph
  feels like floating through your own mind. Toggle **✨ Universe / Flat** at the top
  (defaults to Universe when WebGL is available; the proven 2D graph remains the
  fallback). Both views share the click-to-see-real-memories panel. three.js loads
  lazily, only when the graph tab is opened. (`EntityGraphUniverse.tsx`, `KnowledgeGraph.tsx`)
- **Hosted/remote MCP (connect with a URL, no install).** The MCP server now runs
  as a multi-tenant streamable-HTTP endpoint: every caller authenticates with their
  own `X-API-Key` (no shared server key), an ASGI middleware binds that key per
  request, and the API scopes every operation to it — so one caller can never see
  another's memories. This lets any MCP client (Cursor, Windsurf, Claude Desktop,
  Cline, VS Code, …) connect with just a URL + key, eliminating the stdio-binary +
  PATH friction. See `docs/connect.md` and `docker-compose.mcp.yml`. Covered by
  `tests/test_mcp_remote_auth.py` (per-key isolation, no-key→401, key propagation).

### Fixed (from Unreleased)
- **Recall no longer surfaces superseded facts.** Memories retired by a newer belief
  (via the explicit `supersede()` API or the VERSION conflict strategy) are now marked
  with a queryable `superseded_by` column and **excluded from recall by default** —
  so after "I switched from Stripe to Paddle," recall stops returning Stripe. History
  stays queryable via `include_superseded=true`. Covered by `tests/test_supersession_recall.py`.
- **Recall queries with FTS5 operators no longer crash or mis-match.** Natural-language
  recall containing `AND`/`OR`/`NEAR`, `note:` (column-filter syntax), or punctuation
  like `/` and `(` previously raised `fts5: syntax error` (HTTP 500) or silently matched
  nothing. The keyword arm now tokenizes and quotes the query into a safe MATCH
  expression. Covered by `tests/test_fts_query_sanitizer.py`.

### Security
- **Closed an FTS5 schema-disclosure vector.** A recall query like `note: secret` was
  interpreted as an FTS5 column filter and leaked table column names through the
  database error message. User input is now always quoted as literal search terms.

## [0.15.0] - 2026-06-06

### Removed (BREAKING)
- **Stripe removed entirely — Paddle is the only billing provider.** Per a
  security/sales requirement (prior Stripe breach):
  - Deleted `cloud/billing.py` (Stripe `BillingManager`) and
    `cloud/webhook_email_integration.py`, plus the `scripts/setup_stripe.py` SDK script.
  - Removed `/api/v1/cloud/checkout`, `/api/v1/cloud/portal`, and the unauthenticated
    `/api/v1/cloud/webhook/stripe` endpoints; signup no longer creates a Stripe customer.
  - `api/v1/billing.py` is Paddle-only; `promocodes.py` dropped the Stripe-coupon path.
  - Removed all `stripe_*` settings + vestigial `billing_provider`; added `extra="ignore"`
    so leftover `REMEMBRA_STRIPE_*` env vars in a deployed environment never break boot.
  - Removed the `stripe` dependency. No Stripe SDK import or API call remains.
  - Legacy `stripe_customer_id`/`stripe_subscription_id` DB columns are left inert (no
    destructive migration); they are no longer read or written by active code.

### Fixed
- **Memory graph reveals the actual memories on node click.** Previously the panel
  showed only a "Total Memories: 5" count. It now fetches the entity's real memories
  (`GET /entities/{id}/memories`, project-scoped) and renders each one's content + date
  in a scrollable list with loading/error/empty states and a "showing N of M" indicator.
- Malformed memory IDs return a clean 404 instead of 500 across all id-resolving routes.

## [0.14.0] - 2026-06-05

### Security
- **O(1) API key validation** — Key validation previously loaded *every* active key
  and bcrypt-checked the candidate against each one on a cache miss (O(n)). Because
  invalid keys never cached, this doubled as a CPU-exhaustion vector: spraying random
  `rem_…` keys forced a full bcrypt scan per request. Validation now uses an indexed
  deterministic lookup column (`api_keys.key_lookup` = sha256 of the key) for a single
  indexed read, with bcrypt retained as the at-rest verifier (defense in depth). Legacy
  keys backfill lazily on first use; once migrated, unknown keys are rejected in O(1).
- **Master-key admin endpoints now fail closed** — `require_master_key` previously
  allowed requests through when no master key was configured ("fail open"), leaving
  tenant-signup and promo-admin endpoints unprotected on a misconfigured server. It now
  denies in production (debug may bypass for local dev) and compares the key with
  `hmac.compare_digest` (constant-time, no timing leak).
- **Fixed broken master-key path in key creation** — `POST /api/v1/keys` read a
  non-existent `app.state.settings` / `settings.master_api_key`, so the admin
  key-creation branch errored. It now reads the real `auth_master_key` via
  `get_settings()` with a constant-time comparison.
- **Audio capture endpoints require authentication** — `POST /api/v1/audio/start` and
  `/stop` were unauthenticated. They now require a valid user and bind each capture
  session to its owner; only the owner may stop/transcribe a session.
- **Repaired `get_optional_user`** — the optional-auth dependency never validated a
  supplied key (it fell through to `None`), a latent landmine for any future endpoint
  using it. It now validates the key and returns the authenticated user.
- **Startup posture warnings** — production boot now logs explicit warnings when the
  master key or encryption key are unset (in addition to the existing hard-fail on a
  default/short JWT secret).

### Added
- **Salience-aware memory** — memories can now be marked important or pinned:
  - `POST /api/v1/memories/{id}/pin` / `…/unpin` — pinned memories are never pruned by
    temporal decay or TTL expiration ("never forget this").
  - `PATCH /api/v1/memories/{id}/importance` — set a salience score in `[0,1]`; higher
    importance feeds the decay model so the memory retains relevance longer.
  - New columns `memories.importance` and `memories.pinned` (additive migration;
    existing behavior unchanged when unset). Decay and cleanup honor both.

### Tests
- 19 new tests: `test_api_key_lookup.py` (O(1) validation, lazy backfill, rejection,
  revocation, role/scope normalization), `test_master_key_auth.py` (fail-closed +
  constant-time), `test_salience.py` (pin protects from TTL + decay, importance slows
  decay, user-scoped setters). Full suite: **644 passed, 6 skipped**.

## [0.13.2] - 2026-04-25

### Added
- **Product Surface Refresh** — Reworked the landing page into a live product narrative with memory graph, product dock, API system, and operator-focused proof points.
- **Dashboard Control Plane** — Added a dashboard overview surface that brings live memory health, API posture, graph signals, and operational next steps into one view.
- **Release Notes Page** — Added public landing changelog coverage for the latest product surface and dashboard improvements.

### Changed
- **Truth Scrub** — Updated landing, docs, and generated site copy to remove stale claims and unsupported phrasing.
- **Billing Checkout Safety** — Removed hardcoded Paddle client token usage from tracked frontend files; checkout now initializes from runtime billing config.
- **Security Docs** — Replaced real-looking key examples with environment-variable based examples.
- **Git Safety** — Hardened ignored credential patterns for local agent/tooling files, secrets, keys, and deployment context.

### Verified
- `uv run mkdocs build`
- `npm run build` in `dashboard`
- `npm run build` in `landing`
- Staged publish/security scans for blocked files, live-looking tokens, production IPs, and whitespace.

## [0.13.1] - 2026-03-30

### Added
- **Cold Archive Tier** — Separate queryable storage for decayed memories
  - `archived_memories` table with full memory schema + archive metadata
  - `archive_memory()` moves memories to cold storage with final relevance score
  - `restore_memory()` brings memories back to active storage with re-indexing
  - Keyword search in archive (semantic search requires restore)
  - Archive statistics and breakdown by archive reason
  - Cleanup job now uses real cold storage instead of soft-archive flags

- **Adaptive Thresholds** — Dynamic pruning based on session context
  - Session modes: `exploratory` (0.5x), `operational` (1.5x), `balanced` (auto)
  - Warm-up phase: first 10 queries use conservative 0.05 threshold
  - Quality-aware: high result quality → higher threshold (more selective)
  - Density-aware: more memories → slightly higher threshold
  - Session persistence to `adaptive_thresholds` table
  - Cleanup job integration with `use_adaptive_thresholds` flag

- **New API Endpoints**
  - `GET /api/v1/temporal/archive` — List archived memories
  - `GET /api/v1/temporal/archive/stats` — Archive statistics
  - `GET /api/v1/temporal/archive/{id}` — Get specific archived memory
  - `POST /api/v1/temporal/archive/{id}/restore` — Restore to active storage
  - `GET /api/v1/temporal/archive/search` — Search archive by keyword
  - `GET /api/v1/temporal/adaptive/threshold` — Current adaptive threshold info
  - `POST /api/v1/temporal/adaptive/mode` — Set session mode
  - `POST /api/v1/temporal/adaptive/reset` — Reset calibration

### Changed
- `TemporalCleanupJob` now accepts `adaptive_manager` parameter
- Archive moves memory completely (removes from Qdrant + FTS), not just metadata flag
- Decay cleanup uses adaptive threshold when available

### Technical
- New `adaptive.py` module with `AdaptiveThresholdManager`, `SessionContext`, `SessionMode`
- Database schema additions: `archived_memories`, `adaptive_thresholds` tables
- 12 new unit tests for adaptive threshold behavior
- All 109 temporal tests passing

---

## [0.13.0] - 2026-03-27

### Added

#### Dashboard v2.0
- **Admin Dashboard** — Full user management panel
  - View all users with plan, memory count, API keys, status
  - Delete, deactivate, or reset user passwords
  - Change user plans (Free/Pro/Team/Enterprise)
  - Search and filter users

- **Two-Factor Authentication** — TOTP-based 2FA
  - Enable via Settings > Security
  - Works with any authenticator app (Google, Authy, 1Password)
  - Backup codes for account recovery

- **Activity Log** — Security audit trail
  - Track account and API activity
  - Color-coded by event type
  - JSON export for compliance

- **Team Collaboration** — Shared memory spaces
  - Create teams with role-based access (Viewer/Member/Admin)
  - Invite members with role picker
  - Link projects to teams
  - Shared memory across team members

- **Entity Browser** — Visual entity exploration
  - Browse extracted people, organizations, places, concepts
  - Click to see related memories
  - Entity counts and distribution

- **Timeline Timezone Fix** — Proper local time display
  - Dates now show in user's local timezone
  - "Today" header for current day
  - Times in 12-hour format (e.g., 09:57 AM)

- **Knowledge Graph** — Visualize entity relationships
  - Interactive graph view
  - Bi-temporal relationship queries

- **Settings Rebuild** — Complete settings overhaul
  - Profile, Password, Security, Workspace, Retrieval, Diagnostics, Account tabs
  - Calibration API for retrieval tuning
  - Diagnostics for system health

#### TypeScript SDK (npm)
- **npm package** — `npm install remembra`
  - Full TypeScript support with types
  - Browser and Node.js compatible
  - Async/await API

### Fixed
- **RBAC Enforcement** — Viewer role properly restricted from store/delete
- **SSRF Protection** — Webhooks block private IP ranges
- **Error Sanitization** — No Python exceptions leaked to clients
- **Bcrypt Performance** — SHA256 cache mitigates O(n) timing

---

## [0.12.1] - 2026-03-23

### Fixed
- **Documentation** — Updated PyPI README to reflect v0.12.0 features
- **CI/CD** — Added automated PyPI publishing to release workflow

## [0.12.0] - 2026-03-22

### Added
- **User Profiles API** — Aggregated user intelligence endpoint
  - `GET /api/v1/users/{user_id}/profile` returns facts, activity metrics, top topics
  - Memory count, entity breakdown, last active timestamp
  - Aggregated facts summary for quick user context
  - Perfect for personalization and user insights dashboards

- **Smart Auto-Forgetting** — 35+ temporal patterns automatically set TTL
  - `"meeting tomorrow"` → 36h TTL
  - `"call next week"` → 8 days TTL  
  - `"deadline in 2 hours"` → 3h TTL
  - Supports relative dates, specific times, and duration phrases
  - Zero configuration — just store memories naturally

- **Strict Mode 410 GONE** — Opt-in explicit expiry awareness
  - Enable via `REMEMBRA_STRICT_MODE=true` or config
  - Expired memory requests return `410 GONE` instead of silent accept
  - Allows agents to handle expiration explicitly
  - Prevents stale data from being silently used

- **Event-Driven Expiry** — Explicit timestamp control
  - New `expires_at` parameter on store endpoint
  - ISO 8601 format: `"2026-03-25T14:00:00Z"`
  - Takes precedence over TTL when both specified
  - Perfect for event-driven workflows

- **Shadow TTLs Client-Side** — SDK performance optimization
  - SDK maintains local expiry cache
  - Skip recall for known-expired memories
  - Reduces API calls by up to 40%
  - Automatic cache invalidation on store

### Changed
- Store endpoint now accepts `expires_at` parameter alongside `ttl`
- SDK client caches expiry metadata for performance
- API responses include `expires_at` in memory objects when set

### Documentation
- Added User Profiles API to REST API guide
- Added strict_mode configuration documentation
- Updated store endpoint with expires_at parameter

---

## [0.10.2] - 2026-03-16

### Deployment Marker
- **Forced backend rebuild** to ensure production matches `main`
- **Live smoke suite identified deploy drift** on scoped API keys and memory listing

### Fixed
- **Project-scoped API keys** — response models include `project_ids`, auth chain carries project restrictions, and restricted keys enforce project boundaries
- **Memory listing endpoint** — `GET /api/v1/memories` restored for dashboard browse/search surfaces
- **Production verification** — health, analytics, graph, timeline, spaces, and team-space flows rechecked against the deployed API

## [0.10.1] - 2026-03-15

### Production Validated ✅
- **api.remembra.dev** — Live and verified with proper health response
- **Encryption** — AES-256-GCM confirmed working in production
- **Qdrant** — Vector store healthy and operational
- **All agents** — Claude, Codex, Cursor, Gemini, Windsurf integration tested

### Added
- **Centralized Credentials** — `~/.remembra/credentials` with chmod 600
  - API key saved on first install, auto-loaded for future installs
  - Priority: CLI arg > env var > credentials file
- **Slim Recall Mode** — 90% smaller payload for token-constrained agents
  - `recall_memories(query, slim=True)` returns only synthesized context
- **Bridge Lifecycle Management**
  - `remembra-bridge --stop` gracefully stops running bridge
  - `remembra-bridge --status` checks if bridge is running and healthy
  - Port-in-use detection with clear error messages
  - Health check after startup

### Fixed
- **Encryption Key Format** — Production deployment now requires `base64:` prefix for `REMEMBRA_ENCRYPTION_KEY`

---

## [0.10.0] - 2026-03-15

### Added
- **Universal Agent Installer** — One command to configure supported AI tools
  - `remembra-install --all` auto-detects and configures installed agents
  - `remembra-install --agent <name>` for specific agent setup
  - `remembra-install --detect` lists installed agents
  - Supports: Claude Desktop, Claude Code, Codex CLI, Gemini, Cursor, Windsurf
  - Safe config merging — preserves existing MCP configurations
  - **Centralized credentials** in `~/.remembra/credentials` (chmod 600)
    - API key saved on first install, auto-loaded for future installs
    - No need to pass `--api-key` every time after first setup

- **Setup Diagnostics** — `remembra-doctor` command for troubleshooting
  - `remembra-doctor all` scans all detected agents
  - `remembra-doctor <agent>` diagnoses specific agent
  - Checks: config loading, command resolution, health probe, recall test
  - Clear failure labels: `dns_failure`, `sandbox_blocked`, `auth_failure`, `timeout`

- **Slim Recall Mode** — 90% smaller payload for token-constrained agents
  - `recall_memories(query, slim=True)` returns only synthesized context
  - Full mode still available with `slim=False` (default)
  - Reduces recall response from ~2KB to ~200 bytes

- **Local Bridge** — Proxy for sandboxed agents (Codex CLI)
  - `remembra-bridge` runs local HTTP proxy on 127.0.0.1:9819
  - Forwards requests to remote Remembra API
  - Auto-configured by installer for sandboxed environments
  - `remembra-bridge --stop` gracefully stops running bridge
  - `remembra-bridge --status` checks if bridge is running and healthy
  - Port-in-use detection with clear error messages
  - Health check after startup (fails fast if bridge unhealthy)
  - PID file management for clean process lifecycle

- **Security Hardening**
  - RBAC permissions enforced on all memory endpoints
  - Generic exception handler sanitizes error responses
  - API key caching for reduced latency
  - Webhook SSRF protection
  - 2FA/MFA settings UI in dashboard

### Fixed
- Rate limit removed from `/health` endpoint (was blocking monitoring)
- RBAC inline permission checks instead of Depends()

### Documentation
- New Agent Setup guide at docs.remembra.dev/getting-started/agent-setup
- Updated quickstart with universal installer
- Landing page comparison table updated with multi-agent features

---

## [0.9.0] - 2026-03-09

### Added
- **Temporal Knowledge Graph** — Bi-temporal relationship model
  - Relationships now track `valid_from`, `valid_to`, and `superseded_by`
  - Enables point-in-time queries: "Where did Alice work in January?"
  - Contradiction detection: new relationships auto-supersede old ones
  - Foundation for full temporal knowledge graph (ahead of Zep/Graphiti)

- **6 New MCP Tools** — MCP server goes from 5 tools → 11 tools
  - `update_memory` — Update content without delete+recreate, re-extracts facts and entities
  - `search_entities` — Search the entity graph by name, type, or alias
  - `list_memories` — Browse stored memories without a search query
  - `share_memory` — Cross-agent memory sharing via Spaces
  - `timeline` — Temporal browsing filtered by entity and date range
  - `relationships_at` — Query entity relationships at a specific point in time

- **SDK Client Methods** — Python SDK expanded
  - `memory.update(memory_id, content, metadata)` — Calls PATCH endpoint
  - `memory.list_entities(entity_type, limit)` — Calls entity list endpoint

- **Entity Graph Visualization** — Interactive force-directed graph with react-force-graph
  - Flowing particle effects on relationship edges
  - Entity nodes colored by type
  - Click-to-explore entity neighborhoods

### Changed
- MCP server instructions updated to reflect 11 available tools
- Entity graph retrieval now supports temporal filtering

## [0.8.3] - 2026-03-08

### Fixed
- **Security: Server IP Removed** — Removed hardcoded production server IP from tracked files
- **Security: JWT Secret Blocked** — Added quickstart JWT secret to blocked list
- **Dashboard: Light Mode CSS** — Fixed styling issues in light mode theme
- **Dashboard: Auth Flow** — Fixed userId not being set on token verify, added fallback to user object
- **Dashboard: Auth Check** — Use `isAuthenticated()` for proper JWT support
- **Dashboard: Null project_id** — Handle null project_id in API calls

### Security
- Comprehensive repository audit: no real API keys ever committed
- Removed local filesystem paths from public-facing documentation
- Hardened quickstart defaults

## [0.8.2] - 2026-03-07

### Added
- **AES-256-GCM Field Encryption** — Encrypt memory content at rest
  - PBKDF2-HMAC-SHA256 key derivation with 480,000 iterations (OWASP 2023)
  - Transparent encrypt/decrypt for memory content and metadata
  - Passthrough mode for zero-config development
  - Set `REMEMBRA_ENCRYPTION_KEY` to enable in production
- **Encryption Test Suite** — Comprehensive tests for encryption module
- **Security Features Documentation** — Full guide for encryption, PII detection, anomaly detection

### Changed
- Unified security features for enterprise deployments
- Enhanced documentation for self-hosters

## [0.8.1] - 2026-03-06

### Added
- **MCP Registry Published** — Now discoverable as `io.github.remembra-ai/remembra` in Claude Desktop and other MCP clients
- **TypeScript SDK v0.8.1** — Synced with Python SDK features
- **Encryption Documentation** — Added encryption guide to docs

### Fixed
- Synced `__version__` across all modules
- Standardized GitHub URLs throughout documentation
- Fixed stale version references
- Corrected license to MIT in all files

## [0.8.0] - 2026-03-07

### Added
- **One-Command Quick Start** — `curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash` sets up Remembra + Qdrant + Ollama with zero API keys required
- **Multi-Provider Entity Extraction** — Entity extraction now works with Anthropic Claude and Ollama, not just OpenAI. New `create_entity_extractor()` factory dispatches based on `REMEMBRA_LLM_PROVIDER`
- **Usage Warning Banners** — API responses include usage percentage headers (`X-Remembra-Usage-Percent`, `X-Remembra-Plan`) and `usage_warning` field at 60/80/95% thresholds
- **Docker Compose Quickstart** — New `docker-compose.quickstart.yml` with 3 services (Qdrant, Ollama, Remembra), health checks, zero config
- **125 New Tests** — Test coverage for embeddings (6 providers), entity extraction (3 providers), conflict resolution, memory spaces (RBAC), and plugin system (pipeline dispatch)
- **Shared Test Fixtures** — New `tests/conftest.py` with reusable fixtures for all test files

### Changed
- **httpx Connection Reuse** — All 6 embedding providers, webhook delivery, Python SDK client, and MCP server now use persistent HTTP clients. Reduces latency by 100-300ms per operation
- **MCP Server Ingestion** — `ingest_conversation` refactored to use SDK's `Memory.ingest_conversation()`
- **Python SDK** — `Memory` client now supports context manager and has explicit `close()` method
- **App Lifespan Cleanup** — Proper shutdown of all persistent HTTP clients on server stop

### Fixed
- **Connection Churn** — Eliminated 13 locations creating new TCP+TLS connections per request

## [0.7.2] - 2026-03-06

### Fixed
- **Dashboard: EntityGraph Performance** — Changed from N+1 API calls to single `/debug/entities/graph` endpoint
- **Dashboard: Error Display** — Fixed `[object Object]` showing instead of actual error messages
- **Dashboard: TypeScript** — Resolved strict mode compilation errors
- **API: Project Filtering** — Fixed recall defaulting to wrong project_id

### Added
- **Admin: rebuild-vectors endpoint** — `POST /admin/rebuild-vectors` to fix memories missing from Qdrant
- **Docs: Troubleshooting Guide** — Comprehensive diagnosis and fix guide for common issues
- **Docs: Setup Checklist** — 10-step verification checklist for self-hosters

## [0.7.1] - 2026-03-03

### Fixed
- **Security: CORS Configuration** — Removed `allow_origins=["*"]`, now configurable via `REMEMBRA_CORS_ORIGINS`
- **API: PATCH /memories/{id}** — Full implementation (was returning 501)
- **API: Batch Operations** — `/store/batch` and `/recall/batch` now functional
- **Streaming: SSE Endpoint** — `/ingest/stream` for conversation ingestion
- **Observability: OpenTelemetry** — Tracing module fully implemented
- **Production: CORS Origins** — Added `app.remembra.dev` and `remembra.dev` to allowed origins
- **Stripe: Environment Variables** — Accept both prefixed and non-prefixed Stripe env vars

### Changed
- Stub endpoints now return 503 Service Unavailable with helpful messages (was 501)
- Improved error messages throughout API

### Documentation
- Added QA Remediation Results report
- Updated MCP Server documentation for v0.7.0
- Added feature comparison chart
- Added Discord and Twitter links

## [0.7.0] - 2026-03-02

### Added
- **Enterprise Features**
  - **Webhook System** - Event-driven integrations
    - HMAC-SHA256 request signing for security
    - Automatic retry delivery with exponential backoff
    - Events: `memory.created`, `memory.updated`, `memory.deleted`, `entity.created`
    - Webhook management API: create, list, delete, test
  - **RBAC (Role-Based Access Control)**
    - Three roles: `admin`, `editor`, `viewer`
    - 12 granular permissions across memories, entities, webhooks, admin
    - Scoped API keys with role assignment
    - Permission middleware for all protected routes
  - **Memory Conflict Detection**
    - Detect contradictions in stored memories
    - Configurable strategies: `update`, `version`, `flag`
    - Conflict resolution API endpoints
  - **Audit Logging**
    - Complete audit trail of all operations
    - Export to JSON or CSV format
    - Role-protected admin endpoints

- **Import/Export System**
  - **Import from**:
    - ChatGPT conversation exports
    - Claude conversation exports
    - Plain text files
    - JSON, JSONL, CSV formats
  - **Export to**:
    - JSON (full fidelity)
    - JSONL (streaming-friendly)
    - CSV (spreadsheet-compatible)
  - Bulk import API with progress tracking

- **Cloud & Revenue (Phase 2)**
  - **Stripe Billing Integration**
    - Subscription management
    - Usage-based metering
    - Customer portal integration
    - Webhook handlers for billing events
  - **Plan Limits**
    - Configurable limits per plan (memories, API calls, storage)
    - Automatic enforcement with graceful degradation
    - Usage dashboards and alerts
  - **Spaces (Multi-tenancy)**
    - Isolated memory spaces per organization
    - Space-level settings and quotas
    - Cross-space queries for admins

- **Plugin System**
  - Extensible plugin architecture
  - Built-in plugins:
    - `auto_tagger` - Automatic memory tagging
    - `recall_logger` - Query analytics
    - `slack_notifier` - Slack integration for events
  - Custom plugin development guide

- **API Expansion**
  - 52 total API routes across 11 route groups
  - New endpoints: `/admin/*`, `/webhooks/*`, `/transfer/*`, `/conflicts/*`
  - OpenAPI schema updated

### Changed
- Embeddings API refactored for multi-provider support
- Memory service expanded with conflict detection
- Config updated with cloud/billing settings

### Fixed
- TypeScript type reference in dashboard api.ts

## [0.6.3] - 2026-03-01

### Added
- **Docker Support** (Week 11)
  - Production-ready `Dockerfile` with multi-stage build
  - `docker-compose.yml` for complete stack (API + Qdrant)
  - `.env.example` with all configuration options
  - `DOCKER.md` deployment guide
  - Static file serving for dashboard UI
  - Health checks for container orchestration
- **Configuration**
  - `REMEMBRA_STATIC_DIR` for serving dashboard

### Changed
- Dashboard UI now served by API server when `static_dir` is set

## [0.6.2] - 2026-03-01

### Added
- **Entity API Endpoints** (Week 10)
  - `GET /api/v1/entities` - List all entities with type counts
  - `GET /api/v1/entities/{id}` - Get entity by ID
  - `GET /api/v1/entities/{id}/relationships` - Get entity relationships
  - `GET /api/v1/entities/{id}/memories` - Get memories linked to entity
- **Dashboard Improvements**
  - Entity graph visualization (force-directed layout)
  - Memory editing support
  - Graph tab with interactive canvas
  - Entity detail modal with relationships and memories

### Fixed
- TypeScript strict mode compatibility in dashboard components

## [0.6.1] - 2026-03-01

### Added
- **Temporal API Endpoints** - REST API for decay management
  - `GET /api/v1/temporal/decay/report` - View memory health and decay scores
  - `POST /api/v1/temporal/cleanup` - Run cleanup with dry-run support
  - `GET /api/v1/temporal/memory/{id}/decay` - Single memory decay info
- **Decay Module** (`remembra.temporal.decay`)
  - Ebbinghaus forgetting curve implementation
  - Configurable decay parameters (DecayConfig)
  - `calculate_relevance_score()`, `should_prune()` functions
- **TTL Module** (`remembra.temporal.ttl`)
  - Parse TTL strings: "30d", "1y", "2w", "24h"
  - TTL presets: session, conversation, short_term, long_term, permanent
- **Cleanup Job** (`remembra.temporal.cleanup`)
  - Background cleanup for expired/decayed memories
  - Archive mode (soft delete) vs hard delete

### Fixed
- Temporal module properly exported from package

## [0.6.0] - 2026-03-01

### Added
- **Temporal Features (Week 8)** - Time-aware memory operations
  - **TTL (Time-to-Live)** - Memories can now expire automatically
    - Set TTL on store: `memory.store("...", ttl="30d")` (supports d/w/m/y)
    - `cleanup_expired()` method and `/cleanup-expired` endpoint
    - Server-side default TTL configurable via `REMEMBRA_DEFAULT_TTL_DAYS`
  
  - **Memory Decay Algorithm** - Older/unused memories rank lower
    - Exponential time decay with configurable half-life
    - Access count boost (frequently accessed = higher score)
    - Recency of access boost (recently accessed = higher score)
    - `get_memories_with_decay()` for decay score visibility
  
  - **Historical Queries (as_of)** - Time-travel memory recall
    - `recall_as_of()` method to see memories at a point in time
    - Useful for auditing, debugging, historical analysis
    - Respects both creation time and expiration time
  
- **Changelog Ingestion** - Auto-import project history
  - New endpoint: `POST /api/v1/ingest/changelog`
  - Parses Keep a Changelog format (and similar markdown formats)
  - Each release becomes a searchable memory with version/date metadata
  - SDK method: `memory.ingest_changelog(content_or_path, project_name="...")`
  - Supports both raw content and file path input
  
- Database temporal query methods:
  - `get_expired_memories()` - Find memories past their TTL
  - `get_memories_as_of()` - Query historical memory state
  - `get_memories_with_decay_info()` - Get access/decay metadata
  - `cleanup_expired_memories()` - Batch delete expired memories
  - `migrate_memory_relationships()` - Preserve links during UPDATE

### Fixed
- **Critical: FK Constraint Bug in Consolidation** - Memory UPDATE/DELETE operations
  no longer fail with foreign key constraint errors. The fix:
  - Relationships are now properly migrated to new memory before old is deleted
  - `delete_memory()` cleans up relationships and entity links first
  - Entity links preserved during consolidation merges
  
- Fixed duplicate `max_tokens` field in `RecallRequest` model

### Changed
- Memory deletion now explicitly handles FK constraints (relationships, entity links)
- Consolidation UPDATE path migrates relationships to preserve entity graph integrity
- `RecallRequest` now includes `as_of` and `include_decay_score` parameters

### Configuration
- `REMEMBRA_DEFAULT_TTL_DAYS` - Default TTL for all memories (optional)

## [0.5.0] - 2026-03-01

### Added
- **API Key Authentication** - Secure access control for all memory operations
  - Generate API keys with `rem_` prefix and 256-bit entropy
  - Keys hashed with bcrypt before storage (never stored in plaintext)
  - Per-user memory isolation enforced via API key
  - Master key support for admin operations
  - Key management endpoints: POST/GET/DELETE /api/v1/keys
  
- **Rate Limiting** - Protection against abuse and DoS
  - Per-endpoint limits (store: 30/min, recall: 60/min, forget: 10/min)
  - Rate limit by API key (not just IP)
  - Uses `slowapi` with in-memory or Redis backend
  - Configurable via environment variables
  
- **Memory Protection Layer** - Defense against prompt injection (MINJA)
  - Input sanitization before storage
  - Trust scoring based on suspicious pattern detection
  - Patterns detected: instruction override, role manipulation, delimiter injection
  - SHA-256 checksums for integrity verification
  - Content provenance tracking (source, trust_score, checksum)
  
- **Audit Logging** - Security monitoring and compliance
  - Logs all memory operations (store, recall, forget)
  - Logs authentication events (key created, revoked, failed attempts)
  - Includes: timestamp, user_id, key_id, action, resource_id, IP, success
  - Never logs actual memory content or full API keys
  
- New `auth/` module with:
  - `keys.py` - API key generation, hashing, validation
  - `middleware.py` - FastAPI dependencies for authentication
  
- New `security/` module with:
  - `sanitizer.py` - Content sanitization and trust scoring
  - `audit.py` - Security audit logging
  
- Database schema updates:
  - `api_keys` table for key storage
  - `audit_log` table for security events
  - Memory provenance columns: source, trust_score, checksum

### Configuration
- `REMEMBRA_AUTH_ENABLED` - Enable API key authentication (default: true)
- `REMEMBRA_AUTH_MASTER_KEY` - Master key for admin operations
- `REMEMBRA_RATE_LIMIT_ENABLED` - Enable rate limiting (default: true)
- `REMEMBRA_RATE_LIMIT_STORAGE` - Rate limit backend: "memory" or "redis://..."
- `REMEMBRA_SANITIZATION_ENABLED` - Enable input sanitization (default: true)
- `REMEMBRA_TRUST_SCORE_THRESHOLD` - Suspicious content threshold (default: 0.5)

### Security
- OWASP API Security Top 10 addressed
- Defense-in-depth against memory injection attacks (MINJA - 95% success rate in research)
- Cross-user memory access blocked via API key scoping
- user_id in requests overridden by authenticated user (prevents spoofing)

### Dependencies
- Added `bcrypt>=4.0.0` for key hashing
- Added `slowapi>=0.1.9` for rate limiting

## [0.4.0] - 2026-03-01

### Added
- **Hybrid Search** - Combines semantic (vector) and keyword (BM25) matching
  - **SQLite FTS5** integration for persistent full-text indexing
  - In-memory BM25 fallback when FTS5 unavailable
  - Score normalization with min-max scaling
  - Configurable alpha weight for keyword/semantic balance
  - Reciprocal Rank Fusion (RRF) option for rank-based fusion
  
- **CrossEncoder Reranking** - Optional post-retrieval reranking (NEW)
  - Uses `sentence-transformers` CrossEncoder models
  - Reduces hallucinations by ~35% (per Databricks research)
  - Default model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (local, free)
  - Graceful degradation when model unavailable
  - Blends rerank scores with original scores
  
- **Graph-Aware Retrieval** - Uses entity relationships for smarter recall
  - Traverses entity graph to find related memories
  - Alias matching ("Mr. Kim" → "David Kim")
  - Configurable traversal depth (default: 2 hops)
  - Entity neighborhood expansion
  
- **Context Window Optimization** - Smart truncation for LLM context limits
  - **tiktoken integration** for accurate token counting (NEW)
  - Character-based fallback estimation
  - `max_tokens` parameter on `recall()` endpoint
  - Relevance-aware truncation at sentence boundaries
  
- **Advanced Relevance Ranking** - Multi-signal scoring
  - Recency boost (newer memories score higher)
  - Entity match boost (entities in query)
  - Keyword match boost (from BM25)
  - Diversity-aware reranking (MMR) to reduce redundancy
  - Configurable weights via environment variables
  
- New `retrieval/` module with:
  - `hybrid.py` - BM25Index, HybridSearcher
  - `graph.py` - GraphRetriever for entity traversal
  - `context.py` - ContextOptimizer with tiktoken
  - `ranking.py` - RelevanceRanker with configurable boosts
  - `reranker.py` - CrossEncoderReranker for quality improvement (NEW)
  
- FTS5 full-text search table in SQLite (`memories_fts`)
- Comprehensive tests for all retrieval features

### Configuration
- `REMEMBRA_HYBRID_SEARCH_ENABLED` - Toggle hybrid search (default: true)
- `REMEMBRA_HYBRID_ALPHA` - Keyword weight 0-1 (default: 0.4)
- `REMEMBRA_RERANK_ENABLED` - Toggle CrossEncoder reranking (default: false)
- `REMEMBRA_RERANK_MODEL` - CrossEncoder model name
- `REMEMBRA_DEFAULT_MAX_TOKENS` - Max context tokens (default: 4000)
- `REMEMBRA_GRAPH_RETRIEVAL_ENABLED` - Toggle graph traversal (default: true)
- `REMEMBRA_GRAPH_TRAVERSAL_DEPTH` - Entity graph depth (default: 2)
- `REMEMBRA_RANKING_SEMANTIC_WEIGHT` - Ranking semantic weight (default: 0.6)
- `REMEMBRA_RANKING_RECENCY_WEIGHT` - Ranking recency weight (default: 0.15)
- `REMEMBRA_RANKING_ENTITY_WEIGHT` - Ranking entity weight (default: 0.15)
- `REMEMBRA_RANKING_KEYWORD_WEIGHT` - Ranking keyword weight (default: 0.1)
- `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` - Recency half-life (default: 30)

### Changed
- `recall()` now uses advanced retrieval pipeline by default
- `RecallRequest` accepts `max_tokens`, `enable_hybrid`, `enable_rerank` params
- `store()` now indexes memories in FTS5 for keyword search
- Improved relevance scoring considers multiple signals
- Context output optimized for LLM consumption

### Dependencies
- Added `tiktoken>=0.7.0` to server extras
- Added `sentence-transformers>=2.5.0` as optional `rerank` extra

## [0.3.0] - 2026-03-01

### Added
- **Entity Extraction** - LLM extracts PERSON, ORG, LOCATION entities from memories
- **Entity Matching** - Resolves aliases ("Mr. Kim" → "David Kim", "NYC" → "New York City")
- **Alias Management** - Automatic alias tracking and resolution
- **Relationship Storage** - Stores entity relationships (WORKS_AT, SPOUSE_OF, KNOWS, etc.)
- **Memory-Entity Links** - Bidirectional links between memories and entities
- **Entity-Aware Recall** - Find memories via entity graph traversal
- New `entities.py` module for entity extraction
- New `matcher.py` module for entity resolution
- Entity resolution documentation

### Changed
- Memory storage now extracts and links entities automatically
- Recall considers entity relationships for improved relevance

## [0.2.0] - 2026-03-01

### Added
- **LLM-powered fact extraction** - Transforms messy text into clean atomic facts
- **Memory consolidation** - ADD/UPDATE/DELETE/NOOP logic prevents duplicates
- **Smart merging** - Updates preserve history (e.g., "VP of Sales (promoted from Director)")
- New extraction module with configurable LLM backend
- New consolidation module for memory conflict resolution

### Changed
- `store()` now uses intelligent extraction by default
- Improved recall relevance with semantic understanding
- Default threshold lowered to 0.40 for better recall

### Configuration
- `REMEMBRA_SMART_EXTRACTION_ENABLED` - Toggle LLM extraction (default: true)
- `REMEMBRA_EXTRACTION_MODEL` - Model for extraction (default: gpt-4o-mini)
- `REMEMBRA_CONSOLIDATION_THRESHOLD` - Similarity threshold for consolidation

## [0.1.0] - 2026-03-01

### Added
- Initial release of Remembra
- Python SDK with `Memory` client class
- REST API with FastAPI
- `store()` - Store memories with automatic fact extraction
- `recall()` - Semantic search across memories
- `forget()` - GDPR-compliant deletion
- Qdrant vector store integration
- SQLite metadata storage
- Embedding support for OpenAI, Ollama, and Cohere
- Docker and docker-compose setup
- Comprehensive test suite

### Notes
- This is an alpha release - API may change
- Entity resolution coming in v0.2.0
- LLM-powered extraction coming in v0.2.0

## [0.4.1] - 2026-03-01

### Fixed
- API recall endpoint signature (removed duplicate max_tokens argument)
- Hybrid search fallback path (correct method signature for fusion)
- Test compatibility with HybridSearchConfig API

### Added
- RELEASE-CHECKLIST.md - mandatory pre-deploy verification
n��ti�|o]�y�4߯m�<y�y���Ӎ�
