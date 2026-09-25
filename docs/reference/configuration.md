# Configuration Reference

All environment variables for Remembra.

## Required

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key | `sk-...` |

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HOST` | `0.0.0.0` | Server bind address |
| `REMEMBRA_PORT` | `8787` | Server port |
| `REMEMBRA_WORKERS` | `1` | Number of worker processes |
| `REMEMBRA_LOG_LEVEL` | `INFO` | Logging level |

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DATABASE_PATH` | `./remembra.db` | SQLite database path |
| `QDRANT_HOST` | `localhost` | Qdrant server host |
| `QDRANT_PORT` | `6333` | Qdrant server port |
| `QDRANT_API_KEY` | - | Qdrant API key (if secured) |
| `QDRANT_COLLECTION` | `remembra` | Qdrant collection name |

## Embeddings

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | Provider: `openai`, `ollama`, `cohere` |
| `REMEMBRA_EMBEDDING_MODEL` | `text-embedding-3-small` | Model name |
| `REMEMBRA_EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `COHERE_API_KEY` | - | Cohere API key |

## Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SMART_EXTRACTION_ENABLED` | `true` | Enable LLM extraction |
| `REMEMBRA_EXTRACTION_MODEL` | `gpt-4o-mini` | Model for extraction |
| `REMEMBRA_EXTRACTION_TEMPERATURE` | `0.0` | Extraction temperature |

## Entity Resolution

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENTITY_EXTRACTION_ENABLED` | `true` | Extract entities |
| `REMEMBRA_ENTITY_MATCHING_THRESHOLD` | `0.85` | Alias matching threshold |

## Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_THRESHOLD` | `0.40` | Default similarity threshold |
| `REMEMBRA_DEFAULT_LIMIT` | `10` | Default recall limit |
| `REMEMBRA_DEFAULT_MAX_TOKENS` | `4000` | Max context tokens |

### Hybrid Search

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HYBRID_SEARCH_ENABLED` | `true` | Enable hybrid search |
| `REMEMBRA_HYBRID_ALPHA` | `0.4` | Keyword weight (0-1) |
| `REMEMBRA_HYBRID_FUSION` | `weighted` | Fusion: `weighted` or `rrf` |

### Reranking

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RERANK_ENABLED` | `false` | Enable CrossEncoder reranking |
| `REMEMBRA_RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranker model |
| `REMEMBRA_RERANK_TOP_K` | `20` | Candidates to rerank |

### Ranking Weights

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RANKING_SEMANTIC_WEIGHT` | `0.6` | Semantic score weight |
| `REMEMBRA_RANKING_RECENCY_WEIGHT` | `0.15` | Recency boost weight |
| `REMEMBRA_RANKING_ENTITY_WEIGHT` | `0.15` | Entity match weight |
| `REMEMBRA_RANKING_KEYWORD_WEIGHT` | `0.1` | Keyword match weight |
| `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` | `30` | Recency half-life (days) |

### Graph Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_GRAPH_RETRIEVAL_ENABLED` | `true` | Enable graph traversal |
| `REMEMBRA_GRAPH_TRAVERSAL_DEPTH` | `2` | Max hop depth |

## Temporal

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DEFAULT_TTL_DAYS` | - | Default TTL (optional) |
| `REMEMBRA_DECAY_ENABLED` | `true` | Enable memory decay |
| `REMEMBRA_DECAY_HALF_LIFE_DAYS` | `30` | Decay half-life |
| `REMEMBRA_ACCESS_BOOST_WEIGHT` | `0.2` | Access count boost |

## Security

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTH_ENABLED` | `true` | Enable API key auth |
| `REMEMBRA_AUTH_MASTER_KEY` | - | Master admin key |

### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |
| `REMEMBRA_RATE_LIMIT_STORAGE` | `memory` | Backend: `memory` or `redis://...` |
| `REMEMBRA_RATE_LIMIT_STORE` | `30/minute` | Store endpoint limit |
| `REMEMBRA_RATE_LIMIT_RECALL` | `60/minute` | Recall endpoint limit |
| `REMEMBRA_RATE_LIMIT_FORGET` | `10/minute` | Forget endpoint limit |

### Sanitization

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_SANITIZATION_ENABLED` | `true` | Enable input sanitization |
| `REMEMBRA_TRUST_SCORE_THRESHOLD` | `0.5` | Suspicious content threshold |

## Dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_STATIC_DIR` | - | Path to dashboard build |

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

## Auto-Forgetting (v0.12.0)

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTO_TTL_ENABLED` | `true` | Enable smart auto-forgetting |
| `REMEMBRA_STRICT_MODE` | `false` | Return 410 GONE for expired memories |

Smart auto-forgetting detects 35+ temporal patterns and sets appropriate TTLs:

- "meeting tomorrow" → 36 hours
- "call next week" → 8 days
- "deadline in 2 hours" → 3 hours
- "event next month" → 35 days

No configuration needed—just store memories naturally.

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
OPENAI_API_KEY=sk-your-key-here

# Server
REMEMBRA_HOST=0.0.0.0
REMEMBRA_PORT=8787

# Database
REMEMBRA_DATABASE_PATH=/app/data/remembra.db
QDRANT_HOST=qdrant
QDRANT_PORT=6333

# Security (enable in production!)
REMEMBRA_AUTH_ENABLED=true
REMEMBRA_AUTH_MASTER_KEY=your-secure-master-key
REMEMBRA_RATE_LIMIT_ENABLED=true

# Extraction
REMEMBRA_SMART_EXTRACTION_ENABLED=true
REMEMBRA_EXTRACTION_MODEL=gpt-4o-mini

# Retrieval
REMEMBRA_HYBRID_SEARCH_ENABLED=true
REMEMBRA_RERANK_ENABLED=false
REMEMBRA_DEFAULT_MAX_TOKENS=4000

# Temporal
REMEMBRA_DEFAULT_TTL_DAYS=365
REMEMBRA_DECAY_ENABLED=true
```
