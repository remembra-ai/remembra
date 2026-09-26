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

Retention: the entrypoint writes a litestream config whose replica keeps
`LITESTREAM_RETENTION` of history (default `24h`), so data erased from the live
database leaves the replica within about 48 hours. The privacy page states
24 hours; change both together.

Restore drill (OPS-4, owner): on a scratch host run
`litestream restore -o /tmp/drill.db "$LITESTREAM_REPLICA_URL"` and
`sqlite3 /tmp/drill.db 'select count(*) from memories'`; compare with prod.
Qdrant is not backed up — it is rebuilt from SQLite by the rebuild reindex.

## Account deletion and erasure (R-11, R-23)

`DELETE /api/v1/auth/me` (password, or a code from `POST /api/v1/auth/me/deletion-code`
for Google/GitHub accounts) first cancels, immediately, the Paddle subscription
the account holds and any other billable subscription of its Paddle customer
that is this account's (recorded on it, or its checkout `custom_data` names it,
or no other account shares the customer). One payer email can pay for several
accounts: subscriptions of the same customer that belong to another account are
left running and the owner gets an `account_deletion_shared_customer` alert. A
recorded subscription id Paddle does not know (404: sandbox, Stripe-era or
hand-edited ids) counts as done and alerts `account_deletion_subscription_unknown`
(check nothing still charges that customer elsewhere). If Paddle cannot confirm
a cancel, the request fails with 502, nothing is deleted and the owner gets an
`account_deletion_billing_failed` alert; the user is told to retry when Paddle
was unreachable (429/5xx/timeout) and that support will sort it out when Paddle
refused. Then the account is deactivated (sessions and API keys stop at once)
and `users.deleted_at` is set.

The `account-erasure-loop` task (every `REMEMBRA_ACCOUNT_ERASURE_INTERVAL_SECONDS`,
default 3600) erases accounts deleted more than `REMEMBRA_ACCOUNT_ERASURE_GRACE_DAYS`
ago (default 7, max 30; the privacy page and dashboard say 7): Qdrant points by
`user_id` filter first, in the active collection AND every rollback copy a
rebuild reindex kept (`<base>__rb_*` collections and any collection a
`reindex_jobs` row names; other applications' collections are never touched),
then every SQLite row the account owns in one transaction
(`remembra.account.erasure.ERASURE_RULES`, plus any table found with a
user-keyed column; actor columns such as `added_by` are cleared, never used to
delete).

Crew mode (`crew.db`, feat/crew) is NOT covered yet. Do not pass `crew.db` to
`AccountEraser` bare (it refuses): it takes
`ExtraDatabase("crew", crew_db, rules=CREW_ERASURE_RULES, exempt=...)`, and
feat/crew must first ship those rules with a coverage test that runs
`registry_problems` over the real `CREW_MIGRATIONS` schema. The rules must
delete children before parents; reach session-keyed rows (checkpoints, reports,
batons, footprints, claims) through `crew_sessions.user_id`, message edit
history through `crew_messages.author_user_id`, and votes through `voter_id`;
delete the crews the account owns with all their child rows; clear (NULL) actor
columns (`added_by`, `created_by`, `shared_by`, ...) in other people's crews;
and deal with the `crew_events` hash chain (record erased seqs in
`crew_pruned_ranges`, or redact payloads in place). Until then an account that
used Crew mode keeps its crew rows after erasure, so Crew mode must stay off in
production. It keeps one `account_erased` audit row holding a SHA-256
of the account id and row counts, nothing else. To undo a deletion inside the
grace period: `POST /api/v1/admin/users/{id}/activate?active=true` (the user
then buys again if they had a plan; the cancelled subscription stays cancelled).
`DELETE /api/v1/admin/users/{id}?confirm=true` does the same billing cancel and
erases immediately. It also erases API-signup tenants (`POST /cloud/signup`),
which have no `users` row and no dashboard to delete themselves from. When
Paddle refuses a cancel and the subscription has been checked by hand, add
`&force_billing=skip`: Paddle is not asked, and the owner alert
`account_deletion_billing_skipped` lists the subscription and customer to cancel
in Paddle.

Accounts deleted before this release: the old "Delete account" only set
`is_active = FALSE` (no `deleted_at`, billing NOT cancelled), so the erasure job
never sees them. `GET /api/v1/admin/deactivated-accounts` lists every
deactivated account with no erasure scheduled, with any subscription it still
holds. Superadmin deactivations look the same; the old self-delete logged
`account_deactivated` with the user id, a superadmin deactivation did not. For
each confirmed self-deletion run
`POST /api/v1/admin/deactivated-accounts/{id}/schedule-erasure?confirm=true`
(cancels billing, sets `deleted_at`; the job erases after the grace period) or
the hard delete above.

Backups are not rewritten. Pre-deploy copies (`backups/remembra-predeploy-*`)
keep the newest `REMEMBRA_PRE_MIGRATION_BACKUP_KEEP` (3), so an erased account
leaves them after 3 more deploys. Manual copies made by hand (for example
`/data/remembra-pre-relay-launch.db` from the launch runbook) never age out:
delete them once the release is confirmed.

## Billing flags and Founding 100 (R-26)

Every billing flag (second subscription, Founding payment past seat 100, Team
below 3 seats, refund or chargeback) and every payment that matches no account
or no catalog price sends an operator alert (`REMEMBRA_ALERT_EMAIL` and/or
`REMEMBRA_ALERT_WEBHOOK_URL`; set at least one). `GET /api/v1/admin/billing-flags`
lists flagged accounts; `DELETE /api/v1/admin/billing-flags/{user_id}` clears one
after review. A refund of an account that used over 25% of its credits also alerts.

Founding checkouts hold a seat for 2 hours before payment; seats also stay held
14 days after a founder's subscription ends. When seat 100 is taken the Founding
price is archived in Paddle (`PATCH /prices/{id}`; the API key needs price write
permission) and re-activated at the next Founding checkout after a seat frees.
`GET /api/v1/billing/founding` (public) reports seats left for the pricing page.

## Notes

- `remembra-qdrant`'s Docker healthcheck on Coolify reports *unhealthy*
  because it calls `curl`, which doesn't exist in the qdrant image. The repo's
  compose files now use `bash -c ':> /dev/tcp/127.0.0.1/6333'`; apply the same
  command to the Coolify qdrant service's healthcheck (owner, Coolify UI).
- `remembra-postgres` already runs on the same host — the planned
  SQLite → Postgres migration does not need new infrastructure.
