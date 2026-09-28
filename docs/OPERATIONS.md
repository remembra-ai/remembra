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

## Scheduled snapshots

`python -m remembra.storage.snapshot create` copies the main database **and** `crew.db` (Crew mode's
database, next to it in `/data`) with SQLite's online backup API, so it is safe while the server is running.
Each run writes one folder, `remembra-snapshot-<UTC time>`, holding `remembra.db`, `crew.db` (once Crew mode
has created it) and `manifest.json` with checksums, schema versions and row counts. The folder is readable
by the server's user only (`0700`, files `0600`), like the pre-migration copies. It needs no litestream, no
bucket and no other service.

Run it once a day. On **Coolify**: the API application, **Scheduled Tasks**, **Add**:

| Field | Value |
|---|---|
| Name | `remembra-snapshot` |
| Command | `python -m remembra.storage.snapshot create --out /data/backups --keep 7` |
| Frequency | `0 3 * * *` (every day at 03:00, server time) |
| Container | the API container, if Coolify asks which one |

Any other host runs the same command in the API container from cron, for example
`0 3 * * * docker exec <api container> python -m remembra.storage.snapshot create --out /data/backups --keep 7`.
Run the command once by hand after adding the task and check its output: the manifest as JSON, with
`"crew_included": true` once Crew mode is on. A failed run prints `snapshot create failed: <why>`, exits 1 and
deletes nothing; check the task's run log after the first scheduled run too.

- **Retention:** `--keep 7` keeps the 7 newest snapshots in `--out` and deletes older `remembra-snapshot-*`
  folders only after the new one is written and checked. Nothing else in the folder is touched: the
  pre-migration copies (`remembra-predeploy-*.db`) keep their own limit, and a `*.partial` folder left by a
  crash stays until you delete it. With a daily run, data erased from the live databases leaves the
  snapshots within 7 days. If you publish a retention promise, change both together.
- **Space:** each snapshot is about the size of both databases. Check the volume's free space
  (`docker exec "$CTR" df -h /data`) before you raise `--keep`.
- **Off the volume:** snapshots on `/data` protect against a bad change, not against losing the volume. Copy
  the newest one somewhere else regularly:

```bash
docker exec "$CTR" ls /data/backups
docker cp "$CTR:/data/backups/remembra-snapshot-<stamp>" .
```

Check a snapshot, and restore one with the server stopped (a running process keeps writing to the files it
has open), from a one-off container of the same image on the same volume:

```bash
docker exec "$CTR" python -m remembra.storage.snapshot verify /data/backups/remembra-snapshot-<stamp>
VOL=$(docker inspect "$CTR" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'); echo "$VOL"
IMG=$(docker inspect "$CTR" --format '{{.Config.Image}}'); echo "$IMG"
docker stop "$CTR"
docker run --rm -v "$VOL":/data "$IMG" python -m remembra.storage.snapshot restore /data/backups/remembra-snapshot-<stamp> --force
docker start "$CTR"
```

