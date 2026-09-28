#!/bin/sh
# Remembra Cloud entrypoint.
#
# If LITESTREAM_REPLICA_URL is set, run the server under litestream so the
# SQLite databases are continuously replicated to object storage (S3/R2/Tigris)
# and restored on boot if the volume is empty.
#
# Two files are covered (Crew mode spec D35, WP-16):
#   * the main database   ($REMEMBRA_DB_PATH, else the path in
#     $REMEMBRA_DATABASE_URL, else /data/remembra.db) -> $LITESTREAM_REPLICA_URL
#   * crew.db             ($REMEMBRA_CREW_DB_PATH, else crew.db next to the main
#     database, exactly like remembra.crew.db.resolve_crew_db_path)
#                         -> $LITESTREAM_CREW_REPLICA_URL, else a sibling of the
#     main replica: "<main>-crew" (or "<main>/crew" for a bare bucket URL).
#
# Failure policy (REL-14): a FAILED restore stops the container instead of
# silently booting on an empty database (which would then start replicating
# a brand-new, empty history). Set LITESTREAM_ALLOW_EMPTY_START=1 to override
# deliberately (e.g. first deploy against a fresh bucket that errors).
# For crew.db the same rule applies when Crew mode is on (REMEMBRA_CREW_MODE).
# With Crew mode off a failed crew.db restore only warns: the memory service
# does not need crew.db, and crew.db is then left out of replication so the
# existing crew replica is never overwritten by an empty file.
#
# Retention (R-23): every replica keeps LITESTREAM_RETENTION of history (24h by
# default), checked hourly, so data erased from a live database leaves the backup
# within about 25 hours (docs/OPERATIONS.md). The replicas are described in a
# generated config file ($LITESTREAM_CONFIG, else a temporary file).
set -e

truthy() {
    # Same values as remembra.main.crew_mode_enabled(): 1/true/yes/on, case-insensitive, trimmed.
    # Shell builtins only (this runs before anything else, even on a minimal PATH).
    v=$(set -f; echo $1)
    case "$v" in
        1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) return 0 ;;
        *) return 1 ;;
    esac
}

crew_db_default() {
    # remembra.crew.db.resolve_crew_db_path: crew.db in the main database's directory.
    case "$1" in
        */*) printf '%s/crew.db' "${1%/*}" ;;
        *) printf 'crew.db' ;;
    esac
}

main_db_path() {
    if [ -n "$REMEMBRA_DB_PATH" ]; then
        printf '%s' "$REMEMBRA_DB_PATH"
        return
    fi
    case "$REMEMBRA_DATABASE_URL" in
        sqlite*///*) printf '%s' "${REMEMBRA_DATABASE_URL#*///}" ;;
        *) printf '%s' "/data/remembra.db" ;;
    esac
}

