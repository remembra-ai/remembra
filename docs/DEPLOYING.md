# Deploying Remembra Cloud

**Production truth (as of 2026-07-16):** `api.remembra.dev` runs as the Coolify
application **`remembra-api`** (project `remembra`, uuid `s8sk8kw4gg8oo044808og8kg`)
on a Hetzner server in the US, behind Cloudflare (the origin address and the Coolify URL are kept in the
private operations notes, not in this public repo). It builds
**`Dockerfile.cloud`** from `remembra-ai/remembra` branch `main`.

> The `fly.toml` in this repo is a portable config for a possible future
> Fly.io region/staging environment. It is **not** the production deployment.

## Deploy flow

1. Commit and push to `main` (CI must be green).
2. Trigger the Coolify rebuild — either:
   - **UI:** Coolify (its URL is in the private operations notes) → project *remembra* →
     *remembra-api* → **Redeploy**, or
   - **Hands-off (SSH):**

     ```bash
     ssh coolify 'docker exec coolify php artisan tinker --execute="
       \$app = App\Models\Application::where(\"uuid\",\"s8sk8kw4gg8oo044808og8kg\")->first();
       queue_application_deployment(
         application: \$app,
         deployment_uuid: (string) new Visus\Cuid2\Cuid2(),
         force_rebuild: true,
         no_questions_asked: true,
       );"'
     ```

3. Watch status until `finished`:

   ```bash
   ssh coolify 'docker exec coolify php artisan tinker --execute="
     echo App\Models\ApplicationDeploymentQueue::latest()->first()->status;"'
   ```

4. **Verify live** (the deploy is not done until these pass):

   ```bash
   # new container runs the pushed commit
   ssh coolify 'docker inspect $(docker ps -qf name=s8sk8kw4gg8oo) \
     --format "{{range .Config.Env}}{{println .}}{{end}}" | grep SOURCE_COMMIT'

   # API contract checks
   curl -s -o /dev/null -w "%{http_code} %{content_type}\n" https://api.remembra.dev/v1/__check__   # 404 application/json
   curl -s https://api.remembra.dev/health                                                          # status ok, build_sha = pushed commit
   curl -s -o /dev/null -D - https://api.remembra.dev/health | grep -i x-request-id                 # present
   curl -s https://api.remembra.dev/health/ready | python3 -m json.tool                            # status ok (see below)
   ```

## Rolling back past the plans v2 migration

The first boot of the relay-launch image rewrites paid `pro` / `team` tenants
to `legacy_pro_49` / `legacy_team_199` (recorded in `cloud_migrations`), and new
checkouts can write `solo`. The pre-launch image (`b034314`) cannot parse those
plans: every store and usage call of those accounts returns 500. So:

1. **Before deploying** relay-launch, take a copy (litestream may not be on):

   ```bash
   sqlite3 /data/remembra.db ".backup /data/remembra-pre-relay-launch.db"
   ```

2. **To roll back**, run the verified script against the live database, then
   redeploy `b034314`:

   ```bash
   sqlite3 /data/remembra.db ".backup /data/remembra-pre-rollback.db"
   sqlite3 /data/remembra.db < scripts/maintenance/rollback_plans_v2.sql
   ```

   It maps the plans back (`solo` becomes `pro`), clears the migration record,
   and deactivates agent-bound API keys (the old image ignores `agent_id`, so
   they would act for the whole account). Review deploy-window buyers of new
   plans by hand; redeploying relay-launch later re-runs the migration.


## The website (remembra.dev)

`remembra.dev` is the static site in `landing/`, served by nginx with the config in `landing/nginx.conf`
and `landing/remembra-headers.conf`: clean URLs, the redirects for old paths (`/signup`, `/dashboard`,
`/docs/*`, retired `/changelog/<version>` pages), the security headers and CSP, `/.well-known/security.txt`
and the 404 page. `vercel.json` is not used and has been removed.

**One-time Coolify change** (owner or operator; the static build pack ignores `landing/nginx.conf`):

1. Coolify → the application that serves `remembra.dev` → **General**.
2. **Build Pack:** `Dockerfile` (instead of Static / Nixpacks).
3. **Base Directory:** `/landing`. **Dockerfile Location:** `/Dockerfile`.
4. **Ports Exposes:** `8080` (the container runs nginx as the unprivileged `nginx` user, so it cannot use 80).
5. Keep the domain `https://remembra.dev` (and `https://www.remembra.dev` if it is set). Save, then **Redeploy**.

