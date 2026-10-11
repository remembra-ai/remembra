# Unregistered owner approval integration

This increment supplies the HTTP factory and a SQLite adapter for the scoped
approval service. It installs no production schema, route, event type, outbox
handler, startup hook, action policy, dashboard control or execution adapter.
Import and construction have no registration or schema side effects. All
integration in this increment runs against synthetic temporary databases.

## Storage and atomic notification

`crew/approval_schema.py` contains proposed additive DDL, not a migration runner.
The proposal has a current-record table and append-only version snapshots, with
requester/credential/session/tenant-scoped idempotency, immutable identity and
decision constraints, and links to existing Crew event and outbox rows.
`crew/approval_store.py` never creates these objects. Absent or changed proposal
DDL and disabled foreign keys fail closed.

Each approval call owns its outer Crew transaction. Both coordinator-owned and
raw external transactions are rejected, including an outer transaction that
would later roll back. The event log can join inside that owned transaction.
A scoped transaction port cannot escape its context or be used by a child task.
Each owned commit uses a fresh event-log scope on the same database and bus,
so a child task's inherited, completed event context cannot steal its publication
callback. Rollbacks never publish; later commits still receive a fresh callback.
Every saved version must receive exactly one matching transition before commit;
the adapter rejects a missing audit, a changed request/decision, an invalid
transition, a stale version or terminal-state mutation.

`crew/approval_events.py` proposes one closed, server-only `approval.transition`
event. It does not register that event. Writes require its exact payload schema,
server-only classification and always-moment retention registration. Actor
metadata comes from the authenticated record and current Crew rows. The existing
event log assigns sequence and hash chain and publishes after durable commit.
No second event stream is introduced.

The same transaction enqueues an `approval_notification` in the existing outbox.
Its payload carries approval/version/digest/event and tenant/project references,
not parameters or credentials. The adapter verifies the stored payload after
deterministic enqueue and verifies an event replay's content; matching a key
alone is insufficient. This prevents `INSERT OR IGNORE` from silently accepting
a conflicting notification. The notification kind has no production handler.

The test-only recipient uses the stable outbox identity when an acknowledgement
is lost and delivery retries. That demonstrates durable at-least-once delivery
and recipient deduplication. It does not demonstrate a supported host resuming,
executing an effect or completing a task. Consumption remains absent from HTTP.

## HTTP boundary

`make_approval_router` requires an explicit service and synchronous rate gate.
It provides request/get/decide/revoke only. Existing JWT/key authentication,
human owner/admin step-up and Crew session checks precede body buffering.
Streaming is bounded and authority is revalidated after it. Unexpected async
rate adapters/results fail closed. Closed parsers and fixed error responses
avoid echoing input or policy errors. The harness mounts the factory solely in
an isolated test app and explicitly reports `unsupported_adapter` execution.

The production startup must validate dependencies before mounting. The current
generic approval-conflict response is not an operational readiness interface;
unavailable schema/policy/rate setup must not be advertised as a working feature.

## Required production reviews

| Path | Minimal proposed change and review |
| --- | --- |
| `src/remembra/crew/db.py` (frozen) | Import the reviewed DDL, append a new version after the existing immutable migrations, and include the two new tables in `CREW_TABLES`. No transaction-coordinator changes. |
| `tests/crew/test_storage_migrations.py` (frozen) | Advance Crew-only latest-version expectations, retaining all main-DB expectations and assertions; cover upgrade, second startup, concurrent upgrade and rollback. |
| `src/remembra/main.py` (frozen) | Add an explicit optional router injection seam to existing Crew registration, with the default call unchanged. Passing an instantiated router requires separately reviewed lifecycle, authoritative policy and rate mapping; no automatic default adapter. |
| `src/remembra/crew/schemas.py` | Register the exact server-only event, always-moment rule, route/access catalog and approved snapshot/view shapes. No import-time mutation by the new modules. |
| `src/remembra/crew/erasure.py`, `retention.py` | Review table holdings, immutable history retention, account erasure/tombstones and paired restore before any production DDL. History deletion triggers are a proposal, not an exemption from erasure requirements. |
| `src/remembra/crew/limits.py` | Assign explicit existing synchronous bucket/scopes without consuming guard/claim capacity or falling through to unlimited behavior. |
| `src/remembra/crew/inbox.py`, `notify.py`, `outbox.py`, `views.py`, `core.py`, `reducer.py` | Review authorized recipients, closed notification handler, Needs-you mapping, snapshots and replay parity. No handler currently sends or resumes anything. |

Existing session/auth helpers are reused. No changes to frozen `sessions.py`,
`storage/database.py` or `account/erasure.py` are proposed here; their owners must
review lifecycle/recovery compatibility if an actual integration diff needs
them. Frozen `relay/crew/crewd.py` and `gate.py` remain a later, named host-adapter
review. Approval receipts never bypass receiving-host policies or mandatory
human handoffs. No credential grants or configuration changes are included.

## Testable before registration

The new fixture installs proposed DDL in a temporary Crew database and supplies
event definitions through a restored test-local registry override. It exercises
the real Crew event log, transaction coordinator, sequence/hash chain and outbox
worker. Tests cover missing schema/registration, immutable versions, boundary
isolation, two-connection races, raw/nested transactions, cancellation, every
write/commit disconnect boundary, reopened connections, lost claim responses,
after-commit publication loss, replay, conflicting dedupe payloads, and real
JWT/API-key HTTP journeys. The existing migration runner also exercises the
proposal without modifying its global production registry.

These tests can run completely behind the unregistered harness. They do not
replace production registration review, human dashboard acceptance, paired
restore, supported host effect fencing/idempotency or load and kill-soak gates.
The inherited dashboard npm findings require their separate dependency-owner
classification; a passing Python lockfile audit does not cover that toolchain.
