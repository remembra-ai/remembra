#!/bin/sh
# Remembra Cloud entrypoint.
#
# If LITESTREAM_REPLICA_URL is set, run the server under litestream so the
# SQLite database is continuously replicated to object storage (S3/R2/Tigris)
# and restored on boot if the volume is empty. Without the env var this is a
# strict no-op: the server starts exactly as before.
set -e

DB_PATH="${REMEMBRA_DB_PATH:-/data/remembra.db}"

if [ -n "$LITESTREAM_REPLICA_URL" ] && command -v litestream >/dev/null 2>&1; then
    echo "litestream: replication enabled -> $LITESTREAM_REPLICA_URL"

    # Restore the DB from the replica if the local copy doesn't exist yet
    if [ ! -f "$DB_PATH" ]; then
        echo "litestream: no local db, attempting restore..."
        litestream restore -if-replica-exists -o "$DB_PATH" "$LITESTREAM_REPLICA_URL" || true
    fi

    exec litestream replicate \
        -exec "python -m remembra.main" \
        "$DB_PATH" "$LITESTREAM_REPLICA_URL"
fi

exec python -m remembra.main
