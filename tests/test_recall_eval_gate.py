"""Recall quality gate: the labeled corpus in ``evals/`` must not get worse.

``evals/recall_eval.py`` stores 78 labeled memories and runs 59 labeled queries
through the real ``MemoryService``, SQLite/FTS5 and in-process Qdrant. Two
tiers need no key and give the same numbers on every run:

* ``lexical-hash``: hashed bag-of-words vectors (a floor: no semantics),
* ``keyword-only``: the embedding provider is down for the whole run.

Each tier must stay within ``TOLERANCE`` of ``evals/baseline.json`` on
recall@5, hit@1, MRR and nDCG@5, and two properties hold outright: a
superseded, expired or other-project memory is never returned, and an
``as_of`` query returns what was true then.

After an intended ranking change: ``python -m evals.recall_eval --update-baseline``
and commit the new baseline with the change that caused it.
"""

from __future__ import annotations

import json

import pytest

from evals.recall_eval import BASELINE, GATED_METRICS, GATED_TIERS, TOLERANCE, Report, load_corpus, run_eval
from remembra.models.memory import RecallRequest, StoreRequest
from tests._ret_harness import USER, make_stack


@pytest.fixture(scope="module")
def baseline() -> dict[str, dict[str, float]]:
    return json.loads(BASELINE.read_text())


@pytest.fixture(params=GATED_TIERS)
async def report(request, tmp_path) -> Report:
    return await run_eval(request.param, workdir=tmp_path)


def test_corpus_labels_are_consistent() -> None:
    memories, queries = load_corpus()
    assert len(memories) >= 70 and len(queries) >= 50
    categories = {q["category"] for q in queries}
    assert categories >= {
        "single",
        "paraphrase",
        "update_explicit",
        "update_implicit",
        "as_of",
        "expired",
        "isolation",
        "abstain",
    }
    projects = {m["id"]: m["project"] for m in memories}
    for q in queries:
        # A query is answered from its own project; a forbidden label may be anywhere.
        assert all(projects[label] == q["project"] for label in q.get("relevant", [])), q["id"]


async def test_quality_stays_within_tolerance_of_the_baseline(report: Report, baseline) -> None:
    metrics, expected = report.metrics(), baseline[report.embedder]
    assert metrics["queries"] == expected["queries"], "corpus changed: rerun --update-baseline"
    worse = {name: (metrics[name], expected[name]) for name in GATED_METRICS if metrics[name] < expected[name] - TOLERANCE}
    assert not worse, f"{report.embedder}: (now, baseline) {worse}"
    assert metrics["stale_first_rate"] <= expected["stale_first_rate"] + TOLERANCE


async def test_a_retired_expired_or_other_project_memory_is_never_returned(report: Report) -> None:
    leaks = [(r.id, r.returned) for r in report.results if r.leaked]
    assert not leaks


async def test_as_of_returns_what_was_true_then(report: Report) -> None:
    rows = [r for r in report.results if r.category == "as_of"]
    assert rows and all(r.returned and r.returned[0] in r.relevant for r in rows), [(r.id, r.returned) for r in rows]


@pytest.mark.xfail(
    strict=True,
    reason="StoreRequest.supersedes is recorded on the new row but does not retire the old one; "
    "only MemoryService.supersede() and a consolidation SUPERSEDE do.",
)
async def test_store_with_supersedes_retires_the_old_memory(tmp_path) -> None:
    stack = await make_stack(tmp_path)
    try:
        old = await stack.seed("The API rate limit is 60 requests per minute.")
        await stack.service.store(
            StoreRequest(content="The API rate limit is 120 requests per minute.", user_id=USER, project_id="p", supersedes=old),
            skip_extraction=True,
        )
        found = await stack.service.recall(RecallRequest(query="API rate limit", user_id=USER, project_id="p"))
        assert old not in [m.id for m in found.memories]
    finally:
        await stack.close()
