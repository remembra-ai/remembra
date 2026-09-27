# Configuration Reference

Server settings are environment variables with the `REMEMBRA_` prefix. A variable without the prefix (a bare
`OPENAI_API_KEY`, `QDRANT_HOST`) is not read.

## Required

With the default OpenAI embedding provider:

| Variable | Description | Example |
|----------|-------------|---------|
| `REMEMBRA_OPENAI_API_KEY` | OpenAI API key (embeddings, fact extraction, consolidation, entity matching) | `sk-...` |
| `REMEMBRA_JWT_SECRET` | Secret for dashboard sign-in tokens (JWTs). Unless `REMEMBRA_DEBUG=true`, the server does not start without a unique value of 32+ characters | 64 random hex characters |

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HOST` | `0.0.0.0` | Server bind address |
| `REMEMBRA_PORT` | `8787` | Server port |
| `REMEMBRA_DEBUG` | `false` | Development mode (relaxes the startup security checks) |
| `REMEMBRA_LOG_LEVEL` | `info` | Logging level |
| `REMEMBRA_CORS_ORIGINS` | localhost:3000, localhost:8787, app.remembra.dev, remembra.dev | Allowed browser origins, as a JSON list or comma-separated. Localhost origins are dropped unless `REMEMBRA_DEBUG=true` |

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DATABASE_URL` | `sqlite+aiosqlite:///remembra.db` (`/data/remembra.db` in the Docker image) | SQLite database |
| `REMEMBRA_QDRANT_URL` | `http://qdrant:6333` (`http://localhost:6333` in the Docker image) | Qdrant server |
| `REMEMBRA_QDRANT_API_KEY` | - | Qdrant API key (if secured) |
| `REMEMBRA_QDRANT_COLLECTION` | `memories` | Qdrant collection name |

## Embeddings

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | `openai`, `azure_openai`, `ollama`, `cohere`, `voyage` or `jina` |
| `REMEMBRA_EMBEDDING_MODEL` | `text-embedding-3-small` | Model name |
| `REMEMBRA_EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions (768 for Ollama's `nomic-embed-text`) |
| `REMEMBRA_OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `REMEMBRA_COHERE_API_KEY` | - | Cohere API key |
| `REMEMBRA_VOYAGE_API_KEY` | - | Voyage API key |
| `REMEMBRA_JINA_API_KEY` | - | Jina API key |
| `REMEMBRA_AZURE_OPENAI_API_KEY`, `REMEMBRA_AZURE_OPENAI_ENDPOINT`, `REMEMBRA_AZURE_OPENAI_DEPLOYMENT` | - | Azure OpenAI |

## Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SMART_EXTRACTION_ENABLED` | `true` | Enable LLM extraction |
| `REMEMBRA_EXTRACTION_MODEL` | `gpt-4o-mini` | OpenAI model for fact extraction, consolidation (including sleep-time), entity matching and conversation ingest. Must be an OpenAI model |
| `REMEMBRA_LLM_PROVIDER` | `openai` | Entity-extraction backend only: `openai`, `anthropic`, `ollama` |
| `REMEMBRA_LLM_MODEL` | `gpt-4o-mini` | Entity-extraction model, used only when `REMEMBRA_EXTRACTION_MODEL` does not fit `REMEMBRA_LLM_PROVIDER`. Never changes fact extraction |
| `REMEMBRA_ANTHROPIC_API_KEY` | - | API key for Anthropic entity extraction |

!!! note "Which variable changes which model"
    `REMEMBRA_EXTRACTION_MODEL` is the model that matters: every OpenAI call
    (fact extraction, consolidation, entity matching, conversation ingest, and
    entity extraction with the `openai` provider) uses it. `REMEMBRA_LLM_MODEL`
    only applies to entity extraction on a provider the extraction model does
    not fit, for example `REMEMBRA_LLM_PROVIDER=anthropic` with
    `REMEMBRA_LLM_MODEL=claude-haiku-4-5`. Setting `REMEMBRA_EXTRACTION_MODEL` to
    a Claude or Ollama model does not work: it is sent to OpenAI and rejected.
    At startup the server logs `llm_task_models` with the model of every task,
    and `extraction_model_not_openai` when the extraction model is not an
    OpenAI model.

## Entity Resolution

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENABLE_ENTITY_RESOLUTION` | `true` | Extract and match entities |
| `REMEMBRA_ENTITY_MATCHING_THRESHOLD` | `0.6` | Minimum confidence for the matcher to merge a mention into an existing entity |

## Retrieval

The recall defaults (`limit` 5, `threshold` 0.40) are fields of the recall request, not server settings.

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_CONTEXT_MAX_TOKENS` | `4000` | Max context tokens |

