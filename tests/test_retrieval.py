"""Tests for advanced retrieval features (Week 6).

Tests cover:
1. Hybrid search (BM25 + vector fusion)
2. FTS5 full-text search
3. Graph-aware retrieval
4. CrossEncoder reranking
5. Context optimization with token budgeting
6. Relevance ranking
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

# Import retrieval modules
from remembra.retrieval.hybrid import (
    BM25Index,
    HybridSearcher,
    HybridSearchConfig,
)
from remembra.retrieval.graph import GraphRetriever
from remembra.retrieval.context import ContextOptimizer
from remembra.retrieval.ranking import RelevanceRanker, RankingConfig
from remembra.retrieval.reranker import CrossEncoderReranker


class TestBM25Index:
    """Tests for the BM25 keyword search index."""
    
    def test_tokenize(self):
        """Test text tokenization."""
        tokens = BM25Index.tokenize("Hello, World! This is a TEST.")
        assert tokens == ["hello", "world", "this", "is", "a", "test"]
    
    def test_add_document(self):
        """Test adding a single document."""
        index = BM25Index()
        index.add_document("doc1", "The quick brown fox")
        
        assert "doc1" in index.documents
        assert index.n_docs == 1
        assert index.avg_doc_len == 4  # 4 words
    
    def test_add_documents(self):
        """Test adding multiple documents."""
        index = BM25Index()
        docs = [
            ("doc1", "The quick brown fox"),
            ("doc2", "The lazy dog"),
            ("doc3", "A brown dog jumps"),
        ]
        index.add_documents(docs)
        
        assert index.n_docs == 3
        assert "brown" in index.doc_freq
        assert index.doc_freq["brown"] == 2  # In doc1 and doc3
    
    def test_search_basic(self):
        """Test basic BM25 search."""
        index = BM25Index()
        index.add_documents([
            ("doc1", "The quick brown fox jumps over the lazy dog"),
            ("doc2", "Python is a programming language"),
            ("doc3", "The brown bear was quick"),
        ])
        
        results = index.search("brown quick", limit=10)
        
        assert len(results) > 0
        # doc1 and doc3 should match
        doc_ids = [r[0] for r in results]
        assert "doc1" in doc_ids or "doc3" in doc_ids
    
    def test_search_no_match(self):
        """Test search with no matching terms."""
        index = BM25Index()
        index.add_document("doc1", "Hello world")
        
        results = index.search("nonexistent terms", limit=10)
        assert len(results) == 0
    
    def test_search_returns_matched_terms(self):
        """Test that search returns matched terms."""
        index = BM25Index()
        index.add_document("doc1", "Machine learning is powerful")
        
        results = index.search("machine learning", limit=10)
        
        assert len(results) == 1
        doc_id, score, matched = results[0]
        assert "machine" in matched
        assert "learning" in matched
    
    def test_clear(self):
        """Test clearing the index."""
        index = BM25Index()
        index.add_document("doc1", "Test content")
        assert index.n_docs == 1
        
        index.clear()
        
        assert index.n_docs == 0
        assert len(index.documents) == 0


class TestHybridSearcher:
    """Tests for hybrid semantic + keyword search."""
    
    def test_init_with_config(self):
        """Test initialization with custom config."""
        config = HybridSearchConfig(
            alpha=0.4,
            min_score=0.2,
        )
        searcher = HybridSearcher(config)
        
        assert searcher.config.alpha == 0.4
        assert searcher.config.min_score == 0.2
    
    def test_keyword_search(self):
        """Test keyword-only search using in-memory BM25."""
        searcher = HybridSearcher()
        searcher.index_documents([
            ("id1", "Python programming tutorial"),
            ("id2", "JavaScript web development"),
        ])
        
        # Returns list of (doc_id, score, matched_terms) tuples
        results = searcher.keyword_search("python", limit=5)
        
        assert len(results) == 1
        doc_id, score, matched_terms = results[0]
        assert doc_id == "id1"
        assert "python" in matched_terms
    
    def test_fuse_results(self):
        """Test fusion of semantic and keyword results."""
        searcher = HybridSearcher(HybridSearchConfig(
            alpha=0.4,
            min_score=0.0,
            include_partial=True,
        ))
        
        semantic_results = [
            ("id1", 0.9, {"content": "Python programming"}),
            ("id2", 0.7, {"content": "Machine learning"}),
        ]
        # FTS5-style results: (id, score)
        keyword_results = [
            ("id1", 5.0),
        ]
        
        fused = searcher.fuse_results(semantic_results, keyword_results, limit=10)
        
        assert len(fused) == 2
        # id1 should rank higher due to keyword match + high semantic
        assert fused[0].id == "id1"
        assert fused[0].semantic_score > 0
        assert fused[0].keyword_score > 0
    
    def test_fuse_normalizes_scores(self):
        """Test that fusion normalizes scores properly."""
        searcher = HybridSearcher(HybridSearchConfig(
            alpha=0.5,  # Equal weight
            min_score=0.0,
        ))
        
        semantic_results = [
            ("id1", 0.9, {"content": "Doc 1"}),
            ("id2", 0.3, {"content": "Doc 2"}),
        ]
        keyword_results = [
            ("id1", 10.0),  # High BM25 score
            ("id2", 2.0),   # Low BM25 score
        ]
        
        fused = searcher.fuse_results(semantic_results, keyword_results, limit=10)
        
        # Scores should be normalized 0-1
        for result in fused:
            assert 0 <= result.semantic_score <= 1
            assert 0 <= result.keyword_score <= 1
            assert 0 <= result.combined_score <= 1


class TestContextOptimizer:
    """Tests for context window optimization."""
    
    def test_estimate_tokens(self):
        """Test token estimation."""
        optimizer = ContextOptimizer()
        
        # Short text
        tokens = optimizer.estimate_tokens("Hello world")
        assert tokens > 0
        
        # Empty text
        assert optimizer.estimate_tokens("") == 0
    
    def test_truncate_to_tokens(self):
        """Test text truncation."""
        text = "This is a long sentence. " * 100  # ~500 words
        truncated, was_truncated = ContextOptimizer.truncate_to_tokens(text, 50)
        
        assert was_truncated
        assert len(truncated) < len(text)
        assert truncated.endswith("...")
    
    def test_optimize_basic(self):
        """Test basic context optimization."""
        optimizer = ContextOptimizer(max_tokens=1000)
        
        memories = [
            {"id": "1", "content": "First memory content", "relevance": 0.9},
            {"id": "2", "content": "Second memory content", "relevance": 0.7},
            {"id": "3", "content": "Third memory content", "relevance": 0.5},
        ]
        
        result = optimizer.optimize(memories)
        
        assert result.context != ""
        assert len(result.chunks) > 0
        assert result.total_tokens > 0
    
    def test_optimize_with_token_limit(self):
        """Test optimization respects token limits."""
        optimizer = ContextOptimizer(max_tokens=50)  # Very small limit
        
        memories = [
            {"id": "1", "content": "A" * 500, "relevance": 0.9},  # Very long
            {"id": "2", "content": "Short", "relevance": 0.7},
        ]
        
        result = optimizer.optimize(memories)
        
        assert result.total_tokens <= 60  # Allow some buffer
        # Should have truncated or dropped something
        assert result.truncated_count > 0 or result.dropped_count > 0
    
    def test_optimize_preserves_relevance_order(self):
        """Test that optimization keeps most relevant content."""
        optimizer = ContextOptimizer(max_tokens=100)
        
        memories = [
            {"id": "low", "content": "Low relevance", "relevance": 0.1},
            {"id": "high", "content": "High relevance", "relevance": 0.9},
            {"id": "mid", "content": "Mid relevance", "relevance": 0.5},
        ]
        
        result = optimizer.optimize(memories, sort_by_relevance=True)
        
        # First chunk should be highest relevance
        if result.chunks:
            assert result.chunks[0].id == "high"
    
    def test_count_tokens_accurate(self):
        """Test token counting accuracy indicator."""
        optimizer = ContextOptimizer()
        
        count, is_accurate = optimizer.count_tokens_accurate("Hello world")
        
        assert count > 0
        # is_accurate depends on whether tiktoken is installed
        assert isinstance(is_accurate, bool)


class TestRelevanceRanker:
    """Tests for relevance ranking with multiple signals."""
    
    def test_rank_basic(self):
        """Test basic ranking."""
        ranker = RelevanceRanker()
        
        memories = [
            {"id": "1", "content": "Memory one", "relevance": 0.8},
            {"id": "2", "content": "Memory two", "relevance": 0.9},
            {"id": "3", "content": "Memory three", "relevance": 0.6},
        ]
        
        ranked = ranker.rank(memories)
        
        assert len(ranked) == 3
        # Highest semantic score should be first
        assert ranked[0].id == "2"
    
    def test_recency_boost(self):
        """Test that recent memories get boosted."""
        config = RankingConfig(
            semantic_weight=0.5,
            recency_weight=0.5,
            recency_decay_days=7,
        )
        ranker = RelevanceRanker(config)
        
        now = datetime.utcnow()
        memories = [
            {
                "id": "old",
                "content": "Old memory",
                "relevance": 0.9,
                "created_at": (now - timedelta(days=30)).isoformat(),
            },
            {
                "id": "new",
                "content": "New memory",
                "relevance": 0.7,
                "created_at": now.isoformat(),
            },
        ]
        
        ranked = ranker.rank(memories)
        
        # New memory's recency score should be higher
        new_mem = next(r for r in ranked if r.id == "new")
        old_mem = next(r for r in ranked if r.id == "old")
        assert new_mem.recency_score > old_mem.recency_score
    
    def test_rerank_with_diversity(self):
        """Test diversity-aware reranking."""
        ranker = RelevanceRanker()
        
        # Create similar memories
        memories = [
            {"id": "1", "content": "Python programming tutorial basics", "relevance": 0.9},
            {"id": "2", "content": "Python programming tutorial advanced", "relevance": 0.85},
            {"id": "3", "content": "Machine learning fundamentals", "relevance": 0.8},
        ]
        
        ranked = ranker.rank(memories)
        diverse = ranker.rerank_with_diversity(ranked, diversity_threshold=0.5)
        
        assert len(diverse) <= len(ranked)


class TestCrossEncoderReranker:
    """Tests for CrossEncoder reranking."""
    
    def test_graceful_degradation(self):
        """Test that reranker gracefully degrades when model unavailable."""
        # Don't load the actual model
        reranker = CrossEncoderReranker(enabled=False)
        
        documents = [
            {"id": "1", "content": "Test content", "relevance": 0.8},
            {"id": "2", "content": "More content", "relevance": 0.6},
        ]
        
        results = reranker.rerank("test query", documents)
        
        # Should return results even without model
        assert len(results) == 2
        # Scores should pass through
        assert results[0].original_score == 0.8
    
    def test_rerank_ordering(self):
        """Test that reranking preserves/returns proper order."""
        reranker = CrossEncoderReranker(enabled=False)
        
        documents = [
            {"id": "low", "content": "Irrelevant", "relevance": 0.3},
            {"id": "high", "content": "Very relevant", "relevance": 0.9},
            {"id": "mid", "content": "Somewhat relevant", "relevance": 0.6},
        ]
        
        results = reranker.rerank("query", documents, top_k=2)
        
        assert len(results) == 2
        # Should be sorted by relevance (pass-through mode)
        assert results[0].id == "high"
    
    def test_is_available(self):
        """Test availability check."""
        reranker = CrossEncoderReranker(enabled=False)
        # When disabled, should not be available
        assert reranker.is_available() is False


class TestGraphRetriever:
    """Tests for graph-aware entity retrieval."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database."""
        db = MagicMock()
        db.get_user_entities = AsyncMock(return_value=[])
        db.get_entity_relationships = AsyncMock(return_value=[])
        db.get_entity = AsyncMock(return_value=None)
        db.get_memories_by_entity = AsyncMock(return_value=[])
        return db
    
    @pytest.mark.asyncio
    async def test_search_no_entities(self, mock_db):
        """Test search when no entities match."""
        retriever = GraphRetriever(mock_db)
        
        result = await retriever.search(
            query="random query",
            user_id="user1",
            project_id="default",
        )
        
        assert len(result.memory_ids) == 0
        assert len(result.matched_entities) == 0
    
    @pytest.mark.asyncio
    async def test_find_entity_mentions(self, mock_db):
        """Test finding entity mentions in query."""
        from remembra.models.memory import Entity
        
        # Mock entities
        entities = [
            Entity(
                id="e1",
                canonical_name="David Kim",
                type="person",
                aliases=["Mr. Kim", "Dave"],
            ),
            Entity(
                id="e2",
                canonical_name="Acme Corp",
                type="company",
                aliases=["Acme", "ACME Corporation"],
            ),
        ]
        mock_db.get_user_entities = AsyncMock(return_value=entities)
        
        retriever = GraphRetriever(mock_db)
        
        # Test canonical name match
        matched = await retriever.find_entity_mentions(
            query="Tell me about David Kim",
            user_id="user1",
        )
        assert len(matched) == 1
        assert matched[0].canonical_name == "David Kim"
        
        # Test alias match
        matched = await retriever.find_entity_mentions(
            query="What does Mr. Kim do at Acme?",
            user_id="user1",
        )
        assert len(matched) == 2


