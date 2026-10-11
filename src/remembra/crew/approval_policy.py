"""Closed review-only candidate policy; no host, registry or route is installed.

Artifact revisions name immutable reviewed data, never a running host's HEAD.
Composition must supply verified bytes, the same clock as CrewApprovals, and a
durable tenant-scoped registry serialized with the approval transaction. There
is deliberately no in-memory/default registry and no executable action.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Final, Protocol

from remembra.crew.approval_contracts import ApprovalRequest, Block, Boundary, Contract, Digest, Operation
from remembra.crew.approvals import ApprovalError, ApprovalTransaction

MAX_REVIEW_TTL_MS: Final = 900_000
_WITHDRAWN_REVISION: Final = "0" * 64


class ReviewSelection(Contract):
    """Trusted registry result; not accepted from any public request body."""

    boundary: Boundary
    manifest_digest: Digest
    withdrawn: bool


class ReviewRegistry(Protocol):
    """Durable registration port, implemented/reviewed separately from policy.

    Read using tx's owned serialization boundary. A registration binds boundary,
    resource id and manifest digest immutably. Withdrawal is irreversible; do not
    delete/recreate or reactivate its identity. A replacement needs a new resource
    id. Registry writes share the approval serialization boundary. Restore must
    revalidate against current trusted registration, never resurrect authority.
    """

    async def selection(self, tx: ApprovalTransaction, boundary: Boundary, resource_id: str) -> ReviewSelection | None: ...


# This one closed pilot contains public, reviewed artifacts only. The file
# digests are part of its registry fingerprint, not supplied by the requester.
REVIEW_CANDIDATE_FILES: Final = (
    (
        "dashboard/src/components/crew/inbox/ApprovalReview.tsx",
        "ec66d0d80e1468d9bf8b443e507af6c58071ba846f240eff728b2f23d1b66056",
    ),
    (
        "dashboard/src/components/crew/inbox/__tests__/ApprovalReview.test.tsx",
        "2a4eca46807b219ac020686549a06d8622ca6952963e419ce31c8d68bec79f94",
    ),
    (
        "dashboard/src/components/crew/inbox/__tests__/approvalModel.test.ts",
        "41be215636475dd771c37bfe1737bf08a4dd9ace48b0d8cd610ac4870022b2b3",
    ),
    ("dashboard/src/components/crew/inbox/approvalModel.ts", "93b926d4d59bd48e9cc26ef171f88428cbcb43aa8e0ccc224041c672bbd1562e"),
    ("docs/design/owner-approval-ui.md", "55e85cb9684f4a217654f2c7b592cf0c385ddd9c4ee4e50b9b0fd28e776735f2"),
)
REVIEW_CANDIDATE: Final = Operation(
    action="review_candidate",
    resource_type="reviewed_candidate",
    resource_id="owner_approval_ui_776f2bb",
    scope=tuple(path for path, _ in REVIEW_CANDIDATE_FILES),
    base_revision="8d6481dd90c48d3a2b4ae6e717d34cceaf19fcaf",
    candidate_revision="776f2bba502aadc93bb2c5d526d2d5d36cabe179",
    parameters_json="{}",
)
REVIEW_MANIFEST_DIGEST: Final = hashlib.sha256(
    (
        REVIEW_CANDIDATE.model_dump_json() + "\n" + "\n".join(f"{path}:{digest}" for path, digest in REVIEW_CANDIDATE_FILES)
    ).encode()
).hexdigest()


class ReviewCandidatePolicy:
    """Only owner review of the exact five-file artifact; execution always blocks.

    read_artifact must resolve immutable candidate revision/path bytes from a
    trusted artifact store. It must never read a mutable live worktree by path.
    Construction validates all bytes before this policy can be supplied to a
    service. Registry absence/errors cannot fall back to the checked manifest.
    """

    def __init__(
        self,
        registry: ReviewRegistry,
        *,
        read_artifact: Callable[[str, str], bytes],
        clock: Callable[[], int],
    ) -> None:
        try:
            for path, expected in REVIEW_CANDIDATE_FILES:
                data = read_artifact(REVIEW_CANDIDATE.candidate_revision, path)
                if type(data) is not bytes or hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError
        except Exception:
            raise ApprovalError("approval_artifact_unavailable") from None
        self.registry = registry
        self.clock = clock

    def validate(self, operation: Operation) -> None:
        """Reject all other actions, resources, scope, revisions and parameters."""
        if operation != REVIEW_CANDIDATE:
            raise ApprovalError("unsupported_review_candidate")

    async def current_revision(self, tx: ApprovalTransaction, request: ApprovalRequest) -> tuple[str, str]:
        """Resolve the current registration inside the caller's owned transaction."""
        self.validate(request.operation)
        now = self.clock()
        if type(now) is not int:
            raise ApprovalError("approval_policy_unavailable")
        # CrewApprovals handles elapsed deadlines before/after awaited reads and
        # commits expiration. This additional bound limits new pilot requests.
        if request.expires_at_ms > now + MAX_REVIEW_TTL_MS:
            raise ApprovalError("invalid_expiry")
        try:
            selected = await self.registry.selection(tx, request.boundary, REVIEW_CANDIDATE.resource_id)
            if selected is not None:
                if type(selected) is not ReviewSelection or type(selected.boundary) is not Boundary:
                    raise ValueError
                # Pydantic copy/construct can bypass validators, including on a
                # nested Boundary. Rebuild from explicit fields, not an existing
                # model instance or model_dump (which can warn with raw values).
                selected = ReviewSelection.model_validate(
                    {
                        "boundary": {
                            "crew_id": selected.boundary.crew_id,
                            "tenant_id": selected.boundary.tenant_id,
                            "project_id": selected.boundary.project_id,
                        },
                        "manifest_digest": selected.manifest_digest,
                        "withdrawn": selected.withdrawn,
                    }
                )
                if selected.boundary != request.boundary:
                    raise ValueError
        except Exception:
            raise ApprovalError("approval_registry_unavailable") from None
        if selected is None or selected.withdrawn or selected.manifest_digest != REVIEW_MANIFEST_DIGEST:
            # Returning a mismatching revision lets CrewApprovals commit its
            # irreversible stale invalidation instead of rolling it back.
            return REVIEW_CANDIDATE.base_revision, _WITHDRAWN_REVISION
        return REVIEW_CANDIDATE.base_revision, REVIEW_CANDIDATE.candidate_revision

    async def execution_block(self, tx: ApprovalTransaction, request: ApprovalRequest) -> Block:
        """Review never proves host capability, resumption or permission to act."""
        return Block(reason="unsupported_adapter", resolver="host_operator", next_action="enable_supported_adapter")
