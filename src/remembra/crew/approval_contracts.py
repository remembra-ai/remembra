"""Scoped owner approval value objects; no credential or execution grants.

These internal contracts are deliberately not registered as API/MCP tools. A
reviewed persistence adapter and host executor are required before exposure.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from remembra.crew.schemas import clip_item
from remembra.security.secrets import redact_secrets

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")]
Revision = Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Contract(BaseModel):
    """Immutable, closed values: nested collections must also be immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Principal(Contract):
    """Authenticated requester/executor, built by the server from Crew auth."""

    user_id: Identifier
    credential_id: Identifier
    session_id: Identifier
    agent_id: Identifier


class Boundary(Contract):
    """Existing Crew tenant and project identity, loaded on every operation."""

    crew_id: Identifier
    tenant_id: Identifier  # Crew.owner_user_id, not an agent-supplied tenant
    project_id: Identifier


class Operation(Contract):
    """Exact operation; a server-side allowlisted validator must check semantics.

    Parameters are canonical JSON rather than a mutable dict. They may contain
    safe references/digests but never credentials. No shell execution implied.
    """

    action: Identifier
    resource_type: Identifier
    resource_id: Identifier
    scope: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=100)]
    base_revision: Revision
    candidate_revision: Revision
    parameters_json: Annotated[str, Field(max_length=8192)]

    @field_validator("scope")
    @classmethod
    def exact_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or tuple(sorted(value)) != value:
            raise ValueError("scope must be unique and sorted")
        if any(p.startswith("/") or any(s in {"", ".", ".."} for s in p.split("/")) for p in value):
            raise ValueError("scope must contain exact relative resource paths")
        return value

    @field_validator("parameters_json")
    @classmethod
    def canonical_parameters(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
            canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise ValueError("parameters must be bounded canonical JSON") from exc
        if not isinstance(parsed, dict) or canonical != value:
            raise ValueError("parameters must be a canonical JSON object without duplicate keys")
        if redact_secrets(value).text != value:
            raise ValueError("parameters must use references, never credentials")
        return value


class ApprovalRequest(Contract):
    """All authority-bearing fields participate in the canonical digest."""

    boundary: Boundary
    requester: Principal
    executor: Principal
    operation: Operation
    expires_at_ms: Annotated[int, Field(gt=0)]
    idempotency_key: Identifier

    @property
    def digest(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(body.encode()).hexdigest()


class DecisionState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class Decision(Contract):
    """An immutable human decision, distinct from revocation and consumption."""

    state: Literal[DecisionState.APPROVED, DecisionState.DENIED]
    user_id: Identifier
    auth_time_ms: int
    at_ms: int
    request_digest: Digest


class Invalidation(Contract):
    """Does not overwrite the original human decision."""

    reason: Literal["revoked", "expired", "stale_candidate"]
    actor_id: Identifier
    at_ms: int


class Block(Contract):
    """Host-controlled reason codes; arbitrary agent prose cannot grant access."""

    reason: Literal["host_policy", "source_freeze", "human_handoff_required", "unsupported_adapter"]
    resolver: Literal["human_owner", "host_operator", "source_owner"]
    next_action: Literal["confirm_in_host", "review_source_scope", "enable_supported_adapter"]


class Consumption(Contract):
    """One-time claim receipt. A replay returns it with acquired=False.

    A claim is not execution or completion. Recovery needs an idempotent host
    adapter using this stable receipt id, or an explicit human reconciliation.
    """

    id: Identifier
    request_digest: Digest
    executor: Principal
    at_ms: int


class ApprovalRecord(Contract):
    """Stored snapshot; adapters enforce immutable request/decision/history."""

    id: Identifier
    request: ApprovalRequest
    version: Annotated[int, Field(gt=0)] = 1
    decision: Decision | None = None
    invalidation: Invalidation | None = None
    block: Block | None = None
    consumption: Consumption | None = None

    @property
    def decision_state(self) -> DecisionState:
        if self.decision:
            return self.decision.state
        if self.invalidation and self.invalidation.reason == "expired":
            return DecisionState.EXPIRED
        return DecisionState.PENDING


class ConsumeResult(Contract):
    """Only acquired=True denotes a newly committed claim, never a retry."""

    record: ApprovalRecord
    acquired: bool = False


class AuditEvent(Contract):
    """Append-only transition + notification identity, committed with the row.

    The future adapter maps this to existing Crew event sequencing and outbox;
    it must not store parameters, session tokens or notification body as authority.
    """

    approval_id: Identifier
    version: int
    request_digest: Digest
    kind: Literal["requested", "approved", "denied", "revoked", "expired", "stale_candidate", "blocked", "consumed"]
    actor_id: Identifier
    at_ms: int

    @property
    def delivery_id(self) -> str:
        return f"{self.approval_id}:{self.version}"


def display_data(text: str) -> str:
    """Reuse Crew's injection-safe data formatting and unconditional redaction."""
    return clip_item(redact_secrets(text).text, 500)
