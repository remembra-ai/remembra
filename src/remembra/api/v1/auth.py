"""Authentication API endpoints for user signup, login, and password management."""

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator

from remembra.auth.users import UserManager
from remembra.config import get_settings
from remembra.core.limiter import limiter

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# HTTP Bearer token security
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    """Request body for user signup."""
    email: EmailStr
    password: str = Field(min_length=8, description="Password must be at least 8 characters")
    name: str | None = Field(None, max_length=100, description="User's display name")
    
    @field_validator("name")
    @classmethod
    def sanitize_name(cls, v: str | None) -> str | None:
        """Strip HTML/script tags from name to prevent XSS."""
        if v is None:
            return None
        import re
        # Remove HTML tags
        clean = re.sub(r'<[^>]+>', '', v)
        # Remove common script patterns
        clean = re.sub(r'javascript:', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'on\w+\s*=', '', clean, flags=re.IGNORECASE)
        # Trim and limit
        return clean.strip()[:100] if clean.strip() else None


class SignupResponse(BaseModel):
    """Response for successful signup."""
    id: str
    email: str
    name: str | None
    message: str = "Account created successfully"


class LoginRequest(BaseModel):
    """Request body for user login."""
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Response for successful login."""
    access_token: str
    token_type: str = "bearer"
    user: dict


class LogoutResponse(BaseModel):
    """Response for successful logout."""
    message: str = "Logged out successfully"


class ForgotPasswordRequest(BaseModel):
    """Request body for password reset request."""
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Response for password reset request."""
    message: str = "If an account with this email exists, a reset link has been sent"


class ResetPasswordRequest(BaseModel):
    """Request body for password reset."""
    email: EmailStr
    token: str
    new_password: str = Field(min_length=8, description="New password must be at least 8 characters")


class ResetPasswordResponse(BaseModel):
    """Response for successful password reset."""
    message: str = "Password reset successfully"


class UserResponse(BaseModel):
    """Current user info response."""
    id: str
    email: str
    name: str | None
    email_verified: bool
    is_active: bool
    created_at: str


class ErrorResponse(BaseModel):
    """Error response."""
    detail: str


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def get_user_manager(request: Request) -> UserManager:
    """Get UserManager from app state."""
    settings = get_settings()
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database not initialized",
        )
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret not configured",
        )
    return UserManager(db, settings.jwt_secret)


async def get_current_user_from_jwt(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict:
    """
    Dependency that validates JWT token and returns user info.
    
    Raises 401 if token is missing or invalid.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_manager = await get_user_manager(request)
    
    # Verify JWT token
    payload = user_manager.verify_jwt_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check if token is blacklisted
    if await user_manager.is_token_blacklisted(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been invalidated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user from database
    user = await user_manager.get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "token": credentials.credentials,
    }


# Type alias for authenticated user
CurrentUser = Annotated[dict, Depends(get_current_user_from_jwt)]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")  # Prevent account spam
async def signup(
    request: Request,
    body: SignupRequest,
) -> SignupResponse:
    """
    Create a new user account.
    
    - **email**: Valid email address (will be lowercased)
    - **password**: At least 8 characters
    - **name**: Optional display name
    """
    user_manager = await get_user_manager(request)
    
    user, error = await user_manager.create_user(
        email=body.email,
        password=body.password,
        name=body.name,
    )
    
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )
    
    return SignupResponse(
        id=user.id,
        email=user.email,
        name=user.name,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse}},
)
@limiter.limit("10/minute")  # Prevent brute force
async def login(
    request: Request,
    body: LoginRequest,
) -> LoginResponse:
    """
    Authenticate and get an access token.
    
    - **email**: Registered email address
    - **password**: Account password
    
    Returns a JWT access token valid for 7 days.
    """
    user_manager = await get_user_manager(request)
    
    user, token, error = await user_manager.authenticate(
        email=body.email,
        password=body.password,
    )
    
    if error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error,
        )
    
    return LoginResponse(
        access_token=token,
        user={
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "email_verified": user.email_verified,
        },
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
)
async def logout(
    request: Request,
    current_user: CurrentUser,
) -> LogoutResponse:
    """
    Logout and invalidate the current access token.
    
    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)
    
    await user_manager.invalidate_token(
        user_id=current_user["id"],
        token=current_user["token"],
    )
    
    return LogoutResponse()


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
)
@limiter.limit("3/minute")  # Prevent abuse
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
) -> ForgotPasswordResponse:
    """
    Request a password reset.
    
    - **email**: Email address of the account
    
    If the email exists, a reset token is generated.
    For security, always returns success even if email doesn't exist.
    
    Note: In production, this should send an email with the reset link.
    The reset token is returned in the response for testing purposes only.
    """
    user_manager = await get_user_manager(request)
    
    reset_token, error = await user_manager.create_password_reset_token(body.email)
    
    # In production, send email here instead of returning the token
    # For now, we log it for testing purposes
    if reset_token:
        log.info("password_reset_token_generated", 
                 email=body.email, 
                 token_preview=reset_token[:8] + "...")
    
    # Always return generic message for security (don't reveal if email exists)
    return ForgotPasswordResponse()