### Hybrid Search

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENABLE_HYBRID_SEARCH` | `true` | Enable hybrid search |
| `REMEMBRA_HYBRID_ALPHA` | `0.4` | Keyword weight (0-1) |

### Reranking

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENABLE_RERANKING` | `true` | CrossEncoder reranking. It runs only when the install has the `rerank` extra (the default Docker image does not); `docker-compose.yml` and `docker-compose.prod.yml` set it to `false` |
| `REMEMBRA_RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranker model |
| `REMEMBRA_RERANK_TOP_K` | `20` | Candidates to rerank |

### Ranking Weights

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RANKING_SEMANTIC_WEIGHT` | `0.6` | Semantic score weight |
| `REMEMBRA_RANKING_RECENCY_WEIGHT` | `0.15` | Recency boost weight |
| `REMEMBRA_RANKING_ENTITY_WEIGHT` | `0.15` | Entity match weight |
| `REMEMBRA_RANKING_KEYWORD_WEIGHT` | `0.1` | Keyword match weight |
| `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` | `30` | Recency half-life (days); also the half-life of the recall decay score |

### Graph Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENABLE_GRAPH_RETRIEVAL` | `true` | Enable graph traversal |
| `REMEMBRA_GRAPH_MAX_DEPTH` | `2` | Max hop depth |

## Temporal

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_TTL_DAYS` | - | Default TTL in days for memories stored without one |
| `REMEMBRA_TEMPORAL_CLEANUP_ENABLED` | `false` | Run cleanup on a timer (moves expired memories to the cold archive) |
| `REMEMBRA_TEMPORAL_CLEANUP_INTERVAL_SECONDS` | `3600` | Seconds between timed cleanups |

See [Temporal Memory](../guides/temporal.md) for how decay scores work.

## Sleep-time worker

What each pass does: [Sleep-Time Compute](../guides/sleep-time-compute.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SLEEP_TIME_ENABLED` | `true` | Start the background worker |
| `REMEMBRA_SLEEP_TIME_TRIGGER` | `interval` | `interval` runs it on a timer; any other value means no timed runs |
| `REMEMBRA_SLEEP_TIME_INTERVAL_HOURS` | `6` | Hours between timed runs |
| `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_ENABLED` | `false` | Let the worker delete old notes nobody recalled. Off: it deletes no memories. On: each run deletes up to 100 of an active account's ordinary notes older than the days below that no search has returned and that have no expiry. Handoffs, checkpoints, status values, source records and pinned memories are never deleted |
| `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_DAYS` | `90` | Age in days before decay cleanup (when on) deletes such a note |

## Security

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTH_ENABLED` | `true` | Enable API key auth |
| `REMEMBRA_AUTH_MASTER_KEY` | - | Master key: creates keys for any `user_id`, including admin keys |
| `REMEMBRA_JWT_SECRET` | - | Secret for dashboard sign-in tokens (see Required) |

| `REMEMBRA_SECRET_REDACTION_ENABLED` | `true` | Replace recognised credentials with `[REDACTED:<kind>]` |
| `REMEMBRA_ENCRYPTION_KEY` | - | AES-256-GCM key for the text fields of Qdrant payloads ([what it covers](../guides/security.md#encryption-at-rest)) |

Dashboard sign-ins expire after a fixed 24 hours.

### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |
| `REMEMBRA_RATE_LIMIT_STORAGE` | `memory` | Backend: `memory` or `redis://...` |

The per-route limits (store 30/minute, recall 60/minute, delete 10/minute, and others) are fixed in the code.

### PII Detection

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_PII_DETECTION_ENABLED` | `true` | Scan content for PII |
| `REMEMBRA_PII_MODE` | `redact` | `detect`, `redact` or `block` |
| `REMEMBRA_PII_EXCLUSIONS` | - | PII types to skip, comma-separated |

### Sanitization

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SANITIZATION_ENABLED` | `true` | Enable input sanitization |
| `REMEMBRA_TRUST_SCORE_THRESHOLD` | `0.5` | Suspicious content threshold |

## Dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_STATIC_DIR` | - (`/app/static` in the Docker image) | Path to dashboard build |

