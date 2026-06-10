"""Brain layer endpoints — /api/v1/brain.

Surfaces Remembra's higher-level understanding of a memory graph: themed
communities with summaries, the most central "god node" entities, and surprising
cross-theme links. This is the GraphRAG-style intelligence layer that lets the
product (and the user) reason about *what the memory is about*, not just retrieve
matching facts.

  GET  /brain/communities  — stored themes with summaries (fast, from table)
  GET  /brain/insights     — live god nodes + surprising links + graph stats
  POST /brain/analyze      — recompute + persist the brain layer for a project
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from remembra.auth.middleware import CurrentUser, has_permission, resolve_project_access
from remembra.brain.analyzer import BrainAnalyzer
from remembra.core.limiter import limiter
from remembra.storage.database import Database

router = APIRouter(prefix="/brain", tags=["brain"])


def get_database(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_database)]


class CommunitiesResponse(BaseModel):
    communities: list[dict[str, Any]]
    count: int
    project_id: str


class InsightsResponse(BaseModel):
    num_entities: int
    num_relationships: int
    num_communities: int
    modularity: float
    god_nodes: list[dict[str, Any]]
    surprising_links: list[dict[str, Any]]
    communities: list[dict[str, Any]]


@router.get("/communities", response_model=CommunitiesResponse, summary="List discovered themes")
@limiter.limit("60/minute")
async def list_communities(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None),
) -> CommunitiesResponse:
    """Return the stored communities (themes) for a project, largest first.

    Served from the persisted table — recomputed by the sleep-time worker or via
    POST /brain/analyze. Empty until the first analysis runs.
    """
    if not has_permission(current_user, "memory:recall"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied: memory:recall required")
    resolved = resolve_project_access(current_user, project_id)
    communities = await db.get_communities(current_user.user_id, resolved)
    return CommunitiesResponse(
        communities=communities,
        count=len(communities),
        project_id=resolved or "all",
    )


@router.get("/insights", response_model=InsightsResponse, summary="Live brain insights")
@limiter.limit("20/minute")
async def get_insights(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None),
) -> InsightsResponse:
    """Compute brain insights live (without persisting): central entities,
    surprising cross-theme links, and graph stats. Cheap at per-tenant scale."""
    if not has_permission(current_user, "memory:recall"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied: memory:recall required")
    resolved = resolve_project_access(current_user, project_id)
    result = await BrainAnalyzer(db).analyze(current_user.user_id, resolved or "default", persist=False)
    return InsightsResponse(**result.to_dict())


@router.post("/analyze", response_model=InsightsResponse, summary="Recompute the brain layer")
@limiter.limit("6/minute")
async def analyze_brain(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    project_id: str | None = Query(default=None),
) -> InsightsResponse:
    """Recompute communities + insights and persist them (entity community ids +
    the communities table). This is what the dashboard's "Refresh" action calls
    and what the sleep-time worker runs in the background."""
    if not has_permission(current_user, "memory:store"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied: memory:store required")
    resolved = resolve_project_access(current_user, project_id)
    result = await BrainAnalyzer(db).analyze(current_user.user_id, resolved or "default", persist=True)
    return InsightsResponse(**result.to_dict())
