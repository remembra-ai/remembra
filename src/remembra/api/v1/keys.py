"""API Key management endpoints – /api/v1/keys."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from slowapi import Limiter

from remembra.auth.keys import APIKeyManager
from remembra.auth.middleware import (
    JWTOrAPIKeyUser,
    get_client_ip,
)
from remembra.auth.rbac import Role, RoleManager
from remembra.cloud.limits import EnforceKeyLimit
from remembra.core.limiter import limiter
from remembra.security.audit import AuditLogger

router = APIRouter(prefix="/keys", tags=["api-keys"])


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    """Request to create a new API key."""

    user_id: str | None = Field(None, description="User ID to create key for (required for master key, ignored for JWT)")
    name: str | None = Field(None, description="Human-readable name for the key")
    rate_limit_tier: str = Field("standard", description="Rate limit tier: standard or premium")
    role: str = Field("editor", description="Role: admin, editor, or viewer")
    permission: str | None = Field(None, description="Alias for role (dashboard compatibility)")
    project_ids: list[str] | None = Field(
        None,
        description="Optional list of project IDs this key may access. Omit for all projects.",
    )


class CreateKeyResponse(BaseModel):
    """Response after creating an API key."""

    id: str = Field(..., description="Key ID (use for revocation)")
    key: str = Field(..., description="Full API key (only shown once!)")
    user_id: str
    name: str | None
    rate_limit_tier: str
    role: str = Field(..., description="Assigned role: admin, editor, or viewer")
    project_ids: list[str] = Field(
        default_factory=list,
        description="Project restrictions applied to the key. Empty means all projects.",
    )
    message: str = Field(
        default="Store this key securely. It cannot be retrieved again.", description="Important security notice"
    )


class KeyInfo(BaseModel):
    """API key info (without actual key)."""

    id: str
    user_id: str
    name: str | None
    key_preview: str = Field(..., description="First 8 chars of key ID for display")
    created_at: str
    last_used_at: str | None
    active: bool
    rate_limit_tier: str
    role: str = Field("editor", description="Key role: admin, editor, or viewer")
    project_ids: list[str] = Field(
        default_factory=list,
        description="Project restrictions for this key. Empty means all projects.",
    )
    # Alias for frontend compatibility
    permission: str = Field("editor", description="Alias for role (frontend compatibility)")


class ListKeysResponse(BaseModel):
    """Response for listing API keys."""

    keys: list[KeyInfo]
    count: int


class RevokeKeyResponse(BaseModel):
    """Response after revoking an API key."""

    success: bool
    message: str


class UpdateKeyRequest(BaseModel):
    """Request to update an API key."""

    name: str | None = Field(None, description="New name for the key")
    role: str | None = Field(None, description="New role: admin, editor, or viewer")
    project_ids: list[str] | None = Field(
        None,
        description="Replace the key's allowed projects. Use an empty list to remove restrictions.",
    )


class UpdateKeyResponse(BaseModel):
    """Response after updating an API key."""

    success: bool
    key: KeyInfo
    message: str


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_api_key_manager(request: Request) -> APIKeyManager:
    """Dependency to get the API key manager from app state."""
    return request.app.state.api_key_manager


def get_role_manager(request: Request) -> RoleManager:
    """Dependency to get the role manager from app state."""
    return request.app.state.role_manager


def get_audit_logger(request: Request) -> AuditLogger:
    """Dependency to get the audit logger from app state."""
    return request.app.state.audit_logger


def get_limiter(request: Request) -> Limiter:
    """Dependency to get the rate limiter from app state."""
    return request.app.state.limiter


APIKeyManagerDep = Annotated[APIKeyManager, Depends(get_api_key_manager)]
RoleManagerDep = Annotated[RoleManager, Depends(get_role_manager)]
AuditLoggerDep = Annotated[AuditLogger, Depends(get_audit_logger)]


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def validate_role(role_str: str) -> Role:
    """Validate and convert role string to Role enum."""
    try:
        return Role(role_str.lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role '{role_str}'. Must be one of: admin, editor, viewer",
        )


def generate_key_preview(key_id: str) -> str:
    """Generate a display preview from key ID (e.g., 'ErBdukTD')."""
    # Extract the random part after 'key_' and take first 8 chars
    if key_id.startswith("key_"):
        return key_id[4:12]
    return key_id[:8]


async def get_key_with_role(
    key_manager: APIKeyManager,
    role_manager: RoleManager,
    key_id: str,
) -> KeyInfo | None:
    """Get key info with role attached."""
    key_info = await key_manager.get_key_info(key_id)
    if not key_info:
        return None

    # Get role from RBAC
    key_role = await role_manager.get_role(key_id)
    role_value = key_role.role.value

    return KeyInfo(
        id=key_info.id,
        user_id=key_info.user_id,
        name=key_info.name,
        key_preview=generate_key_preview(key_info.id),
        created_at=key_info.created_at,
        last_used_at=key_info.last_used_at,
        active=key_info.active,
        rate_limit_tier=key_info.rate_limit_tier,
        role=role_value,
        project_ids=key_role.project_ids,
        permission=role_value,  # Alias for frontend compatibility
    )


# ---------------------------------------------------------------------------
# Create Key (JWT auth for own keys, master key for admin)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
)
@limiter.limit("10/hour")
async def create_api_key(
    request: Request,
    body: CreateKeyRequest,
    key_manager: APIKeyManagerDep,
    role_manager: RoleManagerDep,
    audit_logger: AuditLoggerDep,
    current_user: JWTOrAPIKeyUser = None,  # JWT or API key auth (optional)
    _limit: EnforceKeyLimit = None,
) -> CreateKeyResponse:
    """
    Create a new API key.

    **Authentication options:**
    - **JWT token** (Authorization: Bearer): Creates key for authenticated user
    - **Master key** (X-API-Key): Creates key for any user (requires user_id in body)

    The full key is returned ONLY in this response.
    Store it securely - it cannot be retrieved again.

    **Roles:**
    - `admin` - Full access: manage keys, users, memories, audit logs
    - `editor` - Read/write memories, manage own keys (default)
    - `viewer` - Read-only: recall memories, list entities

    **Rate limit:** 10 requests/hour.
    """
    # Determine user_id based on auth method
    if current_user:
        # JWT auth - create key for authenticated user
        user_id = current_user.user_id
    elif body.user_id:
        # Master key auth - check for master key in header
        master_key = request.headers.get("X-API-Key")
        settings = request.app.state.settings
        if not master_key or master_key != settings.master_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Use JWT token or master key.",
            )
        user_id = body.user_id
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Use JWT token or provide user_id with master key.",
        )

    # Support 'permission' alias for 'role' (dashboard compatibility)
    role_str = body.permission or body.role
    role = validate_role(role_str)

    try:
        api_key = await key_manager.create_key(
            user_id=user_id,
            name=body.name,
            rate_limit_tier=body.rate_limit_tier,
        )

        # Assign role to the new key
        await role_manager.assign_role(
            api_key_id=api_key.id,
            role=role,
            project_ids=body.project_ids,
        )

        # Audit log
        await audit_logger.log_key_created(
            user_id=user_id,
            key_id=api_key.id,
            ip_address=get_client_ip(request),
        )

        return CreateKeyResponse(
            id=api_key.id,
            key=api_key.key,
            user_id=api_key.user_id,
            name=api_key.name,
            rate_limit_tier=api_key.rate_limit_tier,
            role=role.value,
            project_ids=body.project_ids or [],
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create API key: {str(e)}",
        )


# ---------------------------------------------------------------------------
# List Keys (authenticated user sees their own keys)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ListKeysResponse,
    summary="List your API keys",
)
@limiter.limit("30/minute")
async def list_api_keys(
    request: Request,
    key_manager: APIKeyManagerDep,
    role_manager: RoleManagerDep,
    current_user: JWTOrAPIKeyUser,
    active_only: bool = False,
) -> ListKeysResponse:
    """
    List all API keys for the authenticated user.

    Supports both JWT Bearer token and API key authentication.

    **Query Parameters:**
    - `active_only` (bool): If true, only return active (non-revoked) keys

    Note: The actual key values are never shown (only metadata).
    """
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Use JWT token or API key.",
        )

    keys = await key_manager.list_keys(current_user.user_id)

    # Filter by active status if requested
    if active_only:
        keys = [k for k in keys if k.active]

    # Enrich with roles and additional fields
    enriched_keys = []
    for k in keys:
        key_role = await role_manager.get_role(k.id)
        role_value = key_role.role.value
        enriched_keys.append(
            KeyInfo(
                id=k.id,
                user_id=k.user_id,
                name=k.name,
                key_preview=generate_key_preview(k.id),
                created_at=k.created_at,
                last_used_at=k.last_used_at,
                active=k.active,
                rate_limit_tier=k.rate_limit_tier,
                role=role_value,
                project_ids=key_role.project_ids,
                permission=role_value,  # Alias for frontend compatibility
            )
        )

    return ListKeysResponse(
        keys=enriched_keys,
        count=len(enriched_keys),
    )


# ---------------------------------------------------------------------------
# Get Key Info
# ---------------------------------------------------------------------------


@router.get(
    "/{key_id}",
    response_model=KeyInfo,
    summary="Get API key info",
)
@limiter.limit("30/minute")
async def get_api_key_info(
    request: Request,
    key_id: str,
    key_manager: APIKeyManagerDep,
    role_manager: RoleManagerDep,
    current_user: JWTOrAPIKeyUser,
) -> KeyInfo:
    """
    Get information about a specific API key.

    Users can only view their own keys.
    The actual key value is never shown.
    """
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Use JWT token or API key.",
        )

    key_info = await get_key_with_role(key_manager, role_manager, key_id)

    if not key_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key {key_id} not found",
        )

    # Security: Ensure user can only see their own keys
    if key_info.user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key {key_id} not found",  # Don't reveal it exists
        )

    return key_info


# ---------------------------------------------------------------------------
# Update Key (PATCH)
# ---------------------------------------------------------------------------


@router.patch(
    "/{key_id}",
    response_model=UpdateKeyResponse,
    summary="Update an API key",
)
@limiter.limit("10/minute")  # Prevent abuse
async def update_api_key(
    request: Request,
    key_id: str,
    body: UpdateKeyRequest,
    key_manager: APIKeyManagerDep,
    role_manager: RoleManagerDep,
    audit_logger: AuditLoggerDep,
    current_user: JWTOrAPIKeyUser,
) -> UpdateKeyResponse:
    """
    Update an API key's name or role.

    Users can update their own keys.
    Only the fields provided will be updated.

    **Roles:**
    - `admin` - Full access
    - `editor` - Read/write memories (default)
    - `viewer` - Read-only access
    """
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Use JWT token or API key.",
        )

    # Get existing key info
    existing_key = await key_manager.get_key_info(key_id)

    if not existing_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key {key_id} not found",
        )

    # Security: Ensure user can only update their own keys
    if existing_key.user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key {key_id} not found",  # Don't reveal it exists
        )

    # Check if key is active
    if not existing_key.active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot update a revoked key",
        )

    # Update name if provided
    if body.name is not None:
        await key_manager.update_key_name(key_id, body.name)

    # Update role if provided
    if body.role is not None or body.project_ids is not None:
        current_role = await role_manager.get_role(key_id)
        role = validate_role(body.role) if body.role is not None else current_role.role
        project_ids = body.project_ids if body.project_ids is not None else current_role.project_ids
        await role_manager.assign_role(
            api_key_id=key_id,
            role=role,
            scopes=current_role.scopes,
            project_ids=project_ids,
        )

    # Get updated key info
    updated_key = await get_key_with_role(key_manager, role_manager, key_id)

    # Audit log
    await audit_logger.log_event(
        user_id=current_user.user_id,
        action="key_updated",
        resource_id=key_id,
        ip_address=get_client_ip(request),
        details={
            "name_updated": body.name is not None,
            "role_updated": body.role is not None,
            "project_ids_updated": body.project_ids is not None,
        },
    )

    return UpdateKeyResponse(
        success=True,
        key=updated_key,
        message=f"API key {key_id} has been updated",
    )


# ---------------------------------------------------------------------------
# Revoke/Delete Key (user can revoke or permanently delete their own keys)
# ---------------------------------------------------------------------------


@router.delete(
    "/{key_id}",
    response_model=RevokeKeyResponse,
    summary="Revoke or permanently delete an API key",
)
@limiter.limit("5/minute")  # Prevent abuse
async def revoke_api_key(
    request: Request,
    key_id: str,
    key_manager: APIKeyManagerDep,
    role_manager: RoleManagerDep,
    audit_logger: AuditLoggerDep,
    current_user: JWTOrAPIKeyUser,
    hard: bool = False,
) -> RevokeKeyResponse:
    """
    Revoke or permanently delete an API key.

    Users can only manage their own keys.

    **Query Parameters:**
    - `hard` (bool): If true, permanently delete the key from the database.
                     If false (default), soft-revoke (mark as inactive).

    **Soft revoke (default):**
    - Key is marked as inactive but remains in database
    - Useful for audit trails and compliance
    - Key cannot be used for authentication

    **Hard delete (`?hard=true`):**
    - Key is permanently removed from database
    - No recovery possible
    - Use for leaked keys or security incidents
    """
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Use JWT token or API key.",
        )

    if hard:
        # Permanently delete the key
        success = await key_manager.delete_key_permanently(key_id, current_user.user_id)
        action = "key_deleted_permanently"
    else:
        # Soft revoke (existing behavior)
        success = await key_manager.revoke_key(key_id, current_user.user_id)
        action = "key_revoked"

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key {key_id} not found or already revoked/deleted",
        )

    # Clean up role assignment
    await role_manager.remove_role(key_id)

    # Audit log
    await audit_logger.log_event(
        user_id=current_user.user_id,
        action=action,
        resource_id=key_id,
        ip_address=get_client_ip(request),
        details={"hard_delete": hard},
    )

    return RevokeKeyResponse(
        success=True,
        message=f"API key {key_id} has been revoked",
    )
