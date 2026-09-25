"""RET-1 / RET-8 / RET-9 / RET-11 / RET-12 / RET-15 / RET-19 through the real recall path."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tests._ret_harness import USER, make_stack, unit, with_cosine
from remembra.extraction import background
from remembra.models.memory import RecallRequest
from remembra.retrieval.intent import infer_mode_by_rules
from remembra.retrieval.ranking import RelevanceRanker
from remembra.storage.database import _build_fts_match_query

pytestmark = pytest.mark.asyncio

QUERY = "what was I just working on"


@pytest.fixture()
async def stack(tmp_path):
    s = await make_stack(tmp_path)
    yield s
    await background.drain(timeout=5.0)
    await s.close()


async def _recall(stack, query: str, **kw: Any):
    return await stack.service.recall(RecallRequest(query=query, user_id=USER, project_id=kw.pop("project_id", "p"), **kw))


# ---------------------------------------------------------------------------
# Required regression (1): recency intent
# ---------------------------------------------------------------------------


async def test_regression_just_working_on_ranks_fresh_memory_first(stack) -> None:
    """Old memory: cosine 0.95, 60 days old. Fresh memory: cosine 0.55, today.
    With no retrieval_mode given, the recency wording must route to 'debug'
    and the fresh memory must rank first."""
    stack.emb.vectors[QUERY] = unit(1)
    old = await stack.seed("Refactored the billing module for Stripe webhooks", vector=with_cosine(0.95), days_ago=60)
    fresh = await stack.seed("Wiring the pending-embeddings worker into store", vector=with_cosine(0.55))

    resp = await _recall(stack, QUERY)

    assert resp.retrieval_mode == "debug"
    assert resp.retrieval_mode_source == "rules"
    assert [m.id for m in resp.memories][:2] == [fresh, old]
    # Scores are absolute similarities, not rescaled to the best hit.
    assert resp.memories[1].semantic_score == pytest.approx(0.95, abs=0.01)
    assert resp.memories[0].semantic_score == pytest.approx(0.55, abs=0.01)


async def test_explicit_mode_is_respected_over_inference(stack) -> None:
    """The same data with an explicit 'balanced' mode keeps similarity on top -
    proving the routing (not a ranking change) is what fixed the query."""
    stack.emb.vectors[QUERY] = unit(1)
    old = await stack.seed("Refactored the billing module for Stripe webhooks", vector=with_cosine(0.95), days_ago=60)
    await stack.seed("Wiring the pending-embeddings worker into store", vector=with_cosine(0.55))

    resp = await _recall(stack, QUERY, retrieval_mode="balanced")

    assert resp.retrieval_mode == "balanced" and resp.retrieval_mode_source == "request"
    assert resp.memories[0].id == old


async def test_balanced_top_hit_is_not_pinned_to_full_semantic_weight() -> None:
    """RET-1: a weak best match (cosine 0.30) used to be rescaled to 1.0 and
    score the full 0.6 semantic weight."""
    ranked = RelevanceRanker().rank(
        [
            {"id": "a", "content": "x", "semantic_score": 0.30},
            {"id": "b", "content": "y", "semantic_score": 0.25},
        ]
    )
    assert ranked[0].id == "a"
    assert ranked[0].semantic_score == pytest.approx(0.30)
    assert ranked[0].final_score < 0.6 * 0.5


@pytest.mark.parametrize(
    ("query", "mode"),
    [
        ("what was I just working on", "debug"),
        ("latest deploy status for remembra", "debug"),
        ("what did we do yesterday", "debug"),
        ("current status of the POS build", "debug"),
        ("how has the trading strategy evolved over time", "strategic"),
        ("how do I configure the litestream replica", "operational"),
        ("who is the accountant for YaadBooks", None),
    ],
)
def test_intent_rules(query: str, mode: str | None) -> None:
    decision = infer_mode_by_rules(query)
    assert (decision.mode if decision else None) == mode


# ---------------------------------------------------------------------------
# RET-1: Jev choice respects typesafe_mode
# ---------------------------------------------------------------------------


class FakeTypeSafe:
    def __init__(self, choice: str, confidence: float) -> None:
        self.choice = choice
        self.confidence = confidence
        self.calls: list[dict[str, Any]] = []

    async def system_one(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"state": state, "questions": questions})
        probs = {m: 0.0 for m in ("debug", "operational", "strategic", "balanced")}
        probs[self.choice] = self.confidence
        return {
            "answers": {
                "mode": {
                    "type": "choice",
                    "choice": self.choice,
                    "probabilities": probs,
                    "confidence": self.confidence,
                }
            },
            "model": "jev-test",
        }


async def _jev_stack(tmp_path, mode: str, choice: str, confidence: float):
    s = await make_stack(tmp_path, typesafe_api_key="k", typesafe_mode=mode)
    fake = FakeTypeSafe(choice, confidence)
    s.service.jev._client = fake  # type: ignore[assignment]
    return s, fake


async def _decision_rows(db) -> list[dict[str, Any]]:
    cursor = await db.conn.execute("SELECT * FROM decision_log WHERE decision_type = 'query_intent'")
    return [dict(r) for r in await cursor.fetchall()]


async def test_jev_enforce_applies_confident_choice_only_when_no_rule_fired(tmp_path) -> None:
    s, fake = await _jev_stack(tmp_path, "enforce", "strategic", 0.9)
    try:
        await s.seed("YaadBooks accountant is Marcia")
        resp = await _recall(s, "who is the accountant for YaadBooks")
        assert (resp.retrieval_mode, resp.retrieval_mode_source) == ("strategic", "jev")
        rows = await _decision_rows(s.db)
        assert len(rows) == 1 and rows[0]["mode"] == "enforce" and rows[0]["jev_decision"] == "strategic"

        # A rule fired: Jev is not even asked.
        calls = len(fake.calls)
        resp = await _recall(s, "what was I just working on")
        assert (resp.retrieval_mode, resp.retrieval_mode_source) == ("debug", "rules")
        assert len(fake.calls) == calls
    finally:
        await s.close()


async def test_jev_enforce_below_threshold_falls_back_to_balanced(tmp_path) -> None:
    s, _ = await _jev_stack(tmp_path, "enforce", "strategic", 0.4)
    try:
        await s.seed("YaadBooks accountant is Marcia")
        resp = await _recall(s, "who is the accountant for YaadBooks")
        assert (resp.retrieval_mode, resp.retrieval_mode_source) == ("balanced", "default")
    finally:
        await s.close()


async def test_jev_shadow_logs_decision_but_never_applies_it(tmp_path) -> None:
    s, fake = await _jev_stack(tmp_path, "shadow", "strategic", 0.95)
    try:
        await s.seed("YaadBooks accountant is Marcia")
        resp = await _recall(s, "who is the accountant for YaadBooks")
        await background.drain(timeout=5.0)
        assert (resp.retrieval_mode, resp.retrieval_mode_source) == ("balanced", "default")
        rows = await _decision_rows(s.db)
        assert len(rows) == 1
        assert rows[0]["mode"] == "shadow"
        assert rows[0]["llm_decision"] == "default:balanced"
        assert rows[0]["jev_decision"] == "strategic"
        assert rows[0]["agreed"] == 0
        assert fake.calls
    finally:
        await s.close()


async def test_jev_off_never_called(tmp_path) -> None:
    s = await make_stack(tmp_path)  # typesafe_mode=off
    try:
        assert s.service.jev.enabled is False
        await s.seed("YaadBooks accountant is Marcia")
        resp = await _recall(s, "who is the accountant for YaadBooks")
        assert resp.retrieval_mode_source == "default"
        assert await _decision_rows(s.db) == []
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# RET-8 / RET-9: context = the returned memories only, honest label
# ---------------------------------------------------------------------------


async def test_context_is_built_from_the_final_limit_only(stack) -> None:
    stack.emb.vectors["billing provider"] = unit(1)
    await stack.seed("We bill customers through Paddle now", vector=with_cosine(0.9))
    await stack.seed("Invoices are generated monthly on the 1st", vector=with_cosine(0.8))
    await stack.seed("GCT is 15 percent in Jamaica", vector=with_cosine(0.7))

    resp = await _recall(stack, "billing provider", limit=1, retrieval_mode="balanced")

    assert len(resp.memories) == 1
    assert "Paddle" in resp.context
    assert "Invoices" not in resp.context and "GCT" not in resp.context
    assert "%" not in resp.context
    assert "(rank " in resp.context


# ---------------------------------------------------------------------------
# RET-11: keyword stopwords
# ---------------------------------------------------------------------------


def test_fts_query_drops_stopwords_and_duplicates() -> None:
    match = _build_fts_match_query("what was I building with Stripe and stripe")
    assert match == '"building" OR "Stripe"'
    assert _build_fts_match_query("what was it") is None


async def test_stopword_only_overlap_is_not_a_keyword_hit(stack) -> None:
    stack.emb.vectors["what is the plan"] = unit(1)
    noise = await stack.seed("the plan is not what it was", vector=unit(0, 1))
    resp = await _recall(stack, "what is the plan", retrieval_mode="balanced")
    hit = next((m for m in resp.memories if m.id == noise), None)
    # 'plan' is a real keyword, so it may match - but only through 'plan'.
    if hit is not None:
        assert hit.match_sources == ["keyword"]
    resp = await _recall(stack, "what is it", retrieval_mode="balanced")
    assert resp.memories == []


# ---------------------------------------------------------------------------
# RET-12: one threshold default everywhere
# ---------------------------------------------------------------------------


def test_threshold_default_is_aligned_across_server_sdk_and_mcp() -> None:
    from remembra.client.memory import Memory
    from remembra.mcp import server as mcp_server

    server_default = RecallRequest.model_fields["threshold"].default
    sdk_default = inspect.signature(Memory.recall).parameters["threshold"].default
    tool = getattr(mcp_server.recall_memories, "fn", mcp_server.recall_memories)
    mcp_default = inspect.signature(tool).parameters["threshold"].default
    assert server_default == sdk_default == mcp_default == 0.40


# ---------------------------------------------------------------------------
# RET-15: near-duplicates
# ---------------------------------------------------------------------------


async def test_near_duplicates_are_collapsed(stack) -> None:
    stack.emb.vectors["deploy target"] = unit(1)
    first = await stack.seed("Remembra API deploys to Coolify", vector=with_cosine(0.9))
    await stack.seed("remembra api deploys to coolify!", vector=with_cosine(0.89))
    other = await stack.seed("TradeMind deploys with docker compose on the VPS", vector=with_cosine(0.6))

    resp = await _recall(stack, "deploy target", limit=5, retrieval_mode="balanced")

    assert [m.id for m in resp.memories] == [first, other]


# ---------------------------------------------------------------------------
# RET-19: feedback has a (small) effect
# ---------------------------------------------------------------------------


async def test_feedback_moves_ranking(stack) -> None:
    stack.emb.vectors["coffee preference"] = unit(1)
    a = await stack.seed("Mani drinks dark roast", vector=with_cosine(0.8))
    b = await stack.seed("Mani drinks Blue Mountain", vector=with_cosine(0.8))
    for signal, mid in (("unhelpful", a), ("helpful", b)):
        await stack.db.save_feedback(feedback_id=f"f-{mid}", memory_id=mid, user_id=USER, signal=signal)

    resp = await _recall(stack, "coffee preference", retrieval_mode="balanced")

    assert [m.id for m in resp.memories][:2] == [b, a]

    # Another user's feedback never counts.
    scores = await stack.db.get_feedback_scores([a, b], "someone-else")
    assert scores == {}
