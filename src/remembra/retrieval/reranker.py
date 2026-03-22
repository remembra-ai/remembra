"""CrossEncoder reranking for improved retrieval accuracy.

This module provides optional reranking using sentence-transformers CrossEncoder
models. Reranking examines full query-document pairs for deeper understanding,
reducing hallucinations by ~35% according to Databricks studies.

The reranker is designed to be optional and gracefully degrades if
sentence-transformers is not installed.
"""

from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

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
        log.warning(
            "sentence_transformers_not_installed",
            message="Install with: pip install sentence-transformers"
        )
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
    ) -> None:
        """
        Initialize the reranker.
        
        Args:
            model_name: HuggingFace model identifier for CrossEncoder
            enabled: Whether reranking is enabled
            blend_original: Whether to blend rerank scores with original scores
            original_weight: Weight for original score when blending (0-1)
        """
        self.model_name = model_name
        self.enabled = enabled
        self.blend_original = blend_original
        self.original_weight = original_weight
        self._model: Any = None
        self._initialized = False
    
    def _ensure_model(self) -> bool:
        """Ensure model is loaded. Returns True if available."""
        if not self.enabled:
            return False
        
        if self._initialized:
            return self._model is not None
        
        self._model = _load_cross_encoder(self.model_name)
        self._initialized = True
        return self._model is not None
    
    def is_available(self) -> bool:
        """Check if reranking is available."""
        return self._ensure_model()
    
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
            results = [
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
        pairs = [
            [query, doc.get(content_key, "")]
            for doc in documents
        ]
        
        # Get CrossEncoder scores
        try:
            rerank_scores = self._model.predict(pairs)
        except Exception as e:
            log.error("rerank_prediction_failed", error=str(e))
            # Fall back to original scores
            rerank_scores = [doc.get(score_key, 0.0) for doc in documents]
        
        # Normalize rerank scores to 0-1 range
        min_score = min(rerank_scores) if rerank_scores else 0
        max_score = max(rerank_scores) if rerank_scores else 1
        score_range = max_score - min_score if max_score != min_score else 1.0
        
        results: list[RerankedResult] = []
        
        for i, doc in enumerate(documents):
            original_score = doc.get(score_key, 0.0)
            raw_rerank = float(rerank_scores[i])
            normalized_rerank = (raw_rerank - min_score) / score_range
            
            if self.blend_original:
                final_score = (
                    self.original_weight * original_score +
                    (1 - self.original_weight) * normalized_rerank
                )
            else:
                final_score = normalized_rerank
            
            results.append(RerankedResult(
                id=str(doc.get("id", "")),
                content=doc.get(content_key, ""),
                original_score=original_score,
                rerank_score=normalized_rerank,
                final_score=final_score,
                payload=doc,
            ))
        
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
        return [
            self.rerank(query, docs, top_k)
            for query, docs in zip(queries, documents_per_query, strict=False)
        ]


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
