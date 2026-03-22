"""Admin endpoints – /api/v1/admin.

Provides audit log export, role management, and superadmin user management.
All endpoints require admin role or equivalent permissions.
Superadmin endpoints require owner_emails access.
"""

import csv
import io
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog

log = structlog.get_logger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.auth.rbac import Permission, Role, RoleManager
from remembra.auth.scopes import RequireAdmin, RequireAuditExport
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier, get_plan
from remembra.config import get_settings
from remembra.core.limiter import limiter
from remembra.security.audit import AuditLogger
from remembra.storage.database import Database

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_audit_logger(request: Request) -> AuditLogger:
    return request.app.state.audit_logger


def get_role_manager(request: Request) -> RoleManager | None:
    return getattr(request.app.state, "role_manager", None)


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_usage_meter(request: Request) -> UsageMeter | None:
    return getattr(request.app.state, "usage_meter", None)


async def get_user_manager(request: Request) -> UserManager:
    """Get UserManager instance (creates on demand like auth.py)."""
    db: Database = request.app.state.db
    settings = get_settings()
    return UserManager(db, settings.jwt_secret)


AuditLoggerDep = Annotated[AuditLogger, Depends(get_audit_logger)]
RoleManagerDep = Annotated[RoleManager | None, Depends(get_role_manager)]
DatabaseDep = Annotated[Database, Depends(get_database)]
UsageMeterDep = Annotated[UsageMeter | None, Depends(get_usage_meter)]
UserManagerDep = Annotated[UserManager, Depends(get_user_manager)]


async def require_superadmin(
    request: Request,
    current_user: CurrentUser,
) -> None:
    """Dependency that checks if the current user is a superadmin (in owner_emails)."""
    settings = get_settings()

    # Get user's email from the database
    db: Database = request.app.state.db
    user_data = await db.get_user_by_id(current_user.user_id)

    if not user_data:
        log.warning("superadmin_check_user_not_found", user_id=current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not found",
        )

    user_email = user_data.get("email", "").lower()
    owner_emails = [e.lower() for e in settings.owner_emails] if settings.owner_emails else []

    log.info("superadmin_check", user_id=current_user.user_id, email=user_email, owner_emails=owner_emails, is_admin=user_email in owner_emails)

    if user_email not in owner_emails:
        log.warning("superadmin_access_denied", user_id=current_user.user_id, email=user_email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access required. Contact support@remembra.dev",
        )


RequireSuperadmin = Annotated[None, Depends(require_superadmin)]


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
# Superadmin User Management Models
# ---------------------------------------------------------------------------


class UserListItem(BaseModel):
    """User summary for list view."""

    id: str
    email: str
    name: str | None
    plan: str
    memories_count: int
    api_keys_count: int
    created_at: str
    last_login_at: str | None
    is_active: bool


class UserListResponse(BaseModel):
    """Response for user list endpoint."""

    users: list[UserListItem]
    total: int


class UserDetailResponse(BaseModel):
    """Full user details including usage."""

    id: str
    email: str
    name: str | None
    plan: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    created_at: str
    last_login_at: str | None
    is_active: bool
    email_verified: bool
    totp_enabled: bool
    usage: dict[str, Any]
    limits: dict[str, Any]


class UpdateUserTierRequest(BaseModel):
    """Request to update a user's plan tier."""

    plan: str = Field(description="Plan tier: free, pro, team, enterprise")


class AdminResetPasswordResponse(BaseModel):
    """Response with temporary password after admin reset."""

    temporary_password: str
    message: str


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
    response_class=StreamingResponse,
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
) -> Response:
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
    response_class=StreamingResponse,
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
) -> Response:
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
        ) from None

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
@limiter.limit("60/minute")
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


def get_sleep_worker(request: Request) -> Any:
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
        ) from e


@router.get(
    "/sleep-time/status",
    summary="Get sleep-time consolidation status",
)
@limiter.limit("30/minute")
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


