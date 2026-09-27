# Operations

How to run a Remembra server built from `Dockerfile.cloud` (the image Remembra Cloud runs): checking a
deploy, backups, migrations, health and alerts, sign-in providers and email. To get a server running first,
see [Docker Deployment](getting-started/docker.md); every setting is in
[Configuration](reference/configuration.md).

The examples use `API=https://memory.example.com` for your API's public URL and `CTR` for the running API
container:

```bash
API=https://memory.example.com
CTR=$(docker ps -qf name=remembra | head -1); echo "$CTR"   # adjust the name filter to your deployment
```

## Checking a deploy

A deploy is done when these pass:

```bash
curl -s "$API/health"                                                   # status ok, build_sha = the deployed commit
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" "$API/v1/__check__"   # 404 application/json
curl -s -o /dev/null -D - "$API/health" | grep -i x-request-id          # present
curl -s "$API/health/ready" | python3 -m json.tool                      # status ok (see Health, readiness, metrics)
```

`REMEMBRA_BUILD_SHA` (a build argument of `Dockerfile.cloud`) is what `/health` reports as `build_sha`; when
it is unset the image falls back to `SOURCE_COMMIT`, which some deploy tools set.

## Working on the database

The image has **no `sqlite3` command-line tool** (`Dockerfile.cloud` installs only `curl` and `libssl3`),
and only `scripts/maintenance/*.py` is copied into it, not the `.sql` files. Use the Python `sqlite3`
module the image does have. The database is `/data/remembra.db` on the `/data` volume unless
`REMEMBRA_DATABASE_URL` says otherwise:

```bash
docker exec "$CTR" sh -c 'echo "$REMEMBRA_DATABASE_URL"'   # sqlite+aiosqlite:////data/remembra.db -> /data/remembra.db
```