Before deploying, run `python scripts/site_predeploy.py` (all gates cleared, docs live, PyPI has the release).

**Verify live** after the redeploy:

```bash
curl -sI https://remembra.dev/ | grep -iE 'strict-transport|content-security|x-frame|x-content-type|referrer-policy|permissions-policy'
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' 'https://remembra.dev/dashboard?checkout=success'  # 301 https://app.remembra.dev/?checkout=success
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://remembra.dev/signup                        # 301 https://app.remembra.dev/signup
curl -s -o /dev/null -w '%{http_code}\n' https://remembra.dev/refunds                                      # 200
curl -s https://remembra.dev/.well-known/security.txt | grep Expires
curl -s -o /dev/null -w '%{http_code}\n' https://remembra.dev/no-such-page                                 # 404
```

Then load the home, pricing and contact pages with the browser console open: a CSP violation there means an
inline script changed without `python scripts/site_csp.py` being run.

## Content-Security-Policy: report-only at launch

Both sites send their CSP as `Content-Security-Policy-Report-Only`: remembra.dev
(`landing/remembra-headers.conf`, written by `scripts/site_csp.py`) and app.remembra.dev
(`dashboard/security-headers.conf`). The browser blocks nothing; it reports each thing the policy would have
blocked to `https://api.remembra.dev/api/v1/csp-report`, and the API logs one `csp_violation` line per
report (page origin and path, directive, blocked origin or keyword, never a query string). HSTS, `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` and COOP stay enforced; `X-Frame-Options`
covers `frame-ancestors`, which browsers ignore in report-only mode. The API's own JSON CSP
(`default-src 'none'`, in `main.py`) is unchanged and enforced.

