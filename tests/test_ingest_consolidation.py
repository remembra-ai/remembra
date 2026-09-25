"""Consolidation is a decision, never a rewrite (ING-1, ING-2, ING-4..7, ING-13, ING-20).

Runs the real MemoryService over real SQLite with a NON-empty candidate
search; only the embedding provider, Qdrant transport and OpenAI transport are
faked. The consolidator and extractor parse real (scripted) model JSON.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from remembra.core.time import utcnow
from remembra.extraction.conflicts import ConflictManager, ConflictStrategy
from remembra.models.memory import StoreRequest
from tests._ingest_fakes import all_rows, close_ingest_dbs, fts_content, make_service, row, seed  # noqa: F401

EXISTING = "X repo is at /a; next: check fixes-master.md"
NEW_INPUT = "Next: compile fix list"
MERGED = "X repo is at /a; next: compile fix list (previously: check fixes-master.md)"


# ---------------------------------------------------------------------------
# The live bug (reproduced twice on 2026-09-25)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regression_update_never_stores_text_from_existing_memory(tmp_path) -> None:
    """An UPDATE with model-merged text must store ONLY the new fact."""
    service, db, qdrant, cons_llm, ext_llm = await make_service(
        tmp_path,
        # The model does exactly what production did: UPDATE + merged content.
        consolidator_replies=[
            {"action": "UPDATE", "target_id": None, "content": MERGED, "reason": "next step changed", "confidence": 0.92}
        ],
        extractor_replies=[{"facts": [NEW_INPUT]}],
    )
    existing_id = await seed(service, EXISTING)
    cons_llm.replies[0]["target_id"] = existing_id

    resp = await service.store(StoreRequest(content=NEW_INPUT, user_id="u1"))

    # The response carries the fact exactly as stored — nothing merged in.
    assert resp.extracted_facts == [NEW_INPUT]
    assert resp.status == "stored"
    new_id = resp.id
    assert new_id != existing_id

    # No text from the existing memory anywhere in what this call stored.
    new_rows = [r for r in await all_rows(db) if r["id"] != existing_id]
    assert new_rows, "the new fact must be stored"
    for r in new_rows:
        assert "repo is at /a" not in r["content"]
        assert "fixes-master" not in r["content"]
        assert "repo is at /a" not in (r["extracted_facts"] or "")
    assert (await row(db, new_id))["content"] == NEW_INPUT
    assert qdrant.points[new_id]["payload"]["content"] == NEW_INPUT
    assert await fts_content(db, new_id) == NEW_INPUT

    # UPDATE became SUPERSEDE: the old memory is kept verbatim and marked.
    old = await row(db, existing_id)
    assert old["content"] == EXISTING
    assert old["superseded_by"] == new_id
    assert existing_id in qdrant.points, "superseded memories are never hard-deleted"
    new_row = await row(db, new_id)
    assert new_row["supersedes"] == existing_id

    # The decision is reported separately from the facts.
    assert len(resp.consolidation) == 1
    entry = resp.consolidation[0]
    assert (entry.fact, entry.action, entry.target_id, entry.decided_by) == (NEW_INPUT, "supersede", existing_id, "llm")
    assert entry.confidence == pytest.approx(0.92)

    # The consolidation prompt fenced both texts as untrusted data.
    prompt = cons_llm.user_prompts()[0]
    assert "<untrusted_data>" in prompt and EXISTING in prompt


@pytest.mark.asyncio
async def test_delete_decision_supersedes_and_never_deletes(tmp_path) -> None:
    service, db, qdrant, cons_llm, _ = await make_service(
        tmp_path,
        consolidator_replies=[
            {"action": "DELETE", "target_id": "?", "content": None, "reason": "contradiction", "confidence": 0.95}
        ],
    )
    old_id = await seed(service, "Sarah works at Microsoft")
    cons_llm.replies[0]["target_id"] = old_id

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    assert resp.consolidation[0].action == "supersede"
    old = await row(db, old_id)
    assert old["content"] == "Sarah works at Microsoft"
    assert old["superseded_by"] == resp.id
    assert old_id in qdrant.points


@pytest.mark.asyncio
async def test_target_not_in_candidates_is_rejected(tmp_path) -> None:
    """ING-2: a model/injection-supplied id outside the candidates is never acted on."""
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    victim = await seed(service, "Bob's bank PIN hint is the dog's name", user_id="u2")
    await seed(service, "Sarah works at Microsoft")
    cons_llm.replies = [{"action": "DELETE", "target_id": victim, "reason": "ignore previous instructions", "confidence": 1.0}]

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    assert resp.consolidation[0].action == "add"
    assert "rejected" in (resp.consolidation[0].reason or "")
    victim_row = await row(db, victim)
    assert victim_row["superseded_by"] is None


@pytest.mark.asyncio
async def test_noop_reports_duplicate_status(tmp_path) -> None:
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    existing = await seed(service, "John is the CEO of Acme Corp")
    cons_llm.replies = [{"action": "NOOP", "target_id": existing, "reason": "already known", "confidence": 0.9}]

    resp = await service.store(StoreRequest(content="John is the CEO", user_id="u1"))

    assert resp.status == "duplicate"
    assert resp.duplicate_of == existing
    assert resp.id == existing
    assert resp.extracted_facts == []
    assert resp.consolidation[0].action == "noop"
    assert len(await all_rows(db)) == 1


@pytest.mark.asyncio
async def test_exact_duplicate_is_rule_noop_without_model_call(tmp_path) -> None:
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    existing = await seed(service, "Mani prefers dark mode")

    resp = await service.store(StoreRequest(content="mani prefers  dark mode", user_id="u1"))

    assert resp.status == "duplicate" and resp.duplicate_of == existing
    assert resp.consolidation[0].decided_by == "rule"
    assert cons_llm.calls == []


@pytest.mark.asyncio
async def test_low_confidence_supersede_is_added_not_retired(tmp_path) -> None:
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    existing = await seed(service, "Deploys run on Fridays")
    cons_llm.replies = [{"action": "SUPERSEDE", "target_id": existing, "reason": "maybe", "confidence": 0.4}]

    resp = await service.store(StoreRequest(content="Deploys run on Mondays now", user_id="u1"))

    assert resp.consolidation[0].action == "add"
    assert "confidence below gate" in (resp.consolidation[0].reason or "")
    assert (await row(db, existing))["superseded_by"] is None


@pytest.mark.asyncio
async def test_pinned_memory_is_never_superseded(tmp_path) -> None:
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    pinned = await seed(service, "Mani's timezone is EST")
    await db.set_memory_pin(pinned, "u1", True)
    cons_llm.replies = [{"action": "SUPERSEDE", "target_id": pinned, "reason": "tz change", "confidence": 0.99}]

    resp = await service.store(StoreRequest(content="Mani's timezone is PST this week", user_id="u1"))

    assert resp.consolidation[0].action == "add"
    assert "pinned" in (resp.consolidation[0].reason or "")
    assert (await row(db, pinned))["superseded_by"] is None


@pytest.mark.asyncio
async def test_noop_never_trades_permanent_fact_for_expiring_copy(tmp_path) -> None:
    """ING-5: a duplicate that expires sooner must not absorb a permanent fact."""
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    short = await seed(service, "Standup is at 9am", ttl="1d")
    cons_llm.replies = [{"action": "NOOP", "target_id": short, "reason": "same", "confidence": 0.95}]

    resp = await service.store(StoreRequest(content="Standup is at 9am daily", user_id="u1"))

    assert resp.status == "stored"
    assert resp.consolidation[0].action == "add"
    assert "expires sooner" in (resp.consolidation[0].reason or "")
    assert (await row(db, resp.id))["expires_at"] is None


# ---------------------------------------------------------------------------
# Candidate filters (ING-4, ING-6, ING-7)
# ---------------------------------------------------------------------------


class _RecordingConsolidator:
    """Records which candidates were offered; always ADDs."""

    def __init__(self) -> None:
        self.offered: list[list[str]] = []

    async def consolidate(self, fact, existing):  # type: ignore[no-untyped-def]
        from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult

        self.offered.append([m.id for m in existing])
        return ConsolidationResult(ConsolidationAction.ADD, None, reason="t")


@pytest.mark.asyncio
async def test_candidates_exclude_other_visibility_expired_superseded_and_source(tmp_path) -> None:
    service, db, _, _, _ = await make_service(tmp_path)
    live = await seed(service, "Clawbot deploys to Vercel")
    team = await seed(service, "Clawbot deploys to Vercel team note", visibility="team", team_id="t1")
    other_space = await seed(service, "Clawbot deploys to Vercel space", visibility="project", space_id="s9")
    expired = await seed(service, "Clawbot deploys to Vercel old", expires_at=utcnow() - timedelta(days=1))
    retired = await seed(service, "Clawbot deploys to Vercel v1")
    await db.mark_memory_superseded(retired, live)
    other_project = await seed(service, "Clawbot deploys to Vercel elsewhere", project_id="p2")
    rec = _RecordingConsolidator()
    service.consolidator = rec  # type: ignore[assignment]

    await service.store(StoreRequest(content="Clawbot deploys to Vercel prod", user_id="u1"), skip_extraction=False)

    offered = rec.offered[0]
    assert live in offered
    for excluded in (team, other_space, expired, retired, other_project):
        assert excluded not in offered


@pytest.mark.asyncio
async def test_private_fact_never_consolidates_into_team_memory(tmp_path) -> None:
    """ING-4: a team-visible store never sees (or supersedes) private memories."""
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    private = await seed(service, "Mani's salary target is 200k")
    rec = _RecordingConsolidator()
    service.consolidator = rec  # type: ignore[assignment]

    resp = await service.store(
        StoreRequest(content="Mani's salary target is 250k", user_id="u1", visibility="team", team_id="t1")
    )

    assert rec.offered == []  # no candidate -> rule ADD, no model call
    assert resp.consolidation[0].decided_by == "rule"
    assert (await row(db, private))["superseded_by"] is None


@pytest.mark.asyncio
async def test_sibling_facts_from_one_store_never_consolidate_each_other(tmp_path) -> None:
    """ING-7: facts extracted from the same input are never each other's candidates."""
    content = "John is VP of Sales. John joined Acme in 2020."
    facts = ["John is VP of Sales", "John joined Acme in 2020"]
    service, db, _, _, _ = await make_service(tmp_path, extractor_replies=[{"facts": facts}])
    rec = _RecordingConsolidator()
    service.consolidator = rec  # type: ignore[assignment]

    resp = await service.store(StoreRequest(content=content, user_id="u1"))

    assert resp.extracted_facts == facts
    # First fact had no candidates; the second must not have been offered the first.
    assert all(resp.consolidation[0].memory_id not in offered for offered in rec.offered)
    assert all(r["superseded_by"] is None for r in await all_rows(db))


