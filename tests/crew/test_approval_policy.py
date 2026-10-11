"""Closed pilot policy with synthetic artifact bytes and a transactional registry."""

import asyncio
import hashlib
import logging
import traceback
import warnings

import pytest
from tests.crew.test_approvals import AGENT, NOW
from tests.crew.test_approvals import h as _base_h

from remembra.crew import approval_policy as policy_module
from remembra.crew.approval_contracts import Boundary
from remembra.crew.approval_policy import (
    MAX_REVIEW_TTL_MS,
    REVIEW_CANDIDATE,
    REVIEW_CANDIDATE_FILES,
    REVIEW_MANIFEST_DIGEST,
    ReviewCandidatePolicy,
    ReviewSelection,
)
from remembra.crew.approvals import ApprovalError, CrewApprovals

base_h = _base_h


class Registry:
    async def selection(self, tx, boundary, resource_id):
        assert tx.conn.in_transaction
        cursor = await tx.conn.execute(
            "SELECT digest, withdrawn FROM test_review_registry WHERE boundary=? AND resource=?",
            (boundary.model_dump_json(), resource_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return ReviewSelection(boundary=boundary, manifest_digest=row[0], withdrawn=bool(row[1])) if row else None


@pytest.fixture
def artifact(monkeypatch):
    # Real bytes are independently verified from committed Git objects. Unit
    # tests use small synthetic bodies, keeping this suite offline/shallow-clone safe.
    data = {path: path.encode() for path, _ in REVIEW_CANDIDATE_FILES}
    monkeypatch.setattr(
        policy_module, "REVIEW_CANDIDATE_FILES", tuple((path, hashlib.sha256(body).hexdigest()) for path, body in data.items())
    )
    calls = []

    def read(revision, path):
        assert revision == REVIEW_CANDIDATE.candidate_revision
        calls.append((revision, path))
        return data[path]

    return read, calls


@pytest.fixture
async def h(base_h, artifact):
    read, _ = artifact
    base_h.policy = ReviewCandidatePolicy(Registry(), read_artifact=read, clock=lambda: base_h.now)
    base_h.service = CrewApprovals(base_h.store, base_h.policy, clock=lambda: base_h.now)
    boundary = Boundary(crew_id=base_h.crew_id, tenant_id="owner", project_id="project")
    async with base_h.db.transaction():
        await base_h.db.conn.execute(
            "CREATE TABLE test_review_registry(boundary TEXT, resource TEXT, digest TEXT, withdrawn INTEGER, "
            "PRIMARY KEY(boundary, resource))"
        )
        await base_h.db.conn.execute(
            "INSERT INTO test_review_registry VALUES (?, ?, ?, 0)",
            (boundary.model_dump_json(), REVIEW_CANDIDATE.resource_id, REVIEW_MANIFEST_DIGEST),
        )
    yield base_h


async def request(h, **kwargs):
    return await h.request(operation=REVIEW_CANDIDATE, **kwargs)


def test_manifest_is_exact_review_artifact_not_host_revision():
    assert REVIEW_CANDIDATE.action == "review_candidate"
    assert REVIEW_CANDIDATE.resource_type == "reviewed_candidate"
    assert REVIEW_CANDIDATE.resource_id == "owner_approval_ui_776f2bb"
    assert REVIEW_CANDIDATE.parameters_json == "{}"
    assert REVIEW_CANDIDATE.base_revision == "8d6481dd90c48d3a2b4ae6e717d34cceaf19fcaf"
    assert REVIEW_CANDIDATE.candidate_revision == "776f2bba502aadc93bb2c5d526d2d5d36cabe179"
    assert REVIEW_CANDIDATE.scope == tuple(path for path, _ in REVIEW_CANDIDATE_FILES)
    assert len(REVIEW_CANDIDATE_FILES) == 5
    assert MAX_REVIEW_TTL_MS == 900_000
    assert (
        hashlib.sha256(
            (REVIEW_CANDIDATE.model_dump_json() + "\n" + "\n".join(f"{p}:{d}" for p, d in REVIEW_CANDIDATE_FILES)).encode()
        ).hexdigest()
        == REVIEW_MANIFEST_DIGEST
    )


def test_startup_verifies_every_exact_path_and_revision(artifact):
    read, calls = artifact
    ReviewCandidatePolicy(Registry(), read_artifact=read, clock=lambda: NOW)
    assert calls == [(REVIEW_CANDIDATE.candidate_revision, path) for path in REVIEW_CANDIDATE.scope]


@pytest.mark.parametrize("value", [b"tampered", "not bytes", None])
def test_startup_rejects_unverified_bytes(value):
    with pytest.raises(ApprovalError, match="approval_artifact_unavailable"):
        ReviewCandidatePolicy(Registry(), read_artifact=lambda *_: value, clock=lambda: NOW)


def test_startup_failure_has_no_raw_adapter_exception_chain():
    def fail(*_):
        raise ValueError("synthetic-private-artifact")

    try:
        ReviewCandidatePolicy(Registry(), read_artifact=fail, clock=lambda: NOW)
    except ApprovalError as error:
        assert "synthetic-private-artifact" not in "".join(traceback.format_exception(error))
    else:
        pytest.fail("expected closed startup")


@pytest.mark.parametrize(
    "change",
    [
        {"action": "apply_reviewed_patch"},
        {"resource_type": "registered_repository_worktree"},
        {"resource_id": "other"},
        {"scope": ("other.py",)},
        {"base_revision": "a" * 40},
        {"candidate_revision": "b" * 40},
        {"parameters_json": '{"execute":true}'},
    ],
)
async def test_rejects_every_changed_authority_field_without_persistence(h, change):
    with pytest.raises(ApprovalError, match="unsupported_review_candidate"):
        await h.request(operation=REVIEW_CANDIDATE.model_copy(update=change))
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


async def test_exact_request_derives_principal_and_has_no_execution_authority(h):
    record = await request(h)
    assert record.request.requester == record.request.executor
    assert record.request.requester.user_id == AGENT.user_id
    decided = await h.decide(record)
    result = await h.consume(decided)
    assert result.acquired is False
    assert result.record.consumption is None
    assert result.record.block.reason == "unsupported_adapter"
    assert result.record.decision == decided.decision
    assert (await h.consume(result.record)).acquired is False


async def test_agent_key_cannot_decide_selected_pilot(h):
    record = await request(h)
    with pytest.raises(Exception) as caught:
        await h.decide(record, user=AGENT)
    assert getattr(caught.value, "status_code", None) == 403 or getattr(caught.value, "code", None) == "human_jwt_required"
    assert (await h.get(record)).decision is None


@pytest.mark.parametrize("delta", [1, MAX_REVIEW_TTL_MS])
async def test_short_ttl_closed_upper_bound_is_accepted(h, delta):
    record = await request(h, expires_at_ms=h.now + delta)
    assert record.request.expires_at_ms == h.now + delta


@pytest.mark.parametrize("delta", [0, -1, MAX_REVIEW_TTL_MS + 1, 86_400_000])
async def test_outside_short_ttl_never_persists(h, delta):
    with pytest.raises(ApprovalError, match="invalid_expiry"):
        await request(h, expires_at_ms=h.now + delta)
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


@pytest.mark.parametrize("state", ["missing", "withdrawn", "replacement"])
async def test_registry_change_commits_stale_invalidation_and_keeps_decision(h, state):
    approved = await h.decide(await request(h))
    async with h.db.transaction():
        if state == "missing":
            await h.db.conn.execute("DELETE FROM test_review_registry")
        elif state == "withdrawn":
            await h.db.conn.execute("UPDATE test_review_registry SET withdrawn=1")
        else:
            await h.db.conn.execute("UPDATE test_review_registry SET digest=?", ("e" * 64,))
    fresh = await h.get(approved)
    assert fresh.invalidation.reason == "stale_candidate"
    assert fresh.decision == approved.decision
    assert (await h.consume(fresh)).acquired is False
    assert (await request(h)).invalidation == fresh.invalidation


async def test_other_tenant_registration_does_not_authorize_request(h):
    async with h.db.transaction():
        await h.db.conn.execute("UPDATE test_review_registry SET boundary=?", ('{"tenant_id":"other"}',))
    with pytest.raises(ApprovalError, match="stale_candidate"):
        await request(h)


@pytest.mark.parametrize(
    "result",
    [
        False,
        {},
        "manifest",
        ReviewSelection(
            boundary=Boundary(crew_id="other", tenant_id="owner", project_id="project"),
            manifest_digest=REVIEW_MANIFEST_DIGEST,
            withdrawn=False,
        ),
    ],
)
async def test_invalid_registry_result_fails_closed(h, result):
    async def selection(*_):
        return result

    h.policy.registry.selection = selection
    with pytest.raises(ApprovalError, match="approval_registry_unavailable"):
        await request(h)


@pytest.mark.parametrize("factory", ["copy", "construct"])
@pytest.mark.parametrize("withdrawn", [None, 0, 1, "false", "", []])
async def test_unvalidated_withdrawn_field_never_means_active(h, factory, withdrawn):
    original = h.policy.registry.selection

    async def selection(*args):
        valid = await original(*args)
        if factory == "copy":
            return valid.model_copy(update={"withdrawn": withdrawn})
        return ReviewSelection.model_construct(
            boundary=valid.boundary, manifest_digest=valid.manifest_digest, withdrawn=withdrawn
        )

    h.policy.registry.selection = selection
    with pytest.raises(ApprovalError, match="^approval_registry_unavailable$"):
        await request(h)
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


@pytest.mark.parametrize("field", ["crew_id", "tenant_id", "project_id"])
@pytest.mark.parametrize("factory", ["copy", "construct"])
async def test_nested_boundary_is_revalidated_from_scalars(h, field, factory):
    original = h.policy.registry.selection

    async def selection(*args):
        valid = await original(*args)
        values = {
            "crew_id": valid.boundary.crew_id,
            "tenant_id": valid.boundary.tenant_id,
            "project_id": valid.boundary.project_id,
        }
        values[field] = None
        boundary = valid.boundary.model_copy(update=values) if factory == "copy" else Boundary.model_construct(**values)
        return valid.model_copy(update={"boundary": boundary})

    h.policy.registry.selection = selection
    with pytest.raises(ApprovalError, match="^approval_registry_unavailable$"):
        await request(h)
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


@pytest.mark.parametrize("digest", [None, 0, b"e" * 64, "not-a-digest"])
async def test_unvalidated_manifest_digest_is_registry_error_not_stale(h, digest):
    original = h.policy.registry.selection

    async def selection(*args):
        valid = await original(*args)
        return valid.model_copy(update={"manifest_digest": digest})

    h.policy.registry.selection = selection
    with pytest.raises(ApprovalError, match="^approval_registry_unavailable$"):
        await request(h)


@pytest.mark.parametrize("missing", ["boundary", "manifest_digest", "withdrawn"])
async def test_incomplete_constructed_selection_fails_closed(h, missing):
    original = h.policy.registry.selection

    async def selection(*args):
        valid = await original(*args)
        values = {"boundary": valid.boundary, "manifest_digest": valid.manifest_digest, "withdrawn": valid.withdrawn}
        del values[missing]
        return ReviewSelection.model_construct(**values)

    h.policy.registry.selection = selection
    with pytest.raises(ApprovalError, match="^approval_registry_unavailable$"):
        await request(h)


@pytest.mark.parametrize("wrong_type", [False, True])
async def test_malformed_field_has_no_serialization_warning_or_raw_error_chain(h, caplog, wrong_type):
    original = h.policy.registry.selection
    rejected = "synthetic private registry input"

    async def selection(*args):
        valid = await original(*args)
        return valid.model_copy(update={"manifest_digest": [rejected] if wrong_type else rejected})

    h.policy.registry.selection = selection
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("error")
        try:
            raise ValueError(rejected)
        except ValueError:
            try:
                await request(h)
            except ApprovalError as error:
                assert str(error) == "approval_registry_unavailable"
                assert rejected not in "".join(traceback.format_exception(error))
                logging.getLogger(__name__).exception("Fixed registry failure")
            else:
                pytest.fail("malformed selection was accepted")
    assert seen == []
    assert rejected not in caplog.text
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


async def test_valid_constructed_selection_is_revalidated_and_accepted(h):
    original = h.policy.registry.selection

    async def selection(*args):
        valid = await original(*args)
        return ReviewSelection.model_construct(
            boundary=valid.boundary.model_copy(), manifest_digest=valid.manifest_digest, withdrawn=False
        )

    h.policy.registry.selection = selection
    assert (await request(h)).request.operation == REVIEW_CANDIDATE


async def test_registry_failure_is_sanitized_and_request_rolls_back(h):
    async def selection(*_):
        raise ValueError("synthetic-private-registry")

    h.policy.registry.selection = selection
    with pytest.raises(ApprovalError) as caught:
        await request(h)
    assert "synthetic-private-registry" not in "".join(traceback.format_exception(caught.value))
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


async def test_registry_cancellation_propagates_without_record(h):
    async def selection(*_):
        raise asyncio.CancelledError

    h.policy.registry.selection = selection
    with pytest.raises(asyncio.CancelledError):
        await request(h)
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM test_approvals"))["n"] == 0


async def test_expiry_crossing_registry_await_commits_expiry_on_decision(h):
    record = await request(h)
    original = h.policy.registry.selection

    async def selection(*args):
        selected = await original(*args)
        h.now = record.request.expires_at_ms
        return selected

    h.policy.registry.selection = selection
    result = await h.decide(record)
    assert result.decision is None
    assert result.invalidation.reason == "expired"
    assert (await h.get(record)).invalidation == result.invalidation
