"""RET-2 / RET-3 / RET-5 / RET-7 / RET-10 / RET-13 / RET-14 / RET-16 / RET-18
through the real recall path (real SQLite + real local Qdrant engine)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from remembra.core.time import utcnow
from remembra.extraction import background
from remembra.models.memory import Entity, RecallRequest
from remembra.storage.memory_rows import memory_from_row
from tests._ret_harness import USER, make_stack, unit, with_cosine

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def stack(tmp_path):
    s = await make_stack(tmp_path)
    yield s
    await background.drain(timeout=5.0)
    await s.close()


async def _recall(stack, query: str | None, **kw: Any):
    kw.setdefault("project_id", "p")
    kw.setdefault("retrieval_mode", "balanced")
    return await stack.service.recall(RecallRequest(query=query, user_id=USER, **kw))


async def _entity(stack, name: str, etype: str, memory_ids: list[str], project_id: str = "p") -> Entity:
    entity = Entity(canonical_name=name, type=etype)
    await stack.db.save_entity(entity, USER, project_id)
    for mid in memory_ids:
        await stack.db.link_memory_to_entity(mid, entity.id)
    return entity


# ---------------------------------------------------------------------------
# Required regression (2): expired memories are never returned
# ---------------------------------------------------------------------------


async def test_regression_expired_memory_never_returned_by_any_path(stack) -> None:
    stack.emb.vectors["stripe webhook secret rotation"] = unit(1)
    past = utcnow() - timedelta(hours=1)
    expired = await stack.seed("Stripe webhook secret rotation is due Friday", vector=with_cosine(0.99), expires_at=past)
    live = await stack.seed("Stripe webhook endpoint moved to /hooks/stripe", vector=with_cosine(0.7))
    await _entity(stack, "Stripe", "company", [expired, live])

    # The row still exists (archival is the cleanup loop's job) - recall must
    # filter it, on every path.
    assert (await stack.row(expired))["expires_at"] is not None

    semantic = await _recall(stack, "stripe webhook secret rotation")
    assert [m.id for m in semantic.memories] == [live]
    assert {"semantic", "keyword", "graph"} <= set(semantic.memories[0].match_sources)

    fts = await stack.db.search_fts("rotation Friday", USER, "p")
    assert expired not in {mid for mid, _ in fts}

    hits = await stack.qdrant.search(unit(1), USER, "p", limit=10, score_threshold=0.0, active_at=utcnow())
    assert expired not in {mid for mid, _, _ in hits}

    await stack.db.conn.execute(
        "UPDATE memories SET metadata = json_set(metadata, '$.k', 'v') WHERE id IN (?, ?)", (expired, live)
    )
    await stack.db.conn.commit()
    by_filter = await _recall(stack, None, filters={"k": "v"}, limit=10)
    assert [m.id for m in by_filter.memories] == [live]


async def test_as_of_before_expiry_still_sees_the_memory(stack) -> None:
    stack.emb.vectors["promo code"] = unit(1)
    mid = await stack.seed("Promo code EARLYBIRD is active", vector=with_cosine(0.9), expires_at=utcnow() - timedelta(days=1))
    await stack.backdate(mid, 5)
    resp = await _recall(stack, "promo code", as_of=utcnow() - timedelta(days=3))
    assert [m.id for m in resp.memories] == [mid]
    resp = await _recall(stack, "promo code")
    assert resp.memories == []


async def test_status_memories_latest_per_key_and_staleness(stack) -> None:
    stack.emb.vectors["deploy status remembra-api"] = unit(1)
    older = await stack.seed(
        "remembra-api: pushed, NOT deployed",
        vector=with_cosine(0.9),
        memory_type="status",
        metadata={"status_key": "deploy:remembra-api"},
    )
    await stack.backdate(older, 12)
    newer = await stack.seed(
        "remembra-api: live on Coolify",
        vector=with_cosine(0.8),
        memory_type="status",
        metadata={"status_key": "deploy:remembra-api"},
    )
    await stack.backdate(newer, 10)
    other_key = await stack.seed(
        "trademind: deployed",
        vector=with_cosine(0.7),
        memory_type="status",
        metadata={"status_key": "deploy:trademind"},
    )

    resp = await _recall(stack, "deploy status remembra-api", include_decay_score=True)

    ids = [m.id for m in resp.memories]
    assert newer in ids and other_key in ids and older not in ids
    stale = next(m for m in resp.memories if m.id == newer)
    assert stale.memory_type == "status"
    assert stale.age_days == 10
    assert stale.staleness_warning is True  # status_stale_days = 7
    assert stale.decay_score is not None and 0.0 < stale.decay_score < 1.0
    fresh = next(m for m in resp.memories if m.id == other_key)
    assert fresh.staleness_warning is False


# ---------------------------------------------------------------------------
# Required regression (3): junk entity "A" never bleeds into queries with "a"
# ---------------------------------------------------------------------------


async def test_regression_entity_a_not_pulled_into_query_with_word_a(stack) -> None:
    stack.emb.vectors["what is a good recipe for jerk chicken"] = unit(1)
    linked = await stack.seed("Alpha sprint retro notes", vector=unit(0, 0, 1))
    await _entity(stack, "A", "person", [linked])
    await _entity(stack, "I", "person", [linked])
    await _entity(stack, "bot", "concept", [linked])
    recipe = await stack.seed("Jerk chicken: pimento wood, scotch bonnet", vector=with_cosine(0.8))

    resp = await _recall(stack, "what is a good recipe for jerk chicken")

    ids = [m.id for m in resp.memories]
    assert linked not in ids
    assert ids == [recipe]
    assert resp.entities == []


async def test_whole_word_entity_match_still_works_and_is_project_scoped(stack) -> None:
    stack.emb.vectors["Paddle payout schedule"] = unit(1)
    here = await stack.seed("Transfers land every second Tuesday", vector=unit(0, 0, 1), project_id="p")
    elsewhere = await stack.seed("Paddle retail pricing sheet", vector=unit(0, 0, 1), project_id="other")
    await _entity(stack, "Paddle", "company", [here], project_id="p")
    await _entity(stack, "Paddle", "company", [elsewhere], project_id="other")
    # Substring must not match: "Paddleboard" is not "Paddle".
    board = await stack.seed("Paddleboard rental in Negril", vector=unit(0, 0, 0, 1))
    await _entity(stack, "Paddleboard", "product", [board])

    resp = await _recall(stack, "Paddle payout schedule")

    ids = [m.id for m in resp.memories]
    assert ids == [here]
    assert resp.memories[0].match_sources == ["graph"]
    assert [e.canonical_name for e in resp.entities] == ["Paddle"]

    # Linked memories of the matched entity are scoped by user AND project.
    ent_rows = await stack.db.get_user_entities(USER, "p")
    paddle = next(e for e in ent_rows if e.canonical_name == "Paddle")
    assert await stack.db.get_memories_by_entity(paddle.id, user_id=USER, project_id="p") == [here]


async def test_two_letter_acronym_needs_type_count_and_exact_case(stack) -> None:
    graph = stack.service.graph_retriever
    many = [await stack.seed(f"AI note {i}") for i in range(3)]
    ai_org = await _entity(stack, "AI", "org", many)
    few = await stack.seed("HP printer jammed")
    await _entity(stack, "HP", "company", [few])
    it = await stack.seed("IT ticket")
    await _entity(stack, "IT", "concept", [it, it])

    names = lambda es: sorted(e.canonical_name for e in es)  # noqa: E731
    assert names(await graph.find_entity_mentions("what did the AI team ship", USER, "p")) == ["AI"]
    assert names(await graph.find_entity_mentions("say ai quietly", USER, "p")) == []
    assert names(await graph.find_entity_mentions("HP laptop", USER, "p")) == []  # only 1 memory
    assert names(await graph.find_entity_mentions("is IT down", USER, "p")) == []  # untyped acronym
    assert ai_org.id


# ---------------------------------------------------------------------------
# Required regression (4): project + filter recall fills the limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain-metadata-pushdown", "encrypted-overfetch"])
async def test_regression_project_filtered_recall_returns_full_limit(tmp_path, encrypted: bool) -> None:
    overrides: dict[str, Any] = {"encryption_key": "k" * 32} if encrypted else {}
    s = await make_stack(tmp_path, **overrides)
    try:
        assert s.qdrant.metadata_filterable is (not encrypted)
        s.emb.vectors["deploy checklist"] = unit(1)
        # 40 closer, non-matching memories in the same project...
        for i in range(40):
            await s.seed(f"noise item {i}", vector=with_cosine(0.95 - i * 0.001), metadata={"kind": "noise"})
        # ...10 superseded matching ones (never returned)...
        for i in range(10):
            mid = await s.seed(f"old checklist {i}", vector=with_cosine(0.9), metadata={"kind": "checklist"})
            await s.db.mark_memory_superseded(mid, "newer")
        # ...matching memories in another project...
        for i in range(8):
            await s.seed(f"other project checklist {i}", vector=with_cosine(0.9), project_id="q", metadata={"kind": "checklist"})
        # ...and exactly 8 live matches, less similar than all the noise.
        wanted = [
            await s.seed(f"checklist step {i}", vector=with_cosine(0.6 - i * 0.01), metadata={"kind": "checklist"})
            for i in range(8)
        ]

        resp = await _recall(s, "deploy checklist", limit=6, filters={"kind": "checklist"})

        assert len(resp.memories) == 6
        assert {m.id for m in resp.memories} <= set(wanted)
        assert all(m.project_id == "p" and m.metadata["kind"] == "checklist" for m in resp.memories)
    finally:
        await s.close()


async def test_superseded_top_hits_do_not_starve_plain_project_recall(stack) -> None:
    stack.emb.vectors["coolify env"] = unit(1)
    for i in range(12):
        mid = await stack.seed(f"stale env note {i}", vector=with_cosine(0.95))
        await stack.db.mark_memory_superseded(mid, "x")
    live = [await stack.seed(f"env var {i} set", vector=with_cosine(0.5)) for i in range(6)]
    resp = await _recall(stack, "coolify env", limit=6)
    assert sorted(m.id for m in resp.memories) == sorted(live)


# ---------------------------------------------------------------------------
# RET-7: scope + memory_type in payload; scope recall keeps vector hits
# ---------------------------------------------------------------------------


async def test_scope_recall_returns_vector_hits_and_payload_has_fields(stack) -> None:
    stack.emb.vectors["acme contract"] = unit(1)
    scoped = await stack.seed("Acme renewal signed for 12 months", vector=with_cosine(0.9), scope="work:acme", memory_type="fact")
    await stack.seed("Personal gym plan", vector=with_cosine(0.9), scope="personal:health")

    resp = await _recall(stack, "acme contract", scope="work")

    assert [m.id for m in resp.memories] == [scoped]
    assert "semantic" in resp.memories[0].match_sources
    assert resp.memories[0].scope == "work:acme" and resp.memories[0].memory_type == "fact"

    payload = (await stack.qdrant.get_raw_payloads([scoped]))[scoped]
    assert payload["memory_type"] == "fact"
    assert payload["scope"] == "work:acme"
    assert payload["scope_prefixes"] == ["work", "work:acme"]
    assert payload["valid_from"] and payload["valid_to"] is None

    # The worker/reindex path (row -> Memory) carries the same fields.
    rebuilt = memory_from_row(await stack.row(scoped))
    assert (rebuilt.memory_type, rebuilt.scope) == ("fact", "work:acme")
    assert rebuilt.valid_from is not None


# ---------------------------------------------------------------------------
# RET-10 / RET-13: keyword hits hydrated; per-request hybrid/rerank flags
# ---------------------------------------------------------------------------


async def test_keyword_only_hit_has_content_and_metadata(stack) -> None:
    stack.emb.vectors["pimento"] = unit(1)
    mid = await stack.seed("Buy pimento wood chips", vector=unit(0, 1), metadata={"list": "groceries"})
    resp = await _recall(stack, "pimento")
    hit = resp.memories[0]
    assert hit.id == mid and hit.match_sources == ["keyword"]
    assert hit.content == "Buy pimento wood chips"
    assert hit.metadata == {"list": "groceries"}
    assert hit.semantic_score == 0.0  # real cosine, not a made-up default


async def test_enable_hybrid_false_disables_keyword_arm(stack) -> None:
    stack.emb.vectors["pimento"] = unit(1)
    await stack.seed("Buy pimento wood chips", vector=unit(0, 1))
    assert (await _recall(stack, "pimento", enable_hybrid=False)).memories == []
    assert len((await _recall(stack, "pimento", enable_hybrid=True)).memories) == 1


async def test_enable_rerank_flag_is_honoured(stack) -> None:
    calls: list[int] = []

    async def fake_arerank(query: str, docs: list[dict[str, Any]], *a: Any, **k: Any) -> list[Any]:
        calls.append(len(docs))
        return []

    stack.service.reranker.enabled = True
    stack.service.reranker.arerank = fake_arerank  # type: ignore[method-assign]
    stack.emb.vectors["x marks"] = unit(1)
    await stack.seed("x marks the spot", vector=with_cosine(0.9))
    await _recall(stack, "x marks", enable_rerank=False)
    assert calls == []
    await _recall(stack, "x marks", enable_rerank=True)
    assert calls == [1]


# ---------------------------------------------------------------------------
# RET-14: filter-only recall uses the same gate
# ---------------------------------------------------------------------------


async def test_filter_only_recall_excludes_superseded_and_source_and_reports_age(stack) -> None:
    live = await stack.seed("Journal: long ES at open", metadata={"kind": "journal"}, scope="trading:es")
    await stack.backdate(live, 3)
    retired = await stack.seed("Journal: old entry", metadata={"kind": "journal"})
    await stack.db.mark_memory_superseded(retired, live)
    await stack.db.save_memory_metadata(
        memory_id="src-1",
        user_id=USER,
        project_id="p",
        content="verbatim source",
        extracted_facts=[],
        metadata={"kind": "journal", "record_kind": "source"},
        created_at=utcnow(),
        memory_type="source",
    )

    resp = await _recall(stack, None, filters={"kind": "journal"}, limit=10)
    assert [m.id for m in resp.memories] == [live]
    m = resp.memories[0]
    assert m.age_days == 3 and m.scope == "trading:es" and m.freshness_score < 1.0

    history = await _recall(stack, None, filters={"kind": "journal"}, limit=10, include_superseded=True)
    assert {m.id for m in history.memories} == {live, retired}


# ---------------------------------------------------------------------------
# RET-16: bounded SQL per recall; RET-18: graph metadata filter
# ---------------------------------------------------------------------------


async def test_recall_sql_statements_do_not_grow_with_candidates(stack) -> None:
    stack.emb.vectors["ledger"] = unit(1)
    ids = [await stack.seed(f"ledger entry {i}", vector=with_cosine(0.9 - i * 0.001)) for i in range(40)]
    await _entity(stack, "ledger", "concept", ids[:20])

    raw = stack.db._connection  # the aiosqlite connection behind GuardedConnection
    counter = {"n": 0}
    original = raw.execute

    def counting(*a: Any, **k: Any) -> Any:
        counter["n"] += 1
        return original(*a, **k)

    raw.execute = counting  # type: ignore[method-assign]
    try:
        resp = await _recall(stack, "ledger", limit=20)
    finally:
        del raw.execute
    assert len(resp.memories) == 20
    assert counter["n"] <= 25, counter["n"]
    rows = await stack.db.get_memories_by_ids([m.id for m in resp.memories])
    assert all(r["access_count"] == 1 for r in rows.values())


async def test_metadata_filter_applies_to_graph_only_hits(stack) -> None:
    stack.emb.vectors["Marcia update"] = unit(1)
    tagged = await stack.seed("Sent the Q3 invoice", vector=unit(0, 0, 1), metadata={"client": "yaadbooks"})
    untagged = await stack.seed("Called about the invoice", vector=unit(0, 0, 0, 1), metadata={"client": "other"})
    await _entity(stack, "Marcia", "person", [tagged, untagged])
    # graph rows carry metadata as JSON text - must be coerced, not dropped
    assert isinstance((await stack.row(tagged))["metadata"], str)

    resp = await _recall(stack, "Marcia update", filters={"client": "yaadbooks"})

    assert [m.id for m in resp.memories] == [tagged]
    assert resp.memories[0].match_sources == ["graph"]


# ---------------------------------------------------------------------------
# RET-5: spaces recall uses real similarity and the same row gate
# ---------------------------------------------------------------------------


class FakeSpaces:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    async def get_accessible_space_ids(self, agent_id: str) -> list[str]:
        return ["s1"]

    async def get_space_memory_ids(self, space_id: str, limit: int = 500) -> list[str]:
        return list(self.ids)


async def test_spaces_recall_scores_with_real_cosine_and_filters_rows(stack) -> None:
    stack.emb.vectors["release checklist"] = unit(1)
    close = await stack.seed("Release checklist lives in Notion", vector=with_cosine(0.9), user_id="teammate")
    far = await stack.seed("Lunch order for Friday", vector=with_cosine(0.1), user_id="teammate")
    gone = await stack.seed(
        "Old release checklist",
        vector=with_cosine(0.95),
        user_id="teammate",
        expires_at=utcnow() - timedelta(minutes=5),
    )
    retired = await stack.seed("Superseded checklist", vector=with_cosine(0.95), user_id="teammate")
    await stack.db.mark_memory_superseded(retired, close)
    stack.service.space_manager = FakeSpaces([close, far, gone, retired])

    resp = await stack.service.recall_across_spaces("release checklist", agent_id=USER, project_id="p", threshold=0.4)

    assert [m.id for m in resp.memories] == [close]
    assert resp.memories[0].semantic_score == pytest.approx(0.9, abs=0.01)
    assert resp.memories[0].relevance != 0.5
    assert "Notion" in resp.context