The restore verifies the snapshot first, never deletes anything (without `--force` it refuses while the
database files exist; with it they are kept as `*.pre-restore-<stamp>`), and restores only the databases the
snapshot holds (a snapshot from before Crew mode leaves `crew.db` alone). See
[Crew mode: Backups](relay/crew.md#backups).

## Backups (litestream)

`Dockerfile.cloud` also ships litestream, for continuous replication to a bucket (a service that then holds
your data). To enable it, set:

```
LITESTREAM_REPLICA_URL=s3://<bucket>/remembra   # plus the bucket's credentials env vars
# optional; the default is a sibling of the main replica: s3://<bucket>/remembra-crew
LITESTREAM_CREW_REPLICA_URL=s3://<bucket>/remembra-crew
```

The entrypoint replicates **both** SQLite files: the main database and `crew.db`. `crew.db` is replicated
whenever it exists; with `REMEMBRA_CREW_MODE` on, the entrypoint creates it (empty, WAL) before litestream
starts, so the first crew write is already replicated.

On boot with an empty volume, the entrypoint restores each database from its replica automatically. **If a
restore fails, the container exits** rather than booting on an empty database; set
`LITESTREAM_ALLOW_EMPTY_START=1` to override deliberately. For `crew.db` this applies when Crew mode is on;
with Crew mode off a failed `crew.db` restore only warns, and `crew.db` is left out of replication for that
boot so the crew replica is never overwritten by an empty file. Without `LITESTREAM_REPLICA_URL`, litestream
is inert and the entrypoint prints a warning on every boot that SQLite is not backed up (the scheduled
snapshots above do not change that message).

Retention: each replica keeps `LITESTREAM_RETENTION` of history (default `24h`) and litestream enforces it
hourly, so data erased from a live database leaves the replica within about 25 hours. If you publish a
retention promise, change both together.

Restore drill, on a scratch host:

```bash
litestream restore -o /tmp/drill.db "$LITESTREAM_REPLICA_URL"
python3 -c "import sqlite3; print(sqlite3.connect('/tmp/drill.db').execute('SELECT COUNT(*) FROM memories').fetchone()[0])"
litestream restore -o /tmp/drill-crew.db "${LITESTREAM_CREW_REPLICA_URL:-${LITESTREAM_REPLICA_URL%/}-crew}"
python3 -c "import sqlite3; c = sqlite3.connect('/tmp/drill-crew.db'); print([c.execute(q).fetchone()[0] for q in ('SELECT MAX(version) FROM schema_version', 'SELECT COUNT(*) FROM crews', 'SELECT COUNT(*) FROM crew_events')])"
```

and compare the counts with the live server. Qdrant is not backed up: it is rebuilt from SQLite by the
rebuild reindex (below).

## Crew mode

Crew mode ships **dark**: `Dockerfile.cloud` sets `REMEMBRA_CREW_MODE=false`, and nothing crew-related runs
until your deployment's environment overrides it. The server reads the flag once at startup. What it switches
is described in [Crew mode: the feature flag](relay/crew.md#the-feature-flag).

Deploying the Crew mode release (0.17.0) changes one thing even with the flag off: the main database gets
migration **v5** (additive columns on `agent_inbox`). A row's own `metadata.project_id` is copied into the new
`project_id` column; untagged rows stay untagged, visible only to unrestricted keys and dashboard logins. A
0.16.1 database records versions 1-4 and 6-10 (released before v5), so the first boot applies only v5, after
the pre-migration backup. The highest version stays **10**, so check the full list, not the highest:

```bash
curl -s "$API/health/ready" | python3 -c 'import sys, json; s = json.load(sys.stdin)["components"]["sqlite"]; print(s["applied_versions"], s["missing_versions"])'
#   [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] []
```

Releases from before Crew mode run on the resulting database unchanged. `crew.db` is created only when Crew
mode is on, next to the main database unless `REMEMBRA_CREW_DB_PATH` moves it (then that path must be on a
persistent volume too, and the snapshot command needs `--crew-db`).

### Turning it on

1. Deploy the release with the flag still off and check it as in [Checking a deploy](#checking-a-deploy);
   `/health/ready` shows `components.crew.status` `disabled`.
2. Confirm backups: the [scheduled snapshot](#scheduled-snapshots) task exists and its last run succeeded (or,
   if you run litestream, `LITESTREAM_REPLICA_URL` is set; the crew replica is derived from it unless
   `LITESTREAM_CREW_REPLICA_URL` is set).
3. Take a snapshot now, inside the API container:
   `python -m remembra.storage.snapshot create --out /data/backups`
4. Set `REMEMBRA_CREW_MODE=true` in the deployment's environment and restart (no rebuild is needed).
5. Check it:

   ```bash
   curl -s "$API/health/ready" | python3 -m json.tool
   #   components.crew: status "ok", schema_version == latest_version
   curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $KEY" "$API/api/v1/crews"
   #   200 (404 means the flag is off)
   docker exec "$CTR" ls -l /data/crew.db
   ```

   The next scheduled snapshot must say `"crew_included": true`.

Crew mode assumes one server process (the image runs a single uvicorn worker). More than one process needs
`REMEMBRA_CREW_DB_TAILER=1`.

### Rollback plan

Crew tables are additive and live in their own file, so no step below needs a data migration. Stop at the
first step that fixes the problem.

1. **Flag off.** Set `REMEMBRA_CREW_MODE=false` and restart. Crew routes return 404, briefs lose the crew
   block, MCP crew tools answer "unavailable"; memory, Relay and the dashboard are unaffected. `crew.db` is
   opened for account erasure only (no crew route or job uses it), so it stays as it was apart from erased
   accounts, and the scheduled snapshots keep copying it; switching the flag on again brings every crew back.
   `/health/ready` shows `components.crew.status: "disabled"`. Agents that joined before the switch keep
   their last local view until they end, so restart them; new sessions get the plain Relay brief.
2. **Local side.** On each machine: exit the agents, then run `remembra-crew connect --uninstall --apply`, and
   `remembra-relay connect --apply` to get the plain Relay hooks back.
3. **Previous image.** If shared code is at fault, deploy the last release before Crew mode (0.16.1) again. It
   runs on the v5 main database (the schema is accepted and the agent inbox reads and writes work) and never
   opens `crew.db`. Inbox messages sent while rolled back keep their project (that release writes the
   `project_id` column when it exists) but carry no crew id, and their sender is recorded as an unverified
   agent (the column defaults). Accounts that release erases keep their `crew.db` rows until this release
   boots again, whose erasure job erases them on its first run (an account with an `account_erased` receipt
   and rows in `crew.db`); keep the rollback short.
4. **Data restore** (only if data is wrong): stop the container, then restore the snapshot taken before the
   flip (step 3 of [Turning it on](#turning-it-on)) with `python -m remembra.storage.snapshot restore <dir>`,
   as in [Scheduled snapshots](#scheduled-snapshots). With litestream, a restore needs a point in time from
   before the problem, because the replica holds the latest state, bad data included:
   `litestream restore -timestamp <RFC3339> -o <file> <replica>` for the affected file, moved into place while
   the container is stopped. `crew.db` gets no automatic pre-migration copy; take a snapshot before an
   upgrade that migrates it.

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
copies are deleted after 3 more deploys, scheduled snapshots once `--keep` newer ones exist, the litestream
replica (when it is on) drops erased data within about 25 hours, and manual copies are never deleted, so delete
those yourself.
