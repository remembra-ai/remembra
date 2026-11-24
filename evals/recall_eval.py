"""Offline recall-quality eval: a labeled corpus run through the real store and recall pipeline.

What is real: ``MemoryService`` (store and recall as shipped), ``Database`` on a
SQLite file (FTS5, migrations), and ``QdrantStore`` on qdrant-client's
in-process engine. Nothing is mocked except, in the default tier, the embedder.

Tiers (``--embedder``):

``lexical-hash`` (default, no key, deterministic)
    A hashed bag-of-words vector. It has no notion of meaning, so this tier is
    a floor: it measures keyword retrieval plus everything the pipeline does
    around the vector search (project scoping, supersession, expiry, ``as_of``,
    ranking). CI gates on it (``tests/test_recall_eval_gate.py``).

``keyword-only`` (no key, deterministic)
    The embedding provider is down for the whole run, the way it is during a
    quota outage: stores are kept and queued, recall answers from keyword
    search alone (``degraded="keyword_only"``). CI gates on it too.

``openai``
    ``text-embedding-3-small`` through the server's own ``OpenAIEmbedder``,
    using ``OPENAI_API_KEY``. This is the number that describes the product.
    About 140 short embedding calls per run. The run stops with an error if
    the provider rejects a call, so a bad key cannot pass as a result.

Facts are stored atomically (``skip_extraction``): the eval measures retrieval,
not LLM fact extraction or consolidation.

    python -m evals.recall_eval                      # lexical floor, table
    python -m evals.recall_eval --embedder openai    # real embeddings
    python -m evals.recall_eval --json out.json      # full per-query results
    python -m evals.recall_eval --update-baseline    # after an intended ranking change
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

CORPUS = Path(__file__).resolve().parent / "corpus"
BASELINE = Path(__file__).resolve().parent / "baseline.json"
GATED_TIERS = ("lexical-hash", "keyword-only")
# Higher is better; CI fails when one drops more than TOLERANCE below the baseline.
GATED_METRICS = ("recall_at_5", "hit_at_1", "mrr", "ndcg_at_5")
TOLERANCE = 0.03
USER = "eval-user"
LIMIT = 5
HASH_DIM = 256

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "by",
        "can",
        "do",
        "does",
        "each",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "she",
        "should",
        "that",
        "the",
        "their",
        "them",
        "there",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
    ]
)


def _tokens(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if tok in _STOPWORDS:
            continue
        for suffix in ("ing", "es", "ed", "s"):
            if len(tok) > len(suffix) + 2 and tok.endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        out.append(tok)
    return out


class LexicalHashEmbedder:
    """Deterministic hashed bag-of-words vectors. No semantics: a floor, not a model."""

    name = "lexical-hash"
    dimensions = HASH_DIM
    breaker = None

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * HASH_DIM
        for tok in _tokens(text):
            vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % HASH_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if not norm:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def close(self) -> None:
        return None


class ProviderDownEmbedder:
    """An embedding provider that is out of quota for the whole run."""

    name = "keyword-only"
    dimensions = HASH_DIM
    breaker = None

    async def embed(self, text: str) -> list[float]:
        from remembra.core.provider_errors import ProviderErrorKind
        from remembra.storage.embeddings import EmbeddingProviderError

        raise EmbeddingProviderError("eval: provider down", 429, kind=ProviderErrorKind.QUOTA_EXHAUSTED, provider="eval")

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def close(self) -> None:
        return None


class _CachedEmbedder:
    """Wraps a provider so each distinct text is embedded once per run."""

    breaker = None

    def __init__(self, inner: Any, name: str, dimensions: int) -> None:
        self._inner = inner
        self.name = name
        self.dimensions = dimensions
        self._cache: dict[str, list[float]] = {}

    async def embed(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = await self._inner.embed(text)
        return self._cache[text]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def close(self) -> None:
        await self._inner.close()


def make_embedder(kind: str) -> Any:
    if kind == "lexical-hash":
        return LexicalHashEmbedder()
    if kind == "keyword-only":
        return ProviderDownEmbedder()
    if kind == "openai":
        from remembra.storage.embeddings import OpenAIEmbedder

        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("REMEMBRA_OPENAI_API_KEY")
        if not key:
            raise SystemExit("--embedder openai needs OPENAI_API_KEY in the environment")
        model = "text-embedding-3-small"
        return _CachedEmbedder(OpenAIEmbedder(api_key=key, model=model), f"openai/{model}", 1536)
    raise SystemExit(f"unknown embedder: {kind}")


def load_corpus(corpus: Path = CORPUS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def read(name: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in (corpus / name).read_text().splitlines() if line.strip()]

    memories, queries = read("memories.jsonl"), read("queries.jsonl")
    labels = {m["id"] for m in memories}
    assert len(labels) == len(memories), "duplicate memory id in corpus"
    assert len({q["id"] for q in queries}) == len(queries), "duplicate query id in corpus"
    for m in memories:
        assert m.get("supersedes") is None or m["supersedes"] in labels, m["id"]
    for q in queries:
        for key in ("relevant", "forbidden", "stale"):
            missing = set(q.get(key, [])) - labels
            assert not missing, f"{q['id']}: unknown {key} labels {sorted(missing)}"
    return memories, queries


@dataclass
class QueryResult:
    id: str
    category: str
    returned: list[str]
    relevant: list[str]
    forbidden: list[str]
    stale: list[str]
    latency_ms: float

    @property
    def answerable(self) -> bool:
        return bool(self.relevant)

    @property
    def recall_at_k(self) -> float:
        return len(set(self.returned) & set(self.relevant)) / len(self.relevant)

    @property
    def hit_at_1(self) -> float:
        return float(bool(self.returned) and self.returned[0] in self.relevant)

    @property
    def reciprocal_rank(self) -> float:
        for rank, label in enumerate(self.returned, 1):
            if label in self.relevant:
                return 1.0 / rank
        return 0.0

    @property
    def ndcg(self) -> float:
        dcg = sum(1.0 / math.log2(rank + 1) for rank, label in enumerate(self.returned, 1) if label in self.relevant)
        ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(self.relevant), LIMIT) + 1))
        return dcg / ideal

    @property
    def leaked(self) -> bool:
        return bool(set(self.returned) & set(self.forbidden))

    @property
    def stale_first(self) -> bool:
        """An out-of-date fact is ranked above its replacement, or the replacement is missing."""
        ranks = {label: i for i, label in enumerate(self.returned)}
        newest = min((ranks[r] for r in self.relevant if r in ranks), default=None)
        oldest = min((ranks[s] for s in self.stale if s in ranks), default=None)
        if newest is None:
            return True
        return oldest is not None and oldest < newest

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "returned": self.returned,
            "relevant": self.relevant,
            "forbidden": self.forbidden,
            "stale": self.stale,
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass
class Report:
    embedder: str
    results: list[QueryResult] = field(default_factory=list)

    def _mean(self, values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    def metrics(self) -> dict[str, float]:
        answerable = [r for r in self.results if r.answerable]
        unanswerable = [r for r in self.results if not r.answerable]
        guarded = [r for r in self.results if r.forbidden]
        updates = [r for r in self.results if r.stale]
        out = {
            "queries": float(len(self.results)),
            "recall_at_5": self._mean([r.recall_at_k for r in answerable]),
            "hit_at_1": self._mean([r.hit_at_1 for r in answerable]),
            "mrr": self._mean([r.reciprocal_rank for r in answerable]),
            "ndcg_at_5": self._mean([r.ndcg for r in answerable]),
            "forbidden_leak_rate": self._mean([float(r.leaked) for r in guarded]),
            "stale_first_rate": self._mean([float(r.stale_first) for r in updates]),
            "empty_when_unanswerable_rate": self._mean([float(not r.returned) for r in unanswerable]),
            "mean_results_when_unanswerable": self._mean([float(len(r.returned)) for r in unanswerable]),
        }
        by_category: dict[str, list[QueryResult]] = defaultdict(list)
        for r in answerable:
            by_category[r.category].append(r)
        for category, rows in sorted(by_category.items()):
            out[f"recall_at_5.{category}"] = self._mean([r.recall_at_k for r in rows])
            out[f"hit_at_1.{category}"] = self._mean([r.hit_at_1 for r in rows])
        return out

    def latency(self) -> dict[str, float]:
        times = sorted(r.latency_ms for r in self.results)
        if not times:
            return {}
        return {
            "recall_p50_ms": round(statistics.median(times), 2),
            "recall_p95_ms": round(times[min(len(times) - 1, int(len(times) * 0.95))], 2),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "embedder": self.embedder,
            "limit": LIMIT,
            "metrics": self.metrics(),
            "latency_in_process": self.latency(),
            "results": [r.to_json() for r in self.results],
        }


async def run_eval(
    embedder_kind: str = "lexical-hash",
    workdir: Path | None = None,
    corpus: Path = CORPUS,
    *,
    retrieval_mode: str | None = None,
    threshold: float | None = None,
) -> Report:
    """Store the corpus, run every query, return the labeled results.

    ``retrieval_mode`` and ``threshold`` override the request defaults, to
    compare ranking presets or similarity cutoffs on the same corpus.
    """
    from qdrant_client import AsyncQdrantClient

    from remembra.config import Settings
    from remembra.core.time import utcnow
    from remembra.models.memory import RecallRequest, StoreRequest
    from remembra.retrieval import graph as graph_module
    from remembra.services.memory import MemoryService
    from remembra.storage.database import Database
    from remembra.storage.qdrant import QdrantStore

    memories, queries = load_corpus(corpus)
    embedder = make_embedder(embedder_kind)
    expect_degraded = embedder_kind == "keyword-only"
    graph_module._query_cache.clear()
    settings = Settings(
        openai_api_key="unused",
        embedding_dimensions=embedder.dimensions,
        qdrant_collection="recall_eval",
        smart_extraction_enabled=False,
        enable_entity_resolution=False,
        enable_reranking=False,
        async_enrichment=False,
        typesafe_mode="off",
        conflict_detection_enabled=False,
    )

    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        db = Database(str(Path(tmp) / "eval.db"))
        await db.connect()
        await db.init_schema()
        qdrant = QdrantStore(settings)
        qdrant._client = AsyncQdrantClient(location=":memory:")
        await qdrant.init_collection()
        service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=embedder)
        try:
            now = utcnow()
            ids: dict[str, str] = {}
            for m in memories:  # corpus order is oldest first within a supersession chain
                if m.get("supersedes"):
                    # The supported way to replace a fact: the old row is kept and marked.
                    replaced = await service.supersede(ids[m["supersedes"]], USER, m["content"], reason="eval corpus update")
                    new_id = replaced.new_memory_id
                else:
                    stored = await service.store(
                        StoreRequest(content=m["content"], user_id=USER, project_id=m["project"]), skip_extraction=True
                    )
                    new_id = stored.id or ""
                    if (stored.status == "pending") != expect_degraded:
                        raise RuntimeError(f"{m['id']}: store status {stored.status!r} with embedder {embedder_kind!r}")
                assert new_id, m["id"]
                ids[m["id"]] = new_id
                stamp = (now - timedelta(days=m["days_ago"])).isoformat()
                await db.conn.execute("UPDATE memories SET created_at = ?, valid_from = ? WHERE id = ?", (stamp, stamp, new_id))
                if m.get("supersedes"):
                    # The replaced fact stopped being valid when its replacement was learned.
                    await db.conn.execute(
                        "UPDATE memories SET superseded_at = ?, valid_to = ? WHERE id = ?",
                        (stamp, stamp, ids[m["supersedes"]]),
                    )
                if m.get("expired_days_ago") is not None:
                    expired = (now - timedelta(days=m["expired_days_ago"])).isoformat()
                    await db.conn.execute("UPDATE memories SET expires_at = ? WHERE id = ?", (expired, new_id))
            await db.conn.commit()
            labels = {real: label for label, real in ids.items()}

            report = Report(embedder=embedder.name)
            for q in queries:
                as_of = now - timedelta(days=q["as_of_days_ago"]) if q.get("as_of_days_ago") is not None else None
                overrides: dict[str, Any] = {}
                if retrieval_mode:
                    overrides["retrieval_mode"] = retrieval_mode
                if threshold is not None:
                    overrides["threshold"] = threshold
                request = RecallRequest(
                    query=q["query"], user_id=USER, project_id=q["project"], limit=LIMIT, as_of=as_of, **overrides
                )
                started = time.perf_counter()
                response = await service.recall(request)
                elapsed = (time.perf_counter() - started) * 1000
                if (response.degraded is not None) != expect_degraded:
                    raise RuntimeError(f"{q['id']}: recall degraded={response.degraded!r} with embedder {embedder_kind!r}")
                report.results.append(
                    QueryResult(
                        id=q["id"],
                        category=q["category"],
                        returned=[labels.get(m.id, m.id) for m in response.memories],
                        relevant=list(q.get("relevant", [])),
                        forbidden=list(q.get("forbidden", [])),
                        stale=list(q.get("stale", [])),
                        latency_ms=elapsed,
                    )
                )
            return report
        finally:
            await qdrant.close()
            await db.close()
            await embedder.close()


def render(report: Report) -> str:
    metrics = report.metrics()
    lines = [f"embedder: {report.embedder}   queries: {int(metrics['queries'])}   top-k: {LIMIT}", ""]
    for key, value in metrics.items():
        if key != "queries":
            lines.append(f"  {key:<36} {value:.4f}")
    lines.append("")
    for key, value in report.latency().items():
        lines.append(f"  {key:<36} {value:.2f}  (in-process, not gated)")
    misses = [r for r in report.results if (r.answerable and r.recall_at_k < 1.0) or r.leaked or (r.stale and r.stale_first)]
    if misses:
        lines += ["", "misses:"]
        for r in misses:
            flags = [name for name, on in (("leak", r.leaked), ("stale-first", bool(r.stale) and r.stale_first)) if on]
            lines.append(f"  {r.id:<14} want {r.relevant} got {r.returned} {' '.join(flags)}".rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline recall-quality eval for Remembra")
    parser.add_argument("--embedder", default="lexical-hash", choices=["lexical-hash", "keyword-only", "openai"])
    parser.add_argument("--mode", help="Ranking preset to force: balanced, debug, operational or strategic")
    parser.add_argument("--threshold", type=float, help="Vector similarity cutoff to force (request default: 0.40)")
    parser.add_argument("--json", dest="json_path", help="Write metrics and per-query results to this file")
    parser.add_argument("--update-baseline", action="store_true", help="Run the keyless tiers and rewrite evals/baseline.json")
    args = parser.parse_args(argv)

    import structlog

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=open(os.devnull, "w")))  # noqa: SIM115
    if args.update_baseline:
        baseline = {tier: asyncio.run(run_eval(tier)).metrics() for tier in GATED_TIERS}
        BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"wrote {BASELINE}")
        return 0
    report = asyncio.run(run_eval(args.embedder, retrieval_mode=args.mode, threshold=args.threshold))
    print(render(report))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report.to_json(), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
