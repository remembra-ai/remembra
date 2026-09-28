# Durability & Recovery

What Remembra guarantees about your data, and what it does not. Every statement on this page is
tied to how the code works today; where a guarantee depends on your deployment, it says so.

## Where data lives

- **SQLite** holds memories (content and metadata), entities, relationships, handoffs, keys and
  accounts. Remembra opens it with `journal_mode = WAL` and `synchronous = NORMAL`.
- **Qdrant** holds one vector per memory for semantic search.
- A **keyword index** (SQLite FTS) is kept alongside the memories.

These three are written without a shared transaction, so they can drift after a partial failure
(see [Consistency between stores](#consistency-between-stores)).

## What a successful store means

When `POST /api/v1/memories` returns success:

- The memory row is **committed to SQLite**. Keyword search and reads by ID see it immediately.
- If the response `status` is `stored`, its vector is **already in Qdrant**: Qdrant upserts wait for
  the write, so semantic search sees it as soon as the call returns.
- If the response `status` is `pending`, the embedding provider was unavailable (for example, out of
  quota). The memory is kept and keyword-searchable right away, and a background worker embeds it
  when the provider recovers. There is **no fixed time bound** for that; until then semantic search
  does not return it. (This is the default: `REMEMBRA_STORE_PENDING_ON_EMBEDDING_FAILURE=true`.)

## Crashes and power loss

- **Process crash** (the server dies, the machine keeps running): SQLite recovers from its write-ahead
  log on restart, and committed memories survive.
- **Power loss or operating-system crash**: with `synchronous = NORMAL`, SQLite syncs the write-ahead
  log to disk at checkpoints, not on every commit. The **most recent commits can be lost**. If you
  cannot accept that, run the database on storage with a battery-backed or power-loss-protected write
  cache, or replicate it continuously (see [Backups](#backups)).
- Remembra does **not** checksum or validate database pages when it reads them. If you suspect
  corruption, run `PRAGMA integrity_check` on a copy, or restore from a backup.

## Consistency between stores

A store or delete writes SQLite, Qdrant and the keyword index one after another. A failure in between
can leave a memory missing from semantic search, or leave an orphan vector.

- A periodic reconcile job reports drift (the `remembra_reconcile_drift` gauge on `/metrics`).
- `python -m remembra.storage.reconcile --repair` re-queues missing vectors and fixes keyword-index
  drift. Orphan vectors are reported, never deleted automatically.

## Degraded modes

- **Embedding provider down or out of quota:** stores are kept as `pending` (above), and recalls answer
  from keyword and graph search, marked `degraded: keyword_only`
  (`REMEMBRA_RECALL_KEYWORD_FALLBACK=true`, the default).

## Backups

- **When a new build starts**, before any schema migration runs, the server copies the database to
  `/data/backups/` (`REMEMBRA_PRE_MIGRATION_BACKUP`, on by default). It does this once per build, even
  when the schema does not change, and a restart of the same build does not copy again. Each copy is
  deleted once 3 newer ones exist. If a copy fails, the new version refuses to start. The copies sit on
  the same volume as the database, so they do not protect against losing the volume.
- **Continuous replication (optional):** the `Dockerfile.cloud` image ships Litestream. Set
  `LITESTREAM_REPLICA_URL` and the container runs the server under Litestream, replicating SQLite to your
  bucket. The default image has no Litestream.
- **Manual copies** are safe while the server runs:

    ```bash
    sqlite3 remembra.db ".backup backup.db"                                   # SQLite
    curl -X POST http://localhost:6333/collections/memories/snapshots        # Qdrant
    ```

  Test a restore regularly; a backup you have never restored is not a backup yet.

## What Remembra does not do

| Not provided | Notes |
|---|---|
| Transactions across SQLite, Qdrant and the keyword index | Drift is reported and repairable (above). |
| Replication or high availability out of the box | Use Litestream for SQLite and a Qdrant cluster if you need them. |
| Multiple writers | SQLite has a single writer; concurrent stores are serialized. |
| Published load-test numbers | See [Benchmarks](../benchmarks.md#performance). |

---

*Questions? Open an issue on [GitHub](https://github.com/remembra-ai/remembra/issues).*
