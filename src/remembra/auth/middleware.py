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
            if db and settings.jwt_secret:
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
            log.debug("jwt_verification_failed", error=str(e))
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
        detail="Authentication required. Use JWT token or API key.",
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
            if db and settings.jwt_secret:
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
            log.debug("jwt_verification_failed", error=str(e))
    
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
            )
    
    return None


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