# ---------------------------------------------------------------------------
# Superadmin User Management Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=UserListResponse,
    summary="List all users (superadmin only)",
)
@limiter.limit("30/minute")
async def list_all_users(
    request: Request,
    db: DatabaseDep,
    usage_meter: UsageMeterDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    search: str | None = Query(None, description="Search by email or name"),
    plan: str | None = Query(None, description="Filter by plan tier"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> UserListResponse:
    """
    List all registered users with their plan and usage summary.

    **Superadmin only** - requires owner_emails access.
    """
    # Build query
    query = "SELECT * FROM users WHERE 1=1"
    params: list[Any] = []

    if search:
        query += " AND (email LIKE ? OR name LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.conn.execute(query, params)
    rows = await cursor.fetchall()

    # Get total count
    count_query = "SELECT COUNT(*) FROM users WHERE 1=1"
    count_params: list[Any] = []
    if search:
        count_query += " AND (email LIKE ? OR name LIKE ?)"
        count_params.extend([f"%{search}%", f"%{search}%"])

    count_cursor = await db.conn.execute(count_query, count_params)
    total = (await count_cursor.fetchone())[0]

    users = []
    for row in rows:
        user_dict = dict(row)
        user_id = user_dict["id"]

        # Get plan from cloud_tenants
        plan_tier = "free"
        if usage_meter:
            plan_tier = (await usage_meter.get_tenant_plan(user_id)).value

        # Filter by plan if specified
        if plan and plan_tier != plan.lower():
            continue

        # Get memory count
        mem_cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ?",
            (user_id,),
        )
        memories_count = (await mem_cursor.fetchone())[0]

        # Get API key count
        key_cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND active = TRUE",
            (user_id,),
        )
        api_keys_count = (await key_cursor.fetchone())[0]

        users.append(
            UserListItem(
                id=user_id,
                email=user_dict["email"],
                name=user_dict.get("name"),
                plan=plan_tier,
                memories_count=memories_count,
                api_keys_count=api_keys_count,
                created_at=user_dict["created_at"],
                last_login_at=user_dict.get("last_login_at"),
                is_active=user_dict.get("is_active", True),
            )
        )

    return UserListResponse(users=users, total=total)


