#!/usr/bin/env bash
# Boot a built Dockerfile.cloud image against a real Qdrant and check that it serves.
#
#   docker buildx build -f Dockerfile.cloud --build-arg REMEMBRA_BUILD_SHA="$SHA" -t remembra-cloud:ci --load .
#   bash scripts/ci/cloud_image_smoke.sh remembra-cloud:ci "$SHA"
#
# Used by the `docker-cloud` CI job and runnable locally (needs only docker,
# curl and python3). No secrets: the OpenAI key is a placeholder, so /health
# (liveness) is checked, while /health/ready reports the provider as degraded.
set -euo pipefail

IMAGE="${1:?usage: cloud_image_smoke.sh <image> [expected-build-sha]}"
EXPECT_SHA="${2:-}"
PORT="${SMOKE_PORT:-18787}"
# Production runs qdrant v1.17.0 (docker-compose.prod.yml); pinned by digest.
QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:v1.17.0@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb}"
TAG="remembra-smoke-$$"
NET="$TAG-net"
APP="$TAG-app"
QDRANT="$TAG-qdrant"
BASE="http://127.0.0.1:$PORT"

cleanup() {
    docker rm -f "$APP" "$QDRANT" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    echo "---- app logs ----" >&2
    docker logs "$APP" 2>&1 | tail -80 >&2 || true
    exit 1
}

wait_healthy() {
    for _ in $(seq 1 90); do
        if [ "$(docker inspect -f '{{.State.Running}}' "$APP" 2>/dev/null)" != "true" ]; then
            fail "the container exited"
        fi
        if curl -fsS "$BASE/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    fail "/health did not answer within 180 s"
}

code_and_type() { curl -s -o /dev/null -w '%{http_code} %{content_type}' "$BASE$1"; }

docker network create "$NET" >/dev/null
docker run -d --name "$QDRANT" --network "$NET" --network-alias qdrant "$QDRANT_IMAGE" >/dev/null
docker run -d --name "$APP" --network "$NET" -p "127.0.0.1:$PORT:8787" \
    -e REMEMBRA_QDRANT_URL=http://qdrant:6333 \
    -e REMEMBRA_OPENAI_API_KEY=ci-placeholder-not-a-key \
    -e REMEMBRA_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
    "$IMAGE" >/dev/null

wait_healthy
HEALTH=$(curl -fsS "$BASE/health")
echo "health: $HEALTH"
EXPECT_SHA="$EXPECT_SHA" python3 - "$HEALTH" <<'PY' || fail "/health is not ok"
import json, os, sys
body = json.loads(sys.argv[1])
assert body["status"] == "ok", body
assert body["dependencies"]["qdrant"]["status"] == "ok", body
expect = os.environ.get("EXPECT_SHA", "")
if expect:
    assert body.get("build_sha") == expect, body
PY

READY=$(curl -fsS "$BASE/health/ready")
python3 - "$READY" <<'PY' || fail "/health/ready"
import json, sys
body = json.loads(sys.argv[1])
sqlite = body["components"]["sqlite"]
assert sqlite["status"] == "ok" and int(sqlite["schema_version"]) >= 4, sqlite
print("ready: sqlite", sqlite, "status", body["status"])
PY

# The API contract and the dashboard the image serves.
[ "$(code_and_type /v1/__check__)" = "404 application/json" ] || fail "/v1/__check__ is not a JSON 404"
[ "$(code_and_type /api/v1/auth/providers)" = "200 application/json" ] || fail "/api/v1/auth/providers"
INDEX=$(curl -fsS "$BASE/login") || fail "GET /login failed"
case "$INDEX" in *'id="root"'*) ;; *) fail "the dashboard index is not served for /login" ;; esac
case "$(code_and_type /oauth/callback)" in "200 text/html"*) ;; *) fail "/oauth/callback is not the dashboard page" ;; esac
# The dashboard page carries a policy that lets its own scripts run; API answers keep default-src 'none'.
PAGE_HEADERS=$(curl -fsS -D - -o /dev/null "$BASE/oauth/callback")
API_HEADERS=$(curl -fsS -D - -o /dev/null "$BASE/health")
case "$PAGE_HEADERS" in *"script-src 'self'"*) ;; *) fail "the dashboard page has no script-src 'self' policy" ;; esac
case "$API_HEADERS" in *"default-src 'none'"*) ;; *) fail "API responses lost default-src 'none'" ;; esac

# What docs/DEPLOYING.md tells operators about the image.
if docker exec "$APP" sh -c 'command -v sqlite3' >/dev/null 2>&1; then fail "the image now has a sqlite3 CLI: update docs/DEPLOYING.md"; fi
docker exec "$APP" python -c 'import sqlite3; c = sqlite3.connect("file:/data/remembra.db?mode=ro", uri=True); print("memories table rows:", c.execute("SELECT COUNT(*) FROM memories").fetchone()[0])' \
    || fail "python sqlite3 cannot read /data/remembra.db"

# Pre-migration backup: a fresh volume has nothing to copy; the next boot of the
# same build copies the database once, and later boots of that build do not.
[ -z "$(docker exec "$APP" sh -c 'ls /data/backups 2>/dev/null')" ] || fail "a backup was written for an empty volume"
for boot in 2 3; do
    docker restart "$APP" >/dev/null
    wait_healthy
    COUNT=$(docker exec "$APP" sh -c 'ls /data/backups/remembra-predeploy-*.db 2>/dev/null | wc -l' | tr -d ' ')
    [ "$COUNT" = "1" ] || fail "boot $boot: expected 1 pre-migration backup, found $COUNT"
done
docker exec "$APP" ls -la /data/backups

echo "OK: $IMAGE boots, serves /health, the dashboard and /oauth/callback, and backs up before migrating"
