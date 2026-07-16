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
   curl -s https://api.remembra.dev/health                                                          # status ok
   curl -s -o /dev/null -D - https://api.remembra.dev/health | grep -i x-request-id                 # present
   ```

## Backups (litestream)

`Dockerfile.cloud` ships litestream. To enable continuous SQLite replication,
set in the Coolify app's environment:

```
LITESTREAM_REPLICA_URL=s3://<bucket>/remembra   # plus the bucket's credentials env vars
```

On boot with an empty volume, the entrypoint restores the database from the
replica automatically. Without the env var, litestream is inert.

## Notes

- `remembra-qdrant`'s Docker healthcheck reports *unhealthy* because it calls
  `curl`, which doesn't exist in the qdrant image. Qdrant itself is fine —
  fix the healthcheck to a TCP check when convenient.
- `remembra-postgres` already runs on the same host — the planned
  SQLite → Postgres migration does not need new infrastructure.
