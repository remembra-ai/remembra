"""FastAPI authentication middleware and dependencies."""

from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from remembra.auth.keys import APIKeyManager
from remembra.config import get_settings

log = structlog.get_logger(__name__)

# API Key header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class AuthenticatedUser:
    """Represents an authenticated user from API key validation."""

    user_id: str
    api_key_id: str
    rate_limit_tier: str
    name: str | None = None
    role: str = "editor"  # Populated by RBAC layer if enabled
    scopes: list[str] | None = None  # Explicit scope restrictions
    project_ids: list[str] | None = None  # Optional project restrictions


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    # Check X-Forwarded-For header (from proxies/load balancers)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Take the first IP in the chain (original client)
        return forwarded.split(",")[0].strip()

    # Check X-Real-IP header (nginx)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip

    # Fallback to direct client IP
    if request.client:
        return request.client.host

    return "unknown"


async def get_api_key_manager(request: Request) -> APIKeyManager:
    """Dependency to get APIKeyManager from app state."""
    return request.app.state.api_key_manager


async def get_current_user(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
) -> AuthenticatedUser:
    """
    Dependency that requires valid authentication (JWT Bearer OR API key).

    Checks in order:
    1. Authorization: Bearer <jwt_token>
    2. X-API-Key header

    Raises 401 if:
    - Auth is enabled and no credentials provided
    - Auth is enabled and credentials are invalid/revoked

    If auth is disabled (dev mode), returns a default user.
    """
    settings = get_settings()

    # If auth is disabled (development), use default user
    if not settings.auth_enabled:
        log.debug("auth_disabled_using_default_user")
        return AuthenticatedUser(
            user_id="default_user",
            api_key_id="dev_key",
            rate_limit_tier="standard",
        )

    # Check for JWT Bearer token first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Remove "Bearer " prefix
        try:
            from remembra.auth.users import UserManager

            db = getattr(request.app.state, "db", None)
            if db is None:
                log.warning("database_not_initialized_for_jwt_auth")
                # Don't fail - fall through to API key check
            elif not settings.jwt_secret:
                log.warning("jwt_secret_not_configured")
                # Don't fail - fall through to API key check
            else:
                user_manager = UserManager(db, settings.jwt_secret)
                payload = user_manager.verify_jwt_token(token)
                if payload and payload.get("sub"):
                    log.debug("auth_jwt_success", user_id=payload.get("sub"))
                    return AuthenticatedUser(
                        user_id=payload.get("sub"),
                        api_key_id="jwt_auth",
                        rate_limit_tier="standard",
                        name=payload.get("email"),
                    )
        except Exception as e:
            log.warning("jwt_verification_failed", error=str(e), error_type=type(e).__name__)
            # Fall through to API key check

    # Check API key
    if api_key:
        key_manager = await get_api_key_manager(request)
        key_info = await key_manager.validate_key(api_key)

        if key_info:
            return AuthenticatedUser(
                user_id=key_info["user_id"],
                api_key_id=key_info["id"],
                rate_limit_tier=key_info.get("rate_limit_tier", "standard"),
                name=key_info.get("name"),
                role=key_info.get("role", "editor"),  # RBAC FIX: Extract role from key
                scopes=key_info.get("scopes"),
                project_ids=key_info.get("project_ids"),
            )
        else:
            log.warning(
                "auth_invalid_api_key",
                ip=get_client_ip(request),
                key_preview=api_key[:12] + "..." if len(api_key) > 12 else api_key,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )

    # No valid auth provided
    log.warning("auth_missing_credentials", ip=get_client_ip(request))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Use X-API-Key header with your API key (rem_...) or Authorization: Bearer with a JWT token.",
        headers={"WWW-Authenticate": "Bearer, ApiKey"},
    )


async def get_optional_user(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
) -> AuthenticatedUser | None:
    """
    Dependency that optionally validates an API key.

    Returns None if no key provided (instead of raising 401).
    Used for endpoints that work with or without auth.
    """
    settings = get_settings()

    if not settings.auth_enabled:
        return AuthenticatedUser(
            user_id="default_user",
            api_key_id="dev_key",
            rate_limit_tier="standard",
        )

    if not api_key:
        return None


