"""Admin endpoints – /api/v1/admin.

Provides audit log export and role management.
All endpoints require admin role or equivalent permissions.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.auth.rbac import Permission, Role, RoleManager
from remembra.auth.scopes import RequireAdmin, RequireAuditExport
from remembra.core.limiter import limiter
from remembra.security.audit import AuditLogger

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_audit_logger(request: Request) -> AuditLogger:
    return request.app.state.audit_logger


def get_role_manager(request: Request) -> RoleManager | None:
    return getattr(request.app.state, "role_manager", None)


AuditLoggerDep = Annotated[AuditLogger, Depends(get_audit_logger)]
RoleManagerDep = Annotated[RoleManager | None, Depends(get_role_manager)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AssignRoleRequest(BaseModel):
    api_key_id: str = Field(description="API key ID to assign the role to")
    role: str = Field(description="Role: admin, editor, viewer")
    scopes: list[str] | None = Field(
        None,
        description="Optional scope restrictions (permission whitelist)",
    )
    project_ids: list[str] | None = Field(
        None,
        description="Optional project restrictions (empty = all projects)",
    )


class RoleListResponse(BaseModel):
    roles: list[dict[str, Any]]
    total: int


class AuditListResponse(BaseModel):
    events: list[dict[str, Any]]
    total: int


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/audit",
    response_model=AuditListResponse,
    summary="List recent audit events",
)
@limiter.limit("30/minute")
async def list_audit_events(
    request: Request,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    _perm: RequireAuditExport,
    user_id: str | None = Query(None, description="Filter by user ID"),
    action: str | None = Query(None, description="Filter by action type"),
    limit: int = Query(100, ge=1, le=1000),
) -> AuditListResponse:
    """List recent audit events. Requires admin:export permission."""
    events = await audit_logger.get_recent_events(
        user_id=user_id,
        action=action,
        limit=limit,
    )
    return AuditListResponse(events=events, total=len(events))


@router.get(
    "/audit/export/json",
    summary="Export audit log as JSON",
)
@limiter.limit("5/minute")
async def export_audit_json(
    request: Request,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    _perm: RequireAuditExport,
    user_id: str | None = Query(None),
    action: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
) -> StreamingResponse:
    """Export audit events as JSON. Requires admin:export permission."""
    events = await audit_logger.get_recent_events(
        user_id=user_id,
        action=action,
        limit=limit,
    )
    content = json.dumps(events, indent=2, default=str)

    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=audit_log.json",
        },
    )


@router.get(
    "/audit/export/csv",
    summary="Export audit log as CSV",
)
@limiter.limit("5/minute")
async def export_audit_csv(
    request: Request,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    _perm: RequireAuditExport,
    user_id: str | None = Query(None),
    action: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
) -> StreamingResponse:
    """Export audit events as CSV. Requires admin:export permission."""
    events = await audit_logger.get_recent_events(
        user_id=user_id,
        action=action,
        limit=limit,
    )

    output = io.StringIO()
    if events:
        fields = list(events[0].keys())
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for event in events:
            writer.writerow({k: str(v) if v is not None else "" for k, v in event.items()})
    else:
        output.write("id,timestamp,user_id,action,api_key_id,resource_id,ip_address,success,error_message\n")

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=audit_log.csv",
        },
    )


# ---------------------------------------------------------------------------
# Role management endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/roles",
    response_model=RoleListResponse,
    summary="List role assignments for your keys",
)
@limiter.limit("30/minute")
async def list_roles(
    request: Request,
    role_manager: RoleManagerDep,
    current_user: CurrentUser,
    _perm: RequireAdmin,
) -> RoleListResponse:
    """List all role assignments for the current user's API keys."""
    if role_manager is None:
        return RoleListResponse(roles=[], total=0)
    roles = await role_manager.list_roles(current_user.user_id)
    return RoleListResponse(roles=roles, total=len(roles))


@router.post(
    "/roles",
    summary="Assign a role to an API key",
)
@limiter.limit("10/minute")
async def assign_role(
    request: Request,
    body: AssignRoleRequest,
    role_manager: RoleManagerDep,
    current_user: CurrentUser,
    _perm: RequireAdmin,
) -> dict[str, Any]:
    """Assign or update a role on an API key. Requires admin role."""
    if role_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RBAC is not enabled on this instance.",
        )

    try:
        role = Role(body.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {body.role}. Valid roles: admin, editor, viewer",
        )

    result = await role_manager.assign_role(
        api_key_id=body.api_key_id,
        role=role,
        scopes=body.scopes,
        project_ids=body.project_ids,
    )

    return {
        "api_key_id": result.api_key_id,
        "role": result.role.value,
        "scopes": result.scopes,
        "project_ids": result.project_ids,
        "permissions": [p.value for p in result.permissions],
    }


