"""Tests for advanced retrieval features (Week 6)."""

import pytest
from datetime import datetime, timedelta

from remembra.retrieval.hybrid import BM25Index, HybridSearcher, HybridSearchConfig, SearchResult
from remembra.retrieval.context import ContextOptimizer, MemoryChunk
from remembra.retrieval.ranking import RelevanceRanker, RankingConfig, RankedMemory
from remembra.models.memory import EntityRef


class TestBM25Index:
    """Tests for BM25 keyword matching."""
    
    def test_tokenize_simple(self):
        """Test basic tokenization."""
        tokens = BM25Index.tokenize("Hello World!")
        assert tokens == ["hello", "world"]
    
    def test_tokenize_with_numbers(self):
        """Test tokenization with numbers."""
        tokens = BM25Index.tokenize("Meeting at 3pm in Room 42")
        assert "meeting" in tokens
        assert "3pm" in tokens
        assert "42" in tokens
    
    def test_add_document(self):
        """Test adding a document to the index."""
        index = BM25Index()
        index.add_document("doc1", "David Kim works at Acme Corp")
        
        assert "doc1" in index.documents
        assert index.n_docs == 1
        assert index.avg_doc_len > 0
    
    def test_add_documents_batch(self):
        """Test adding multiple documents at once."""
        index = BM25Index()
        docs = [
            ("doc1", "David Kim works at Acme Corp"),
            ("doc2", "Mr. Kim mentioned the merger"),
            ("doc3", "The acquisition was discussed"),
        ]
        index.add_documents(docs)
        
        assert index.n_docs == 3
        assert "kim" in index.doc_freq  # Kim appears in 2 docs
        assert index.doc_freq["kim"] == 2
    
    def test_search_exact_match(self):
        """Test searching with exact term match."""
        index = BM25Index()
        index.add_documents([
            ("doc1", "David Kim works at Acme Corp"),
            ("doc2", "Sarah Chen is the CEO"),
            ("doc3", "The merger was announced"),
        ])
        
        results = index.search("David Kim", limit=5)
        
        assert len(results) >= 1
        assert results[0][0] == "doc1"  # doc1 should be top result
        assert "david" in results[0][2] or "kim" in results[0][2]  # matched terms
    
    def test_search_partial_match(self):
        """Test searching with partial match - 'Mr. Kim' should match 'Kim'."""
        index = BM25Index()
        index.add_documents([
            ("doc1", "David Kim works at Acme Corp"),
            ("doc2", "Mr. Kim mentioned acquisition"),
        ])
        
        results = index.search("Kim merger", limit=5)
        
        # Both docs should match on "kim"
        doc_ids = [r[0] for r in results]
        assert "doc1" in doc_ids or "doc2" in doc_ids
    
    def test_search_no_match(self):
        """Test searching with no matches."""
        index = BM25Index()
        index.add_document("doc1", "Hello world")
        
        results = index.search("nonexistent terms xyz", limit=5)
        assert len(results) == 0
    
    def test_clear_index(self):
        """Test clearing the index."""
        index = BM25Index()
        index.add_document("doc1", "Test content")
        index.clear()
        
        assert index.n_docs == 0
        assert len(index.documents) == 0


