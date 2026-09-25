"""FastAPI authentication middleware and dependencies."""

import contextvars
import hmac
import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

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
    agent_id: str | None = None  # Agent-scoped key: relay writes are attributed to this agent


# In-process principal for the remote MCP connector (remembra.connector).
#
# Connector tools call the REST routes in-process (ASGI, no network) so every
# policy those routes enforce — RBAC, project restriction, PII, sanitizer,
# limits, audit — applies unchanged. The connector's OAuth access tokens are
# NOT accepted as HTTP credentials anywhere: only code in this process can set
# this variable, for the duration of one in-process call, with a principal it
# built from a validated grant (least-privilege scopes, bound projects).
_connector_principal: contextvars.ContextVar["AuthenticatedUser | None"] = contextvars.ContextVar(
    "remembra_connector_principal", default=None
)


@contextmanager
def connector_principal(user: "AuthenticatedUser") -> Iterator[None]:
    """Authenticate in-process REST calls made inside this block as ``user``."""
    token = _connector_principal.set(user)
    try:
        yield
    finally:
        _connector_principal.reset(token)


def resolve_api_key(request: Request, api_key: str | None) -> str | None:
    """Return the API key to validate.

    Prefers the X-API-Key header, but also accepts a ``rem_`` API key passed as a
    Bearer token. Many HTTP clients default to ``Authorization: Bearer`` and a
    ``rem_`` key is unmistakably an API key (never a JWT), so we route it to
    API-key validation instead of rejecting it with a confusing 401.
    """
    if api_key:
        return api_key
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token.startswith("rem_"):
            return token
    return None


@lru_cache(maxsize=16)
def _trusted_networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:
            log.warning("trusted_proxy_cidr_invalid", cidr=cidr)
    return tuple(networks)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None, networks: tuple[Any, ...]) -> bool:
    return ip is not None and any(ip.version == net.version and ip in net for net in networks)


# Cloudflare's published edge ranges (https://www.cloudflare.com/ips-v4 and /ips-v6).
CLOUDFLARE_RANGES: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


def _cf_connecting_ip(request: Request) -> str | None:
    value = request.headers.get("CF-Connecting-IP")
    ip = _parse_ip(value) if value else None
    return str(ip) if ip is not None else None


def get_client_ip(request: Request) -> str:
    """Return the real client IP.

    ``X-Forwarded-For`` / ``X-Real-IP`` are honoured ONLY when the direct peer is a
    configured trusted proxy (``REMEMBRA_TRUSTED_PROXIES``); otherwise any client
    could spoof its address to evade rate limits or poison audit logs. The
    forwarded chain is walked right-to-left and the first hop that is not itself
    a trusted proxy is the client.

    Behind Cloudflare (``trust_cloudflare_proxies``, on by default) the hop that
    reached our proxy is a Cloudflare edge address shared by many users; when
    the chain arrives at a Cloudflare range, the client is Cloudflare's
    ``CF-Connecting-IP``. Cloudflare ranges are only consulted inside a chain
    that already starts at a trusted peer, so a direct client cannot use them.
    """
    peer = request.client.host if request.client else None
    settings = get_settings()
    networks = _trusted_networks(tuple(getattr(settings, "trusted_proxies", None) or ()))
    cloudflare = _trusted_networks(CLOUDFLARE_RANGES) if getattr(settings, "trust_cloudflare_proxies", False) else ()
    peer_ip = _parse_ip(peer) if peer else None
    if peer and _is_trusted(peer_ip, cloudflare):
        # Cloudflare connects to us directly.
        cf_client = _cf_connecting_ip(request)
        if cf_client:
            return cf_client
    if peer and (_is_trusted(peer_ip, networks) or _is_trusted(peer_ip, cloudflare)):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            for hop in reversed(hops):
                hop_ip = _parse_ip(hop)
                if hop_ip is None:
                    break  # malformed chain: stop trusting it
                if _is_trusted(hop_ip, networks):
                    continue
                if _is_trusted(hop_ip, cloudflare):
                    cf_client = _cf_connecting_ip(request)
                    if cf_client:
                        return cf_client
                    continue
                return str(hop_ip)
            else:
                if hops and _parse_ip(hops[0]) is not None:
                    return str(_parse_ip(hops[0]))
        real_ip = request.headers.get("X-Real-IP")
        if real_ip and _parse_ip(real_ip) is not None:
            return str(_parse_ip(real_ip))
    return peer or "unknown"