crew_replica_url() {
    if [ -n "$LITESTREAM_CREW_REPLICA_URL" ]; then
        printf '%s' "$LITESTREAM_CREW_REPLICA_URL"
        return
    fi
    base="${LITESTREAM_REPLICA_URL%/}"
    rest="${base#*://}"
    case "$rest" in
        */*) printf '%s' "${base}-crew" ;;   # s3://bucket/remembra -> s3://bucket/remembra-crew
        *) printf '%s' "${base}/crew" ;;     # s3://bucket          -> s3://bucket/crew
    esac
}

check_config_value() {
    # Values are written as double-quoted YAML scalars. Litestream expands $VAR in its config, so a
    # literal "$" (or a quote, backslash or newline) cannot be written safely: refuse loudly instead.
    case "$1" in
        *'$'*|*'"'*|*'\'*|*'
'*)
            echo "litestream: refusing to write a config value containing \$, quotes, backslashes or a newline: $1" >&2
            exit 1
            ;;
    esac
}

# restore_db <label> <db path> <replica url> <fatal: 1|0>
# Returns 0 when the database is present (or legitimately absent: no replica yet).
restore_db() {
    label="$1"; path="$2"; url="$3"; fatal="$4"
    [ -f "$path" ] && return 0
    echo "litestream: no local $label db, attempting restore..."
    if ! litestream restore -if-replica-exists -o "$path" "$url"; then
        echo "litestream: RESTORE FAILED for $label ($path) on an empty volume." >&2
        if [ "$fatal" != "1" ]; then
            echo "litestream: Crew mode is off, starting without $label; it is left out of replication until restored" >&2
            return 1
        fi
        if [ "${LITESTREAM_ALLOW_EMPTY_START:-}" != "1" ]; then
            echo "litestream: refusing to start on an empty database (set LITESTREAM_ALLOW_EMPTY_START=1 to override)" >&2
            exit 1
        fi
        echo "litestream: LITESTREAM_ALLOW_EMPTY_START=1 — starting with an EMPTY database ($label)" >&2
        return 0
    fi
    if [ -f "$path" ]; then
        echo "litestream: $label restore complete"
    else
        echo "litestream: no $label replica exists yet — starting fresh"
    fi
    return 0
}

DB_PATH="$(main_db_path)"
CREW_DB_PATH="${REMEMBRA_CREW_DB_PATH:-$(crew_db_default "$DB_PATH")}"
CREW_MODE=0
if truthy "${REMEMBRA_CREW_MODE:-}"; then
    CREW_MODE=1
fi

if [ -n "$LITESTREAM_REPLICA_URL" ]; then
    if ! command -v litestream >/dev/null 2>&1; then
        echo "litestream: LITESTREAM_REPLICA_URL is set but the litestream binary is missing — refusing to start without backups" >&2
        exit 1
    fi
    echo "litestream: replication enabled"
    CREW_REPLICA_URL="$(crew_replica_url)"
    if [ "${CREW_REPLICA_URL%/}" = "${LITESTREAM_REPLICA_URL%/}" ]; then
        echo "litestream: LITESTREAM_CREW_REPLICA_URL must differ from LITESTREAM_REPLICA_URL" >&2
        exit 1
    fi

    for value in "$DB_PATH" "$LITESTREAM_REPLICA_URL" "$CREW_DB_PATH" "$CREW_REPLICA_URL"; do
        check_config_value "$value"
    done

    # Retention (R-23): every replica keeps this much history and litestream checks
    # it every hour, so data erased from a live database leaves the backup within
    # this window plus an hour (about 25 hours at the default). docs/OPERATIONS.md
    # states that figure. The public retention pages describe no continuous backup;
    # if Remembra Cloud turns this on, add the figure there too. The CLI form cannot
    # set retention, so the replicas are described in a generated config file.
    RETENTION="${LITESTREAM_RETENTION:-24h}"
    case "$RETENTION" in
        *[!0-9hms]*|"") echo "litestream: LITESTREAM_RETENTION must look like 24h, 90m or 3600s" >&2; exit 1 ;;
    esac

    mkdir -p "$(dirname "$DB_PATH")" "$(dirname "$CREW_DB_PATH")"
    restore_db main "$DB_PATH" "$LITESTREAM_REPLICA_URL" 1

    REPLICATE_CREW=0
    if restore_db crew "$CREW_DB_PATH" "$CREW_REPLICA_URL" "$CREW_MODE"; then
        if [ ! -f "$CREW_DB_PATH" ] && [ "$CREW_MODE" = "1" ]; then
            # Crew mode on and no crew.db yet: create it (empty, WAL) now so litestream
            # tracks it from the first write the server makes.
            python -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("PRAGMA journal_mode=WAL"); c.close()' "$CREW_DB_PATH"
            echo "litestream: created an empty crew.db for Crew mode"
        fi
        if [ -f "$CREW_DB_PATH" ]; then
            REPLICATE_CREW=1
        fi
    fi

    CONFIG="${LITESTREAM_CONFIG:-$(mktemp "${TMPDIR:-/tmp}/litestream.XXXXXX")}"
    {
        echo "dbs:"
        echo "  - path: \"$DB_PATH\""
        echo "    replicas:"
        echo "      - url: \"$LITESTREAM_REPLICA_URL\""
        echo "        retention: $RETENTION"
        echo "        retention-check-interval: 1h"
        if [ "$REPLICATE_CREW" = "1" ]; then
            echo "  - path: \"$CREW_DB_PATH\""
            echo "    replicas:"
            echo "      - url: \"$CREW_REPLICA_URL\""
            echo "        retention: $RETENTION"
            echo "        retention-check-interval: 1h"
        fi
    } > "$CONFIG"
    echo "litestream: replica retention $RETENTION"
    if [ "$REPLICATE_CREW" = "1" ]; then
        echo "litestream: replicating the main db and crew.db"
    else
        echo "litestream: replicating the main db (no crew.db on this volume)"
    fi

    exec litestream replicate -config "$CONFIG" -exec "python -m remembra.main"
fi

echo "litestream: replication DISABLED (LITESTREAM_REPLICA_URL unset) — SQLite is NOT being backed up" >&2
exec python -m remembra.main
