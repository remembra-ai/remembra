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

    # Retention (R-23): the replica keeps this much history, so data erased from
    # the live database leaves the backup within about twice this window. The
    # privacy page states 24 hours; keep them in step. The CLI form cannot set
    # retention, so the replica is described in a generated config file.
    RETENTION="${LITESTREAM_RETENTION:-24h}"
    case "$RETENTION" in
        *[!0-9hms]*|"") echo "litestream: LITESTREAM_RETENTION must look like 24h, 90m or 3600s" >&2; exit 1 ;;
    esac
    case "$LITESTREAM_REPLICA_URL$DB_PATH" in
        *\"*|*\\*) echo "litestream: replica URL and database path must not contain quotes or backslashes" >&2; exit 1 ;;
    esac
    LITESTREAM_CONFIG="${LITESTREAM_CONFIG:-/tmp/litestream.yml}"
    cat > "$LITESTREAM_CONFIG" <<YAML
dbs:
  - path: "$DB_PATH"
    replicas:
      - url: "$LITESTREAM_REPLICA_URL"
        retention: $RETENTION
        retention-check-interval: 1h
YAML
    echo "litestream: replica retention $RETENTION"

    exec litestream replicate \
        -config "$LITESTREAM_CONFIG" \
        -exec "python -m remembra.main"
fi

echo "litestream: replication DISABLED (LITESTREAM_REPLICA_URL unset) — SQLite is NOT being backed up" >&2
exec python -m remembra.main