@router.delete(
    "/roles/{api_key_id}",
    summary="Remove role from API key",
)
@limiter.limit("10/minute")
async def remove_role(
    request: Request,
    api_key_id: str,
    role_manager: RoleManagerDep,
    current_user: CurrentUser,
    _perm: RequireAdmin,
) -> dict[str, str]:
    """Remove a role assignment from an API key (reverts to default editor)."""
    if role_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RBAC is not enabled on this instance.",
        )

    deleted = await role_manager.remove_role(api_key_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No role assignment found for key {api_key_id}",
        )
    return {"status": "removed", "api_key_id": api_key_id}


@router.get(
    "/permissions",
    summary="List all available permissions",
)
async def list_permissions(request: Request) -> dict[str, Any]:
    """List all available permissions and default role mappings."""
    return {
        "permissions": [p.value for p in Permission],
        "roles": {
            role.value: [p.value for p in perms]
            for role, perms in {
                Role.ADMIN: set(Permission),
                Role.EDITOR: {
                    Permission.MEMORY_STORE,
                    Permission.MEMORY_RECALL,
                    Permission.MEMORY_DELETE,
                    Permission.KEY_LIST,
                    Permission.ENTITY_READ,
                    Permission.WEBHOOK_MANAGE,
                    Permission.CONFLICT_MANAGE,
                },
                Role.VIEWER: {
                    Permission.MEMORY_RECALL,
                    Permission.KEY_LIST,
                    Permission.ENTITY_READ,
                },
            }.items()
        },
    }


# ---------------------------------------------------------------------------
# Sleep-Time Compute (Phase 3)
# ---------------------------------------------------------------------------


def get_sleep_worker(request: Request):
    """Get sleep-time worker from app state."""
    return getattr(request.app.state, "sleep_worker", None)


@router.post(
    "/sleep-time/run",
    summary="Trigger sleep-time consolidation",
)
@limiter.limit("1/minute")
async def trigger_consolidation(
    request: Request,
    current_user: CurrentUser,
    _perm: RequireAdmin,
    user_id: str | None = Query(None, description="Consolidate specific user only"),
) -> dict[str, Any]:
    """
    Manually trigger sleep-time consolidation.
    
    This runs the background memory improvement process:
    - Deduplication across sessions
    - Entity resolution
    - Importance rescoring
    - Decay cleanup
    
    **Admin only.** Rate limited to 1 per minute.
    """
    sleep_worker = get_sleep_worker(request)
    
    if sleep_worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sleep-time compute is not enabled. Set REMEMBRA_SLEEP_TIME_ENABLED=true",
        )
    
    try:
        report = await sleep_worker.run_consolidation(user_id=user_id)
        
        return {
            "status": "completed",
            "started_at": report.started_at.isoformat(),
            "completed_at": report.completed_at.isoformat() if report.completed_at else None,
            "stats": {
                "memories_scanned": report.memories_scanned,
                "duplicates_merged": report.duplicates_merged,
                "entities_resolved": report.entities_resolved,
                "relationships_discovered": report.relationships_discovered,
                "importance_rescored": report.importance_rescored,
                "memories_decayed": report.memories_decayed,
            },
            "errors": report.errors,
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Consolidation failed: {str(e)}",
        )


@router.get(
    "/sleep-time/status",
    summary="Get sleep-time consolidation status",
)
async def consolidation_status(
    request: Request,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """
    Get the status of sleep-time consolidation.
    
    Returns:
    - Whether sleep-time compute is enabled
    - Last run timestamp
    - Whether a consolidation is currently running
    """
    sleep_worker = get_sleep_worker(request)
    
    if sleep_worker is None:
        return {
            "enabled": False,
            "message": "Sleep-time compute is not enabled",
        }
    
    return {
        "enabled": True,
        "running": sleep_worker.running,
        "last_run": sleep_worker.last_run.isoformat() if sleep_worker.last_run else None,
    }
