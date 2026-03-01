"""Hybrid search combining semantic (vector) and keyword (BM25) matching.

This module implements a hybrid search strategy that:
1. Performs vector search via Qdrant for semantic similarity
2. Performs BM25 keyword matching for exact term matches
3. Fuses scores using configurable weights (RRF or linear combination)
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class HybridSearchConfig:
    """Configuration for hybrid search."""
    
    # Weight for semantic (vector) search scores (0-1)
    semantic_weight: float = 0.7
    # Weight for keyword (BM25) search scores (0-1)
    keyword_weight: float = 0.3
    # BM25 parameters
    bm25_k1: float = 1.5  # Term frequency saturation
    bm25_b: float = 0.75  # Document length normalization
    # Use Reciprocal Rank Fusion instead of linear combination
    use_rrf: bool = False
    # RRF constant (only used if use_rrf=True)
    rrf_k: int = 60


@dataclass
class SearchResult:
    """A single search result with combined scoring."""
    
    id: str
    content: str
    # Individual scores
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    # Combined/final score
    combined_score: float = 0.0
    # Optional payload
    payload: dict[str, Any] = field(default_factory=dict)
    # Debug info
    matched_terms: list[str] = field(default_factory=list)


class BM25Index:
    """
    Simple BM25 index for keyword matching.
    
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
        """
        Tokenize text into lowercase words.
        Removes punctuation and splits on whitespace.
        """
        # Convert to lowercase and extract word tokens
        text = text.lower()
        # Remove punctuation but keep alphanumeric and spaces
        tokens = re.findall(r'\b[a-z0-9]+\b', text)
        return tokens
    
    def add_document(self, doc_id: str, content: str) -> None:
        """Add a document to the index."""
        tokens = self.tokenize(content)
        self.documents[doc_id] = tokens
        self.doc_content[doc_id] = content
        
        # Update document frequencies
        unique_tokens = set(tokens)
        for token in unique_tokens:
            self.doc_freq[token] = self.doc_freq.get(token, 0) + 1
        
        # Recalculate average document length
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
        
        # Recalculate stats once at the end
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
        # Standard IDF formula with smoothing
        return math.log((self.n_docs - df + 0.5) / (df + 0.5) + 1.0)
    
    def _score_document(self, doc_id: str, query_tokens: list[str]) -> tuple[float, list[str]]:
        """
        Calculate BM25 score for a document against query tokens.
        Returns (score, matched_terms).
        """
        doc_tokens = self.documents.get(doc_id, [])
        doc_len = len(doc_tokens)
        
        if doc_len == 0 or self.avg_doc_len == 0:
            return 0.0, []
        
        score = 0.0
        matched_terms: list[str] = []
        
        # Count term frequencies in document
        term_freq: dict[str, int] = {}
        for token in doc_tokens:
            term_freq[token] = term_freq.get(token, 0) + 1
        
        for term in query_tokens:
            tf = term_freq.get(term, 0)
            if tf == 0:
                continue
            
            matched_terms.append(term)
            idf = self._idf(term)
            
            # BM25 term score
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
        
        # Sort by score descending
        results.sort(key=lambda x: x[1], reverse=True)
        
        return results[:limit]


