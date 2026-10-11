# Owner approval review surface

`ApprovalReview` is an unregistered detail component for the existing Needs-you
surface. It presents the reviewed request/get/decide/revoke transport snapshot.
This increment does not mount a route or call an endpoint.

The view shows the authenticated requester and intended executor references,
tenant/project, exact resource, scope, base/candidate revisions, digest and expiry.
Request parameters render as escaped plain text. Ordinary inbox prose is never
parsed as approval authority. The immutable decision remains visible alongside
expiry, revocation, stale-candidate, claim and host-block information. A receipt
or decision never renders as delivered, acknowledged, running or completed.

The containing authenticated Crew screen supplies current access, connection
status, its ticking server-adjusted clock and the time of the last authoritative
fetch. A disconnected, refreshing, older-than-30-second or invalid snapshot
disables decisions. These are presentation protections; the server must still
revalidate human JWT, current owner/admin membership, step-up, digest, version,
scope, expiry and candidate at the write boundary.

`DecisionPorts.humanAction` must be the existing `useHumanAction().run`; its
existing `StepUpDialog` remains owned by the containing screen. The `decide`
adapter sends only the captured crew/id/digest/version/choice through the
reviewed authenticated API. It must preserve the existing step-up error mapping.
There is no default network implementation, API-key decision fallback or
consume/executor callback.

The component serializes a click immediately and makes no optimistic decision
update. Success, conflict, cancellation and an ambiguous lost response all cause
an authoritative refresh. Errors are fixed text; raw server/adapter exceptions
are neither displayed nor logged. A same-version background refresh cannot
clear an in-flight click lock. Loading/error transitions retain known details
with actions disabled; temporarily absent data also retains the pending lock in
the containing component. After the attempt settles, a later authoritative
fetch may enable retry against the observed snapshot; changed crew, identity,
version or digest has separate attempt state. The parent must keep this container
mounted during background fetches and must not advance the fetch
timestamp for cached or failed fetches. Navigation must keep callbacks bound to
their originating crew and request. Full navigation/remount and concurrent tabs
still rely on the server's atomic decision/version checks. Manual refresh catches
both synchronous adapter throws and rejected promises without exposing inputs.

Unit tests cover state transitions, exact decision payloads, human-only access,
expiry/freshness, escaping and ambiguous responses. Server-rendered component
checks cover semantics and disabled controls. These do not replace interactive
keyboard/focus testing or the real authenticated owner journey after API/inbox
wiring. Live integration must cover step-up cancellation, duplicate clicks,
cross-tab concurrent decisions, reconnect, revocation during a request, and
refresh races without accepting older versions. Revocation transport and its
confirmation UI remain an explicit integration task. Supported host execution
also remains unfinished; this surface makes no capability grant.