@router.get(
    "/users/{user_id}",
    response_model=UserDetailResponse,
    summary="Get user details (superadmin only)",
)
@limiter.limit("30/minute")
async def get_user_details(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    usage_meter: UsageMeterDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> UserDetailResponse:
    """
    Get full user details including usage and plan limits.

    **Superadmin only** - requires owner_emails access.
    """
    user_data = await db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    # Get tenant info (plan, Stripe IDs)
    tenant = None
    plan_tier = PlanTier.FREE
    if usage_meter:
        tenant = await usage_meter.get_tenant(user_id)
        plan_tier = await usage_meter.get_tenant_plan(user_id)

    # Get usage snapshot
    usage = {}
    if usage_meter:
        snapshot = await usage_meter.get_usage_snapshot(user_id)
        usage = {
            "memories_stored": snapshot.memories_stored,
            "recalls_this_month": snapshot.recalls_this_month,
            "stores_this_month": snapshot.stores_this_month,
            "api_keys_active": snapshot.api_keys_active,
        }

    # Get plan limits
    limits_obj = get_plan(plan_tier)
    limits = {
        "max_memories": limits_obj.max_memories,
        "max_recalls_per_month": limits_obj.max_recalls_per_month,
        "max_stores_per_month": limits_obj.max_stores_per_month,
        "max_api_keys": limits_obj.max_api_keys,
        "has_webhooks": limits_obj.has_webhooks,
        "has_priority_support": limits_obj.has_priority_support,
    }

    return UserDetailResponse(
        id=user_data["id"],
        email=user_data["email"],
        name=user_data.get("name"),
        plan=plan_tier.value,
        stripe_customer_id=tenant.get("stripe_customer_id") if tenant else None,
        stripe_subscription_id=tenant.get("stripe_subscription_id") if tenant else None,
        created_at=user_data["created_at"],
        last_login_at=user_data.get("last_login_at"),
        is_active=user_data.get("is_active", True),
        email_verified=user_data.get("email_verified", False),
        totp_enabled=user_data.get("totp_enabled", False),
        usage=usage,
        limits=limits,
    )


@router.patch(
    "/users/{user_id}/tier",
    summary="Update user's plan tier (superadmin only)",
)
@limiter.limit("10/minute")
async def update_user_tier(
    request: Request,
    user_id: str,
    body: UpdateUserTierRequest,
    db: DatabaseDep,
    usage_meter: UsageMeterDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> dict[str, Any]:
    """
    Update a user's plan tier manually (bypasses Stripe).

    Use this for:
    - Upgrading users to Pro/Team/Enterprise manually
    - Handling enterprise sales
    - Fixing billing issues

    **Superadmin only** - requires owner_emails access.
    """
    # Validate user exists
    user_data = await db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    # Validate plan tier
    try:
        new_plan = PlanTier(body.plan.lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid plan: {body.plan}. Valid plans: free, pro, team, enterprise",
        ) from None

    if usage_meter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud features not enabled",
        )

    # Register/update tenant with new plan
    await usage_meter.register_tenant(user_id, plan=new_plan)

    return {
        "status": "updated",
        "user_id": user_id,
        "email": user_data["email"],
        "new_plan": new_plan.value,
        "message": f"User upgraded to {new_plan.value} plan",
    }


@router.delete(
    "/users/{user_id}",
    summary="Delete user and all data (superadmin only)",
)
@limiter.limit("5/minute")
async def delete_user(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    confirm: bool = Query(False, description="Must be true to confirm deletion"),
) -> dict[str, Any]:
    """
    Permanently delete a user and ALL their data.

    This is a destructive operation that removes:
    - User account
    - All memories
    - All API keys
    - All entities and relationships
    - All usage records

    **Superadmin only** - requires owner_emails access.
    **Requires confirm=true** to execute.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add ?confirm=true to confirm permanent deletion",
        )

    user_data = await db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    email = user_data["email"]

    # Delete in order to handle foreign keys
    # 1. Delete memories and related data
    await db.conn.execute(
        "DELETE FROM memory_entities WHERE memory_id IN (SELECT id FROM memories WHERE user_id = ?)",
        (user_id,),
    )
    await db.conn.execute("DELETE FROM memories_fts WHERE user_id = ?", (user_id,))
    await db.conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))

    # 2. Delete entities and relationships
    await db.conn.execute(
        "DELETE FROM relationships WHERE from_entity_id IN (SELECT id FROM entities WHERE user_id = ?)",
        (user_id,),
    )
    await db.conn.execute(
        "DELETE FROM relationships WHERE to_entity_id IN (SELECT id FROM entities WHERE user_id = ?)",
        (user_id,),
    )
    await db.conn.execute("DELETE FROM entities WHERE user_id = ?", (user_id,))

    # 3. Delete API keys
    await db.conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))

    # 4. Delete audit logs for user
    await db.conn.execute("DELETE FROM audit_log WHERE user_id = ?", (user_id,))

    # 5. Delete cloud tenant record
    await db.conn.execute("DELETE FROM cloud_tenants WHERE user_id = ?", (user_id,))
    await db.conn.execute("DELETE FROM cloud_usage_daily WHERE user_id = ?", (user_id,))

    # 6. Delete password reset tokens
    await db.conn.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))

    # 7. Delete token blacklist entries
    await db.conn.execute("DELETE FROM token_blacklist WHERE user_id = ?", (user_id,))

    # 8. Finally delete user
    await db.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    await db.conn.commit()

    return {
        "status": "deleted",
        "user_id": user_id,
        "email": email,
        "message": "User and all associated data permanently deleted",
    }


@router.post(
    "/users/{user_id}/reset-password",
    response_model=AdminResetPasswordResponse,
    summary="Admin reset user password (superadmin only)",
)
@limiter.limit("10/minute")
async def admin_reset_password(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    user_manager: UserManagerDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> AdminResetPasswordResponse:
    """
    Reset a user's password to a temporary random password.

    The temporary password is returned in the response and should
    be communicated to the user securely. They should change it
    immediately upon login.

    **Superadmin only** - requires owner_emails access.
    """
    user_data = await db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    # Generate a random temporary password
    temp_password = secrets.token_urlsafe(12)

    # Hash and update
    password_hash = user_manager.hash_password(temp_password)
    await db.update_user_password(user_id, password_hash)

    return AdminResetPasswordResponse(
        temporary_password=temp_password,
        message=f"Password reset for {user_data['email']}. User must change password on next login.",
    )


@router.post(
    "/users/{user_id}/activate",
    summary="Activate/deactivate user account (superadmin only)",
)
@limiter.limit("10/minute")
async def toggle_user_active(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    active: bool = Query(..., description="Set account active status"),
) -> dict[str, Any]:
    """
    Activate or deactivate a user account.

    Deactivated users cannot log in but their data is preserved.

    **Superadmin only** - requires owner_emails access.
    """
    user_data = await db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    await db.conn.execute(
        "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
        (active, datetime.now(UTC).isoformat(), user_id),
    )
    await db.conn.commit()

    status_str = "activated" if active else "deactivated"
    return {
        "status": status_str,
        "user_id": user_id,
        "email": user_data["email"],
        "is_active": active,
    }


@router.get(
    "/stats",
    summary="Get platform statistics (superadmin only)",
)
@limiter.limit("30/minute")
async def get_platform_stats(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> dict[str, Any]:
    """
    Get overall platform statistics.

    **Superadmin only** - requires owner_emails access.
    """
    # User stats
    user_cursor = await db.conn.execute("SELECT COUNT(*) FROM users")
    total_users = (await user_cursor.fetchone())[0]

    active_cursor = await db.conn.execute("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
    active_users = (await active_cursor.fetchone())[0]

    # Memory stats
    mem_cursor = await db.conn.execute("SELECT COUNT(*) FROM memories")
    total_memories = (await mem_cursor.fetchone())[0]

    # API key stats
    key_cursor = await db.conn.execute("SELECT COUNT(*) FROM api_keys WHERE active = TRUE")
    active_keys = (await key_cursor.fetchone())[0]

    # Plan distribution
    plan_cursor = await db.conn.execute("""
        SELECT plan, COUNT(*) as count 
        FROM cloud_tenants 
        GROUP BY plan
    """)
    plan_rows = await plan_cursor.fetchall()
    plan_distribution = {row["plan"]: row["count"] for row in plan_rows}

    # Add users without tenant record as free
    tenants_count = sum(plan_distribution.values())
    if tenants_count < total_users:
        plan_distribution["free"] = plan_distribution.get("free", 0) + (total_users - tenants_count)

    # Recent signups (last 7 days)
    week_ago = (datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
    recent_cursor = await db.conn.execute(
        "SELECT COUNT(*) FROM users WHERE created_at >= ?",
        (week_ago,),
    )
    recent_signups = (await recent_cursor.fetchone())[0]

    return {
        "users": {
            "total": total_users,
            "active": active_users,
            "recent_signups_7d": recent_signups,
        },
        "memories": {
            "total": total_memories,
        },
        "api_keys": {
            "active": active_keys,
        },
        "plans": plan_distribution,
    }


@router.post(
    "/rebuild-vectors",
    summary="Rebuild missing vector embeddings for memories (superadmin only)",
)
@limiter.limit("1/minute")
async def rebuild_vectors(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    user_id: str | None = Query(default=None, description="Scope to specific user"),
    dry_run: bool = Query(default=True, description="Preview without making changes"),
) -> dict[str, Any]:
    """
    Find memories in SQLite that are missing from Qdrant and re-embed them.

    This fixes memories that were stored but not properly vectorized.

    **Superadmin only** - requires owner_emails access.

    Args:
        user_id: Optional - scope to a specific user's memories
        dry_run: If True, only report what would be done without making changes
    """
    from remembra.services.memory import MemoryService

    # Get services from app state
    memory_service: MemoryService = request.app.state.memory_service
    qdrant = memory_service.qdrant
    embeddings = memory_service.embeddings

    # Build query
    if user_id:
        cursor = await db.conn.execute(
            "SELECT id, user_id, project_id, content FROM memories WHERE user_id = ?",
            (user_id,),
        )
    else:
        cursor = await db.conn.execute("SELECT id, user_id, project_id, content FROM memories")

    rows = await cursor.fetchall()

    missing = []
    rebuilt = []
    errors = []

    for row in rows:
        mem_id, mem_user_id, _mem_project_id, content = row["id"], row["user_id"], row["project_id"], row["content"]

        # Check if exists in Qdrant (use get_by_id)
        try:
            existing = await qdrant.get_by_id(mem_id)
            exists = existing is not None
        except Exception:
            exists = False

        if not exists:
            missing.append(
                {
                    "id": mem_id,
                    "user_id": mem_user_id,
                    "content_preview": content[:100] if content else "",
                }
            )

            if not dry_run:
                try:
                    from remembra.core.models import Memory

                    # Get full memory data from SQLite
                    full_cursor = await db.conn.execute(
                        """SELECT id, user_id, project_id, content, metadata, 
                                  created_at, expires_at, extracted_facts
                           FROM memories WHERE id = ?""",
                        (mem_id,),
                    )
                    mem_row = await full_cursor.fetchone()

                    if mem_row:
                        # Generate embedding
                        embedding = await embeddings.embed(content)

                        # Create Memory object and upsert to Qdrant
                        memory = Memory(
                            id=mem_row["id"],
                            user_id=mem_row["user_id"],
                            project_id=mem_row["project_id"],
                            content=mem_row["content"],
                            extracted_facts=json.loads(mem_row["extracted_facts"] or "[]"),
                            entities=[],
                            embedding=embedding,
                            metadata=json.loads(mem_row["metadata"] or "{}"),
                            created_at=datetime.fromisoformat(mem_row["created_at"]),
                            expires_at=datetime.fromisoformat(mem_row["expires_at"]) if mem_row["expires_at"] else None,
                        )
                        await qdrant.upsert(memory)
                        rebuilt.append(mem_id)
                except Exception as e:
                    errors.append({"id": mem_id, "error": str(e)})

    return {
        "dry_run": dry_run,
        "total_memories_checked": len(rows),
        "missing_from_qdrant": len(missing),
        "rebuilt": len(rebuilt) if not dry_run else 0,
        "errors": len(errors) if not dry_run else 0,
        "missing_memories": missing[:50],  # Limit preview
        "error_details": errors[:10] if errors else [],
    }


@router.post(
    "/users/{user_id}/sync-team-plan",
    summary="Sync team plans with user's billing (superadmin only)",
)
@limiter.limit("10/minute")
async def sync_user_team_plans(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
) -> dict[str, Any]:
    """
    Sync all teams owned by a user to match their billing plan.

    Use this to fix teams that are out of sync with the owner's
    actual subscription (e.g., team shows "Pro" but billing is "Enterprise").

    **Superadmin only** - requires owner_emails access.
    """
    from remembra.cloud.plans import PlanTier, get_plan

    # Get user
    user_data = await db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    # Get user's current billing plan
    usage_meter = getattr(request.app.state, "usage_meter", None)
    if not usage_meter:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud features not enabled",
        )

    tenant = await usage_meter.get_tenant(user_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No billing record for user {user_id}",
        )

    plan_str = tenant.get("plan", "free")
    plan_tier = PlanTier(plan_str)
    plan_limits = get_plan(plan_tier)

    # Get team manager
    team_manager = getattr(request.app.state, "team_manager", None)
    if not team_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Team collaboration not enabled",
        )

    # Update all teams owned by this user
    teams_updated = await team_manager.update_owner_teams_plan(
        owner_id=user_id,
        plan=plan_str,
        max_seats=plan_limits.max_users,
    )

    return {
        "status": "synced",
        "user_id": user_id,
        "email": user_data["email"],
        "billing_plan": plan_str,
        "max_seats": plan_limits.max_users,
        "teams_updated": teams_updated,
        "message": f"Updated {teams_updated} team(s) to {plan_str} plan",
    }