async def get_user_from_jwt_or_api_key(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
) -> AuthenticatedUser | None:
    """
    Dependency that validates either JWT Bearer token OR API key.

    Checks in order:
    1. Authorization: Bearer <jwt_token>
    2. X-API-Key header

    Returns None if neither provided (instead of raising 401).
    Used for endpoints that accept both auth methods.
    """
    settings = get_settings()

    if not settings.auth_enabled:
        return AuthenticatedUser(
            user_id="default_user",
            api_key_id="dev_key",
            rate_limit_tier="standard",
        )

    # Check for JWT Bearer token first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Remove "Bearer " prefix
        try:
            # Create UserManager to verify JWT
            from remembra.auth.users import UserManager

            db = getattr(request.app.state, "db", None)
            if db is None:
                log.debug("database_not_initialized_for_jwt_auth_optional")
                # Don't fail - fall through to API key check
            elif not settings.jwt_secret:
                log.debug("jwt_secret_not_configured_optional")
                # Don't fail - fall through to API key check
            else:
                user_manager = UserManager(db, settings.jwt_secret)
                payload = user_manager.verify_jwt_token(token)
                if payload:
                    return AuthenticatedUser(
                        user_id=payload.get("sub"),
                        api_key_id="jwt_auth",
                        rate_limit_tier="standard",
                        name=payload.get("email"),
                    )
        except Exception as e:
            log.debug("jwt_verification_failed_optional", error=str(e), error_type=type(e).__name__)

    # Fall back to API key
    if api_key:
        key_manager = await get_api_key_manager(request)
        key_info = await key_manager.validate_key(api_key)
        if key_info:
            return AuthenticatedUser(
                user_id=key_info["user_id"],
                api_key_id=key_info["id"],
                rate_limit_tier=key_info.get("rate_limit_tier", "standard"),
                name=key_info.get("name"),
                role=key_info.get("role", "editor"),
                scopes=key_info.get("scopes"),
                project_ids=key_info.get("project_ids"),
            )

    return None


def ensure_project_access(user: AuthenticatedUser, project_id: str) -> str:
    """Validate that the authenticated user can access a specific project."""
    if user.project_ids and project_id not in user.project_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"No access to project '{project_id}'.",
        )
    return project_id


def resolve_project_access(
    user: AuthenticatedUser,
    project_id: str | None,
) -> str | None:
    """
    Resolve the effective project for project-scoped keys.

    Unrestricted users keep the caller-provided value.
    Restricted keys:
    - may access an explicitly requested allowed project
    - default to the sole allowed project if only one exists
    - must provide `project_id` explicitly when multiple projects are allowed
    """
    if not user.project_ids:
        return project_id

    if project_id:
        return ensure_project_access(user, project_id)

    if len(user.project_ids) == 1:
        return user.project_ids[0]

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This API key is restricted to multiple projects. Provide project_id explicitly.",
    )


async def require_master_key(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
) -> None:
    """
    Dependency that requires the master key for admin operations.

    Used for key management endpoints.
    """
    settings = get_settings()

    # If auth disabled, allow through
    if not settings.auth_enabled:
        return

    # If no master key configured, allow through (not recommended for production)
    if not settings.auth_master_key:
        log.warning("master_key_not_configured", endpoint=str(request.url.path))
        return

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master key required for this operation.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Check against master key
    if api_key != settings.auth_master_key:
        log.warning(
            "master_key_invalid",
            ip=get_client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid master key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# Type aliases for FastAPI Depends
CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
OptionalUser = Annotated[AuthenticatedUser | None, Depends(get_optional_user)]
JWTOrAPIKeyUser = Annotated[AuthenticatedUser | None, Depends(get_user_from_jwt_or_api_key)]
RequireMasterKey = Annotated[None, Depends(require_master_key)]


# ---------------------------------------------------------------------------
# RBAC Permission Checking
# ---------------------------------------------------------------------------

# Role hierarchy: admin > editor > viewer
# Permission names aligned with remembra.auth.rbac.Permission
ROLE_PERMISSIONS = {
    "admin": {
        "memory:store",
        "memory:recall",
        "memory:delete",
        "entity:read",
        "entity:merge",
        "webhook:manage",
        "admin:audit",
        "admin:users",
        "key:create",
        "key:list",
        "key:revoke",
    },
    "editor": {
        "memory:store",
        "memory:recall",
        "memory:delete",
        "entity:read",
        "key:list",
        "webhook:manage",
        "conflict:manage",
    },
    "viewer": {
        "memory:recall",
        "entity:read",
        "key:list",
    },
}


def has_permission(user: AuthenticatedUser, permission: str) -> bool:
    """Check if user has a specific permission based on their role."""
    role_perms = ROLE_PERMISSIONS.get(user.role, set())

    # If user has explicit scopes, use those instead of role defaults
    if user.scopes:
        return permission in user.scopes

    return permission in role_perms


def require_permission(permission: str):
    """
    Dependency factory that requires a specific permission.

    Usage:
        @router.post("/memories")
        async def store_memory(
            _perm: RequirePermission("memory:create"),
            current_user: CurrentUser,
        ):
            ...
    """

    async def check_permission(current_user: CurrentUser) -> None:
        if not has_permission(current_user, permission):
            log.warning(
                "permission_denied",
                user_id=current_user.user_id,
                role=current_user.role,
                required_permission=permission,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission} required",
            )
        return None

    return Depends(check_permission)


# Permission dependency factories (aligned with remembra.auth.rbac.Permission)
def require_memory_store():
    return require_permission("memory:store")


def require_memory_recall():
    return require_permission("memory:recall")


def require_memory_delete():
    return require_permission("memory:delete")


def require_entity_read():
    return require_permission("entity:read")


def require_entity_merge():
    return require_permission("entity:merge")


def require_webhook_manage():
    return require_permission("webhook:manage")


def require_audit_read():
    return require_permission("admin:audit")


def require_user_manage():
    return require_permission("admin:users")
