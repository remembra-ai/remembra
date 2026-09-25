"""RET-4: CrossEncoder off the event loop, absolute scores, logit cutoff, readiness."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from remembra.core.readiness import DEGRADED, OK, ReadinessChecker
from remembra.extraction import background
from remembra.models.memory import RecallRequest
from remembra.retrieval.reranker import CrossEncoderReranker, sigmoid
from tests._ret_harness import USER, make_stack, unit, with_cosine

pytestmark = pytest.mark.asyncio


class FakeCrossEncoder:
    """Stands in for sentence_transformers.CrossEncoder: fixed logits per document."""

    def __init__(self, logits: dict[str, float]) -> None:
        self.logits = logits
        self.threads: list[str] = []

    def predict(self, pairs: list[list[str]]) -> list[float]:
        self.threads.append(threading.current_thread().name)
        return [self.logits.get(doc, -12.0) for _query, doc in pairs]


def _loaded(model: FakeCrossEncoder, **kw: Any) -> CrossEncoderReranker:
    reranker = CrossEncoderReranker(enabled=True, **kw)
    reranker._model = model
    reranker._initialized = True
    return reranker


async def test_predict_runs_in_a_worker_thread_and_keeps_raw_logits() -> None:
    model = FakeCrossEncoder({"good": 4.0, "bad": -6.0})
    reranker = _loaded(model)
    results = await reranker.arerank("q", [{"id": "1", "content": "good"}, {"id": "2", "content": "bad"}])
    assert model.threads and model.threads[0] != threading.main_thread().name
    by_id = {r.id: r for r in results}
    assert by_id["1"].raw_score == 4.0 and by_id["2"].raw_score == -6.0
    assert results[0].id == "1"


async def test_scores_are_absolute_not_min_max() -> None:
    """A lone weak document must not be promoted to 1.0."""
    reranker = _loaded(FakeCrossEncoder({"weak": -5.0}), blend_original=False)
    [only] = await reranker.arerank("q", [{"id": "w", "content": "weak"}])
    assert only.rerank_score == pytest.approx(sigmoid(-5.0))
    assert only.rerank_score < 0.01


async def test_min_logit_cutoff_drops_irrelevant_documents() -> None:
    reranker = _loaded(FakeCrossEncoder({"good": 2.0, "junk": -9.0}), min_logit=-5.0)
    results = await reranker.arerank("q", [{"id": "g", "content": "good"}, {"id": "j", "content": "junk"}])
    assert [r.id for r in results] == ["g"]
    assert reranker.status()["last_run"] == {"input": 2, "dropped_below_min_logit": 1}


async def test_recall_uses_reranker_scores_and_cutoff(tmp_path) -> None:
    s = await make_stack(tmp_path, enable_reranking=True, rerank_min_logit=-5.0)
    try:
        s.emb.vectors["billing provider"] = unit(1)
        paddle = await s.seed("We bill through Paddle", vector=with_cosine(0.6))
        noise = await s.seed("Billing provider trivia from 2019", vector=with_cosine(0.9))
        model = FakeCrossEncoder({"We bill through Paddle": 5.0, "Billing provider trivia from 2019": -8.0})
        s.service.reranker._model = model
        s.service.reranker._initialized = True

        resp = await s.service.recall(
            RecallRequest(query="billing provider", user_id=USER, project_id="p", retrieval_mode="balanced")
        )

        assert [m.id for m in resp.memories] == [paddle]  # the -8 logit is cut off
        assert noise not in {m.id for m in resp.memories}
        assert model.threads and model.threads[0] != threading.main_thread().name
    finally:
        await background.drain(timeout=5.0)
        await s.close()


async def test_readiness_reports_reranker_state(monkeypatch) -> None:
    import remembra.core.readiness as readiness

    monkeypatch.setattr(
        readiness.importlib.util, "find_spec", lambda name, *a: object() if name == "sentence_transformers" else None
    )
    settings = SimpleNamespace(enable_reranking=True, rerank_model="m")

    unavailable = CrossEncoderReranker(enabled=True)
    unavailable._initialized = True
    unavailable._model = None
    unavailable.last_error = "model unavailable (sentence-transformers missing or load failed)"
    check = ReadinessChecker(settings=settings, reranker=unavailable)._check_reranker()
    assert check["status"] == DEGRADED and check["state"] == "unavailable"

    loaded = _loaded(FakeCrossEncoder({}))
    check = ReadinessChecker(settings=settings, reranker=loaded)._check_reranker()
    assert check["status"] == OK and check["state"] == "loaded"

    lazy = CrossEncoderReranker(enabled=True)
    check = ReadinessChecker(settings=settings, reranker=lazy)._check_reranker()
    assert check["status"] == OK and check["state"] == "not_loaded"
