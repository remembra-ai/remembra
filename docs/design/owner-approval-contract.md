# Scoped owner approval contract

Status: internal first increment for independent security review. No production
API, MCP tool, migration, worker, dashboard control or host executor is installed.
The absence of a production store and policy adapter is intentional and fails
closed. This contract does not make Relay approvals available to customers yet.

## Authority and scope

`CrewApprovals` uses the existing `AuthenticatedUser`, Crew membership/project
checks, session-token validation and fresh human JWT step-up. API keys, including
admin keys, cannot decide. The development auth shortcut cannot decide either.
The HTTP boundary must call `get_current_user` and derive the login time through
`jwt_auth_time_ms`; neither principal nor authentication time belongs in a body.
Ordinary inbox text, sender labels, delivery and acknowledgements never decide.

The server derives tenant, project, requester, credential and session. The first
increment binds execution to the same requesting credential/session. A different
agent, credential or resumed session needs a new reviewed request; no implicit
transfer is allowed. An operation binds action, typed resource, exact sorted
scope, base and candidate revision, canonical validated parameters, expiry and
idempotency key. Its digest includes the complete request and intended executor.
The trusted action registry must reject unsupported actions, unknown parameters,
wildcards and semantics it cannot validate. There is no default action registry.

Owner/admin decisions require a fresh dashboard login and the exact displayed
request digest and version. Competing decisions have one winner. Decisions are
immutable: expiry, revocation and stale-candidate invalidation are recorded
separately, preserving who decided what. Invalidated requests cannot be revived
by changing a candidate back. Requests have a maximum 24-hour lifetime.

## Consumption and recovery

Approval does not execute an action. `consume` rechecks membership, session,
request digest, candidate/base revision, expiry, approver membership and host
policy inside one serialized transaction. Source freezes and mandatory human
handoffs remain authoritative. A block carries a bounded reason, resolver and
next action along with the original request scope and expiry.

Only the first committed claim returns `acquired=True`. Retries return the same
receipt with `acquired=False`; they never authorize a second operation. A claim
is not a completion receipt. If a process disconnects after commit but before
receiving the response, a supported host adapter must reconcile using the stable
receipt id. An external side effect cannot be made exactly-once just by claiming
a database row. A future adapter must provide durable idempotency/fencing and
recheck source/host policy at the effect boundary, or expose a human handoff.

Delivery is also separate. An outbox poll is a read; unread/pending does not prove
unseen. A delivery ACK is not task completion or evidence that a host resumed.
No host resume capability is claimed by this increment.

## Persistence integration review requirements

The `ApprovalStore` protocol requires a serializable transaction shared with
Crew authority reads, immutable request and decision fields, version compare and
swap, a unique requester-scoped idempotency key, append-only audit and a durable
deduplicated notification written with every transition. A process-local lock or
an in-memory production adapter cannot satisfy this contract.

The tests add synthetic tables only to isolated temporary databases and reuse
`CrewDatabase` transactions and `CrewStore.enqueue_outbox`. The synthetic adapter
is a conformance harness, not a production migration. It does not register an
outbox handler or assert that a production event schema accepts new event kinds.

Before public wiring, independently review:

- Additive persistence, indexes, immutable-history enforcement, retention/erasure
  and backup/restore behavior; re-use the existing Crew event sequence/hash chain
  and outbox instead of creating another event or delivery system.
- Authoritative revision lookup and fencing, credential/session revocation,
  membership race serialization and supported-host execution idempotency.
- API/MCP request/get/bounded-wait routes with authenticated identity, budgets
  and rate limits; JWT-only decision UI using the existing Needs-you flow.
- Event/notification redaction and scope. Agent text must remain untrusted data;
  display uses existing Crew neutralization and unconditional secret redaction.
- Supported resume and explicit blocked-state integration with existing presence,
  tasks and checkpoint recovery. Keep last poll, delivered, ACKed, running,
  awaiting owner and host-policy blocked as distinct facts.
- Task-linked receipts and existing observed/self-reported/server-verified Crew
  evidence. A claim or notification must never replace report completion gates.

## Verification

Tests cover tenant/project isolation, real HTTP JWT authentication and step-up,
admin API-key rejection, exact scope/digest, stale revisions, expiry, revocation,
two-connection decision/consumption races, replay, audit immutability and injected
disconnects after row write, audit append, outbox append, before commit and after
commit. Recovery closes/reopens SQLite and verifies one durable transition and
notification per version. These are contract-level tests, not production rollout
or host-resume acceptance. Independent review, integration and real-interface
acceptance remain release gates.
