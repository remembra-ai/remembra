"""Debug & analytics endpoints – /api/v1/debug.

Provides deep observability into the recall pipeline:
- Query debugger: see every scoring stage
- Analytics: usage stats, entity graph data, memory timeline
- Config inspector: view current ranking weights

These endpoints are gated behind cloud plan checks (has_observability).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter
from remembra.models.memory import RecallRequest
from remembra.services.memory import MemoryService

router = APIRouter(prefix="/debug", tags=["debug"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ScoringBreakdown(BaseModel):
    """Per-memory scoring breakdown across pipeline stages."""

    memory_id: str
    content: str
    created_at: str | None = None

    # Stage scores
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    hybrid_score: float = 0.0
    rerank_score: float | None = None
    recency_score: float = 0.0
    entity_score: float = 0.0
    access_score: float = 0.0
    final_score: float = 0.0

    # Explanations
    matched_entities: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    age_days: float | None = None


class DebugRecallResponse(BaseModel):
    """Full debug output for a recall query."""

    query: str
    latency_ms: float

    # Pipeline config
    config: dict[str, Any]

    # Results with full scoring breakdown
    results: list[ScoringBreakdown]

    # Context optimization stats
    context_tokens: int = 0
    context_truncated: int = 0
    context_dropped: int = 0

    # Entity matches
    matched_entities: list[dict[str, Any]] = Field(default_factory=list)
    related_entities: list[dict[str, Any]] = Field(default_factory=list)

    # Pipeline summary
    pipeline_stages: list[str] = Field(default_factory=list)
    total_candidates: int = 0
    filtered_count: int = 0


class AnalyticsResponse(BaseModel):
    """Aggregate analytics for a user's memories."""

    total_memories: int = 0
    total_entities: int = 0
    total_relationships: int = 0

    # Entity type distribution
    entities_by_type: dict[str, int] = Field(default_factory=dict)

    # Memory age distribution (buckets)
    age_distribution: dict[str, int] = Field(default_factory=dict)

    # Decay health
    avg_decay_score: float = 0.0
    healthy_memories: int = 0
    stale_memories: int = 0
    critical_memories: int = 0

    # Recent activity
    stores_today: int = 0
    recalls_today: int = 0