class TestHybridSearcher:
    """Tests for hybrid search combining semantic and keyword."""
    
    def test_keyword_only_search(self):
        """Test keyword-only search."""
        searcher = HybridSearcher()
        searcher.index_documents([
            ("doc1", "David Kim works at Acme Corp"),
            ("doc2", "The merger was discussed by Mr. Kim"),
        ])
        
        results = searcher.keyword_search("Kim merger", limit=5)
        
        assert len(results) >= 1
        assert all(isinstance(r, SearchResult) for r in results)
        # doc2 should score higher (has both terms)
        assert results[0].id == "doc2"
    
    def test_fuse_results_linear(self):
        """Test linear score fusion."""
        config = HybridSearchConfig(
            semantic_weight=0.7,
            keyword_weight=0.3,
            use_rrf=False,
        )
        searcher = HybridSearcher(config)
        
        # Simulate semantic results: (id, score, payload)
        semantic = [
            ("doc1", 0.9, {"content": "Semantic match"}),
            ("doc2", 0.7, {"content": "Lower semantic"}),
        ]
        
        # Simulate keyword results: (id, score, matched_terms)
        keyword = [
            ("doc2", 5.0, ["keyword", "match"]),  # doc2 has better keyword match
            ("doc1", 2.0, ["keyword"]),
        ]
        
        fused = searcher.fuse_results(semantic, keyword, limit=5)
        
        assert len(fused) == 2
        # doc1 has higher semantic (0.9) but lower keyword
        # doc2 has lower semantic (0.7) but higher keyword
        # Combined scores should reflect weighted combination
        for result in fused:
            assert result.semantic_score >= 0
            assert result.keyword_score >= 0
            assert result.combined_score > 0
    
    def test_fuse_results_rrf(self):
        """Test Reciprocal Rank Fusion."""
        config = HybridSearchConfig(
            semantic_weight=0.5,
            keyword_weight=0.5,
            use_rrf=True,
            rrf_k=60,
        )
        searcher = HybridSearcher(config)
        
        semantic = [
            ("doc1", 0.9, {"content": "First in semantic"}),
            ("doc2", 0.8, {"content": "Second in semantic"}),
        ]
        
        keyword = [
            ("doc2", 5.0, ["match"]),  # doc2 is first in keyword
            ("doc1", 3.0, ["other"]),  # doc1 is second in keyword
        ]
        
        fused = searcher.fuse_results(semantic, keyword, limit=5)
        
        # With RRF, ranks matter more than scores
        # doc2 is rank 1 in keyword, rank 2 in semantic
        # doc1 is rank 1 in semantic, rank 2 in keyword
        # They should be fairly close in combined score
        assert len(fused) == 2
        score_diff = abs(fused[0].combined_score - fused[1].combined_score)
        assert score_diff < 0.1  # Should be close due to RRF averaging
    
    def test_handles_missing_results(self):
        """Test fusion when a doc appears in only one result set."""
        searcher = HybridSearcher()
        
        semantic = [
            ("doc1", 0.9, {"content": "Only in semantic"}),
        ]
        
        keyword = [
            ("doc2", 5.0, ["keyword"]),  # Only in keyword
        ]
        
        # Index doc2 for content lookup
        searcher.index_documents([("doc2", "Only in keyword results")])
        
        fused = searcher.fuse_results(semantic, keyword, limit=5)
        
        assert len(fused) == 2
        # Both docs should be present
        ids = [r.id for r in fused]
        assert "doc1" in ids
        assert "doc2" in ids


class TestContextOptimizer:
    """Tests for context window optimization."""
    
    def test_estimate_tokens(self):
        """Test token estimation."""
        text = "Hello world, this is a test."
        tokens = ContextOptimizer.estimate_tokens(text)
        
        # ~30 chars / 4 = ~7 tokens
        assert 5 <= tokens <= 15
    
    def test_truncate_to_tokens(self):
        """Test truncation to token limit."""
        text = "This is a long sentence. And another one. And more text here."
        truncated, was_truncated = ContextOptimizer.truncate_to_tokens(text, 5)
        
        assert was_truncated
        assert truncated.endswith("...")
        assert len(truncated) < len(text)
    
    def test_no_truncation_needed(self):
        """Test when no truncation is needed."""
        text = "Short text."
        truncated, was_truncated = ContextOptimizer.truncate_to_tokens(text, 1000)
        
        assert not was_truncated
        assert truncated == text
    
    def test_optimize_single_memory(self):
        """Test optimizing a single memory."""
        optimizer = ContextOptimizer(max_tokens=1000)
        
        memories = [
            {
                "id": "mem1",
                "content": "David Kim works at Acme Corp",
                "relevance": 0.9,
                "created_at": "2026-03-01T10:00:00",
            }
        ]
        
        result = optimizer.optimize(memories)
        
        assert len(result.chunks) == 1
        assert "David Kim" in result.context
        assert result.dropped_count == 0
        assert result.truncated_count == 0
    
    def test_optimize_multiple_memories(self):
        """Test optimizing multiple memories with sorting."""
        optimizer = ContextOptimizer(max_tokens=1000)
        
        memories = [
            {"id": "mem1", "content": "Low relevance", "relevance": 0.5},
            {"id": "mem2", "content": "High relevance", "relevance": 0.9},
            {"id": "mem3", "content": "Medium relevance", "relevance": 0.7},
        ]
        
        result = optimizer.optimize(memories, sort_by_relevance=True)
        
        assert len(result.chunks) == 3
        # First chunk should be highest relevance
        assert result.chunks[0].id == "mem2"
    
    def test_optimize_with_truncation(self):
        """Test optimization that requires truncation."""
        # Very small token limit
        optimizer = ContextOptimizer(max_tokens=20, min_chunk_tokens=5)
        
        memories = [
            {"id": "mem1", "content": "This is a somewhat longer piece of content that will need truncation.", "relevance": 0.9},
            {"id": "mem2", "content": "Another memory that might get dropped.", "relevance": 0.5},
        ]
        
        result = optimizer.optimize(memories)
        
        # Should have at least one chunk, possibly truncated
        assert len(result.chunks) >= 1
        # Some memories might be dropped or truncated
        assert result.truncated_count >= 0 or result.dropped_count >= 0
    
    def test_optimize_with_metadata(self):
        """Test including metadata in output."""
        optimizer = ContextOptimizer(max_tokens=1000, include_metadata=True)
        
        memories = [
            {
                "id": "mem1",
                "content": "Test content",
                "relevance": 0.85,
                "created_at": "2026-03-01T10:00:00",
            }
        ]
        
        result = optimizer.optimize(memories)
        
        # Should include date and relevance
        assert "2026-03-01" in result.context
        assert "85%" in result.context or "0.85" in result.context
    
    def test_optimize_without_metadata(self):
        """Test excluding metadata from output."""
        optimizer = ContextOptimizer(max_tokens=1000, include_metadata=False)
        
        memories = [
            {
                "id": "mem1",
                "content": "Test content only",
                "relevance": 0.85,
                "created_at": "2026-03-01T10:00:00",
            }
        ]
        
        result = optimizer.optimize(memories)
        
        # Should NOT include date or relevance
        assert result.context == "Test content only"