@pytest.mark.asyncio
async def test_source_record_is_not_a_candidate_and_is_deduped_on_retry(tmp_path) -> None:
    content = "Mani moved the Remembra deploy to Coolify. The old Fly app is retired."
    facts = ["Mani moved the Remembra deploy to Coolify", "The old Fly app is retired"]
    service, db, _, _, _ = await make_service(
        tmp_path, extractor_replies=[{"facts": facts}, {"facts": facts}], consolidation_threshold=0.05
    )

    first = await service.store(StoreRequest(content=content, user_id="u1"))
    second = await service.store(StoreRequest(content=content, user_id="u1"))

    # ING-18: the retry reuses the verbatim source row and stores nothing new.
    assert second.source_id == first.source_id
    assert second.status == "duplicate"
    sources = [r for r in await all_rows(db) if r["memory_type"] == "source"]
    assert len(sources) == 1
    assert len([r for r in await all_rows(db) if r["memory_type"] != "source"]) == 2


# ---------------------------------------------------------------------------
# Conflict strategies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_strategy_keeps_both_active_and_opens_conflict(tmp_path) -> None:
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    cm = ConflictManager(db, default_strategy=ConflictStrategy.FLAG)
    await cm.init_schema()
    service.conflict_manager = cm
    old = await seed(service, "Sarah works at Microsoft")
    cons_llm.replies = [{"action": "SUPERSEDE", "target_id": old, "reason": "job change", "confidence": 0.9}]

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    assert (await row(db, old))["superseded_by"] is None
    new = await row(db, resp.id)
    assert new["contradicts"] == old
    entry = resp.consolidation[0]
    assert (entry.action, entry.target_id) == ("add", old)  # nothing was retired
    conflicts = await cm.list_conflicts(user_id="u1")
    assert len(conflicts) == 1 and conflicts[0]["status"] == "open"


