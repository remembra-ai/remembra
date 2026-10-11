# Scoped owner approval contract

Status: internal first increment for independent security review. No production
API, MCP tool, migration, worker, dashboard control or host executor is installed.
The absence of a production store and policy adapter is intentional and fails
closed. This contract does not make Relay approvals available to customers yet.

An unregistered `approval_inputs` parser prepares the future HTTP boundary. It
accepts closed request/decision/revocation/claim bodies, rejects duplicate JSON
keys and non-JSON constants, and limits input to 32 KiB. Rejections carry one fixed
message with no original exception chain. Synthetic HTTP tests verify that
credential-like values and attacker-chosen extra keys appear in neither responses
nor standard exception logs. Routes must authenticate first, enforce the byte
limit while streaming, and call these parsers explicitly. Automatic FastAPI body
validation, raw Pydantic `.errors()`, body logging and traceback-local capture can
expose rejected input and must not be used at this boundary. Parsing is structural
validation only; it does not grant authority or validate action semantics.

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

Each approval call must own its commit. The service and adapter reject an already
active transaction, including one opened directly on the Crew database. Results
leave the context only after that commit; nesting into a caller-owned transaction
could otherwise expose a claim that a later outer rollback undoes. A caller that
hits a busy connection retries using the same request identity after the other
transaction finishes. Independent connections still serialize through SQLite.
Expiry is rechecked after awaited revision and host-policy work, immediately
before creating the decision/claim. Executors must still check expiry at the
actual effect boundary; a claim does not extend its request's lifetime.

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

## Next proposed production slice

The next review unit is persistent request/get/decision/revocation and a human
decision view in the existing Needs-you flow. Keep consume and external execution
unexposed until an effect adapter is independently accepted. A decided request
must remain visibly blocked with `unsupported_adapter` where the host cannot
resume it. This slice needs no new persistent credentials or broader permissions.

The proposed paths below identify the integration review surface; they are not
installed by this increment.

| Area | Smallest proposed change | Review boundary |
| --- | --- | --- |
| Persistence | New `crew/approval_store.py` implementing the port over Crew SQLite; request snapshot, version, separate immutable decision/invalidation/consumption facts; unique requester-scoped idempotency and claim constraints | Additive migration and transaction integration in `crew/db.py` are frozen and need source-owner review before edits |
| Events | Extend the closed schemas and `CrewEventLog` with server-produced approval transitions and reference-only payloads | Existing sequence/hash chain and atomic event/outbox commit must pass adapter conformance and crash tests; no parallel audit stream |
| Delivery | Add a reviewed approval-notification kind/handler to existing Crew outbox, with stable approval/version identity and authorized recipient scope | A committed notification and an inbox ACK provide no proof of host resume; replay must deduplicate after every crash boundary |
| API | New `api/v1/crew_approvals.py` under `/api/v1/crews/{crew_id}/approvals` for request/get/decide/revoke, using existing JWT/session/access helpers and limits | Service initialization and route registration through frozen `main.py` require separate review; do not hide registration in an unfrozen router to bypass it |
| UI | Extend existing Crew Needs-you navigation with a decision detail view | Display requester, exact action/resource/scope/revision, expiry and digest; require fresh human JWT and version; render agent content as redacted untrusted data |

The persistence proposal stores authority references and canonical operation
data, never bearer credentials. The Crew event hash chain remains the transition
audit source. Request/decision immutability, optimistic version checks and unique
consumption are database-enforced, with each service call owning its commit.
Migration tests must exercise upgrade from the current schema, close/reopen,
rollback on injected failure, two independent connections and backup/restore.
Retention and account-erasure behavior need explicit review before immutable
history is applied to production data; frozen erasure/storage files are not
implicitly authorized by this proposal.

The first accepted action also needs a narrow server allowlist and authoritative
revision provider. A repository patch request can be a pilot only after the
source owner defines its resource mapping, exact permitted paths and candidate
fence. No arbitrary action string, wildcard scope, agent-supplied current revision
or detached preview may become executable. Membership, key/session revocation,
step-up age and expiry are revalidated at their decision or claim boundary.

The API must derive identity and login age through existing authenticated helpers.
Its body cannot provide a tenant, principal, executor, credential or auth time.
Test the real middleware and route handler with forged/expired JWTs, revoked
keys/sessions, removed owner membership, stale version/digest and a malicious
identifier in every rejection path. Rate-limit and body-limit failures must also
use safe errors without recording raw bodies. Reconnect reads use the existing
Crew event cursor/snapshot facilities; bounded waiting should reuse those streams.

The next host adapter is a separate review unit. It must advertise actual resume
support, fence the exact operation and expiry at the effect boundary, and recover
by stable receipt id after a lost response. Mandatory human handoffs and source
freezes remain blocks even after an owner decision. If an additional host asks
for persistent connector access, obtain approval for that exact access before
connecting it; no such grant is needed or requested for this internal slice.

Keep pending decision, committed decision, claim, delivery, ACK, running and
completion separate in the UI. Link receipts to existing tasks and reports and
reuse observed/self-reported/server-verified evidence labels and release gates.
Independent integration review, real dashboard acceptance, migration/recovery
evidence and terminal exact-candidate CI are prerequisites to merge or rollout.
