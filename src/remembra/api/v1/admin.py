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
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from remembra.auth.middleware import AuthenticatedUser, CurrentUser
from remembra.auth.rbac import ROLE_LEVEL, SYNTHETIC_KEY_IDS, KeyRole, Permission, Role, RoleManager
from remembra.auth.scopes import RequireAdmin, RequireAuditExport
from remembra.auth.superadmin import RequireSuperadmin, is_superadmin
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier, get_plan
from remembra.config import get_settings
from remembra.core.limiter import limiter
from remembra.security.audit import AuditAction, AuditLogger
from remembra.services.relay_metrics import relay_metrics
from remembra.storage.database import Database

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _parse_audit_action(action: str | None) -> AuditAction | None:
    """Coerce a query-string action filter into an ``AuditAction``.

    Returns ``None`` when no filter is supplied. Raises a 400 for an
    unknown action so callers get a clean error instead of a 500.
    """
    if action is None:
        return None
    try:
        return AuditAction(action)
    except ValueError as exc:
        valid = ", ".join(a.value for a in AuditAction)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown audit action: {action}. Valid: {valid}",
        ) from exc


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_audit_logger(request: Request) -> AuditLogger:
    logger: AuditLogger = request.app.state.audit_logger
    return logger


def get_role_manager(request: Request) -> RoleManager | None:
    return getattr(request.app.state, "role_manager", None)


def get_database(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


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
# Tenant scoping helpers
# ---------------------------------------------------------------------------


async def _audit_scope(request: Request, current_user: AuthenticatedUser, requested_user_id: str | None) -> str | None:
    """Tenant admins only ever see their own tenant's audit trail.

    Platform superadmins may filter by any user id (or see all when omitted).
    """
    if await is_superadmin(request, current_user):
        return requested_user_id
    if requested_user_id and requested_user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audit access is limited to your own account.",
        )
    return current_user.user_id


async def _require_own_key(request: Request, current_user: AuthenticatedUser, api_key_id: str) -> None:
    """404 unless ``api_key_id`` is a real key owned by the caller."""
    if api_key_id in SYNTHETIC_KEY_IDS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not an API key.")
    key_manager = getattr(request.app.state, "api_key_manager", None)
    key_info = await key_manager.get_key_info(api_key_id) if key_manager is not None else None
    if key_info is None or key_info.user_id != current_user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Key {api_key_id} not found")