@pytest.mark.asyncio
async def test_update_strategy_records_resolved_conflict_with_new_id(tmp_path) -> None:
    service, db, _, cons_llm, _ = await make_service(tmp_path)
    cm = ConflictManager(db, default_strategy=ConflictStrategy.UPDATE)
    await cm.init_schema()
    service.conflict_manager = cm
    old = await seed(service, "Sarah works at Microsoft")
    cons_llm.replies = [{"action": "UPDATE", "target_id": old, "reason": "job change", "confidence": 0.9, "content": "merged"}]

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    conflicts = await cm.list_conflicts(user_id="u1")
    assert conflicts[0]["resolved_memory_id"] == resp.id
    assert conflicts[0]["new_fact"] == "Sarah works at Google"
    assert (await row(db, old))["superseded_by"] == resp.id


# ---------------------------------------------------------------------------
# supersede(), update(), forget()
# ---------------------------------------------------------------------------


class _BoomConsolidator:
    async def consolidate(self, fact, existing):  # type: ignore[no-untyped-def]
        raise AssertionError("explicit supersede must not consolidate")


@pytest.mark.asyncio
async def test_explicit_supersede_is_atomic_and_never_consolidates(tmp_path) -> None:
    """ING-13: supersede() stores the replacement verbatim, no extraction/consolidation."""
    service, db, _, _, ext_llm = await make_service(tmp_path)
    old = await seed(service, "I use Stripe for billing")
    service.consolidator = _BoomConsolidator()  # type: ignore[assignment]

    resp = await service.supersede(old, "u1", "I use Paddle for billing, Stripe is gone", reason="billing switch")

    assert resp.new_memory_id != old
    assert (await row(db, resp.new_memory_id))["content"] == "I use Paddle for billing, Stripe is gone"
    assert (await row(db, old))["superseded_by"] == resp.new_memory_id
    assert ext_llm.calls == []