def _mark_rate_limit_identity(request: Request, user_id: str) -> None:
    """Record the validated identity so rate limits key on the account, not on a header."""
    try:
        request.state.rate_limit_identity = f"user:{user_id}"
    except Exception:  # pragma: no cover - non-Starlette request doubles in unit tests
        pass


def _user_from_key_info(key_info: dict[str, Any]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=key_info["user_id"],
        api_key_id=key_info["id"],
        rate_limit_tier=key_info.get("rate_limit_tier", "standard"),
        name=key_info.get("name"),
        role=key_info.get("role", "editor"),
        scopes=key_info.get("scopes"),
        project_ids=key_info.get("project_ids"),
        agent_id=(key_info.get("agent_id") or None),
    )


async def _account_is_active(request: Request, user_id: str) -> bool:
    """False only when a user row exists and is deactivated (tenants without rows are allowed)."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return True
    try:
        user_row = await db.get_user_by_id(user_id)
    except Exception as e:
        log.error("account_active_check_failed", user_id=user_id, error_type=type(e).__name__)
        return False  # fail closed
    if not user_row:
        return True
    return bool(user_row.get("is_active", True))


async def authenticate_jwt(request: Request, token: str) -> AuthenticatedUser | None:
    """Validate a dashboard JWT end-to-end.

    Rejects tokens that are expired/invalid, blacklisted (logout), issued before
    the user's last password change / session invalidation, or that belong to a
    missing or deactivated account. Returns None when the token is not usable.
    """
    settings = get_settings()
    db = getattr(request.app.state, "db", None)
    if db is None or not settings.jwt_secret:
        log.warning("jwt_auth_unavailable", db_ready=db is not None)
        return None

    from remembra.auth.users import UserManager
    from remembra.security import state as security_state

    user_manager = UserManager(db, settings.jwt_secret)
    payload = user_manager.verify_jwt_token(token)
    sub = payload.get("sub") if payload else None
    if not payload or not sub:
        return None
    try:
        if await user_manager.is_token_blacklisted(token):
            log.info("jwt_rejected_blacklisted", user_id=sub)
            return None
        user_row = await db.get_user_by_id(sub)
        if not user_row or not user_row.get("is_active", True):
            log.info("jwt_rejected_inactive_or_missing", user_id=sub)
            return None
        valid_after = await security_state.get_tokens_valid_after_ms(db, sub)
        if valid_after and security_state.token_issued_at_ms(payload) < valid_after:
            log.info("jwt_rejected_invalidated_session", user_id=sub)
            return None
    except Exception as e:
        # Fail closed: a JWT we cannot fully validate is not accepted.
        log.error("jwt_validation_error", error_type=type(e).__name__)
        return None

    return AuthenticatedUser(
        user_id=sub,
        api_key_id="jwt_auth",
        rate_limit_tier="standard",
        name=payload.get("email"),
    )


async def authenticate_api_key(request: Request, api_key: str) -> AuthenticatedUser | None:
    """Validate an API key and the owning account's active status."""
    key_manager = await get_api_key_manager(request)
    key_info = await key_manager.validate_key(api_key)
    if not key_info:
        return None
    if not await _account_is_active(request, key_info["user_id"]):
        log.info("api_key_rejected_inactive_account", user_id=key_info["user_id"], key_id=key_info["id"])
        return None
    return _user_from_key_info(key_info)