def _enforce_no_escalation(
    caller: KeyRole,
    role: Role,
    scopes: list[str] | None,
    project_ids: list[str] | None,
) -> None:
    """A role assignment may never grant more than the caller holds."""
    if ROLE_LEVEL[role] > ROLE_LEVEL[caller.role]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot grant a role above your own.")
    if caller.scopes and (not scopes or not set(scopes) <= set(caller.scopes)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scoped keys may only grant a subset of their own scopes.",
        )
    if caller.project_ids and (not project_ids or not set(project_ids) <= set(caller.project_ids)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project-restricted keys may only grant a subset of their own projects.",
        )


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
        user_id=await _audit_scope(request, current_user, user_id),
        action=_parse_audit_action(action),
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
        user_id=await _audit_scope(request, current_user, user_id),
        action=_parse_audit_action(action),
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
        user_id=await _audit_scope(request, current_user, user_id),
        action=_parse_audit_action(action),
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
    """Assign or update a role on one of the caller's own API keys. Requires admin role.

    The assignment can never exceed the caller's own role, scopes, or projects.
    """
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

    if body.scopes:
        valid_perms = {p.value for p in Permission}
        unknown = [scope for scope in body.scopes if scope not in valid_perms]
        if unknown:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown scopes: {unknown}")

    await _require_own_key(request, current_user, body.api_key_id)
    _enforce_no_escalation(_perm, role, body.scopes, body.project_ids)

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

    await _require_own_key(request, current_user, api_key_id)
    # Removing a role reverts the key to an unrestricted editor.
    _enforce_no_escalation(_perm, Role.EDITOR, None, None)

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

    # Tenant admins consolidate only their own data; platform-wide or other-tenant
    # runs are reserved for superadmins.
    if not await is_superadmin(request, current_user):
        if user_id and user_id != current_user.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only run consolidation for your own account.",
            )
        user_id = current_user.user_id

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
        log.error("consolidation_failed", error_type=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Consolidation failed. See server logs.",
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
    count_row = await count_cursor.fetchone()
    assert count_row is not None  # COUNT(*) always returns exactly one row
    total = count_row[0]

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
        mem_row = await mem_cursor.fetchone()
        assert mem_row is not None  # COUNT(*) always returns exactly one row
        memories_count = mem_row[0]

        # Get API key count
        key_cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND active = TRUE",
            (user_id,),
        )
        key_row = await key_cursor.fetchone()
        assert key_row is not None  # COUNT(*) always returns exactly one row
        api_keys_count = key_row[0]

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
    usage: dict[str, Any] = {}
    if usage_meter:
        snapshot = await usage_meter.get_usage_snapshot(user_id)
        usage = {
            "memories_stored": snapshot.memories_stored,
            "recalls_this_month": snapshot.recalls_this_month,
            "stores_this_month": snapshot.stores_this_month,
            "api_keys_active": snapshot.api_keys_active,
        }

    # Get plan limits (effective: seat-scaled, notice-aware) and per-tenant AI spend
    limits_obj = get_plan(plan_tier)
    limits = {
        "max_memories": limits_obj.max_memories,
        "max_recalls_per_month": limits_obj.max_recalls_per_month,
        "smart_credits_per_month": limits_obj.max_smart_credits_per_month,
        "max_api_keys": limits_obj.max_api_keys,
        "has_webhooks": limits_obj.has_webhooks,
        "has_priority_support": limits_obj.has_priority_support,
    }
    if usage_meter:
        account = await usage_meter.get_account(user_id)
        balance = await usage_meter.get_credit_balance(account)
        limits["max_memories"] = account.memory_cap
        limits["smart_credits"] = account.credit_limit
        usage["smart_credits_used"] = balance.used
        usage["smart_credits_reserved"] = balance.reserved
        usage["llm_usd_this_period"] = round(balance.llm_usd, 4)

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


