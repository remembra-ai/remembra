"""
Scope enforcement via FastAPI dependencies.

Provides composable ``Depends(...)`` helpers that check the current user's
role and permissions before allowing access to an endpoint.

Usage in route files::

    from remembra.auth.scopes import RequireStore, RequireAdmin

    @router.post("/memories", ...)
    async def store_memory(
        ...,
        _perm: RequireStore,           # 403 if no memory:store permission
    ): ...

    @router.get("/admin/audit", ...)
    async def audit_export(
        ...,
        _perm: RequireAdmin,           # 403 if not admin role
    ): ...
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.auth.rbac import SYNTHETIC_KEY_IDS, KeyRole, Permission, Role, RoleManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core enforcement dependency
# ---------------------------------------------------------------------------


async def _get_role_manager(request: Request) -> RoleManager | None:
    """Get the RoleManager from app state (may be None if RBAC is disabled)."""
    return getattr(request.app.state, "role_manager", None)


async def _get_key_role(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> KeyRole:
    """Resolve the KeyRole for the current authenticated user's API key."""
    if user.api_key_id in SYNTHETIC_KEY_IDS:
        # Dashboard (JWT) and dev sessions are not API keys: they must never pick up
        # a role row stored under the shared synthetic id, which would apply to
        # every such session at once.
        return KeyRole(api_key_id=user.api_key_id, role=Role.EDITOR)
    manager = await _get_role_manager(request)
    if manager is None:
        # RBAC not enabled — grant full editor permissions (backwards-compatible)
        return KeyRole(api_key_id=user.api_key_id, role=Role.EDITOR)
    return await manager.get_role(user.api_key_id)


def require_permission(perm: Permission) -> Any:
    """Factory: create a dependency that requires a specific permission."""

    async def _check(
        key_role: KeyRole = Depends(_get_key_role),
    ) -> KeyRole:
        if not key_role.has_permission(perm):
            logger.warning(
                "Permission denied: key=%s perm=%s role=%s",
                key_role.api_key_id,
                perm.value,
                key_role.role.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required: {perm.value}",
            )
        return key_role

    return _check


def require_role(role: Role) -> Any:
    """Factory: create a dependency that requires at least a given role level."""
    # Role hierarchy: admin > editor > viewer
    hierarchy = {Role.ADMIN: 3, Role.EDITOR: 2, Role.VIEWER: 1}

    async def _check(
        key_role: KeyRole = Depends(_get_key_role),
    ) -> KeyRole:
        required_level = hierarchy.get(role, 0)
        actual_level = hierarchy.get(key_role.role, 0)
        if actual_level < required_level:
            logger.warning(
                "Role denied: key=%s required=%s actual=%s",
                key_role.api_key_id,
                role.value,
                key_role.role.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role.value}' or higher required.",
            )
        return key_role

    return _check


def require_project_access(project_id_param: str = "project_id") -> Any:
    """Factory: create a dependency that checks project-level access."""

    async def _check(
        request: Request,
        key_role: KeyRole = Depends(_get_key_role),
    ) -> KeyRole:
        # Try query param first, then path param, then body
        project_id = request.query_params.get(project_id_param) or "default"
        if not key_role.has_project_access(project_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No access to project '{project_id}'.",
            )
        return key_role

    return _check


# ---------------------------------------------------------------------------
# Pre-built dependencies for common permissions
# ---------------------------------------------------------------------------

RequireStore = Annotated[KeyRole, Depends(require_permission(Permission.MEMORY_STORE))]
RequireRecall = Annotated[KeyRole, Depends(require_permission(Permission.MEMORY_RECALL))]
RequireDelete = Annotated[KeyRole, Depends(require_permission(Permission.MEMORY_DELETE))]
RequireKeyCreate = Annotated[KeyRole, Depends(require_permission(Permission.KEY_CREATE))]
RequireWebhook = Annotated[KeyRole, Depends(require_permission(Permission.WEBHOOK_MANAGE))]
RequireConflict = Annotated[KeyRole, Depends(require_permission(Permission.CONFLICT_MANAGE))]
RequireAdmin = Annotated[KeyRole, Depends(require_role(Role.ADMIN))]
RequireAuditExport = Annotated[KeyRole, Depends(require_permission(Permission.ADMIN_EXPORT))]