class HybridSearcher:
    """
    Hybrid searcher combining semantic and keyword search.
    
    This class coordinates between vector search (from Qdrant) and
    BM25 keyword search, fusing their results using configurable strategies.
    """
    
    def __init__(self, config: HybridSearchConfig | None = None):
        self.config = config or HybridSearchConfig()
        self.bm25_index = BM25Index(
            k1=self.config.bm25_k1,
            b=self.config.bm25_b,
        )
    
    def index_documents(self, documents: list[tuple[str, str]]) -> None:
        """
        Index documents for keyword search.
        
        Args:
            documents: List of (id, content) tuples
        """
        self.bm25_index.clear()
        self.bm25_index.add_documents(documents)
        log.debug("bm25_indexed", count=len(documents))
    
    def keyword_search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Perform keyword-only search using BM25.
        
        Args:
            query: Search query string
            limit: Maximum results to return
            
        Returns:
            List of SearchResult objects with keyword_score populated
        """
        bm25_results = self.bm25_index.search(query, limit=limit)
        
        results = []
        for doc_id, score, matched_terms in bm25_results:
            results.append(SearchResult(
                id=doc_id,
                content=self.bm25_index.doc_content.get(doc_id, ""),
                keyword_score=score,
                combined_score=score,
                matched_terms=matched_terms,
            ))
        
        return results
    
    def fuse_results(
        self,
        semantic_results: list[tuple[str, float, dict[str, Any]]],
        keyword_results: list[tuple[str, float, list[str]]],
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Fuse semantic and keyword search results.
        
        Args:
            semantic_results: From Qdrant - list of (id, score, payload)
            keyword_results: From BM25 - list of (id, score, matched_terms)
            limit: Max results to return
            
        Returns:
            List of SearchResult with combined scores
        """
        # Build maps for quick lookup
        semantic_map: dict[str, tuple[float, dict[str, Any]]] = {
            str(r[0]): (r[1], r[2]) for r in semantic_results
        }
        keyword_map: dict[str, tuple[float, list[str]]] = {
            r[0]: (r[1], r[2]) for r in keyword_results
        }
        
        # Get all unique IDs
        all_ids = set(semantic_map.keys()) | set(keyword_map.keys())
        
        if not all_ids:
            return []
        
        # Normalize scores if using linear combination
        if not self.config.use_rrf:
            # Find max scores for normalization
            max_semantic = max((s[0] for s in semantic_map.values()), default=1.0)
            max_keyword = max((s[0] for s in keyword_map.values()), default=1.0)
            
            # Avoid division by zero
            max_semantic = max(max_semantic, 0.001)
            max_keyword = max(max_keyword, 0.001)
        
        results: list[SearchResult] = []
        
        for doc_id in all_ids:
            sem_data = semantic_map.get(doc_id)
            kw_data = keyword_map.get(doc_id)
            
            sem_score = sem_data[0] if sem_data else 0.0
            payload = sem_data[1] if sem_data else {}
            kw_score = kw_data[0] if kw_data else 0.0
            matched_terms = kw_data[1] if kw_data else []
            
            content = payload.get("content", "") or self.bm25_index.doc_content.get(doc_id, "")
            
            if self.config.use_rrf:
                # Reciprocal Rank Fusion
                combined = self._rrf_score(doc_id, semantic_results, keyword_results)
            else:
                # Linear combination with normalization
                norm_sem = sem_score / max_semantic if sem_score > 0 else 0.0
                norm_kw = kw_score / max_keyword if kw_score > 0 else 0.0
                combined = (
                    self.config.semantic_weight * norm_sem +
                    self.config.keyword_weight * norm_kw
                )
            
            results.append(SearchResult(
                id=doc_id,
                content=content,
                semantic_score=sem_score,
                keyword_score=kw_score,
                combined_score=combined,
                payload=payload,
                matched_terms=matched_terms,
            ))
        
        # Sort by combined score
        results.sort(key=lambda r: r.combined_score, reverse=True)
        
        return results[:limit]
    
    def _rrf_score(
        self,
        doc_id: str,
        semantic_results: list[tuple[str, float, dict[str, Any]]],
        keyword_results: list[tuple[str, float, list[str]]],
    ) -> float:
        """
        Calculate Reciprocal Rank Fusion score.
        
        RRF formula: score = Σ (1 / (k + rank_i)) for each ranking
        
        This is rank-based rather than score-based, making it more robust
        to score distribution differences between systems.
        """
        k = self.config.rrf_k
        rrf_score = 0.0
        
        # Find rank in semantic results
        for rank, (sid, _, _) in enumerate(semantic_results, start=1):
            if str(sid) == doc_id:
                rrf_score += self.config.semantic_weight / (k + rank)
                break
        
        # Find rank in keyword results
        for rank, (kid, _, _) in enumerate(keyword_results, start=1):
            if kid == doc_id:
                rrf_score += self.config.keyword_weight / (k + rank)
                break
        
        return rrf_score
    
    async def search(
        self,
        query: str,
        semantic_results: list[tuple[str, float, dict[str, Any]]],
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Perform hybrid search combining provided semantic results with keyword search.
        
        This method assumes documents have already been indexed via index_documents().
        
        Args:
            query: Search query
            semantic_results: Pre-computed semantic search results from Qdrant
            limit: Maximum results to return
            
        Returns:
            List of SearchResult with fused scores
        """
        # Perform keyword search
        kw_raw = self.bm25_index.search(query, limit=limit * 2)  # Get more for fusion
        
        log.debug(
            "hybrid_search",
            query_len=len(query),
            semantic_count=len(semantic_results),
            keyword_count=len(kw_raw),
        )
        
        # Fuse results
        return self.fuse_results(semantic_results, kw_raw, limit=limit)