class TestIntegration:
    """Integration tests for the full retrieval pipeline."""
    
    @pytest.mark.asyncio
    async def test_hybrid_graph_fusion(self):
        """Test combining hybrid search with graph retrieval."""
        # 1. Create hybrid searcher
        searcher = HybridSearcher(HybridSearchConfig(
            alpha=0.4,
            min_score=0.0,
        ))
        
        # 2. Index documents
        docs = [
            ("mem1", "David Kim is the CEO of Acme Corp"),
            ("mem2", "The quarterly earnings report was positive"),
            ("mem3", "Mr. Kim announced the merger yesterday"),
        ]
        searcher.index_documents(docs)
        
        # 3. Simulate semantic results
        semantic_results = [
            ("mem1", 0.8, {"content": docs[0][1]}),
            ("mem3", 0.75, {"content": docs[2][1]}),
        ]
        
        # 4. Get keyword results
        keyword_results_raw = searcher.keyword_search("David Kim CEO", limit=5)
        keyword_results = [(r[0], r[1]) for r in keyword_results_raw]  # (id, score)
        
        # 5. Fuse
        fused = searcher.fuse_results(semantic_results, keyword_results, limit=5)
        
        assert len(fused) > 0
        
        # 6. Apply ranking
        ranker = RelevanceRanker()
        memories = [
            {
                "id": r.id,
                "content": r.content,
                "relevance": r.combined_score,
                "keyword_score": r.keyword_score,
            }
            for r in fused
        ]
        ranked = ranker.rank(memories, query="David Kim CEO")
        
        # 7. Optimize context
        optimizer = ContextOptimizer(max_tokens=500)
        context_mems = [
            {"id": r.id, "content": r.content, "relevance": r.final_score}
            for r in ranked
        ]
        optimized = optimizer.optimize(context_mems)
        
        assert optimized.context != ""
        assert optimized.total_tokens > 0
    
    def test_full_pipeline_components(self):
        """Test that all pipeline components initialize correctly."""
        # All these should construct without errors
        searcher = HybridSearcher()
        ranker = RelevanceRanker()
        optimizer = ContextOptimizer()
        reranker = CrossEncoderReranker(enabled=False)
        
        assert searcher is not None
        assert ranker is not None
        assert optimizer is not None
        assert reranker is not None
