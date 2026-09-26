# Deploying Remembra Cloud

**Production truth (as of 2026-07-16):** `api.remembra.dev` runs as the Coolify
application **`remembra-api`** (project `remembra`, uuid `s8sk8kw4gg8oo044808og8kg`)
on the Hetzner server `178.156.226.84`, behind Cloudflare. It builds
**`Dockerfile.cloud`** from `remembra-ai/remembra` branch `main`.

> The `fly.toml` in this repo is a portable config for a possible future
> Fly.io region/staging environment. It is **not** the production deployment.

## Deploy flow

1. Commit and push to `main` (CI must be green).
2. Trigger the Coolify rebuild — either:
   - **UI:** Coolify (`http://178.156.226.84:8000`) → project *remembra* →
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
# optional; the default is a sibling of the main replica: s3://<bucket>/remembra-crew
LITESTREAM_CREW_REPLICA_URL=s3://<bucket>/remembra-crew
```

The entrypoint writes a litestream config covering **both** SQLite files: the
main database and `crew.db` (Crew mode's database, next to it in `/data`).
`crew.db` is replicated whenever it exists; with `REMEMBRA_CREW_MODE` on, the
entrypoint creates it (empty, WAL) before litestream starts, so the first crew
write is already replicated.

On boot with an empty volume, the entrypoint restores each database from its
replica automatically. **If a restore fails, the container exits** rather
than booting on an empty database; set `LITESTREAM_ALLOW_EMPTY_START=1` to
override deliberately. For `crew.db` this applies when Crew mode is on; with
Crew mode off a failed `crew.db` restore only warns, and `crew.db` is left out
of replication for that boot so the crew replica is never overwritten by an
empty file. Without `LITESTREAM_REPLICA_URL`, litestream is inert and the
entrypoint prints a warning on every boot that SQLite is not backed up.

Restore drill (OPS-4, owner): on a scratch host run

```bash
litestream restore -o /tmp/drill.db "$LITESTREAM_REPLICA_URL"
sqlite3 /tmp/drill.db 'select count(*) from memories'
litestream restore -o /tmp/drill-crew.db "${LITESTREAM_CREW_REPLICA_URL:-${LITESTREAM_REPLICA_URL%/}-crew}"
sqlite3 /tmp/drill-crew.db 'select max(version) from schema_version; select count(*) from crews; select count(*) from crew_events'
```

and compare with prod. Qdrant is not backed up — it is rebuilt from SQLite by
the rebuild reindex.

**Point-in-time snapshots.** Independently of litestream, take a consistent,
checksummed copy of both files (safe while the server runs) before any risky
change:

```bash
docker exec "$CTR" \
  python -m remembra.storage.snapshot create --out /data/backups
```

`verify <dir>` re-checks one; `restore <dir> [--force]` puts one back (server
stopped; existing files are kept as `*.pre-restore-<stamp>`). See
[Crew mode: Backups](relay/crew.md#backups).

## Crew mode

Crew mode ships **dark**: `Dockerfile.cloud` sets `REMEMBRA_CREW_MODE=false`,
and nothing crew-related runs until the Coolify env overrides it. The server
reads the flag once at startup. What it switches is described in
[Crew mode: the feature flag](relay/crew.md#the-feature-flag).

Deploying the Crew mode code changes one thing even with the flag off: the
main database gets migration **v5** (additive columns on `agent_inbox`,
backfilled once). Releases from before Crew mode run on a v5 database
unchanged.

### Turning it on

1. Deploy the release with the flag still off and verify live as above.
2. Confirm backups: `LITESTREAM_REPLICA_URL` is set (the crew replica is
   derived from it unless `LITESTREAM_CREW_REPLICA_URL` is set).
3. Take a snapshot: `python -m remembra.storage.snapshot create --out /data/backups`
   inside the container.
4. Set `REMEMBRA_CREW_MODE=true` in the Coolify env and redeploy (a restart
   is enough; no rebuild is needed).
5. Verify live:

   ```bash
   # boot log: "litestream: replicating the main db and crew.db"
   curl -s https://api.remembra.dev/health/ready | python3 -m json.tool
   #   components.crew: status "ok", schema_version == latest_version
   curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $KEY" https://api.remembra.dev/api/v1/crews
   #   200 (404 means the flag is off)
   docker exec "$CTR" ls -l /data/crew.db
   ```

   then run the crew E2E against the live URL with the dedicated test account.

Crew mode assumes one server process (production runs a single uvicorn
worker). More than one process needs `REMEMBRA_CREW_DB_TAILER=1`.

### Rollback plan

Crew tables are additive and live in their own file, so no step below needs a
data migration. Stop at the first step that fixes the problem.

1. **Flag off.** Set `REMEMBRA_CREW_MODE=false` and redeploy. Crew routes
   return 404, briefs lose the crew block, MCP crew tools answer
   "unavailable"; memory, Relay and the dashboard are unaffected. `crew.db` is
   not opened, so it stays exactly as it was and keeps being replicated;
   switching the flag on again brings every crew back. `/health/ready` shows
   `components.crew.status: "disabled"`. Agents that joined before the switch
   keep their last local view until they end, so restart them; new sessions
   get the plain Relay brief.
2. **Local side.** On each machine: exit the agents, then run
   `remembra-crew connect --uninstall --apply`, and `remembra-relay connect --apply`
   to get the plain Relay hooks back.
3. **Previous image.** If shared code is at fault, redeploy the last pre-crew
   commit through Coolify. It runs on the v5 main database (checked by booting
   the pre-crew code against a v5 database: the schema is accepted and the
   agent inbox reads and writes work) and never opens `crew.db`. Inbox
   messages sent while rolled back carry no project scope, so only keys
   without a project restriction see them.
4. **Data restore** (only if data is wrong): stop the container, then restore
   the snapshot taken before the flip. Litestream holds the *latest* state,
   bad data included, so a litestream restore needs a point in time from
   before the problem: `litestream restore -timestamp <RFC3339> -o <file> <replica>`
   for the affected file, moved into place while the container is stopped.

## Notes

- `remembra-qdrant`'s Docker healthcheck on Coolify reports *unhealthy*
  because it calls `curl`, which doesn't exist in the qdrant image. The repo's
  compose files now use `bash -c ':> /dev/tcp/127.0.0.1/6333'`; apply the same
  command to the Coolify qdrant service's healthcheck (owner, Coolify UI).
- `remembra-postgres` already runs on the same host — the planned
  SQLite → Postgres migration does not need new infrastructure.