## Webhooks

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_WEBHOOKS_ENABLED` | `false` | Turn on webhooks ([Webhooks](../guides/webhooks.md)) |
| `REMEMBRA_WEBHOOK_TIMEOUT` | `10` | Seconds to wait for your endpoint |
| `REMEMBRA_WEBHOOK_MAX_RETRIES` | `3` | Delivery attempts in total |

## Sign in with GitHub / Google

A provider is enabled only when its client ID and secret, `REMEMBRA_PUBLIC_URL`
and `REMEMBRA_PUBLIC_DASHBOARD_URL` are all set; otherwise its routes answer 404
and the dashboard hides its button. Setup: [Sign-in providers](../guides/sign-in-providers.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_PUBLIC_URL` | - | Public HTTPS origin of the API (e.g. `https://api.remembra.dev`). Callback URLs are `<this>/api/v1/auth/oauth/{github,google}/callback` |
| `REMEMBRA_PUBLIC_DASHBOARD_URL` | - | Public HTTPS origin of the dashboard (e.g. `https://app.remembra.dev`). Sign-in only ever redirects here; also the base of emailed verification links (default `https://app.remembra.dev`) |
| `REMEMBRA_GITHUB_CLIENT_ID` | - | GitHub OAuth app client ID |
| `REMEMBRA_GITHUB_CLIENT_SECRET` | - | GitHub OAuth app client secret |
| `REMEMBRA_GOOGLE_CLIENT_ID` | - | Google OAuth web client ID |
| `REMEMBRA_GOOGLE_CLIENT_SECRET` | - | Google OAuth web client secret |

## Expiry

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_CHECKPOINT_DEFAULT_TTL` | `7d` | TTL for a `memory_type="checkpoint"` store that sets no `ttl` or `expires_at`. Same format as a store `ttl` ([TTL formats](../guides/temporal.md#ttl-formats)); the server does not start if it is not one. Blank: no default |
| `REMEMBRA_STRICT_MODE` | `false` | A GET or PATCH of an expired memory returns `410 GONE` |

Smart auto-forgetting (a TTL from phrases such as "meeting tomorrow") is not a server setting. It runs in the
Python SDK, when you create the client with `Memory(auto_expire_temporal=True)`; see the
[Python SDK guide](../guides/python-sdk.md).

## Cloud metering and billing (Remembra Cloud)

Only used when `REMEMBRA_CLOUD_ENABLED=true`. See
[Cloud Plans & Smart Credits](plans-and-credits.md) for what the plans include.

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_CLOUD_ENABLED` | `false` | Plan limits, smart-credit metering and billing |
| `REMEMBRA_PADDLE_PRICE_SOLO_MONTHLY` | - | Paddle price ID (`pri_...`) for Solo $12/mo |
| `REMEMBRA_PADDLE_PRICE_SOLO_ANNUAL` | - | Paddle price ID for Solo $120/yr |
| `REMEMBRA_PADDLE_PRICE_PRO_MONTHLY` | - | Paddle price ID for Pro $29/mo |
| `REMEMBRA_PADDLE_PRICE_PRO_ANNUAL` | - | Paddle price ID for Pro $290/yr |
| `REMEMBRA_PADDLE_PRICE_TEAM_SEAT_MONTHLY` | - | Paddle price ID for Team $15/seat/mo (quantity-based; set `quantity.minimum = 3` on the Paddle price) |
| `REMEMBRA_PADDLE_PRICE_TEAM_SEAT_ANNUAL` | - | Paddle price ID for Team $150/seat/yr |
| `REMEMBRA_PADDLE_PRICE_FOUNDING_ANNUAL` | - | Paddle price ID for Founding 100 Solo $108/yr |
| `REMEMBRA_MEMORY_CAP_NOTICE_EFFECTIVE_AT` | - | When reduced memory caps start to apply (notice date + 30 days). Unset keeps the previous caps |
| `REMEMBRA_FREE_BREAKER_ENABLED` | `true` | Pause all free-tier AI enrichment once the month's free AI budget is spent |
| `REMEMBRA_FREE_BREAKER_MIN_USD` | `50` | Free-tier AI budget floor per month |
| `REMEMBRA_FREE_BREAKER_REVENUE_PCT` | `0.20` | Budget as a share of last month's net paid revenue, when known |
| `REMEMBRA_CREDIT_RESERVATION_STALE_MINUTES` | `15` | Credit holds older than this whose work is no longer running are released (charged at the chunk minimum) |
| `REMEMBRA_UNVERIFIED_CREDIT_CAP_EFFECTIVE_AT` | - | Free accounts created at or after this time get 25 credits until the email is verified. Unset = off. Enable it only once the dashboard verify-email page (`/verify-email`) is live. Applies to dashboard signups (verify via `/auth/verify-email`) and to `/cloud/signup` tenants (verify via `/cloud/verify-email`); accounts from Sign in with GitHub / Google start verified |
| `REMEMBRA_ANNUAL_CREDIT_UPFRONT_MONTHS` | `12` | Annual plans: months of credits available in the first month, one more each month after (`12` = whole bank up front) |
| `REMEMBRA_TYPESAFE_USD_PER_REQUEST` | `0.0005` | Dollar cost metered per TypeSafe (Jev) request. Set it to your TypeSafe contract price |
| `REMEMBRA_ENRICHMENT_GLOBAL_CONCURRENCY` | `16` | Enrichment jobs running at once across all tenants |
| `REMEMBRA_ENRICHMENT_DEFAULT_CONCURRENCY` | `4` | Per-tenant enrichment concurrency when no plan applies |
| `REMEMBRA_ENRICHMENT_MAX_PENDING_PER_TENANT` | `200` | Queued enrichment jobs per tenant before entity linking is skipped |