Why: the dashboard's Paddle hosts could not be checked against a live checkout, and Paddle publishes no CSP
list. Its docs only say to load Paddle.js from `https://cdn.paddle.com/`
([Include Paddle.js](https://developer.paddle.com/paddlejs/include-paddlejs)), which `script-src` allows.
The checkout frame hosts (`buy.paddle.com`, `sandbox-buy.paddle.com`) and `connect-src https://*.paddle.com`
are our best reading. An enforced policy with a wrong host would break checkout.

**Deploy order.** Deploy the API first (it serves `/api/v1/csp-report`); a site deployed earlier only gets
404s for its reports. If `REMEMBRA_CORS_ORIGINS` is set on the API, it must include `https://remembra.dev`
and `https://app.remembra.dev`, or browsers drop the reports at the CORS check.

**Check that it is live:**

```bash
curl -sI https://remembra.dev/ | grep -i '^content-security-policy'      # content-security-policy-report-only: ... report-uri ...
curl -sI https://app.remembra.dev/ | grep -i '^content-security-policy'  # the same header name
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/csp-report' \
  --data '{"csp-report":{"document-uri":"https://remembra.dev/","effective-directive":"script-src-elem","blocked-uri":"inline"}}' \
  https://api.remembra.dev/api/v1/csp-report                               # 204, and one csp_violation line in the API log
```

**Read the reports** in the API container's log (Coolify → remembra-api → Logs, or `docker logs`), filtered
on `csp_violation`. During the week, run a sandbox Solo checkout (overlay) and open a `/pay?_ptxn=` link so
the Paddle paths are exercised. Reports whose `blocked` is `chrome-extension`, `moz-extension` or
`safari-web-extension` come from visitors' browser extensions and can be ignored. Anything else is either a
host to add to the policy (for example a Paddle host on `/pay` or during checkout) or a page to fix.

**Switch to enforcing** after seven days with no reports other than extension noise, including at least one
completed sandbox checkout and one `/pay` link in that window:

1. remembra.dev: in `scripts/site_csp.py` set `CSP_HEADER = "Content-Security-Policy"`, run
   `python scripts/site_csp.py`, and set `CSP_HEADER` the same in `tests/test_landing_nginx.py`.
2. app.remembra.dev: in `dashboard/security-headers.conf` rename the header to `Content-Security-Policy` and
   update the comment; set `CSP_HEADER` the same in `tests/test_dashboard_nginx.py`.
3. Keep `report-uri`: enforced violations are still reported.
4. Run the checks (pytest, `python scripts/site_csp.py --check`), redeploy the dashboard and the landing, rerun
   the curl checks above (the header is now `content-security-policy`), then do one more sandbox checkout with
   the browser console open.

To go back, set the header name to `Content-Security-Policy-Report-Only` again and redeploy; nothing else changes.

## Health, readiness, metrics

| Endpoint | Purpose | Status codes |
|---|---|---|
| `GET /health` | **Liveness.** Cheap; checks Qdrant over the gRPC client the app actually uses. This is what the Docker `HEALTHCHECK` calls. | 200 ok / 503 if Qdrant unreachable |
| `GET /health/ready` | **Readiness.** SQLite, Qdrant (+ collection dimension check), embedding provider (circuit breaker state, last error kind, missing key, cached active probe), LLM breaker, reranker installed, pending-embedding queue. | **Always 200**; body `status` is `ok` or `degraded` with `degraded_components` |
| `GET /metrics` | Prometheus text format. Requires `Authorization: Bearer $REMEMBRA_METRICS_TOKEN`. | 404 when no token is configured, 401 on a wrong token |

`/health/ready` never returns 5xx on purpose — **do not** wire it to container
restarts. A provider outage (e.g. OpenAI quota exhausted) would otherwise
become a restart loop. Use it for monitoring/alerting. Its active embedding
probe makes at most one real embedding call per
`REMEMBRA_READINESS_PROBE_INTERVAL_SECONDS` (default 300) no matter how often
it is polled, and none while the embedding circuit is open.

What "degraded" means per component:

- `embeddings.reason = quota_exhausted` — the provider account is out of
  credits. Store/recall return **503** with an operator message and
  `Retry-After: 900`. Top up the account; the circuit probes again after
  `REMEMBRA_PROVIDER_QUOTA_RESET_SECONDS` (default 900 s) and closes itself.
- `embeddings.reason = auth` / `missing_credentials` — fix/rotate the key.
- `embeddings.reason = rate_limited` — transient; clients get 429 with the
  upstream `Retry-After`.
- `qdrant.reason = dimension_mismatch` — the collection was built for a
  different embedding model. Fix `REMEMBRA_EMBEDDING_MODEL`/`_DIMENSIONS` or
  run a rebuild reindex.
- `reranker` — `sentence-transformers` missing while `REMEMBRA_ENABLE_RERANKING=true`.
- `pending_embeddings.status = warn` — dead-lettered rows exist (see below).

Key metrics: `remembra_embedding_errors_total{provider,kind}`,
`remembra_store_failures_total{reason}`, `remembra_recall_failures_total{reason}`,
`remembra_recall_degraded_total{mode}`, `remembra_circuit_breaker_state{name}`
(0 closed, 1 half-open, 2 open), `remembra_circuit_breaker_opens_total{name,kind}`,
`remembra_pending_embeddings{status}`, `remembra_reconcile_drift{type}`,
`remembra_llm_fallbacks_total{component}`.

### Operator alerts

The first time a provider circuit opens because of `quota_exhausted` or
`auth`, Remembra sends one alert (then stays quiet for
`REMEMBRA_ALERT_COOLDOWN_SECONDS`, default 3600):

- `REMEMBRA_ALERT_WEBHOOK_URL` — JSON POST (includes a Slack/Discord-style `text` field).
- `REMEMBRA_ALERT_EMAIL` — email via Resend (needs `REMEMBRA_RESEND_API_KEY`).

Without either, the alert is still logged at `critical` (`operator_alert`).

## Reliability environment variables (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `REMEMBRA_EMBEDDING_TIMEOUT_SECONDS` | 20 | Per-request embedding HTTP timeout (connect 5 s) |
| `REMEMBRA_EMBEDDING_BREAKER_FAILURE_THRESHOLD` | 5 | Consecutive 5xx/timeout/429 failures before the circuit opens |
| `REMEMBRA_EMBEDDING_BREAKER_RESET_SECONDS` | 30 | Open → one half-open probe after this long |
| `REMEMBRA_PROVIDER_QUOTA_RESET_SECONDS` | 900 | Open time after `quota_exhausted` (embeddings and LLM) |
| `REMEMBRA_LLM_TIMEOUT_SECONDS` / `REMEMBRA_LLM_MAX_RETRIES` | 20 / 1 | Extraction/consolidation OpenAI client |
| `REMEMBRA_READINESS_PROBE_INTERVAL_SECONDS` | 300 | Max one active embedding probe per interval |
| `REMEMBRA_PENDING_EMBEDDINGS_WORKER_ENABLED` | true | Background re-embedding worker |
| `REMEMBRA_PENDING_EMBEDDINGS_POLL_SECONDS` / `_BATCH_SIZE` / `_MAX_ATTEMPTS` | 5 / 20 / 12 | Worker tuning |
| `REMEMBRA_TEMPORAL_CLEANUP_ENABLED` / `_INTERVAL_SECONDS` | true / 3600 | TTL loop; expired memories are **archived** (restorable), not deleted |
| `REMEMBRA_RECONCILE_INTERVAL_HOURS` | 24 | Report-only drift scan (0 disables) |
| `REMEMBRA_QDRANT_INIT_RETRIES` | 5 | Startup Qdrant attempts (1, 2, 4, 8 s backoff) |
| `REMEMBRA_BACKGROUND_TASK_CONCURRENCY` | 16 | Bound for tracked background work |
| `REMEMBRA_METRICS_TOKEN` | unset | Enables `/metrics` |
| `REMEMBRA_ALERT_WEBHOOK_URL` / `REMEMBRA_ALERT_EMAIL` / `REMEMBRA_ALERT_COOLDOWN_SECONDS` | unset / unset / 3600 | Operator alerts |

`REMEMBRA_BUILD_SHA` falls back to Coolify's `SOURCE_COMMIT` when unset, so
`/health` shows the deployed commit.

## Drift repair and vector-store rebuild

```bash
# Inside the container: report SQLite <-> Qdrant <-> FTS drift (JSON on stdout)
python -m remembra.storage.reconcile
# ...and repair: re-queue rows missing a vector (the worker re-embeds them),
# index rows missing from FTS, drop FTS rows with no memory.
python -m remembra.storage.reconcile --repair
```

Orphan vectors (Qdrant points with no SQLite row) are **reported only** — one
can be the only surviving copy of a memory, so they are never deleted
automatically.

**Qdrant lost or corrupted?** A global reindex now runs in *rebuild* mode: it
creates a new collection, re-embeds every non-source memory from SQLite with
the full payload, then swaps the app to it (persisted in SQLite table
`vector_store_state`, honored at boot). The previous collection is kept for
rollback. Jobs are resumable (`reindex_jobs.cursor`); a restart marks a running
job `interrupted`, a provider outage marks it `paused`.

## Image contents

`Dockerfile.cloud` installs Python dependencies at the exact versions in
`uv.lock` (`scripts/install-locked-deps.sh`) including the `rerank` extra with
**CPU-only** PyTorch, and bakes the default reranker model into `HF_HOME=/opt/hf-cache`.
Measured impact: site-packages 165 MB → 1.2 GB (+~1.05 GB) plus ~90 MB model;
the default CUDA torch wheels would have added ~6 GB. The litestream tarball is
verified against a pinned SHA-256.

## Backups (litestream)

`Dockerfile.cloud` ships litestream. To enable continuous SQLite replication,
set in the Coolify app's environment:

```
LITESTREAM_REPLICA_URL=s3://<bucket>/remembra   # plus the bucket's credentials env vars
```

On boot with an empty volume, the entrypoint restores the database from the
replica automatically. **If that restore fails, the container exits** rather
than booting on an empty database; set `LITESTREAM_ALLOW_EMPTY_START=1` to
override deliberately. Without `LITESTREAM_REPLICA_URL`, litestream is inert and
the entrypoint prints a warning on every boot that SQLite is not backed up.

Restore drill (OPS-4, owner): on a scratch host run
`litestream restore -o /tmp/drill.db "$LITESTREAM_REPLICA_URL"` and
`sqlite3 /tmp/drill.db 'select count(*) from memories'`; compare with prod.
Qdrant is not backed up — it is rebuilt from SQLite by the rebuild reindex.

## Notes

- `remembra-qdrant`'s Docker healthcheck on Coolify reports *unhealthy*
  because it calls `curl`, which doesn't exist in the qdrant image. The repo's
  compose files now use `bash -c ':> /dev/tcp/127.0.0.1/6333'`; apply the same
  command to the Coolify qdrant service's healthcheck (owner, Coolify UI).
- `remembra-postgres` already runs on the same host — the planned
  SQLite → Postgres migration does not need new infrastructure.