# deleted_at written by the superadmin hard delete: long past any grace period, so the erasure job
# erases the account on its next run if the immediate erasure fails.
_ERASE_NOW_STAMP = "1970-01-01T00:00:00+00:00"


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
    force_billing: Literal["skip"] | None = Query(
        None,
        description=(
            "skip: delete without asking Paddle to cancel anything (when Paddle refuses and the subscription "
            "was checked by hand). The owner alert lists what to cancel in Paddle."
        ),
    ),
) -> dict[str, Any]:
    """
    Permanently delete a user and ALL their data, now.

    Works for dashboard users and for API-signup tenants (``POST /cloud/signup``),
    which have no ``users`` row. Cancels every Paddle subscription of the
    account that can still bill (502 and nothing deleted if the billing
    provider cannot confirm it, unless ``force_billing=skip``), then erases
    every row the account owns in every table and its Qdrant vectors, and
    writes a content-free ``account_erased`` audit receipt.

    **Superadmin only** - requires owner_emails access.
    **Requires confirm=true** to execute.
    """
    from remembra.account.deletion import BillingCancelError, cancel_billing, mark_deleted, skip_billing
    from remembra.account.erasure import eraser_for

    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add ?confirm=true to confirm permanent deletion",
        )

    meter = getattr(request.app.state, "usage_meter", None)
    user_data = await db.get_user_by_id(user_id)
    tenant = await meter.get_tenant(user_id) if meter is not None and not user_data else None
    if not user_data and not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    email = (user_data or tenant or {}).get("email")
    cancelled: list[str] = []
    if force_billing == "skip":
        await skip_billing(meter, user_id, app_state=request.app.state)
    else:
        try:
            cancelled = (await cancel_billing(meter, user_id, app_state=request.app.state)).cancelled
        except BillingCancelError as e:
            detail = str(e) if e.transient else f"{e} Superadmin: add force_billing=skip once Paddle is checked by hand."
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from e

    if user_data:
        # Deactivate and stamp the deletion before erasing, dated so the erasure job treats it as due:
        # if the erasure below fails, the account is not left active with its billing cancelled, and
        # the job (or a retry of this request) finishes it.
        await mark_deleted(db, meter, user_id)
        await db.conn.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (_ERASE_NOW_STAMP, user_id))
        await db.conn.commit()
    try:
        receipt = await eraser_for(request.app.state).erase(user_id)
    except Exception as e:
        log.error("admin_account_erasure_failed", user_id=user_id, error_type=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Billing was handled ({len(cancelled)} subscription(s) cancelled"
                + (", Paddle skipped" if force_billing == "skip" else "")
                + f") but the erasure failed ({type(e).__name__}). "
                + (
                    "The account is deactivated and marked for erasure: the erasure job retries on its next run, "
                    "or repeat this request."
                    if user_data
                    else "Repeat this request to finish (cancelling billing again is safe)."
                )
            ),
        ) from e

    return {
        "status": "deleted",
        "user_id": user_id,
        "email": email,
        "account_kind": "dashboard" if user_data else "api_tenant",
        "subscriptions_cancelled": len(cancelled),
        "billing_skipped": force_billing == "skip",
        "rows_deleted": receipt.total_rows,
        "vectors_deleted": receipt.vectors,
        "receipt": f"sha256:{receipt.digest}",
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

    if active:
        # Also undoes a self-serve deletion still inside its grace period.
        await db.conn.execute(
            "UPDATE users SET is_active = ?, deleted_at = NULL, updated_at = ? WHERE id = ?",
            (active, datetime.now(UTC).isoformat(), user_id),
        )
    else:
        await db.conn.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (active, datetime.now(UTC).isoformat(), user_id),
        )
    await db.conn.commit()

    if not active:
        # Deactivation must cut API access too, not just dashboard login.
        from remembra.auth.users import revoke_user_access

        await revoke_user_access(db, user_id)

    status_str = "activated" if active else "deactivated"
    return {
        "status": status_str,
        "user_id": user_id,
        "email": user_data["email"],
        "is_active": active,
    }


class BillingFlagRow(BaseModel):
    user_id: str
    email: str | None = None
    plan: str | None = None
    billing_flag: str
    billing_interval: str | None = None
    seats: int | None = None
    founding: bool = False
    paddle_customer_id: str | None = None
    paddle_subscription_id: str | None = None
    updated_at: str | None = None


class BillingFlagsResponse(BaseModel):
    flags: list[BillingFlagRow]
    total: int


@router.get(
    "/billing-flags",
    response_model=BillingFlagsResponse,
    summary="Accounts with a billing flag to review (superadmin only)",
)
@limiter.limit("30/minute")
async def list_billing_flags(
    request: Request,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    usage_meter: UsageMeterDep,
) -> BillingFlagsResponse:
    """Every account whose billing needs a human: a second subscription, a Founding
    payment past seat 100, Team seats below the minimum, a refund or chargeback.
    Each flag also sent the owner an alert when it was set."""
    if usage_meter is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Cloud features not enabled")
    rows = await usage_meter.list_billing_flags()
    flags = [
        BillingFlagRow(
            user_id=str(r["user_id"]),
            email=r.get("email"),
            plan=r.get("plan"),
            billing_flag=str(r["billing_flag"]),
            billing_interval=r.get("billing_interval"),
            seats=r.get("seats"),
            founding=bool(r.get("founding")),
            paddle_customer_id=r.get("stripe_customer_id"),
            paddle_subscription_id=r.get("stripe_subscription_id"),
            updated_at=r.get("updated_at"),
        )
        for r in rows
    ]
    return BillingFlagsResponse(flags=flags, total=len(flags))