A missing Paddle price ID makes checkout for that plan unavailable (HTTP 503
with a clear message); nothing falls back to a guessed price. Webhooks map
plans from the price ID only: an event whose price is not configured here is
ignored and logged (`paddle_event_unknown_price`), never read from
`custom_data`. Team and Founding 100 are sold only through
`POST /api/v1/billing/checkout` (the client config lists single-quantity
plans only).

A webhook credits a **new** purchase to an account only when its
`custom_data.remembra_user_id` carries the server's signature
(`custom_data.remembra_binding`, set by server checkout or handed to the
signed-in account by `GET /api/v1/billing/client-config`), or when the Paddle
customer is the one recorded for that account. Renewals and changes of a
subscription the account already holds need neither. Events only change the
subscription the account holds: a cancel or update for another subscription
changes nothing, and a payment for a second subscription while one is active is
not applied (the account gets `billing_flag = second_subscription_review` and
the operator alert fires). Checkout returns 409 for an account that already
holds an active subscription; plan changes go through the Paddle portal.

## Signup protection

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_TURNSTILE_SECRET` | - | Cloudflare Turnstile secret. When set, `POST /api/v1/auth/signup` and `POST /api/v1/cloud/signup` require a Turnstile token (`turnstile_token` field or `CF-Turnstile-Response` header), verified server-side |
| `REMEMBRA_TURNSTILE_SITE_KEY` | - | Turnstile site key (public). Published by `GET /api/v1/auth/providers` while the secret is also set; the dashboard then renders the widget on Sign up. Set both or neither |
| `REMEMBRA_SIGNUP_IP_RATE_LIMIT` | `3/hour` | Signups per client network (/24 for IPv4, /56 for IPv6) |
| `REMEMBRA_SIGNUP_DOMAIN_RATE_LIMIT` | `20/day` | Signups per email domain |
| `REMEMBRA_SIGNUP_ATTEMPT_IP_RATE_LIMIT` | `30/hour` | Signup attempts per client network before Turnstile runs (only with Turnstile on). The two limits above are charged only after Turnstile passes |
| `REMEMBRA_TRUST_CLOUDFLARE_PROXIES` | `true` | Behind Cloudflare, take the client IP from `CF-Connecting-IP` when the forwarded chain reaches a Cloudflare edge range (only when the direct peer is a trusted proxy) |
| `REMEMBRA_SIGNUP_DOMAIN_LIMIT_EXEMPT` | major mailbox providers | JSON list of domains exempt from the per-domain limit |
| `REMEMBRA_RATE_LIMIT_STORAGE` | `memory` | Rate-limit backend for all limits: `memory` (per process) or `redis://host:6379/0` (shared; needs the `redis` package from the `cloud` extra) |

## Example .env File

```bash
# Required
REMEMBRA_OPENAI_API_KEY=sk-your-key-here
REMEMBRA_JWT_SECRET=replace-with-64-random-hex-characters

# Server
REMEMBRA_HOST=0.0.0.0
REMEMBRA_PORT=8787

# Database
REMEMBRA_DATABASE_URL=sqlite:////data/remembra.db
REMEMBRA_QDRANT_URL=http://qdrant:6333

# Security (enable in production!)
REMEMBRA_AUTH_ENABLED=true
REMEMBRA_AUTH_MASTER_KEY=your-secure-master-key
REMEMBRA_RATE_LIMIT_ENABLED=true

# Extraction
REMEMBRA_SMART_EXTRACTION_ENABLED=true
REMEMBRA_EXTRACTION_MODEL=gpt-4o-mini

# Retrieval
REMEMBRA_ENABLE_HYBRID_SEARCH=true
REMEMBRA_ENABLE_RERANKING=false
REMEMBRA_CONTEXT_MAX_TOKENS=4000

# Temporal
REMEMBRA_DEFAULT_TTL_DAYS=365
```