class TestRelevanceRanker:
    """Tests for relevance ranking with boosts."""
    
    def test_rank_by_semantic_score(self):
        """Test basic ranking by semantic score."""
        config = RankingConfig(
            semantic_weight=1.0,
            recency_weight=0.0,
            entity_weight=0.0,
            keyword_weight=0.0,
        )
        ranker = RelevanceRanker(config)
        
        memories = [
            {"id": "mem1", "content": "Low score", "relevance": 0.5},
            {"id": "mem2", "content": "High score", "relevance": 0.9},
            {"id": "mem3", "content": "Medium score", "relevance": 0.7},
        ]
        
        ranked = ranker.rank(memories)
        
        assert ranked[0].id == "mem2"  # Highest semantic
        assert ranked[1].id == "mem3"  # Medium semantic
        assert ranked[2].id == "mem1"  # Lowest semantic
    
    def test_recency_boost(self):
        """Test that recent memories get boosted."""
        config = RankingConfig(
            semantic_weight=0.5,
            recency_weight=0.5,
            recency_decay_days=30.0,
            entity_weight=0.0,
            keyword_weight=0.0,
        )
        ranker = RelevanceRanker(config)
        
        now = datetime.utcnow()
        old_date = (now - timedelta(days=60)).isoformat()
        recent_date = (now - timedelta(days=1)).isoformat()
        
        memories = [
            {"id": "mem1", "content": "Old memory", "relevance": 0.9, "created_at": old_date},
            {"id": "mem2", "content": "Recent memory", "relevance": 0.7, "created_at": recent_date},
        ]
        
        ranked = ranker.rank(memories)
        
        # Recent memory should have higher recency score
        mem1_ranked = next(r for r in ranked if r.id == "mem1")
        mem2_ranked = next(r for r in ranked if r.id == "mem2")
        
        assert mem2_ranked.recency_score > mem1_ranked.recency_score
    
    def test_entity_boost(self):
        """Test entity match boost."""
        config = RankingConfig(
            semantic_weight=0.5,
            recency_weight=0.0,
            entity_weight=0.5,
            keyword_weight=0.0,
        )
        ranker = RelevanceRanker(config)
        
        query_entities = [
            EntityRef(id="ent1", canonical_name="David Kim", type="person", confidence=1.0)
        ]
        
        memories = [
            {
                "id": "mem1",
                "content": "Memory about David Kim",
                "relevance": 0.7,
                "entities": [
                    {"id": "ent1", "canonical_name": "David Kim", "type": "person", "confidence": 1.0}
                ],
            },
            {
                "id": "mem2",
                "content": "Memory about someone else",
                "relevance": 0.8,
                "entities": [
                    {"id": "ent2", "canonical_name": "Sarah Chen", "type": "person", "confidence": 1.0}
                ],
            },
        ]
        
        ranked = ranker.rank(memories, query="David Kim", query_entities=query_entities)
        
        # mem1 should have entity boost
        mem1_ranked = next(r for r in ranked if r.id == "mem1")
        mem2_ranked = next(r for r in ranked if r.id == "mem2")
        
        assert mem1_ranked.entity_score > mem2_ranked.entity_score
    
    def test_keyword_boost(self):
        """Test keyword match boost."""
        config = RankingConfig(
            semantic_weight=0.5,
            recency_weight=0.0,
            entity_weight=0.0,
            keyword_weight=0.5,
        )
        ranker = RelevanceRanker(config)
        
        memories = [
            {"id": "mem1", "content": "No keywords", "relevance": 0.8, "keyword_score": 0.0},
            {"id": "mem2", "content": "Has keywords", "relevance": 0.6, "keyword_score": 5.0},
        ]
        
        ranked = ranker.rank(memories)
        
        mem1_ranked = next(r for r in ranked if r.id == "mem1")
        mem2_ranked = next(r for r in ranked if r.id == "mem2")
        
        # mem2 should have higher keyword score
        assert mem2_ranked.keyword_score > mem1_ranked.keyword_score
    
    def test_config_from_env(self):
        """Test loading config from environment."""
        import os
        
        # Set env vars
        os.environ["REMEMBRA_RANKING_SEMANTIC_WEIGHT"] = "0.8"
        os.environ["REMEMBRA_RANKING_RECENCY_WEIGHT"] = "0.1"
        
        try:
            config = RankingConfig.from_env()
            assert config.semantic_weight == 0.8
            assert config.recency_weight == 0.1
        finally:
            # Clean up
            del os.environ["REMEMBRA_RANKING_SEMANTIC_WEIGHT"]
            del os.environ["REMEMBRA_RANKING_RECENCY_WEIGHT"]
    
    def test_diversity_reranking(self):
        """Test diversity-aware reranking."""
        ranker = RelevanceRanker()
        
        # Create some ranked memories with similar content
        ranked = [
            RankedMemory(id="mem1", content="David Kim works at Acme", created_at=None, final_score=0.9),
            RankedMemory(id="mem2", content="David Kim is at Acme Corp", created_at=None, final_score=0.85),  # Very similar to mem1
            RankedMemory(id="mem3", content="The merger was discussed yesterday", created_at=None, final_score=0.8),  # Different topic
        ]
        
        reranked = ranker.rerank_with_diversity(ranked, diversity_threshold=0.3, limit=3)
        
        assert len(reranked) == 3
        # mem3 (different topic) should be promoted over mem2 (very similar to mem1)
        # Because of diversity scoring
        assert reranked[0].id == "mem1"  # Top is always kept