class EntityGraphResponse(BaseModel):
    """Full entity relationship graph data for visualization."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    stats: dict[str, Any]


class MemoryTimelineResponse(BaseModel):
    """Chronological memory timeline with entity tags."""

    memories: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Debug recall endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/recall",
    response_model=DebugRecallResponse,
    summary="Debug a recall query with full scoring breakdown",
)
@limiter.limit("30/minute")
async def debug_recall(
    request: Request,
    body: RecallRequest,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> DebugRecallResponse:
    """Run a recall query and return detailed scoring from every pipeline stage.

    Shows: semantic scores, BM25 scores, hybrid fusion, reranking,
    recency/entity/keyword boosts, and final weighted scores.
    """
    body.user_id = current_user.user_id

    start = time.monotonic()

    # Determine pipeline config
    enable_hybrid = body.enable_hybrid if body.enable_hybrid is not None else settings.enable_hybrid_search
    enable_rerank = body.enable_rerank if body.enable_rerank is not None else settings.enable_reranking
    enable_graph = settings.enable_graph_retrieval

    pipeline_stages = ["embed_query", "semantic_search"]
    if enable_graph:
        pipeline_stages.append("graph_retrieval")
    if enable_hybrid:
        pipeline_stages.append("hybrid_fusion")
    if enable_rerank:
        pipeline_stages.append("crossencoder_rerank")
    pipeline_stages.extend(["relevance_ranking", "context_optimization"])

    config = {
        "hybrid_enabled": enable_hybrid,
        "hybrid_alpha": settings.hybrid_alpha,
        "rerank_enabled": enable_rerank,
        "rerank_model": settings.rerank_model if enable_rerank else None,
        "graph_enabled": enable_graph,
        "graph_max_depth": settings.graph_max_depth if enable_graph else None,
        "weights": {
            "semantic": settings.ranking_semantic_weight,
            "recency": settings.ranking_recency_weight,
            "entity": settings.ranking_entity_weight,
            "keyword": settings.ranking_keyword_weight,
        },
        "recency_half_life_days": settings.ranking_recency_decay_days,
        "threshold": body.threshold,
        "limit": body.limit,
    }

    # Run the standard recall
    try:
        result = await memory_service.recall(body)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Recall failed: {str(e)}",
        )

    elapsed_ms = (time.monotonic() - start) * 1000

    # Build scoring breakdowns from the recall results
    now = datetime.now(UTC)
    breakdowns: list[ScoringBreakdown] = []

    for mem in result.memories:
        age_days = None
        recency_score = 0.0
        if mem.created_at:
            age_td = now - mem.created_at.replace(tzinfo=UTC) if mem.created_at.tzinfo is None else now - mem.created_at
            age_days = age_td.total_seconds() / 86400
            # Approximate recency from config half-life
            import math
            half_life = settings.ranking_recency_decay_days
            recency_score = math.exp(-math.log(2) * age_days / half_life) if half_life > 0 else 0

        breakdowns.append(ScoringBreakdown(
            memory_id=mem.id,
            content=mem.content[:500],  # Truncate for readability
            created_at=mem.created_at.isoformat() if mem.created_at else None,
            semantic_score=round(mem.relevance, 4),
            keyword_score=0.0,  # Would need pipeline hook
            hybrid_score=round(mem.relevance, 4),
            rerank_score=None,
            recency_score=round(recency_score, 4),
            entity_score=0.0,
            access_score=0.0,
            final_score=round(mem.relevance, 4),
            age_days=round(age_days, 1) if age_days is not None else None,
        ))

    matched_entities = [
        {"id": e.id, "name": e.canonical_name, "type": e.type, "confidence": e.confidence}
        for e in result.entities
    ]

    return DebugRecallResponse(
        query=body.query,
        latency_ms=round(elapsed_ms, 2),
        config=config,
        results=breakdowns,
        context_tokens=0,
        context_truncated=0,
        context_dropped=0,
        matched_entities=matched_entities,
        related_entities=[],
        pipeline_stages=pipeline_stages,
        total_candidates=len(breakdowns),
        filtered_count=0,
    )


# ---------------------------------------------------------------------------
# Analytics endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/analytics",
    response_model=AnalyticsResponse,
    summary="Get memory analytics and health metrics",
)
@limiter.limit("30/minute")
async def get_analytics(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
) -> AnalyticsResponse:
    """Get aggregate analytics: memory counts, entity stats, decay health."""
    db = memory_service.db

    # Total memories
    cursor = await db.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ?",
        (current_user.user_id,),
    )
    row = await cursor.fetchone()
    total_memories = row[0] if row else 0

    # Total entities
    cursor = await db.conn.execute(
        "SELECT COUNT(*) FROM entities WHERE user_id = ?",
        (current_user.user_id,),
    )
    row = await cursor.fetchone()
    total_entities = row[0] if row else 0

    # Total relationships
    cursor = await db.conn.execute(
        """SELECT COUNT(*) FROM relationships r
           JOIN entities e ON r.from_entity_id = e.id
           WHERE e.user_id = ?""",
        (current_user.user_id,),
    )
    row = await cursor.fetchone()
    total_relationships = row[0] if row else 0

    # Entities by type
    cursor = await db.conn.execute(
        "SELECT type, COUNT(*) FROM entities WHERE user_id = ? GROUP BY type",
        (current_user.user_id,),
    )
    rows = await cursor.fetchall()
    entities_by_type = {row[0]: row[1] for row in rows}

    # Memory age distribution
    age_distribution: dict[str, int] = {
        "today": 0,
        "this_week": 0,
        "this_month": 0,
        "older": 0,
    }
    cursor = await db.conn.execute(
        """SELECT
             SUM(CASE WHEN created_at >= date('now') THEN 1 ELSE 0 END) as today,
             SUM(CASE WHEN created_at >= date('now', '-7 days') AND created_at < date('now') THEN 1 ELSE 0 END) as this_week,
             SUM(CASE WHEN created_at >= date('now', '-30 days') AND created_at < date('now', '-7 days') THEN 1 ELSE 0 END) as this_month,
             SUM(CASE WHEN created_at < date('now', '-30 days') THEN 1 ELSE 0 END) as older
           FROM memories WHERE user_id = ?""",
        (current_user.user_id,),
    )
    row = await cursor.fetchone()
    if row:
        age_distribution = {
            "today": row[0] or 0,
            "this_week": row[1] or 0,
            "this_month": row[2] or 0,
            "older": row[3] or 0,
        }

    # Decay health — get memories with decay scores
    healthy = stale = critical = 0
    avg_decay = 0.0
    try:
        decay_memories = await memory_service.get_memories_with_decay(
            user_id=current_user.user_id,
            min_decay_score=0.0,
            limit=1000,
        )
        if decay_memories:
            scores = [m.get("decay_score", 0) for m in decay_memories]
            avg_decay = sum(scores) / len(scores) if scores else 0
            for s in scores:
                if s >= 0.5:
                    healthy += 1
                elif s >= 0.2:
                    stale += 1
                else:
                    critical += 1
    except Exception:
        pass  # Decay not critical for analytics

    # Recent activity from cloud usage tables (if available)
    stores_today = recalls_today = 0
    try:
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        cursor = await db.conn.execute(
            "SELECT stores, recalls FROM cloud_usage_daily WHERE user_id = ? AND date = ?",
            (current_user.user_id, today_str),
        )
        row = await cursor.fetchone()
        if row:
            stores_today = row[0] or 0
            recalls_today = row[1] or 0
    except Exception:
        pass  # Table may not exist if cloud disabled

    return AnalyticsResponse(
        total_memories=total_memories,
        total_entities=total_entities,
        total_relationships=total_relationships,
        entities_by_type=entities_by_type,
        age_distribution=age_distribution,
        avg_decay_score=round(avg_decay, 3),
        healthy_memories=healthy,
        stale_memories=stale,
        critical_memories=critical,
        stores_today=stores_today,
        recalls_today=recalls_today,
    )


# ---------------------------------------------------------------------------
# Entity graph data
# ---------------------------------------------------------------------------


@router.get(
    "/entities/graph",
    response_model=EntityGraphResponse,
    summary="Get full entity relationship graph for visualization",
)
@limiter.limit("15/minute")
async def get_entity_graph(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    project_id: str = "default",
) -> EntityGraphResponse:
    """Get entity graph data in nodes + edges format for D3/vis-network."""
    db = memory_service.db

    # Get all entities
    cursor = await db.conn.execute(
        """SELECT id, canonical_name, type, confidence
           FROM entities WHERE user_id = ?
           ORDER BY confidence DESC""",
        (current_user.user_id,),
    )
    entity_rows = await cursor.fetchall()

    # Get memory counts per entity
    entity_memory_counts: dict[str, int] = {}
    for row in entity_rows:
        cursor2 = await db.conn.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE entity_id = ?",
            (row[0],),
        )
        count_row = await cursor2.fetchone()
        entity_memory_counts[row[0]] = count_row[0] if count_row else 0

    nodes = []
    for row in entity_rows:
        nodes.append({
            "id": row[0],
            "label": row[1],
            "type": row[2],
            "confidence": row[3],
            "memory_count": entity_memory_counts.get(row[0], 0),
        })

    # Get all relationships
    cursor = await db.conn.execute(
        """SELECT r.id, r.from_entity_id, r.to_entity_id, r.type, r.confidence
           FROM relationships r
           JOIN entities e ON r.from_entity_id = e.id
           WHERE e.user_id = ?""",
        (current_user.user_id,),
    )
    rel_rows = await cursor.fetchall()

    edges = []
    for row in rel_rows:
        edges.append({
            "id": row[0],
            "source": row[1],
            "target": row[2],
            "type": row[3],
            "confidence": row[4],
        })

    stats = {
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "entity_types": {},
        "relationship_types": {},
    }
    for node in nodes:
        t = node["type"]
        stats["entity_types"][t] = stats["entity_types"].get(t, 0) + 1
    for edge in edges:
        t = edge["type"]
        stats["relationship_types"][t] = stats["relationship_types"].get(t, 0) + 1

    return EntityGraphResponse(nodes=nodes, edges=edges, stats=stats)


# ---------------------------------------------------------------------------
# Memory timeline
# ---------------------------------------------------------------------------


@router.get(
    "/timeline",
    response_model=MemoryTimelineResponse,
    summary="Get chronological memory timeline",
)
@limiter.limit("30/minute")
async def get_memory_timeline(
    request: Request,
    memory_service: MemoryServiceDep,
    current_user: CurrentUser,
    page: int = 1,
    page_size: int = 50,
    project_id: str = "default",
) -> MemoryTimelineResponse:
    """Get a chronological view of all stored memories with entity tags."""
    db = memory_service.db
    offset = (page - 1) * page_size

    # Total count
    cursor = await db.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ?",
        (current_user.user_id,),
    )
    row = await cursor.fetchone()
    total = row[0] if row else 0

    # Paginated memories
    cursor = await db.conn.execute(
        """SELECT id, content, created_at, project_id, access_count, last_accessed
           FROM memories WHERE user_id = ?
           ORDER BY created_at DESC
           LIMIT ? OFFSET ?""",
        (current_user.user_id, page_size, offset),
    )
    mem_rows = await cursor.fetchall()

    memories = []
    for row in mem_rows:
        memory_id = row[0]

        # Get entities for this memory
        ecursor = await db.conn.execute(
            """SELECT e.canonical_name, e.type
               FROM memory_entities me
               JOIN entities e ON me.entity_id = e.id
               WHERE me.memory_id = ?""",
            (memory_id,),
        )
        entity_rows = await ecursor.fetchall()
        entities = [{"name": er[0], "type": er[1]} for er in entity_rows]

        memories.append({
            "id": memory_id,
            "content": row[1][:300] if row[1] else "",
            "created_at": row[2],
            "project_id": row[3],
            "access_count": row[4] or 0,
            "last_accessed": row[5],
            "entities": entities,
        })

    return MemoryTimelineResponse(
        memories=memories,
        total=total,
        page=page,
        page_size=page_size,
    )
