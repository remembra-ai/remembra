"""Hybrid search combining semantic (vector) and keyword (FTS5/BM25) matching.

This module implements industry-standard hybrid search that:
1. Performs vector search via Qdrant for semantic similarity
2. Performs BM25 keyword matching via SQLite FTS5 (primary)
3. Falls back to in-memory BM25Index when FTS5 unavailable
4. Normalizes and fuses scores with configurable alpha weight

Based on research from Stack Overflow, Elastic, Zep, and InfinityFlow.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class HybridSearchConfig:
    """Configuration for hybrid search.
    
    Research-backed defaults:
    - alpha=0.4 gives good balance (keyword 40%, semantic 60%)
    - Higher alpha = more weight to keyword/BM25 matches
    """
    
    # Alpha: weight for keyword (BM25) scores
    # Final = alpha * keyword + (1-alpha) * semantic
    alpha: float = 0.4
    
    # Minimum score threshold for results
    min_score: float = 0.1
    
    # Whether to include results that only appear in one search
    include_partial: bool = True


@dataclass
class SearchResult:
    """A single search result with hybrid scoring."""
    
    id: str
    content: str
    # Individual scores (normalized 0-1)
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    # Combined/final score
    combined_score: float = 0.0
    # Source: 'both', 'semantic', 'keyword'
    source: str = "both"
    # Optional payload from vector store
    payload: dict[str, Any] = field(default_factory=dict)


class BM25Index:
    """
    In-memory BM25 index for keyword matching.
    
    Used as fallback when SQLite FTS5 is unavailable or for testing.
    BM25 (Best Matching 25) is a ranking function used for keyword search.
    It considers term frequency, document length, and inverse document frequency.
    """
    
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        # Document storage: id -> tokenized content
        self.documents: dict[str, list[str]] = {}
        # Document metadata: id -> original content
        self.doc_content: dict[str, str] = {}
        # Document frequencies: term -> count of docs containing term
        self.doc_freq: dict[str, int] = {}
        # Average document length
        self.avg_doc_len: float = 0.0
        # Total documents
        self.n_docs: int = 0
    
    @staticmethod
    def tokenize(text: str) -> list[str]:
        """Tokenize text into lowercase words."""
        text = text.lower()
        tokens = re.findall(r'\b[a-z0-9]+\b', text)
        return tokens
    
    def add_document(self, doc_id: str, content: str) -> None:
        """Add a document to the index."""
        tokens = self.tokenize(content)
        self.documents[doc_id] = tokens
        self.doc_content[doc_id] = content
        
        unique_tokens = set(tokens)
        for token in unique_tokens:
            self.doc_freq[token] = self.doc_freq.get(token, 0) + 1
        
        self.n_docs = len(self.documents)
        total_len = sum(len(toks) for toks in self.documents.values())
        self.avg_doc_len = total_len / self.n_docs if self.n_docs > 0 else 0.0
    
    def add_documents(self, documents: list[tuple[str, str]]) -> None:
        """Add multiple documents at once. List of (id, content) tuples."""
        for doc_id, content in documents:
            tokens = self.tokenize(content)
            self.documents[doc_id] = tokens
            self.doc_content[doc_id] = content
            
            unique_tokens = set(tokens)
            for token in unique_tokens:
                self.doc_freq[token] = self.doc_freq.get(token, 0) + 1
        
        self.n_docs = len(self.documents)
        total_len = sum(len(toks) for toks in self.documents.values())
        self.avg_doc_len = total_len / self.n_docs if self.n_docs > 0 else 0.0
    
    def clear(self) -> None:
        """Clear all documents from the index."""
        self.documents.clear()
        self.doc_content.clear()
        self.doc_freq.clear()
        self.avg_doc_len = 0.0
        self.n_docs = 0
    
    def _idf(self, term: str) -> float:
        """Calculate inverse document frequency for a term."""
        df = self.doc_freq.get(term, 0)
        if df == 0:
            return 0.0
        return math.log((self.n_docs - df + 0.5) / (df + 0.5) + 1.0)
    
    def _score_document(self, doc_id: str, query_tokens: list[str]) -> tuple[float, list[str]]:
        """Calculate BM25 score for a document against query tokens."""
        doc_tokens = self.documents.get(doc_id, [])
        doc_len = len(doc_tokens)
        
        if doc_len == 0 or self.avg_doc_len == 0:
            return 0.0, []
        
        score = 0.0
        matched_terms: list[str] = []
        
        term_freq: dict[str, int] = {}
        for token in doc_tokens:
            term_freq[token] = term_freq.get(token, 0) + 1
        
        for term in query_tokens:
            tf = term_freq.get(term, 0)
            if tf == 0:
                continue
            
            matched_terms.append(term)
            idf = self._idf(term)
            
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_doc_len))
            term_score = idf * (numerator / denominator)
            score += term_score
        
        return score, matched_terms
    
    def search(
        self, 
        query: str, 
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[tuple[str, float, list[str]]]:
        """
        Search for documents matching the query.
        
        Returns:
            List of (doc_id, score, matched_terms) tuples, sorted by score desc.
        """
        query_tokens = self.tokenize(query)
        
        if not query_tokens:
            return []
        
        results: list[tuple[str, float, list[str]]] = []
        
        for doc_id in self.documents:
            score, matched = self._score_document(doc_id, query_tokens)
            if score > min_score:
                results.append((doc_id, score, matched))
        
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


def min_max_normalize(scores: list[float]) -> list[float]:
    """
    Normalize scores to 0-1 range using min-max scaling.
    
    This is the standard approach recommended by Elastic and other
    hybrid search implementations.
    """
    if not scores:
        return []
    
    min_score = min(scores)
    max_score = max(scores)
    score_range = max_score - min_score
    
    if score_range == 0:
        # All scores are the same
        return [0.5] * len(scores)
    
    return [(s - min_score) / score_range for s in scores]


class HybridSearcher:
    """
    Hybrid searcher combining semantic (vector) and keyword (FTS5/BM25) search.
    
    Primary: SQLite FTS5 for persistent BM25 keyword matching
    Fallback: In-memory BM25Index when FTS5 unavailable
    
    Score fusion formula: final = alpha * keyword + (1-alpha) * semantic
    """
    
    def __init__(self, config: HybridSearchConfig | None = None):
        self.config = config or HybridSearchConfig()
        # In-memory BM25 for fallback/testing
        self._bm25_index = BM25Index()
    
    def index_documents(self, documents: list[tuple[str, str]]) -> None:
        """
        Index documents in the in-memory BM25 index.
        
        Use this when FTS5 is unavailable or for testing.
        
        Args:
            documents: List of (id, content) tuples
        """
        self._bm25_index.clear()
        self._bm25_index.add_documents(documents)
        log.debug("bm25_indexed_inmemory", count=len(documents))
    
    def keyword_search(self, query: str, limit: int = 10) -> list[tuple[str, float, list[str]]]:
        """
        Perform keyword-only search using in-memory BM25.
        
        Returns:
            List of (doc_id, score, matched_terms) tuples
        """
        return self._bm25_index.search(query, limit=limit)
    
    def fuse_results(
        self,
        semantic_results: list[tuple[str, float, dict[str, Any]]],
        keyword_results: list[tuple[str, float]],
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Fuse semantic and keyword search results.
        
        Args:
            semantic_results: From Qdrant - list of (id, score, payload)
            keyword_results: From FTS5 - list of (id, bm25_score)
            limit: Max results to return
            
        Returns:
            List of SearchResult with combined scores
        """
        # Build maps for quick lookup
        semantic_map: dict[str, tuple[float, dict[str, Any]]] = {
            str(r[0]): (r[1], r[2]) for r in semantic_results
        }
        keyword_map: dict[str, float] = {
            r[0]: r[1] for r in keyword_results
        }
        
        # Get all unique IDs
        all_ids = set(semantic_map.keys())
        if self.config.include_partial:
            all_ids |= set(keyword_map.keys())
        else:
            # Only include IDs that appear in both
            all_ids &= set(keyword_map.keys())
        
        if not all_ids:
            return []
        
        # Extract scores for normalization
        semantic_scores = [semantic_map.get(id, (0.0, {}))[0] for id in all_ids]
        keyword_scores = [keyword_map.get(id, 0.0) for id in all_ids]
        
        # Normalize to 0-1 range
        norm_semantic = min_max_normalize(semantic_scores)
        norm_keyword = min_max_normalize(keyword_scores)
        
        results: list[SearchResult] = []
        
        for i, doc_id in enumerate(all_ids):
            sem_data = semantic_map.get(doc_id)
            kw_score_raw = keyword_map.get(doc_id, 0.0)
            
            sem_score = norm_semantic[i] if sem_data else 0.0
            kw_score = norm_keyword[i] if kw_score_raw > 0 else 0.0
            
            payload = sem_data[1] if sem_data else {}
            content = payload.get("content", "")
            
            # Determine source
            if doc_id in semantic_map and doc_id in keyword_map:
                source = "both"
            elif doc_id in semantic_map:
                source = "semantic"
            else:
                source = "keyword"
            
            # Hybrid fusion: alpha * keyword + (1-alpha) * semantic
            combined = (
                self.config.alpha * kw_score +
                (1 - self.config.alpha) * sem_score
            )
            
            # Apply minimum threshold
            if combined < self.config.min_score and source != "both":
                continue
            
            results.append(SearchResult(
                id=doc_id,
                content=content,
                semantic_score=sem_score,
                keyword_score=kw_score,
                combined_score=combined,
                source=source,
                payload=payload,
            ))
        
        # Sort by combined score
        results.sort(key=lambda r: r.combined_score, reverse=True)
        
        log.debug(
            "hybrid_fusion_complete",
            semantic_count=len(semantic_results),
            keyword_count=len(keyword_results),
            fused_count=len(results[:limit]),
        )
        
        return results[:limit]
    
    async def search(
        self,
        semantic_results: list[tuple[str, float, dict[str, Any]]],
        keyword_results: list[tuple[str, float]],
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Perform hybrid search by fusing semantic and keyword results.
        
        This is the main entry point. Call Qdrant and FTS5 externally,
        then pass results here for fusion.
        
        Args:
            semantic_results: From Qdrant.search() - (id, score, payload)
            keyword_results: From Database.search_fts() - (id, bm25_score)
            limit: Maximum results to return
            
        Returns:
            List of SearchResult with fused scores
        """
        return self.fuse_results(semantic_results, keyword_results, limit)


# Convenience function for simple cases
def fuse_scores(
    semantic_score: float,
    keyword_score: float,
    alpha: float = 0.4,
) -> float:
    """
    Fuse a single pair of scores.
    
    Args:
        semantic_score: Normalized semantic similarity (0-1)
        keyword_score: Normalized BM25 score (0-1)
        alpha: Keyword weight (default: 0.4)
        
    Returns:
        Combined score (0-1)
    """
    return alpha * keyword_score + (1 - alpha) * semantic_score
