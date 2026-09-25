"""CrossEncoder reranking for improved retrieval accuracy.

This module provides optional reranking using sentence-transformers CrossEncoder
models. Reranking examines full query-document pairs for deeper understanding,
reducing hallucinations by ~35% according to Databricks studies.

The reranker is designed to be optional and gracefully degrades if
sentence-transformers is not installed.
"""

import asyncio
import math
import threading
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_LOAD_LOCK = threading.Lock()


def sigmoid(x: float) -> float:
    """Map a raw CrossEncoder logit to (0, 1) without looking at the other candidates."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


# Lazy import for optional dependency
_cross_encoder = None
_cross_encoder_loaded = False


def _load_cross_encoder(model_name: str) -> Any:
    """Lazy load CrossEncoder to avoid import errors if not installed."""
    global _cross_encoder, _cross_encoder_loaded

    if _cross_encoder_loaded:
        return _cross_encoder

    try:
        from sentence_transformers import CrossEncoder

        _cross_encoder = CrossEncoder(model_name)
        _cross_encoder_loaded = True
        log.info("cross_encoder_loaded", model=model_name)
        return _cross_encoder
    except ImportError:
        log.warning("sentence_transformers_not_installed", message="Install with: pip install sentence-transformers")
        _cross_encoder_loaded = True  # Mark as attempted
        return None
    except Exception as e:
        log.error("cross_encoder_load_failed", error=str(e))
        _cross_encoder_loaded = True
        return None


@dataclass
class RerankedResult:
    """A single reranked result with scores."""

    id: str
    content: str
    original_score: float
    rerank_score: float
    final_score: float
    payload: dict[str, Any] | None = None
    # Raw CrossEncoder logit (None in pass-through mode). rerank_score is its
    # sigmoid - an absolute value, not min-max scaled across the batch (RET-4).
    raw_score: float | None = None


class CrossEncoderReranker:
    """
    Reranks retrieval results using a CrossEncoder model.

    CrossEncoders examine the full query-document pair together,
    providing more accurate relevance scores than bi-encoders
    (which embed query and document separately).

    Recommended for top-k results from initial retrieval (k=20-50).
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        enabled: bool = True,
        blend_original: bool = True,
        original_weight: float = 0.3,
        min_logit: float | None = None,
    ) -> None:
        """
        Initialize the reranker.

        Args:
            model_name: HuggingFace model identifier for CrossEncoder
            enabled: Whether reranking is enabled
            blend_original: Whether to blend rerank scores with original scores
            original_weight: Weight for original score when blending (0-1)
            min_logit: Absolute cutoff on the raw logit; lower-scoring documents
                are dropped. None keeps everything.
        """
        self.model_name = model_name
        self.enabled = enabled
        self.blend_original = blend_original
        self.original_weight = original_weight
        self.min_logit = min_logit
        self._model: Any = None
        self._initialized = False
        self.last_error: str | None = None
        self.last_run: dict[str, Any] | None = None

    def _ensure_model(self) -> bool:
        """Ensure model is loaded. Returns True if available. Thread-safe."""
        if not self.enabled:
            return False

        if self._initialized:
            return self._model is not None

        with _LOAD_LOCK:
            if not self._initialized:
                self._model = _load_cross_encoder(self.model_name)
                if self._model is None:
                    self.last_error = "model unavailable (sentence-transformers missing or load failed)"
                self._initialized = True
        return self._model is not None

    def is_available(self) -> bool:
        """Check if reranking is available."""
        return self._ensure_model()

    def status(self) -> dict[str, Any]:
        """State for readiness: disabled | not_loaded | loaded | unavailable."""
        if not self.enabled:
            state = "disabled"
        elif not self._initialized:
            state = "not_loaded"
        else:
            state = "loaded" if self._model is not None else "unavailable"
        return {
            "state": state,
            "model": self.model_name,
            "min_logit": self.min_logit,
            "last_error": self.last_error,
            "last_run": self.last_run,
        }

    async def arerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int | None = None,
        content_key: str = "content",
        score_key: str = "relevance",
    ) -> list[RerankedResult]:
        """:meth:`rerank` in a worker thread: model load and ``predict`` are
        CPU-bound and would otherwise block the event loop (RET-4)."""
        return await asyncio.to_thread(self.rerank, query, documents, top_k, content_key, score_key)

    def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int | None = None,
        content_key: str = "content",
        score_key: str = "relevance",
    ) -> list[RerankedResult]:
        """
        Rerank documents using CrossEncoder.

        Args:
            query: Search query
            documents: List of document dicts with content and score
            top_k: Number of top results to return (None = all)
            content_key: Key for document content in dict
            score_key: Key for original relevance score in dict

        Returns:
            List of RerankedResult sorted by final_score descending
        """
        if not documents:
            return []

        if not self._ensure_model():
            # Gracefully degrade: return documents sorted by original score
            log.debug("reranker_unavailable_passthrough", count=len(documents))
            results: list[RerankedResult] = [
                RerankedResult(
                    id=str(doc.get("id", "")),
                    content=doc.get(content_key, ""),
                    original_score=doc.get(score_key, 0.0),
                    rerank_score=doc.get(score_key, 0.0),
                    final_score=doc.get(score_key, 0.0),
                    payload=doc,
                )
                for doc in documents
            ]
            # Sort by score and apply top_k
            results.sort(key=lambda r: r.final_score, reverse=True)
            if top_k:
                return results[:top_k]
            return results

        # Build query-document pairs
        pairs = [[query, doc.get(content_key, "")] for doc in documents]

        # Get CrossEncoder scores
        try:
            logits: list[float | None] = [float(x) for x in self._model.predict(pairs)]
            self.last_error = None
        except Exception as e:
            log.error("rerank_prediction_failed", error=str(e))
            self.last_error = f"predict failed: {type(e).__name__}"
            logits = [None] * len(documents)

        results = []
        dropped = 0

        for i, doc in enumerate(documents):
            original_score = float(doc.get(score_key, 0.0) or 0.0)
            raw = logits[i]
            if raw is None:
                # Prediction failed: pass the original score through.
                results.append(
                    RerankedResult(
                        id=str(doc.get("id", "")),
                        content=doc.get(content_key, ""),
                        original_score=original_score,
                        rerank_score=original_score,
                        final_score=original_score,
                        payload=doc,
                    )
                )
                continue
            if self.min_logit is not None and raw < self.min_logit:
                dropped += 1
                continue
            # Absolute probability, NOT min-max scaled: the best of a bad batch
            # must not become 1.0 (RET-4).
            prob = sigmoid(raw)
            if self.blend_original:
                final_score = self.original_weight * original_score + (1 - self.original_weight) * prob
            else:
                final_score = prob

            results.append(
                RerankedResult(
                    id=str(doc.get("id", "")),
                    content=doc.get(content_key, ""),
                    original_score=original_score,
                    rerank_score=prob,
                    final_score=final_score,
                    payload=doc,
                    raw_score=raw,
                )
            )
        self.last_run = {"input": len(documents), "dropped_below_min_logit": dropped}

        # Sort by final score
        results.sort(key=lambda r: r.final_score, reverse=True)

        log.debug(
            "rerank_complete",
            input_count=len(documents),
            output_count=len(results[:top_k] if top_k else results),
        )

        if top_k:
            return results[:top_k]
        return results

    def batch_rerank(
        self,
        queries: list[str],
        documents_per_query: list[list[dict[str, Any]]],
        top_k: int | None = None,
    ) -> list[list[RerankedResult]]:
        """
        Rerank multiple queries in batch for efficiency.

        Args:
            queries: List of search queries
            documents_per_query: List of document lists (one per query)
            top_k: Number of top results per query

        Returns:
            List of RerankedResult lists (one per query)
        """
        return [self.rerank(query, docs, top_k) for query, docs in zip(queries, documents_per_query, strict=False)]


# Singleton instance for common use
_default_reranker: CrossEncoderReranker | None = None


def get_reranker(
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    enabled: bool = True,
) -> CrossEncoderReranker:
    """
    Get or create a reranker instance.

    Uses a singleton pattern for the default model to avoid
    loading the model multiple times.
    """
    global _default_reranker

    if _default_reranker is None or _default_reranker.model_name != model_name:
        _default_reranker = CrossEncoderReranker(model_name=model_name, enabled=enabled)

    return _default_reranker