**Consistent copy** (SQLite's online backup API, safe while the app is writing):

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker exec -i "$CTR" python - /data/remembra.db "/data/remembra-manual-$STAMP.db" <<'PY'
# consistent copy
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
src.backup(dst)
dst.close()
src.close()
chk = sqlite3.connect(dst_path)
print(dst_path, chk.execute("PRAGMA integrity_check").fetchone()[0],
      "memories:", chk.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
chk.close()
PY
docker cp "$CTR:/data/remembra-manual-$STAMP.db" .   # keep a copy off the volume too
```

Expected: the path, `ok` and the memory count. Delete manual copies once you no longer need them: they are
never aged out, and they keep data that account erasure has since removed.

## Automatic pre-migration backup

Every new build copies the database **before** any schema migration runs, with the same online backup API,
then checks the copy (`PRAGMA quick_check`):

- **Where:** `/data/backups/remembra-predeploy-<build>-<UTC time>.db` (a `backups` folder next to the
  database; files `0600`, folder `0700`). `<build>` is the first 12 characters of the build's commit
  (`REMEMBRA_BUILD_SHA`, or `SOURCE_COMMIT`).
- **When:** once per build. A restart of the same build does not copy again, and a fresh volume with no
  database has nothing to copy.
- **How many:** the newest 3 are kept; older ones are deleted after a new copy is written.
- **If it fails** (not enough free space for 1.2 × the database and its WAL plus 64 MB, an I/O error, a copy
  that fails its check): the new container exits before migrating and the database is left untouched. Free
  space, or deliberately skip the copy with `REMEMBRA_PRE_MIGRATION_BACKUP=false`, then redeploy.
- **It is on the same volume** as the database. It protects against a bad migration, not against losing the
  volume: turn on litestream (below) and copy important snapshots off the volume.

| Variable | Default | Meaning |
|---|---|---|
| `REMEMBRA_PRE_MIGRATION_BACKUP` | `true` | Take the copy before migrations run |
| `REMEMBRA_PRE_MIGRATION_BACKUP_KEEP` | `3` | Copies to keep, newest first (1 to 50) |
| `REMEMBRA_PRE_MIGRATION_BACKUP_DIR` | `backups` next to the database (`/data/backups`) | Where the copies go |

To restore one, stop the app and put it in place of the database (this loses every write made after the
copy was taken):

```bash
docker exec "$CTR" ls -la /data/backups
VOL=$(docker inspect "$CTR" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'); echo "$VOL"
RUID=$(docker exec "$CTR" id -u remembra); RGID=$(docker exec "$CTR" id -g remembra)
docker stop "$CTR"
docker run --rm -v "$VOL":/data alpine sh -c "cp /data/backups/remembra-predeploy-<build>-<time>.db /data/remembra.db \
  && rm -f /data/remembra.db-wal /data/remembra.db-shm && chown $RUID:$RGID /data/remembra.db && ls -la /data"
```

The copy was taken by `<build>` before it migrated, so it matches the image that ran before `<build>`: start
that one. Then `docker exec "$CTR" python -m remembra.storage.reconcile` reports SQLite and Qdrant drift
(vectors written after the copy show as orphan vectors; the report changes nothing).

## Rolling back past the plans v2 migration

With cloud billing on, the first boot of the relay-launch release (plans v2) rewrites paid `pro` / `team`
tenants to `legacy_pro_49` / `legacy_team_199` (recorded in `cloud_migrations`), and new checkouts can write
`solo`. Releases before it (`b034314`) cannot parse those plans: every store and usage call of those accounts
returns 500. To roll back, take a consistent copy (above), apply `scripts/maintenance/rollback_plans_v2.sql`
to the live database, then deploy the older image. The script is not in the image, so copy it in from a
checkout of this repository:

```bash
docker cp scripts/maintenance/rollback_plans_v2.sql "$CTR:/tmp/rollback_plans_v2.sql"
docker exec -i "$CTR" python - /data/remembra.db /tmp/rollback_plans_v2.sql <<'PY'
# apply rollback
import sqlite3, sys
db_path, sql_path = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db_path)
with open(sql_path) as f:
    conn.executescript(f.read())
print(conn.execute("SELECT plan, COUNT(*) FROM cloud_tenants GROUP BY plan").fetchall())
conn.close()
PY
```

The printed plans must be only `free`, `pro`, `team` and `enterprise`. The script maps the plans back
(`solo` becomes `pro`), clears the migration record, and deactivates agent-bound API keys (the old image
ignores `agent_id`, so they would act for the whole account). Review accounts that bought a new plan during
the deploy window by hand; deploying relay-launch again re-runs the migration.

## Backups (litestream)

`Dockerfile.cloud` ships litestream. To enable continuous SQLite replication, set:

```
LITESTREAM_REPLICA_URL=s3://<bucket>/remembra   # plus the bucket's credentials env vars
```

On boot with an empty volume, the entrypoint restores the database from the replica automatically. **If
that restore fails, the container exits** rather than booting on an empty database; set
`LITESTREAM_ALLOW_EMPTY_START=1` to override deliberately. Without `LITESTREAM_REPLICA_URL`, litestream is
inert and the entrypoint prints a warning on every boot that SQLite is not backed up.

Retention: the replica keeps `LITESTREAM_RETENTION` of history (default `24h`), so data erased from the live
database leaves the replica within about 48 hours. If you publish a retention promise, change both together.

Restore drill, on a scratch host:

```bash
litestream restore -o /tmp/drill.db "$LITESTREAM_REPLICA_URL"
python3 -c "import sqlite3; print(sqlite3.connect('/tmp/drill.db').execute('SELECT COUNT(*) FROM memories').fetchone()[0])"
```

and compare the count with the live server. Qdrant is not backed up: it is rebuilt from SQLite by the
rebuild reindex (below).

## Health, readiness, metrics

| Endpoint | Purpose | Status codes |
|---|---|---|
| `GET /health` | **Liveness.** Cheap; checks Qdrant over the gRPC client the app uses. This is what the Docker `HEALTHCHECK` calls. | 200 ok / 503 if Qdrant is unreachable |
| `GET /health/ready` | **Readiness.** SQLite, Qdrant (and the collection's dimension), the embedding provider (circuit breaker state, last error kind, missing key, cached active probe), the LLM breaker, the reranker, the pending-embedding queue. | **Always 200**; body `status` is `ok` or `degraded` with `degraded_components` |
| `GET /metrics` | Prometheus text format. Requires `Authorization: Bearer $REMEMBRA_METRICS_TOKEN`. | 404 when no token is configured, 401 on a wrong token |

`/health/ready` never returns 5xx on purpose: **do not** wire it to container restarts, or a provider outage
(for example an exhausted OpenAI quota) becomes a restart loop. Use it for monitoring and alerts. Its active
embedding probe makes at most one real embedding call per `REMEMBRA_READINESS_PROBE_INTERVAL_SECONDS`
(default 300), and none while the embedding circuit is open.

What "degraded" means per component:

- `embeddings.reason = quota_exhausted`: the provider account is out of credits. With the defaults, stores are
  kept (status `pending`, keyword-searchable, embedded later by the worker) and recalls answer from keyword and
  graph search (`degraded: keyword_only`). Only when `REMEMBRA_STORE_PENDING_ON_EMBEDDING_FAILURE` /
  `REMEMBRA_RECALL_KEYWORD_FALLBACK` are turned off (and on other routes that must embed) do they return
  **503** with `Retry-After: 900`. Top up the account; the circuit probes again after
  `REMEMBRA_PROVIDER_QUOTA_RESET_SECONDS` (default 900 s) and closes itself.
- `embeddings.reason = auth` / `missing_credentials`: fix or rotate the key.
- `embeddings.reason = rate_limited`: transient; clients get 429 with the upstream `Retry-After`.
- `qdrant.reason = dimension_mismatch`: the collection was built for a different embedding model. Fix
  `REMEMBRA_EMBEDDING_MODEL` / `_DIMENSIONS`, or run a rebuild reindex.
- `reranker`: `sentence-transformers` is missing while `REMEMBRA_ENABLE_RERANKING=true`.
- `pending_embeddings.status = warn`: dead-lettered rows exist (see Drift repair).

Key metrics: `remembra_embedding_errors_total{provider,kind}`, `remembra_store_failures_total{reason}`,
`remembra_recall_failures_total{reason}`, `remembra_recall_degraded_total{mode}`,
`remembra_circuit_breaker_state{name}` (0 closed, 1 half-open, 2 open),
`remembra_circuit_breaker_opens_total{name,kind}`, `remembra_pending_embeddings{status}`,
`remembra_reconcile_drift{type}`, `remembra_llm_fallbacks_total{component}`. The names are part of the
operations contract: dashboards and alerts depend on them.

### Operator alerts

The first time a provider circuit opens because of `quota_exhausted` or `auth`, Remembra sends one alert,
then stays quiet for `REMEMBRA_ALERT_COOLDOWN_SECONDS` (default 3600):

- `REMEMBRA_ALERT_WEBHOOK_URL`: a JSON POST (with a Slack/Discord-style `text` field).
- `REMEMBRA_ALERT_EMAIL`: an email via Resend (needs `REMEMBRA_RESEND_API_KEY`).

Without either, the alert is still logged at `critical` (`operator_alert`). Cloud billing events (payments
that match no account, refunds, billing flags) use the same channels.

## Reliability settings (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `REMEMBRA_EMBEDDING_TIMEOUT_SECONDS` | 20 | Per-request embedding HTTP timeout (connect 5 s) |
| `REMEMBRA_EMBEDDING_BREAKER_FAILURE_THRESHOLD` | 5 | Consecutive 5xx/timeout/429 failures before the circuit opens |
| `REMEMBRA_EMBEDDING_BREAKER_RESET_SECONDS` | 30 | Open, then one half-open probe after this long |
| `REMEMBRA_PROVIDER_QUOTA_RESET_SECONDS` | 900 | Open time after `quota_exhausted` (embeddings and LLM) |
| `REMEMBRA_LLM_TIMEOUT_SECONDS` / `REMEMBRA_LLM_MAX_RETRIES` | 20 / 1 | Extraction and consolidation OpenAI client |
| `REMEMBRA_READINESS_PROBE_INTERVAL_SECONDS` | 300 | At most one active embedding probe per interval |
| `REMEMBRA_PENDING_EMBEDDINGS_WORKER_ENABLED` | true | Background re-embedding worker |
| `REMEMBRA_PENDING_EMBEDDINGS_POLL_SECONDS` / `_BATCH_SIZE` / `_MAX_ATTEMPTS` | 5 / 20 / 12 | Worker tuning |
| `REMEMBRA_TEMPORAL_CLEANUP_ENABLED` / `_INTERVAL_SECONDS` | true / 3600 | TTL loop; expired memories are **archived** (restorable), not deleted |
| `REMEMBRA_RECONCILE_INTERVAL_HOURS` | 24 | Report-only drift scan (0 disables) |
| `REMEMBRA_QDRANT_INIT_RETRIES` | 5 | Startup Qdrant attempts (1, 2, 4, 8 s backoff) |
| `REMEMBRA_BACKGROUND_TASK_CONCURRENCY` | 16 | Bound for tracked background work |
| `REMEMBRA_METRICS_TOKEN` | unset | Enables `/metrics` |
| `REMEMBRA_ALERT_WEBHOOK_URL` / `REMEMBRA_ALERT_EMAIL` / `REMEMBRA_ALERT_COOLDOWN_SECONDS` | unset / unset / 3600 | Operator alerts |

## Drift repair and vector-store rebuild

```bash
# Inside the container: report SQLite <-> Qdrant <-> FTS drift (JSON on stdout)
python -m remembra.storage.reconcile
# ...and repair: re-queue rows missing a vector (the worker re-embeds them),
# index rows missing from FTS, drop FTS rows with no memory.
python -m remembra.storage.reconcile --repair
```

Orphan vectors (Qdrant points with no SQLite row) are **reported only**: one can be the only surviving copy
of a memory, so they are never deleted automatically.

**Qdrant lost or corrupted?** A global reindex runs in *rebuild* mode: it creates a new collection,
re-embeds every non-source memory from SQLite with the full payload, then swaps the app to it (persisted in
the SQLite table `vector_store_state`, honored at boot). The previous collection is kept for rollback. Jobs
are resumable (`reindex_jobs.cursor`); a restart marks a running job `interrupted`, a provider outage marks
it `paused`.

## Image contents

`Dockerfile.cloud` installs the Python dependencies at the exact versions in `uv.lock`
(`scripts/install-locked-deps.sh`), including the `rerank` extra with **CPU-only** PyTorch, and bakes the
default reranker model into `HF_HOME=/opt/hf-cache` (about 1.2 GB of site-packages plus a 90 MB model). The
litestream tarball is verified against a pinned SHA-256.

## Dashboard and docs images

`dashboard/Dockerfile` (the dashboard) and `docs.Dockerfile` (this documentation site) serve static files
with nginx running as the unprivileged `nginx` user on port **8080**, like `landing/Dockerfile`. Each has its
own full config (`dashboard/nginx.conf`, `docs-nginx/nginx.conf`). Every base image is pinned by tag and
`@sha256` digest, and Dependabot (docker) proposes digest updates. The docs image also sends the security
headers in `docs-nginx/remembra-headers.conf`: HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`,
`Permissions-Policy`, and a report-only CSP whose `script-src` hashes `scripts/docs_csp.py` recomputes from
the built site on every image build.

Upgrading from an image that listened on port 80: change the container port your proxy or deploy tool sends
traffic to from 80 to 8080 in the same deploy, or the proxy gets no answer (502).

Check after the deploy (with your own hosts):

```bash
DASH=https://app.example.com; DOCS=https://docs.example.com
curl -s -o /dev/null -w '%{http_code}\n' "$DASH/health"                   # 200
curl -sI "$DOCS/" | grep -iE 'strict-transport|x-frame|x-content-type|referrer-policy|content-security'
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$DOCS/guides"   # 301 .../guides/
curl -s "$DOCS/no-such-page" | grep -c 'nginx/'                          # 0 (the MkDocs 404 page)
```

## Social sign-in (GitHub, Google)

The API is the OAuth client. The provider sends the browser back to the API, and the API sends it on to the
dashboard's `/oauth/callback` page with a one-time login code. Register exactly these redirect URIs (they
are `<REMEMBRA_PUBLIC_URL>/api/v1/auth/oauth/<provider>/callback`; shown here for
`REMEMBRA_PUBLIC_URL=https://memory.example.com`):

| Provider | Where | Value |
|---|---|---|
| GitHub | github.com, Settings, Developer settings, **OAuth Apps**, *Authorization callback URL* | `https://memory.example.com/api/v1/auth/oauth/github/callback` |
| Google | Google Cloud console, APIs & Services, Credentials, **OAuth client ID** (type *Web application*), *Authorized redirect URIs* | `https://memory.example.com/api/v1/auth/oauth/google/callback` |

- GitHub asks for the `user:email` scope, Google for `openid email profile`. Google needs no *Authorized
  JavaScript origins*: the exchange is server side.
- A GitHub OAuth App takes one callback URL, so use a separate app for a staging host. Google takes several
  redirect URIs on one client.
- Set `REMEMBRA_PUBLIC_URL` (the API), `REMEMBRA_PUBLIC_DASHBOARD_URL` (the dashboard), and
  `REMEMBRA_GITHUB_CLIENT_ID` / `_SECRET`, `REMEMBRA_GOOGLE_CLIENT_ID` / `_SECRET`. A provider appears on the
  sign-in page only when its id, its secret and both public URLs are set.
- The dashboard and the API must be the same site (for example `app.` and `api.` of one domain), or the
  browser does not send the cookie that binds the login code to it.

Check after setting the variables:

```bash
curl -s "$API/api/v1/auth/providers"                                    # lists github / google
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$API/api/v1/auth/oauth/github/start"
#   302 https://github.com/login/oauth/authorize?...redirect_uri=<your callback URI, URL-encoded>...
```

## Transactional email (Resend)

With `REMEMBRA_RESEND_API_KEY` set, the API sends: the welcome at signup (with the verify link and the
install lines, never an API key), email verification, password reset, "a new API key was created", plan
changes, payment failures, subscription ends, memory-cap warnings, team invites and "sign-in method added".
Without the key, nothing is sent and nothing fails.

- Content: `src/remembra/cloud/email_templates.py`. Every email has an HTML and a plain-text part; links go
  to `REMEMBRA_PUBLIC_DASHBOARD_URL`.
- Sender: `REMEMBRA_EMAIL_FROM`, with `Reply-To: REMEMBRA_EMAIL_REPLY_TO`, so that mailbox must receive mail.
- Preview every email without sending: `python scripts/preview_emails.py` (writes `build/email-previews/`).
  With the key set, `python scripts/preview_emails.py --send-to you@example.com` sends each sample to that one
  address, to check delivery, the text part and Reply-To.

## Content-Security-Policy: report-only

The dashboard (`dashboard/security-headers.conf`), the marketing site (`landing/remembra-headers.conf`,
written by `scripts/site_csp.py`) and the docs site (`docs-nginx/remembra-headers.conf`, written by
`scripts/docs_csp.py`) send their CSP as `Content-Security-Policy-Report-Only`. The browser blocks
nothing; it reports each thing the policy would have blocked to the API's `POST /api/v1/csp-report`, and the
API logs one `csp_violation` line per report (page origin and path, directive, blocked origin or keyword,
never a query string). HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` and
COOP stay enforced; `X-Frame-Options` covers `frame-ancestors`, which browsers ignore in report-only mode.
The API's own JSON CSP (`default-src 'none'`) is enforced.

Deploy the API before the sites (it serves the report endpoint). If `REMEMBRA_CORS_ORIGINS` is set, it must
include the sites' origins, or browsers drop the reports at the CORS check. Reports whose `blocked` is
`chrome-extension`, `moz-extension` or `safari-web-extension` come from visitors' browser extensions and can
be ignored; anything else is a host to add to the policy or a page to fix.

**Switch to enforcing** after a week with no reports other than extension noise (including a completed
checkout, if you take payments):

1. Marketing site: in `scripts/site_csp.py` set `CSP_HEADER = "Content-Security-Policy"`, run
   `python scripts/site_csp.py`, and set `CSP_HEADER` the same in `tests/test_landing_nginx.py`.
2. Dashboard: in `dashboard/security-headers.conf` rename the header to `Content-Security-Policy` and update
   its comment; set `CSP_HEADER` the same in `tests/test_dashboard_nginx.py`.
3. Docs site: in `scripts/docs_csp.py` set `CSP_HEADER = "Content-Security-Policy"`, run
   `python scripts/docs_csp.py <built site>`, and update `tests/test_docs_nginx.py` to read that header.
4. Keep `report-uri`: enforced violations are still reported.
5. Run the tests and `python scripts/site_csp.py --check`, redeploy the sites, and check the header with
   `curl -sI`.

To go back, set the header name to `Content-Security-Policy-Report-Only` again and redeploy.

## Account deletion and erasure

`DELETE /api/v1/auth/me` (with the password, or a code from `POST /api/v1/auth/me/deletion-code` for Google
and GitHub accounts) cancels the account's paid subscription (cloud billing), deactivates the account at once
(sessions and API keys stop working) and schedules erasure. The `account-erasure-loop` task (every
`REMEMBRA_ACCOUNT_ERASURE_INTERVAL_SECONDS`, default 3600) erases accounts deleted more than
`REMEMBRA_ACCOUNT_ERASURE_GRACE_DAYS` ago (default 7, max 30): their Qdrant points (in the active collection
and every rollback copy a rebuild kept), then every SQLite row they own, in one transaction. It keeps one
`account_erased` audit row with a SHA-256 of the account id and row counts.

Inside the grace period, `POST /api/v1/admin/users/{id}/activate?active=true` undoes a deletion;
`DELETE /api/v1/admin/users/{id}?confirm=true` erases at once. Backups are not rewritten: pre-migration
copies age out after 3 more deploys, the litestream replica within about 48 hours, and manual copies never,
so delete those yourself.