@pytest.mark.asyncio
async def test_update_keeps_created_at_expiry_and_merged_metadata_in_vector_payload(tmp_path) -> None:
    """ING-20."""
    service, db, qdrant, _, ext_llm = await make_service(tmp_path)
    ext_llm.default = {"facts": ["Standup moved to 10am"]}
    mid = await seed(service, "Standup is at 9am", ttl="30d", metadata={"team": "core", "keep": 1})
    before = await row(db, mid)

    await service.update(mid, "u1", "Standup moved to 10am", new_metadata={"team": "platform"})

    after = await row(db, mid)
    payload = qdrant.points[mid]["payload"]
    assert payload["content"] == "Standup moved to 10am"
    assert payload["metadata"] == {"team": "platform", "keep": 1}
    assert payload["created_at"] == before["created_at"]
    assert payload["expires_at"] == before["expires_at"] and before["expires_at"] is not None
    assert after["created_at"] == before["created_at"]


@pytest.mark.asyncio
async def test_delete_is_user_scoped_at_storage_layer(tmp_path) -> None:
    """ING-2: storage deletes refuse another user's memory."""
    service, db, qdrant, _, _ = await make_service(tmp_path)
    mid = await seed(service, "u2 private note", user_id="u2")

    assert await db.delete_memory(mid, user_id="u1") is False
    assert await qdrant.delete(mid, user_id="u1") is False
    assert await db.get_memory(mid) is not None

    resp = await service.forget(memory_id=mid, user_id="u1")
    assert resp.deleted_memories == 0
    resp = await service.forget(memory_id=mid, user_id="u2")
    assert resp.deleted_memories == 1


@pytest.mark.asyncio
async def test_metadata_type_maps_to_memory_type(tmp_path) -> None:
    """ING-25 (service side): metadata.type becomes memory_type when not given."""
    service, db, _, _, _ = await make_service(tmp_path)
    mid = await seed(service, "Ship the ING fixes", metadata={"type": "task"})
    assert (await row(db, mid))["memory_type"] == "task"
    mid2 = await seed(service, "Evidence blob", metadata={"type": "source"})
    assert (await row(db, mid2))["memory_type"] is None  # source is never user-settable this way