async def get_api_key_manager(request: Request) -> APIKeyManager:
    """Dependency to get APIKeyManager from app state."""
    manager: APIKeyManager = request.app.state.api_key_manager
    return manager


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
    principal = _connector_principal.get()
    if principal is not None:
        _mark_rate_limit_identity(request, principal.user_id)
        return principal

    settings = get_settings()

    # If auth is disabled (development), use default user
    if not settings.auth_enabled:
        log.debug("auth_disabled_using_default_user")
        return AuthenticatedUser(
            user_id="default_user",
            api_key_id="dev_key",
            rate_limit_tier="standard",
        )

    # Check for JWT Bearer token first (a rem_ key sent as Bearer is handled below)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer ") and not auth_header[7:].strip().startswith("rem_"):
        jwt_user = await authenticate_jwt(request, auth_header[7:].strip())
        if jwt_user:
            _mark_rate_limit_identity(request, jwt_user.user_id)
            return jwt_user

    # Check API key (also accepts a rem_ key sent as a Bearer token)
    api_key = resolve_api_key(request, api_key)
    if api_key:
        key_user = await authenticate_api_key(request, api_key)
        if key_user:
            _mark_rate_limit_identity(request, key_user.user_id)
            return key_user
        # Never log any part of a rejected credential.
        log.warning("auth_invalid_api_key", ip=get_client_ip(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # No valid auth provided
    log.warning("auth_missing_credentials", ip=get_client_ip(request))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Authentication required. Use X-API-Key header with your API key (rem_...) or Authorization: Bearer with a JWT token."
        ),
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

    api_key = resolve_api_key(request, api_key)
    if not api_key:
        return None

    # A key was supplied — validate it and return the user if valid, else None.
    key_user = await authenticate_api_key(request, api_key)
    if key_user:
        _mark_rate_limit_identity(request, key_user.user_id)
    return key_user


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

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer ") and not auth_header[7:].strip().startswith("rem_"):
        jwt_user = await authenticate_jwt(request, auth_header[7:].strip())
        if jwt_user:
            _mark_rate_limit_identity(request, jwt_user.user_id)
            return jwt_user

    # Fall back to API key (also accepts a rem_ key sent as a Bearer token)
    api_key = resolve_api_key(request, api_key)
    if api_key:
        key_user = await authenticate_api_key(request, api_key)
        if key_user:
            _mark_rate_limit_identity(request, key_user.user_id)
            return key_user

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


def resolve_project_or_default(user: AuthenticatedUser, project_id: str | None) -> str:
    """Resolve the project for a write/scoped operation.

    Omitted ``project_id`` pins a single-project key to its one project (and
    defaults unrestricted callers to ``"default"``); an explicit value is
    checked against the key's allowed projects.
    """
    return resolve_project_access(user, project_id) or "default"


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

    # Fail CLOSED: if no master key is configured, deny admin operations in
    # production rather than waving them through. Debug/dev may bypass to keep
    # local setup frictionless.
    if not settings.auth_master_key:
        if settings.debug:
            log.warning("master_key_not_configured_dev_bypass", endpoint=str(request.url.path))
            return
        log.error("master_key_not_configured_denying", endpoint=str(request.url.path))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This operation requires a master key, which is not configured on the server.",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master key required for this operation.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Constant-time comparison to avoid leaking the master key via timing.
    if not hmac.compare_digest(api_key, settings.auth_master_key):
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


def require_permission(permission: str) -> Any:
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
def require_memory_store() -> Any:
    return require_permission("memory:store")


def require_memory_recall() -> Any:
    return require_permission("memory:recall")


def require_memory_delete() -> Any:
    return require_permission("memory:delete")


def require_entity_read() -> Any:
    return require_permission("entity:read")


def require_entity_merge() -> Any:
    return require_permission("entity:merge")


def require_webhook_manage() -> Any:
    return require_permission("webhook:manage")


def require_audit_read() -> Any:
    return require_permission("admin:audit")


def require_user_manage() -> Any:
    return require_permission("admin:users")
