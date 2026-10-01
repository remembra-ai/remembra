# Vector erasure and recovery

Pending embedding writes, explicit memory/project wipes, account erasure and
scheduled TTL cleanup share a POSIX lock beside the main SQLite database.
Embedding calls happen before taking the lock. Workers re-read the canonical
row and their unique claim token under the lock before dispatching a vector
write; deletions wait for an already-dispatched cooperating worker. SQLite
transactions do not remain open across provider or Qdrant calls.

All API processes must share the same main database and lock file on a local
filesystem with POSIX lock support. Do not unlink the `.vectors.lock` sidecar
while the service runs. This is not coordination across independent hosts or
separate copies of the database.

Process death can release a lock while a request is still running remotely.
Migration 12 therefore records canonical UUID deletions in
`vector_erasure_markers` in that deletion's SQLite transaction. Markers retain
only opaque memory IDs and maintenance timestamps, not owner IDs, credentials
or memory text. SQLite-only identifiers cannot have Qdrant points and are not
retained in this ledger. A background recovery task runs independently of the embedding
breaker and pending-worker setting. It repeatedly removes absent canonical IDs
from the application's active and rebuild/rollback collection family. A restored
live canonical row is excluded. Other applications' collections are untouched.

Markers intentionally survive successful sweeps: a delayed remote request can
commit after an earlier sweep. Do not delete or trim this table while old writes
could still arrive. Include it in SQLite backups. This minimal maintenance
state is retained to prevent deleted vector payloads returning.

Account erasure also retains a content-free digest and erasure time in
`erased_account_fences`, seeded from existing successful erasure receipts.
Canonical inserts, bulk imports and archive restores check this fence in the
same SQL statement as the write. A request authorized before erasure cannot
recreate the account's memory rows afterward. These digests and the vector
markers are explicit maintenance exemptions in the erasure inventory.

This provides eventual reconciliation when Qdrant is available, rather than an
atomic transaction across SQLite and Qdrant. During an outage, cleanup retries;
the canonical-row read boundary continues to hide orphaned payloads. It does not
serialize arbitrary external writers, or ordinary updates/rebuild writes;
those deleted UUIDs are covered by later marker reconciliation. It does not
rewrite old backups, identify historical vector orphans created before markers
existed, or prove deletion from arbitrary external copies. Run the existing
reconciliation report to assess historical drift separately.
