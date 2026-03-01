"""Advanced retrieval module for Remembra v0.4.0.

Provides:
- Hybrid search (semantic + keyword via BM25/FTS5)
- Graph-aware retrieval (entity relationships)
- Context window optimization (smart truncation with tiktoken)
- Relevance tuning (recency, entity match boosts)
- CrossEncoder reranking (optional, reduces hallucinations)
"""

from remembra.retrieval.hybrid import HybridSearcher, SearchResult, HybridSearchConfig, BM25Index
from remembra.retrieval.graph import GraphRetriever, GraphSearchResult
from remembra.retrieval.context import ContextOptimizer, OptimizedContext
from remembra.retrieval.ranking import RelevanceRanker, RankingConfig
from remembra.retrieval.reranker import CrossEncoderReranker, RerankedResult, get_reranker

__all__ = [
    # Hybrid search
    "HybridSearcher",
    "SearchResult", 
    "HybridSearchConfig",
    "BM25Index",
    # Graph retrieval
    "GraphRetriever",
    "GraphSearchResult",
    # Context optimization
    "ContextOptimizer",
    "OptimizedContext",
    # Ranking
    "RelevanceRanker",
    "RankingConfig",
    # Reranking
    "CrossEncoderReranker",
    "RerankedResult",
    "get_reranker",
]
