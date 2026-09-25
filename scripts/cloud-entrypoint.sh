#!/bin/sh
# Remembra Cloud entrypoint.
#
# If LITESTREAM_REPLICA_URL is set, run the server under litestream so the
# SQLite database is continuously replicated to object storage (S3/R2/Tigris)
# and restored on boot if the volume is empty.
#
# Failure policy (REL-14): a FAILED restore stops the container instead of
# silently booting on an empty database (which would then start replicating
# a brand-new, empty history). Set LITESTREAM_ALLOW_EMPTY_START=1 to override
# deliberately (e.g. first deploy against a fresh bucket that errors).
set -e

DB_PATH="${REMEMBRA_DB_PATH:-/data/remembra.db}"

if [ -n "$LITESTREAM_REPLICA_URL" ]; then
    if ! command -v litestream >/dev/null 2>&1; then
        echo "litestream: LITESTREAM_REPLICA_URL is set but the litestream binary is missing — refusing to start without backups" >&2
        exit 1
    fi
    echo "litestream: replication enabled"

    # Restore the DB from the replica if the local copy doesn't exist yet
    if [ ! -f "$DB_PATH" ]; then
        echo "litestream: no local db, attempting restore..."
        if ! litestream restore -if-replica-exists -o "$DB_PATH" "$LITESTREAM_REPLICA_URL"; then
            echo "litestream: RESTORE FAILED for an empty volume." >&2
            if [ "${LITESTREAM_ALLOW_EMPTY_START:-}" != "1" ]; then
                echo "litestream: refusing to start on an empty database (set LITESTREAM_ALLOW_EMPTY_START=1 to override)" >&2
                exit 1
            fi
            echo "litestream: LITESTREAM_ALLOW_EMPTY_START=1 — starting with an EMPTY database" >&2
        elif [ -f "$DB_PATH" ]; then
            echo "litestream: restore complete"
        else
            echo "litestream: no replica exists yet — starting fresh"
        fi
    fi

    exec litestream replicate \
        -exec "python -m remembra.main" \
        "$DB_PATH" "$LITESTREAM_REPLICA_URL"
fi

echo "litestream: replication DISABLED (LITESTREAM_REPLICA_URL unset) — SQLite is NOT being backed up" >&2
exec python -m remembra.main
