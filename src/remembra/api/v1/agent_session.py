"""Agent session endpoints — session brief, status upsert, timeline.

- ``GET  /api/v1/session/brief``   see ``api/v1/relay.py`` (brief + relay pickup)
- ``POST /api/v1/session/status``  upsert a status value by key (supersedes the prior value)
- ``GET  /api/v1/session/status``  current status values for a project
- ``GET  /api/v1/timeline``        chronological memories with a created_at range filter

Everything is scoped to the authenticated user and to projects the caller's
key may access (``resolve_project_access``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, has_permission, resolve_project_access
from remembra.cloud.limits import gate_write, record_relay_usage
from remembra.config import get_settings
from remembra.core.limiter import limiter
from remembra.services.agent_session import AgentSessionService

router = APIRouter(tags=["agent-session"])


def _service(request: Request) -> AgentSessionService:
    return AgentSessionService(
        db=request.app.state.db,
        memory_service=getattr(request.app.state, "memory_service", None),
    )


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _require(current_user: Any, permission: str) -> None:
    if not has_permission(current_user, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {permission} required",
        )


def screen_text(request: Request, text: str, apply_pii: bool = True) -> tuple[str, float, str | None]:
    """Same content protections as POST /memories: PII policy + sanitizer.

    Returns ``(text, trust_score, checksum)``; raises 400 when the PII policy
    blocks the content outright. ``apply_pii=False`` is for text assembled
    from values that were already PII-scrubbed one by one (relay close-out):
    only the sanitizer runs.
    """
    pii_detector = getattr(request.app.state, "pii_detector", None) if apply_pii else None
    if pii_detector:
        pii_result = pii_detector.scan(text, source="user_input")
        if pii_result.has_pii:
            if pii_result.blocked:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "PII_DETECTED",
                        "message": "Content contains sensitive information that cannot be stored",
                        "types": [m.type for m in pii_result.matches],
                    },
                )
            if pii_result.redacted_content:
                text = pii_result.redacted_content
    trust_score, checksum = 1.0, None
    sanitizer = getattr(request.app.state, "sanitizer", None)
    if get_settings().sanitization_enabled and sanitizer is not None:
        analysis = sanitizer.analyze(text, source="user_input")
        text, trust_score, checksum = analysis.content, analysis.trust_score, analysis.checksum
    return text, trust_score, checksum


# ---------------------------------------------------------------------------
# Status upsert
# ---------------------------------------------------------------------------


class StatusUpsertRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=128, description="Status key, e.g. 'deploy:remembra-api'")
    value: str = Field(..., min_length=1, max_length=5000, description="Current value for the key")
    project_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl: str | None = Field(default=None, description="Optional TTL for this value, e.g. '30d'")


@router.post("/session/status", summary="Set the current value for a status key")
@limiter.limit("60/minute")
async def upsert_status(
    request: Request,
    body: StatusUpsertRequest,
    current_user: CurrentUser,
    response: Response,
) -> dict[str, Any]:
    """Store-by-key: the new value supersedes the previous value for the same
    (user, project, key). Re-sending the current value is a no-op.

    A status write is a relay event: stored atomically, never enriched, never
    billed in smart credits (plan relay burst limit and memory cap apply)."""
    _require(current_user, "memory:store")
    project = resolve_project_access(current_user, body.project_id) or "default"
    value = body.value
    await gate_write(request, response, current_user.user_id, [value], atomic=[True], project_ids=[project], relay=True)

    value, trust_score, checksum = screen_text(request, body.value)

    try:
        result = await _service(request).upsert_status(
            user_id=current_user.user_id,
            project_id=project,
            key=body.key,
            value=value,
            metadata=body.metadata,
            ttl=body.ttl,
            trust_score=trust_score,
            checksum=checksum,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    if result["changed"]:
        await record_relay_usage(request, current_user.user_id)
    return {"project_id": project, **result}


@router.get("/session/status", summary="Current status values for a project")
@limiter.limit("60/minute")
async def list_status(
    request: Request,
    current_user: CurrentUser,
    project_id: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, Any]:
    _require(current_user, "memory:recall")
    project = resolve_project_access(current_user, project_id) or "default"
    items = await _service(request).list_status(current_user.user_id, project)
    return {"project_id": project, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


@router.get("/timeline", summary="Chronological memories with a created_at range filter")
@limiter.limit("60/minute")
async def timeline(
    request: Request,
    current_user: CurrentUser,
    project_id: Annotated[str | None, Query(max_length=128)] = None,
    start: Annotated[datetime | None, Query(description="Inclusive lower bound on created_at (ISO 8601)")] = None,
    end: Annotated[datetime | None, Query(description="Exclusive upper bound on created_at (ISO 8601)")] = None,
    entity: Annotated[str | None, Query(max_length=256, description="Exact entity name or alias")] = None,
    memory_type: Annotated[list[str] | None, Query(description="Only these memory types")] = None,
    include_superseded: bool = False,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    _require(current_user, "memory:recall")
    if start is not None and end is not None and _naive_utc(start) >= _naive_utc(end):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start must be before end")
    project = resolve_project_access(current_user, project_id)
    result = await _service(request).timeline(
        user_id=current_user.user_id,
        project_id=project,
        start=start,
        end=end,
        entity=entity,
        memory_types=memory_type,
        include_superseded=include_superseded,
        limit=limit,
        offset=offset,
        newest_first=order == "desc",
    )
    return {
        "project_id": project,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "entity": entity,
        "limit": limit,
        "offset": offset,
        **result,
    }