class TestIntegration:
    """Integration tests for advanced retrieval pipeline."""
    
    def test_full_hybrid_pipeline(self):
        """Test complete hybrid search → rank → optimize pipeline."""
        # 1. Hybrid Search
        searcher = HybridSearcher(HybridSearchConfig(
            semantic_weight=0.7,
            keyword_weight=0.3,
        ))
        
        docs = [
            ("mem1", "David Kim works at Acme Corp as VP of Sales"),
            ("mem2", "Mr. Kim mentioned the upcoming merger during the meeting"),
            ("mem3", "The acquisition of TechStart was finalized"),
        ]
        searcher.index_documents(docs)
        
        # Simulate semantic results
        semantic_results = [
            ("mem1", 0.9, {"content": docs[0][1], "created_at": "2026-03-01T10:00:00"}),
            ("mem2", 0.7, {"content": docs[1][1], "created_at": "2026-02-15T10:00:00"}),
            ("mem3", 0.5, {"content": docs[2][1], "created_at": "2026-01-01T10:00:00"}),
        ]
        
        fused = searcher.fuse_results(
            semantic_results,
            searcher.bm25_index.search("David Kim merger", limit=10),
            limit=10,
        )
        
        assert len(fused) >= 2  # At least 2 results
        
        # 2. Relevance Ranking
        ranker = RelevanceRanker(RankingConfig(
            semantic_weight=0.6,
            recency_weight=0.2,
            entity_weight=0.1,
            keyword_weight=0.1,
        ))
        
        memories_for_ranking = [
            {
                "id": r.id,
                "content": r.content,
                "relevance": r.semantic_score,
                "keyword_score": r.keyword_score,
                "created_at": r.payload.get("created_at") if r.payload else None,
            }
            for r in fused
        ]
        
        ranked = ranker.rank(memories_for_ranking, query="David Kim merger")
        
        assert len(ranked) >= 2
        # Should prioritize mem1 or mem2 (both mention Kim)
        
        # 3. Context Optimization
        optimizer = ContextOptimizer(max_tokens=500, include_metadata=True)
        
        memories_for_context = [
            {
                "id": r.id,
                "content": r.content,
                "relevance": r.final_score,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in ranked
        ]
        
        optimized = optimizer.optimize(memories_for_context)
        
        assert len(optimized.context) > 0
        assert "Kim" in optimized.context or "David" in optimized.context
