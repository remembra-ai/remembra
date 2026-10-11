"""Owner approval state machine over a reviewed transactional persistence port.

No production adapter, migration, public route, inbox parser or action runner is
installed here. Caller credentials are validated by existing HTTP auth first;
Crew membership/session/step-up checks are repeated inside the transaction.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol
from uuid import uuid4

from remembra.auth.middleware import AuthenticatedUser
from remembra.crew.access import HUMAN_CREDENTIAL_ID, CrewAccess, load_crew, not_found
from remembra.crew.approval_contracts import (
    ApprovalRecord,
    ApprovalRequest,
    AuditEvent,
    Block,
    Boundary,
    ConsumeResult,
    Consumption,
    Decision,
    DecisionState,
    Invalidation,
    Operation,
    Principal,
)
from remembra.crew.claims import authenticate_session


class ApprovalError(Exception):
    """Stable, non-sensitive domain error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ApprovalTransaction(Protocol):
    """Serializable Crew transaction; all writes/audit/outbox roll back together.

    A production adapter MUST use the existing Crew transaction and event log,
    maintain a unique (boundary, requester, idempotency_key), and compare versions
    on save. It MUST preserve earlier audit events and immutable request/decision
    fields. No per-process lock can satisfy the cross-worker contract.
    """

    @property
    def conn(self) -> Any: ...

    async def get(self, boundary: Boundary, approval_id: str) -> ApprovalRecord | None: ...

    async def find_request(self, request: ApprovalRequest) -> ApprovalRecord | None: ...

    async def save(self, record: ApprovalRecord, *, expected_version: int | None) -> None: ...

    async def append(self, event: AuditEvent, boundary: Boundary) -> None:
        """Append immutable audit AND durable deduplicated notification atomically."""
        ...


class ApprovalStore(Protocol):
    """Fail-closed storage port; there is intentionally no default implementation."""

    def transaction(self) -> AbstractAsyncContextManager[ApprovalTransaction]: ...


class ApprovalPolicy(Protocol):
    """Trusted server adapter, never populated from an API request or inbox text.

    validate must reject unknown actions/resources/parameters and enforce exact
    scope. current_revision must read authoritative candidate state under the
    same serialization boundary (or return a fenced revision). Host/source rules
    and required human handoffs are evaluated by execution_block on every claim.
    Executors must also recheck them at the effect boundary; this receipt does
    not supersede them or make an external side effect transactional.
    """

    def validate(self, operation: Operation) -> None: ...

    async def current_revision(self, tx: ApprovalTransaction, request: ApprovalRequest) -> tuple[str, str]: ...

    async def execution_block(self, tx: ApprovalTransaction, request: ApprovalRequest) -> Block | None: ...


def _boundary(access: CrewAccess) -> Boundary:
    return Boundary(crew_id=access.crew.id, tenant_id=access.crew.owner_user_id, project_id=access.crew.project_id)


async def _principal(tx: ApprovalTransaction, access: CrewAccess, token: str | None) -> Principal:
    user = access.user
    row = await authenticate_session(tx.conn, token, user_id=user.user_id, crew_id=access.crew.id, agent_id=user.agent_id)
    return Principal(user_id=user.user_id, credential_id=user.api_key_id, session_id=row["id"], agent_id=row["agent_id"])


