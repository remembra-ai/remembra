"""Relevance ranking with configurable boosts.

This module provides post-retrieval ranking that considers:
1. Semantic similarity score
2. Recency boost (newer = higher)
3. Entity match boost (entities in query)
4. Keyword match boost (from hybrid search)
5. Access frequency boost (popular memories)
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.core.time import utcnow
from remembra.models.memory import EntityRef

log = structlog.get_logger(__name__)


@dataclass
class RankingConfig:
    """Configuration for relevance ranking weights.

    All weights should sum to approximately 1.0 for normalized output,
    but this is not enforced.
    """

    # Base semantic similarity weight
    semantic_weight: float = 0.6

    # Boost for recent memories (decays over time)
    recency_weight: float = 0.15
    recency_decay_days: float = 30.0  # Half-life in days

    # Boost for memories with matching entities
    entity_weight: float = 0.15
    entity_boost_per_match: float = 0.1  # Per matched entity
    entity_max_boost: float = 0.3  # Cap on entity boost

    # Boost from keyword matching (BM25)
    keyword_weight: float = 0.1

    # Boost for frequently accessed memories
    access_weight: float = 0.0  # Disabled by default
    access_log_base: float = 10.0  # Use log scale for access counts

    @classmethod
    def for_mode(cls, mode: str) -> "RankingConfig":
        """
        Get preset configuration for a retrieval mode.

        Modes (from Marty's feedback):
        - balanced: Default balanced weights (same as from_env)
        - debug: High recency bias (what just happened?)
        - operational: Stability-weighted (reliable facts, entity-heavy)
        - strategic: Historical depth (patterns over time, low recency decay)

        Args:
            mode: One of "balanced", "debug", "operational", "strategic"

        Returns:
            RankingConfig with preset weights for the mode
        """
        if mode == "debug":
            # Debug mode: Heavy recency bias for recent context
            return cls(
                semantic_weight=0.35,
                recency_weight=0.45,
                recency_decay_days=7.0,  # Fast decay
                entity_weight=0.10,
                keyword_weight=0.10,
                access_weight=0.0,
            )
        elif mode == "operational":
            # Operational mode: Stable, entity-focused facts
            return cls(
                semantic_weight=0.50,
                recency_weight=0.10,
                recency_decay_days=90.0,  # Slow decay
                entity_weight=0.25,
                keyword_weight=0.15,
                access_weight=0.0,
            )
        elif mode == "strategic":
            # Strategic mode: Historical depth for pattern analysis
            return cls(
                semantic_weight=0.70,
                recency_weight=0.05,
                recency_decay_days=365.0,  # Very slow decay
                entity_weight=0.15,
                keyword_weight=0.10,
                access_weight=0.0,
            )
        else:
            # Balanced (default)
            return cls.from_env()

    @classmethod
    def from_env(cls) -> "RankingConfig":
        """
        Create RankingConfig from environment variables.

        Environment variables (all optional):
        - REMEMBRA_RANKING_SEMANTIC_WEIGHT
        - REMEMBRA_RANKING_RECENCY_WEIGHT
        - REMEMBRA_RANKING_RECENCY_DECAY_DAYS
        - REMEMBRA_RANKING_ENTITY_WEIGHT
        - REMEMBRA_RANKING_KEYWORD_WEIGHT
        - REMEMBRA_RANKING_ACCESS_WEIGHT
        """
        import os

        def get_float(key: str, default: float) -> float:
            val = os.getenv(f"REMEMBRA_RANKING_{key}")
            if val is None:
                return default
            try:
                return float(val)
            except ValueError:
                log.warning(f"Invalid value for REMEMBRA_RANKING_{key}: {val}")
                return default

        return cls(
            semantic_weight=get_float("SEMANTIC_WEIGHT", 0.6),
            recency_weight=get_float("RECENCY_WEIGHT", 0.15),
            recency_decay_days=get_float("RECENCY_DECAY_DAYS", 30.0),
            entity_weight=get_float("ENTITY_WEIGHT", 0.15),
            keyword_weight=get_float("KEYWORD_WEIGHT", 0.1),
            access_weight=get_float("ACCESS_WEIGHT", 0.0),
        )


@dataclass
class RankedMemory:
    """A memory with ranking scores."""

    id: str
    content: str
    created_at: datetime | None

    # Component scores (0-1 normalized)
    semantic_score: float = 0.0
    recency_score: float = 0.0
    entity_score: float = 0.0
    keyword_score: float = 0.0
    access_score: float = 0.0

    # Final combined score
    final_score: float = 0.0

    # Additional data
    payload: dict[str, Any] | None = None
    matched_entities: list[EntityRef] | None = None
    matched_keywords: list[str] | None = None


class RelevanceRanker:
    """
    Ranks memories using a weighted combination of signals.

    This ranker takes raw search results and applies configurable
    boosts for recency, entity matches, etc.
    """

    def __init__(self, config: RankingConfig | None = None) -> None:
        """
        Initialize the ranker.

        Args:
            config: Ranking configuration. If None, loads from environment.
        """
        self.config = config or RankingConfig.from_env()

    def _compute_recency_score(self, created_at: datetime | None, config: RankingConfig | None = None) -> float:
        """
        Compute recency score using exponential decay.

        Score = exp(-age_days * ln(2) / half_life)

        This gives score=1.0 for now, score=0.5 at half_life days ago.
        """
        if not created_at:
            return 0.5  # Default for unknown dates

        cfg = config or self.config
        now = utcnow()

        try:
            # Handle timezone-aware datetimes
            if created_at.tzinfo is not None:
                now = now.replace(tzinfo=UTC)

            age_days = (now - created_at).total_seconds() / 86400

            if age_days < 0:
                return 1.0  # Future dates get max score

            decay_rate = math.log(2) / cfg.recency_decay_days
            score = math.exp(-age_days * decay_rate)

            return min(1.0, max(0.0, score))

        except Exception as e:
            log.warning("recency_score_error", error=str(e))
            return 0.5

    def _compute_entity_score(
        self,
        memory_entities: list[EntityRef] | None,
        query_entities: list[EntityRef] | None,
        query: str = "",
        config: RankingConfig | None = None,
    ) -> float:
        """
        Compute entity match score.

        Boosts memories that contain entities mentioned in the query.
        """
        if not memory_entities:
            return 0.0

        cfg = config or self.config
        boost = 0.0

        # Check if query_entities are provided (direct matches)
        if query_entities:
            query_entity_ids = {e.id for e in query_entities}
            for entity in memory_entities:
                if entity.id in query_entity_ids:
                    boost += cfg.entity_boost_per_match * entity.confidence

        # Also check entity names in query string (whole-word, no junk names - RET-3)
        from remembra.retrieval.graph import ENTITY_MENTION_STOPLIST, MIN_MENTION_LEN, mention_in_query

        for entity in memory_entities:
            name = (entity.canonical_name or "").strip()
            if len(name) < MIN_MENTION_LEN or name.casefold() in ENTITY_MENTION_STOPLIST:
                continue
            if query_entities and entity.id in {e.id for e in query_entities}:
                continue  # already counted above
            if mention_in_query(name, query):
                boost += cfg.entity_boost_per_match * entity.confidence

        return min(boost, cfg.entity_max_boost)

    def _compute_access_score(self, access_count: int) -> float:
        """
        Compute access frequency score using logarithmic scaling.

        log10(1 + access_count) / log10(1 + max_expected)
        """
        if access_count <= 0:
            return 0.0

        # Normalize using log scale
        # Assume max ~1000 accesses for normalization
        max_expected = 1000

        score = math.log(1 + access_count, self.config.access_log_base)
        max_score = math.log(1 + max_expected, self.config.access_log_base)

        return min(1.0, score / max_score)

    def rank(
        self,
        memories: list[dict[str, Any]],
        query: str = "",
        query_entities: list[EntityRef] | None = None,
        retrieval_mode: str = "balanced",
        feedback_weight: float = 0.0,
    ) -> list[RankedMemory]:
        """
        Rank memories using weighted combination of signals.

        Args:
            memories: List of memory dicts with keys:
                - id: Memory ID
                - content: Memory content
                - semantic_score (or relevance): ABSOLUTE similarity in [0, 1];
                  it is not rescaled against the other candidates, so a weak
                  best match stays weak (RET-1)
                - rerank_score: optional CrossEncoder probability; when present
                  it is the relevance component instead of semantic_score
                - feedback_score: optional net feedback in [-1, 1]
                - created_at: ISO timestamp
                - keyword_score: BM25 score (optional)
                - entities: List of EntityRef (optional)
                - access_count: Access frequency (optional)
            query: Original search query
            query_entities: Entities found in the query
            retrieval_mode: One of "balanced", "debug", "operational", "strategic"
                - debug: High recency bias (what just happened?)
                - operational: Stability/entity-weighted (reliable facts)
                - strategic: Historical depth (patterns over time)

        Returns:
            List of RankedMemory sorted by final_score descending
        """
        if not memories:
            return []

        # Select config based on retrieval mode
        # If mode is non-default, use preset; otherwise use instance config
        if retrieval_mode and retrieval_mode != "balanced":
            config = RankingConfig.for_mode(retrieval_mode)
            log.debug(
                "using_retrieval_mode",
                mode=retrieval_mode,
                semantic_weight=config.semantic_weight,
                recency_weight=config.recency_weight,
            )
        else:
            config = self.config

        ranked: list[RankedMemory] = []

        # Keyword (BM25) scores are unbounded, so they are scaled by the batch
        # max. Semantic scores are cosine similarities and stay absolute.
        max_keyword = max(m.get("keyword_score", 0) or 0 for m in memories) or 1.0

        for memory in memories:
            memory_id = str(memory.get("id", ""))
            content = memory.get("content", "")

            # Parse created_at
            created_at: datetime | None = None
            if memory.get("created_at"):
                try:
                    created_at_str = memory["created_at"]
                    if isinstance(created_at_str, datetime):
                        created_at = created_at_str
                    else:
                        created_at = datetime.fromisoformat(str(created_at_str).replace("Z", "+00:00").split("+")[0])
                except (ValueError, TypeError):
                    pass

            # Get entity refs if available
            memory_entities: list[EntityRef] | None = None
            if memory.get("entities") and isinstance(memory["entities"], list):
                memory_entities = []
                for e in memory["entities"]:
                    if isinstance(e, EntityRef):
                        memory_entities.append(e)
                    elif isinstance(e, dict):
                        memory_entities.append(
                            EntityRef(
                                id=e.get("id", ""),
                                canonical_name=e.get("canonical_name", ""),
                                type=e.get("type", "unknown"),
                                confidence=e.get("confidence", 1.0),
                            )
                        )

            # Compute component scores
            raw_semantic = memory["semantic_score"] if "semantic_score" in memory else memory.get("relevance", 0)
            semantic_score = min(1.0, max(0.0, float(raw_semantic or 0.0)))
            rerank = memory.get("rerank_score")
            relevance_component = min(1.0, max(0.0, float(rerank))) if rerank is not None else semantic_score

            raw_keyword = memory.get("keyword_score", 0) or 0
            keyword_score = raw_keyword / max_keyword if max_keyword > 0 else 0

            recency_score = self._compute_recency_score(created_at, config)

            entity_score = self._compute_entity_score(memory_entities, query_entities, query, config)

            access_score = self._compute_access_score(memory.get("access_count", 0))

            # Compute weighted final score
            final_score = (
                config.semantic_weight * relevance_component
                + config.recency_weight * recency_score
                + config.entity_weight * entity_score
                + config.keyword_weight * keyword_score
                + config.access_weight * access_score
                + feedback_weight * float(memory.get("feedback_score", 0.0) or 0.0)
            )

            ranked.append(
                RankedMemory(
                    id=memory_id,
                    content=content,
                    created_at=created_at,
                    semantic_score=semantic_score,
                    recency_score=recency_score,
                    entity_score=entity_score,
                    keyword_score=keyword_score,
                    access_score=access_score,
                    final_score=final_score,
                    payload=memory.get("payload"),
                    matched_entities=memory_entities,
                    matched_keywords=memory.get("matched_keywords"),
                )
            )

        # Sort by final score descending
        ranked.sort(key=lambda r: r.final_score, reverse=True)

        log.debug(
            "ranking_complete",
            input_count=len(memories),
            top_score=ranked[0].final_score if ranked else 0,
        )

        return ranked

    def rerank_with_diversity(
        self,
        ranked: list[RankedMemory],
        diversity_threshold: float = 0.8,
        limit: int | None = None,
    ) -> list[RankedMemory]:
        """
        Rerank to promote diversity while maintaining relevance.

        Uses Maximal Marginal Relevance (MMR) approach: avoids
        selecting memories that are too similar to already-selected ones.

        Args:
            ranked: Already-ranked memories
            diversity_threshold: Similarity threshold (0-1, lower = more diverse)
            limit: Max memories to return

        Returns:
            Reranked list prioritizing diversity
        """
        if not ranked or len(ranked) <= 1:
            return ranked[:limit] if limit else ranked

        selected: list[RankedMemory] = [ranked[0]]  # Always take the top one
        remaining = ranked[1:]

        target_count = limit or len(ranked)

        while remaining and len(selected) < target_count:
            best_candidate: RankedMemory | None = None
            best_mmr_score = -1.0

            for candidate in remaining:
                # Calculate max similarity to already selected
                max_similarity = 0.0
                for sel in selected:
                    # Simple content-based similarity (Jaccard on words)
                    cand_words = set(candidate.content.lower().split())
                    sel_words = set(sel.content.lower().split())

                    if cand_words or sel_words:
                        jaccard = len(cand_words & sel_words) / len(cand_words | sel_words)
                        max_similarity = max(max_similarity, jaccard)

                # MMR score: relevance - lambda * max_similarity
                mmr_score = candidate.final_score - diversity_threshold * max_similarity

                if mmr_score > best_mmr_score:
                    best_mmr_score = mmr_score
                    best_candidate = candidate

            if best_candidate:
                selected.append(best_candidate)
                remaining.remove(best_candidate)
            else:
                break

        return selected