@router.delete(
    "/billing-flags/{user_id}",
    summary="Clear an account's billing flag after review (superadmin only)",
)
@limiter.limit("30/minute")
async def clear_billing_flag(
    request: Request,
    user_id: str,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    usage_meter: UsageMeterDep,
) -> dict[str, Any]:
    if usage_meter is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Cloud features not enabled")
    tenant = await usage_meter.get_tenant(user_id)
    if tenant is None or not tenant.get("billing_flag"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No billing flag on that account")
    await usage_meter.set_billing_flag(user_id, None)
    log.info("billing_flag_cleared", user_id=user_id, flag=tenant.get("billing_flag"), by=current_user.user_id)
    return {"status": "cleared", "user_id": user_id, "flag": tenant.get("billing_flag")}


class DeactivatedAccountRow(BaseModel):
    user_id: str
    email: str | None = None
    created_at: str | None = None
    deactivated_at: str | None = Field(None, description="Last update of the row: the deactivation, unless changed since")
    plan: str | None = None
    paddle_subscription_id: str | None = Field(None, description="Paid subscription the account still holds, if any")
    paddle_customer_id: str | None = None


class DeactivatedAccountsResponse(BaseModel):
    accounts: list[DeactivatedAccountRow]
    total: int
    note: str


@router.get(
    "/deactivated-accounts",
    response_model=DeactivatedAccountsResponse,
    summary="Deactivated accounts with no erasure scheduled (superadmin only)",
)
@limiter.limit("30/minute")
async def list_deactivated_accounts(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    usage_meter: UsageMeterDep,
) -> DeactivatedAccountsResponse:
    """Accounts that are deactivated but will never be erased: ``is_active`` false, ``deleted_at`` empty.

    Before R-11, "Delete account" in Settings only deactivated the account
    (no ``deleted_at``, billing not cancelled), which looks the same as a
    superadmin deactivation. Review each: a self-deletion goes to
    ``POST /admin/deactivated-accounts/{id}/schedule-erasure?confirm=true``
    (cancels billing, then the erasure job removes it after the grace period)
    or ``DELETE /admin/users/{id}?confirm=true`` (now).
    """
    cursor = await db.conn.execute(
        "SELECT id, email, created_at, updated_at FROM users"
        " WHERE (is_active = 0 OR is_active = 'false') AND deleted_at IS NULL ORDER BY updated_at"
    )
    accounts: list[DeactivatedAccountRow] = []
    for user_id, email, created_at, updated_at in await cursor.fetchall():
        tenant = await usage_meter.get_tenant(str(user_id)) if usage_meter is not None else None
        accounts.append(
            DeactivatedAccountRow(
                user_id=str(user_id),
                email=email,
                created_at=str(created_at) if created_at else None,
                deactivated_at=str(updated_at) if updated_at else None,
                plan=(tenant or {}).get("plan"),
                paddle_subscription_id=UsageMeter.active_subscription_id(tenant),
                paddle_customer_id=(tenant or {}).get("stripe_customer_id"),
            )
        )
    return DeactivatedAccountsResponse(
        accounts=accounts,
        total=len(accounts),
        note=(
            "Self-deletions made before R-11 and superadmin deactivations look alike here. Before release, "
            "'Delete account' logged account_deactivated with the user id; a superadmin deactivation did not."
        ),
    )


@router.post(
    "/deactivated-accounts/{user_id}/schedule-erasure",
    summary="Treat a deactivated account as a self-serve deletion (superadmin only)",
)
@limiter.limit("10/minute")
async def schedule_erasure_of_deactivated(
    request: Request,
    user_id: str,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    confirm: bool = Query(False, description="Must be true"),
) -> dict[str, Any]:
    """Cancel the account's billing and stamp ``deleted_at``: the erasure job removes it after the grace period.

    Only for a deactivated account with no erasure scheduled (see
    ``GET /admin/deactivated-accounts``). Undo inside the grace period with
    ``POST /admin/users/{id}/activate?active=true``.
    """
    from datetime import timedelta

    from remembra.account.deletion import BillingCancelError, cancel_billing, mark_deleted

    if not confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Add ?confirm=true to schedule erasure")
    cursor = await db.conn.execute("SELECT is_active, deleted_at FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found")
    if row[1] is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Erasure is already scheduled")
    if row[0] and str(row[0]).lower() not in ("0", "false"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The account is active; deactivate it first or use DELETE /admin/users/{id}",
        )
    meter = getattr(request.app.state, "usage_meter", None)
    try:
        cancelled = (await cancel_billing(meter, user_id, app_state=request.app.state)).cancelled
    except BillingCancelError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    deleted_at = await mark_deleted(db, meter, user_id)
    erase_after = deleted_at + timedelta(days=get_settings().account_erasure_grace_days)
    log.info("deactivated_account_erasure_scheduled", user_id=user_id, by=current_user.user_id)
    return {
        "status": "scheduled",
        "user_id": user_id,
        "subscriptions_cancelled": len(cancelled),
        "deleted_at": deleted_at.isoformat(),
        "erasure_after": erase_after.isoformat(),
    }


@router.get(
    "/relay/metrics",
    summary="Relay activation funnel and weekly pickups / handoff health (superadmin only)",
)
@limiter.limit("30/minute")
async def get_relay_metrics(
    request: Request,
    db: DatabaseDep,
    current_user: CurrentUser,
    _superadmin: RequireSuperadmin,
    weeks: Annotated[int, Query(ge=1, le=52, description="Length of the weekly series")] = 8,
    since: Annotated[
        str | None, Query(max_length=40, description="Only users who signed up at or after this ISO date/time")
    ] = None,
) -> dict[str, Any]:
    """Activation (True North: a handoff picked up by a different agent) from first-party rows.

    ``funnel``: signups, users with a handoff, users with a cross-agent pickup,
    activated users (pickup by another agent within 7 days of the handoff) and
    median hours from signup to each. ``weekly``: pickups, users picking up,
    repeat users, and the share of handoffs by health grade.

    **Superadmin only.** Counts and times only; no memory content is read.
    """
    since_dt: datetime | None = None
    if since and since.strip():
        try:
            since_dt = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="since must be an ISO date/time") from None
    return await relay_metrics(db, weeks=weeks, since=since_dt)


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
    user_row = await user_cursor.fetchone()
    assert user_row is not None  # COUNT(*) always returns exactly one row
    total_users = user_row[0]

    active_cursor = await db.conn.execute("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
    active_row = await active_cursor.fetchone()
    assert active_row is not None  # COUNT(*) always returns exactly one row
    active_users = active_row[0]

    # Memory stats
    mem_cursor = await db.conn.execute("SELECT COUNT(*) FROM memories")
    mem_row = await mem_cursor.fetchone()
    assert mem_row is not None  # COUNT(*) always returns exactly one row
    total_memories = mem_row[0]

    # API key stats
    key_cursor = await db.conn.execute("SELECT COUNT(*) FROM api_keys WHERE active = TRUE")
    key_row = await key_cursor.fetchone()
    assert key_row is not None  # COUNT(*) always returns exactly one row
    active_keys = key_row[0]

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
    recent_row = await recent_cursor.fetchone()
    assert recent_row is not None  # COUNT(*) always returns exactly one row
    recent_signups = recent_row[0]

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

    rows = list(await cursor.fetchall())

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
                    from remembra.models.memory import Memory

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