class CrewApprovals:
    """Internal service; creation, decision, invalidation and claim are distinct."""

    def __init__(self, store: ApprovalStore, policy: ApprovalPolicy, *, clock: Callable[[], int] | None = None) -> None:
        self.store = store
        self.policy = policy
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)

    async def _access(
        self,
        tx: ApprovalTransaction,
        crew_id: str,
        user: AuthenticatedUser,
        *,
        human: bool = False,
        auth_time_ms: int | None = None,
        write: bool = False,
    ) -> CrewAccess:
        # Unlike Crew's development shortcut, approval decisions always need JWT.
        access = await load_crew(
            tx.conn,
            crew_id,
            user,
            "crew:override" if human else ("crew:write" if write else "crew:read"),
            step_up=human,
            auth_time_ms=auth_time_ms,
            now_ms=self.clock(),
        )
        if human and user.api_key_id != HUMAN_CREDENTIAL_ID:
            raise ApprovalError("human_jwt_required")
        return access

    async def _record(self, tx: ApprovalTransaction, access: CrewAccess, approval_id: str) -> ApprovalRecord:
        record = await tx.get(_boundary(access), approval_id)
        if record is None or record.request.boundary != _boundary(access):
            raise not_found()
        return record

    async def _save(
        self,
        tx: ApprovalTransaction,
        record: ApprovalRecord,
        kind: Any,
        actor_id: str,
        *,
        previous: ApprovalRecord | None = None,
    ) -> ApprovalRecord:
        await tx.save(record, expected_version=previous.version if previous else None)
        await tx.append(
            AuditEvent(
                approval_id=record.id,
                version=record.version,
                request_digest=record.request.digest,
                kind=kind,
                actor_id=actor_id,
                at_ms=self.clock(),
            ),
            record.request.boundary,
        )
        return record

    async def _fresh(self, tx: ApprovalTransaction, record: ApprovalRecord) -> ApprovalRecord:
        if record.invalidation or record.consumption or (record.decision and record.decision.state == DecisionState.DENIED):
            return record
        reason: Literal["expired", "stale_candidate"] | None = None
        if self.clock() >= record.request.expires_at_ms:
            reason = "expired"
        elif await self.policy.current_revision(tx, record.request) != (
            record.request.operation.base_revision,
            record.request.operation.candidate_revision,
        ):
            reason = "stale_candidate"
        if reason is None:
            return record
        changed = record.model_copy(
            update={
                "invalidation": Invalidation(reason=reason, actor_id="server", at_ms=self.clock()),
                "version": record.version + 1,
            }
        )
        return await self._save(tx, changed, reason, "server", previous=record)

    async def request(
        self,
        *,
        crew_id: str,
        user: AuthenticatedUser,
        session_token: str | None,
        operation: Operation,
        expires_at_ms: int,
        idempotency_key: str,
    ) -> ApprovalRecord:
        """Create a requester/executor-bound request using authenticated identity.

        This increment deliberately restricts execution to the requesting session
        and credential; cross-agent delegation needs a separately reviewed binding.
        """
        async with self.store.transaction() as tx:
            access = await self._access(tx, crew_id, user, write=True)
            principal = await _principal(tx, access, session_token)
            self.policy.validate(operation)
            request = ApprovalRequest(
                boundary=_boundary(access),
                requester=principal,
                executor=principal,
                operation=operation,
                expires_at_ms=expires_at_ms,
                idempotency_key=idempotency_key,
            )
            previous = await tx.find_request(request)
            if previous:
                if previous.request != request:
                    raise ApprovalError("idempotency_conflict")
                return await self._fresh(tx, previous)
            if not self.clock() < expires_at_ms <= self.clock() + 86_400_000:
                raise ApprovalError("invalid_expiry")
            if await self.policy.current_revision(tx, request) != (operation.base_revision, operation.candidate_revision):
                raise ApprovalError("stale_candidate")
            return await self._save(tx, ApprovalRecord(id=f"apr_{uuid4().hex}", request=request), "requested", user.user_id)

    async def get(self, *, crew_id: str, approval_id: str, user: AuthenticatedUser) -> ApprovalRecord:
        """Authoritative lookup; membership and tenant/project scope are rechecked."""
        async with self.store.transaction() as tx:
            access = await self._access(tx, crew_id, user)
            return await self._fresh(tx, await self._record(tx, access, approval_id))

    async def decide(
        self,
        *,
        crew_id: str,
        approval_id: str,
        user: AuthenticatedUser,
        auth_time_ms: int | None,
        expected_version: int,
        request_digest: str,
        approve: bool,
    ) -> ApprovalRecord:
        """Fresh authenticated human owner/admin decision with optimistic concurrency."""
        if type(approve) is not bool:
            raise ApprovalError("invalid_decision")
        async with self.store.transaction() as tx:
            access = await self._access(tx, crew_id, user, human=True, auth_time_ms=auth_time_ms)
            assert auth_time_ms is not None  # require_human(step_up=True) checked the authenticated JWT time
            record = await self._record(tx, access, approval_id)
            if record.version != expected_version or record.request.digest != request_digest:
                raise ApprovalError("decision_conflict")
            if record.decision or record.invalidation or record.consumption:
                raise ApprovalError("decision_closed")
            fresh = await self._fresh(tx, record)
            if fresh.invalidation:
                return fresh  # Commit expiry/staleness; never roll it back via an exception.
            state: Literal[DecisionState.APPROVED, DecisionState.DENIED]
            state = DecisionState.APPROVED if approve else DecisionState.DENIED
            changed = record.model_copy(
                update={
                    "version": record.version + 1,
                    "decision": Decision(
                        state=state,
                        user_id=user.user_id,
                        auth_time_ms=auth_time_ms,
                        at_ms=self.clock(),
                        request_digest=record.request.digest,
                    ),
                }
            )
            return await self._save(tx, changed, state.value, user.user_id, previous=record)

    async def revoke(
        self,
        *,
        crew_id: str,
        approval_id: str,
        user: AuthenticatedUser,
        auth_time_ms: int | None,
        expected_version: int,
        request_digest: str,
    ) -> ApprovalRecord:
        """Invalidate unconsumed authority while retaining the original decision."""
        async with self.store.transaction() as tx:
            access = await self._access(tx, crew_id, user, human=True, auth_time_ms=auth_time_ms)
            record = await self._record(tx, access, approval_id)
            if record.version != expected_version or record.request.digest != request_digest:
                raise ApprovalError("decision_conflict")
            if record.consumption:
                raise ApprovalError("already_consumed")
            if record.invalidation:
                return record
            changed = record.model_copy(
                update={
                    "version": record.version + 1,
                    "invalidation": Invalidation(reason="revoked", actor_id=user.user_id, at_ms=self.clock()),
                }
            )
            return await self._save(tx, changed, "revoked", user.user_id, previous=record)

    async def consume(
        self,
        *,
        crew_id: str,
        approval_id: str,
        user: AuthenticatedUser,
        session_token: str | None,
        request_digest: str,
    ) -> ConsumeResult:
        """Atomically claim once; retries never authorize a second operation.

        A blocked result retains approval but grants no claim. A lost response to
        a committed claim returns acquired=False and the stable receipt, requiring
        host reconciliation instead of blind replay. No action is executed here.
        """
        async with self.store.transaction() as tx:
            access = await self._access(tx, crew_id, user, write=True)
            principal = await _principal(tx, access, session_token)
            record = await self._record(tx, access, approval_id)
            if principal != record.request.executor or principal != record.request.requester:
                raise not_found()
            if record.request.digest != request_digest:
                raise ApprovalError("scope_mismatch")
            if record.consumption:
                return ConsumeResult(record=record)
            record = await self._fresh(tx, record)
            if record.invalidation or not record.decision or record.decision.state != DecisionState.APPROVED:
                return ConsumeResult(record=record)
            # Recheck membership, not a fabricated login or refreshed step-up.
            # This does not create a credential for the recorded approving human.
            async with tx.conn.execute(
                "SELECT 1 FROM crews c LEFT JOIN crew_members m ON m.crew_id = c.id AND m.user_id = ? "
                "WHERE c.id = ? AND (c.owner_user_id = ? OR m.role IN ('owner', 'admin'))",
                (record.decision.user_id, crew_id, record.decision.user_id),
            ) as cur:
                if await cur.fetchone() is None:
                    raise ApprovalError("approver_no_longer_authorized")
            self.policy.validate(record.request.operation)
            block = await self.policy.execution_block(tx, record.request)
            record = await self._fresh(tx, record)
            if record.invalidation:
                return ConsumeResult(record=record)
            if block:
                if record.block != block:
                    changed = record.model_copy(update={"version": record.version + 1, "block": block})
                    record = await self._save(tx, changed, "blocked", user.user_id, previous=record)
                return ConsumeResult(record=record)
            changed = record.model_copy(
                update={
                    "version": record.version + 1,
                    "block": None,
                    "consumption": Consumption(
                        id=f"apc_{uuid4().hex}", request_digest=record.request.digest, executor=principal, at_ms=self.clock()
                    ),
                }
            )
            await self._save(tx, changed, "consumed", user.user_id, previous=record)
            return ConsumeResult(record=changed, acquired=True)