@router.post(
    "/reset-password",
    response_model=ResetPasswordResponse,
    responses={400: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")  # Prevent brute force token guessing
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
) -> ResetPasswordResponse:
    """
    Reset password using a reset token.
    
    - **email**: Email address of the account
    - **token**: Reset token from forgot-password request
    - **new_password**: New password (at least 8 characters)
    """
    user_manager = await get_user_manager(request)
    
    success, error = await user_manager.reset_password(
        email=body.email,
        token=body.token,
        new_password=body.new_password,
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Failed to reset password",
        )
    
    return ResetPasswordResponse()


@router.get(
    "/me",
    response_model=UserResponse,
    responses={401: {"model": ErrorResponse}},
)
async def get_me(
    request: Request,
    current_user: CurrentUser,
) -> UserResponse:
    """
    Get current authenticated user's information.
    
    Requires a valid Bearer token in the Authorization header.
    """
    try:
        user_manager = await get_user_manager(request)
        
        user = await user_manager.get_user_by_id(current_user["id"])
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        
        return UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
            email_verified=bool(user.email_verified),  # Ensure boolean
            is_active=bool(user.is_active),  # Ensure boolean
            created_at=user.created_at.isoformat() if user.created_at else datetime.utcnow().isoformat(),
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error("get_me_failed", user_id=current_user.get("id"), error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve user profile: {str(e)}",
        )


class UpdateProfileRequest(BaseModel):
    """Request body for updating user profile."""
    name: str | None = Field(None, max_length=100, description="User's display name")


class UpdateProfileResponse(BaseModel):
    """Response for successful profile update."""
    id: str
    email: str
    name: str | None
    message: str = "Profile updated successfully"


@router.patch(
    "/me",
    response_model=UpdateProfileResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def update_profile(
    request: Request,
    body: UpdateProfileRequest,
    current_user: CurrentUser,
) -> UpdateProfileResponse:
    """
    Update current user's profile information.
    
    - **name**: Updated display name (optional)
    
    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)
    
    user, error = await user_manager.update_profile(
        user_id=current_user["id"],
        name=body.name,
    )
    
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )
    
    return UpdateProfileResponse(
        id=user.id,
        email=user.email,
        name=user.name,
    )


class ChangePasswordRequest(BaseModel):
    """Request body for changing password."""
    current_password: str = Field(description="Current password for verification")
    new_password: str = Field(min_length=8, description="New password (at least 8 characters)")


class ChangePasswordResponse(BaseModel):
    """Response for successful password change."""
    message: str = "Password changed successfully"


@router.post(
    "/change-password",
    response_model=ChangePasswordResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    current_user: CurrentUser,
) -> ChangePasswordResponse:
    """
    Change current user's password.
    
    - **current_password**: Current password for security verification
    - **new_password**: New password (at least 8 characters)
    
    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)
    
    success, error = await user_manager.change_password(
        user_id=current_user["id"],
        current_password=body.current_password,
        new_password=body.new_password,
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Failed to change password",
        )
    
    return ChangePasswordResponse()


class DeleteAccountRequest(BaseModel):
    """Request body for account deletion."""
    password: str = Field(description="Password for confirmation")


class DeleteAccountResponse(BaseModel):
    """Response for successful account deletion."""
    message: str = "Account has been deactivated"


@router.delete(
    "/me",
    response_model=DeleteAccountResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def delete_account(
    request: Request,
    body: DeleteAccountRequest,
    current_user: CurrentUser,
) -> DeleteAccountResponse:
    """
    Deactivate current user's account.
    
    - **password**: Password for security confirmation
    
    This is a soft delete - the account is deactivated but data is retained.
    Contact support if you need complete data deletion.
    
    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)
    
    success, error = await user_manager.delete_account(
        user_id=current_user["id"],
        password=body.password,
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Failed to deactivate account",
        )
    
    # Invalidate current token
    await user_manager.invalidate_token(current_user["id"], current_user["token"])
    
    return DeleteAccountResponse()
