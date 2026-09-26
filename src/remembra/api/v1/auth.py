"""Authentication API endpoints for user signup, login, and password management."""

import asyncio
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator

from remembra.auth.middleware import authenticate_jwt, get_client_ip
from remembra.auth.superadmin import account_is_owner
from remembra.auth.users import UserManager, email_verified_on_another_account
from remembra.cloud.signup_guard import TURNSTILE_HEADER, guard_signup
from remembra.config import get_settings
from remembra.core.limiter import limiter
from remembra.core.time import utcnow
from remembra.security import state as security_state

# Email imports (optional - only if cloud module available)
try:
    from remembra.cloud.email import EmailProvider, EmailService

    EMAIL_AVAILABLE = True
except ImportError:
    EMAIL_AVAILABLE = False

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# HTTP Bearer token security
bearer_scheme = HTTPBearer(auto_error=False)

DEFAULT_DASHBOARD_URL = "https://app.remembra.dev"


def dashboard_link(path: str) -> str:
    """Absolute dashboard URL for emailed links (``REMEMBRA_PUBLIC_DASHBOARD_URL``, else the hosted app)."""
    return f"{get_settings().public_dashboard_url or DEFAULT_DASHBOARD_URL}{path}"


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    """Request body for user signup."""

    email: EmailStr
    password: str = Field(
        min_length=8, description="Password must be at least 8 chars: uppercase, lowercase, number, special char"
    )
    name: str | None = Field(None, max_length=100, description="User's display name")
    turnstile_token: str | None = Field(
        None, max_length=4096, description="Cloudflare Turnstile token (required when Turnstile is enabled)"
    )

    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        """Enforce password complexity requirements."""
        import re

        errors = []
        if len(v) < 8:
            errors.append("at least 8 characters")
        if not re.search(r"[A-Z]", v):
            errors.append("one uppercase letter")
        if not re.search(r"[a-z]", v):
            errors.append("one lowercase letter")
        if not re.search(r"\d", v):
            errors.append("one number")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~]', v):
            errors.append("one special character (!@#$%^&*...)")
        if errors:
            raise ValueError(f"Password must contain: {', '.join(errors)}")
        return v

    @field_validator("name")
    @classmethod
    def sanitize_name(cls, v: str | None) -> str | None:
        """Strip HTML/script tags from name to prevent XSS."""
        if v is None:
            return None
        import re

        # Remove HTML tags
        clean = re.sub(r"<[^>]+>", "", v)
        # Remove common script patterns
        clean = re.sub(r"javascript:", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"on\w+\s*=", "", clean, flags=re.IGNORECASE)
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
    totp_code: str | None = Field(None, min_length=6, max_length=6, description="6-digit TOTP code (required if 2FA enabled)")


class LoginResponse(BaseModel):
    """Response for successful login."""

    access_token: str | None = None
    token_type: str = "bearer"
    user: dict[str, Any] | None = None
    requires_2fa: bool = False
    message: str | None = None


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
    new_password: str = Field(min_length=8, description="New password with complexity requirements")

    @field_validator("new_password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        """Enforce password complexity requirements."""
        import re

        errors = []
        if len(v) < 8:
            errors.append("at least 8 characters")
        if not re.search(r"[A-Z]", v):
            errors.append("one uppercase letter")
        if not re.search(r"[a-z]", v):
            errors.append("one lowercase letter")
        if not re.search(r"\d", v):
            errors.append("one number")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~]', v):
            errors.append("one special character (!@#$%^&*...)")
        if errors:
            raise ValueError(f"Password must contain: {', '.join(errors)}")
        return v


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
    is_admin: bool = False
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
) -> dict[str, Any]:
    """
    Dependency that validates JWT token and returns user info.

    Raises 401 if token is missing or invalid.
    Raises 500 if server is misconfigured.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_manager = await get_user_manager(request)
    except HTTPException:
        # Re-raise HTTP exceptions (500 from get_user_manager)
        raise
    except Exception as e:
        log.error("get_user_manager_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service unavailable",
        )

    # Full validation: signature/expiry, logout blacklist, password-change
    # invalidation, and account active status (fails closed).
    session = await authenticate_jwt(request, credentials.credentials)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user from database
    try:
        user = await user_manager.get_user_by_id(session.user_id)
    except Exception as e:
        log.error("get_user_by_id_failed", error=str(e), user_id=session.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user data",
        )

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
CurrentUser = Annotated[dict[str, Any], Depends(get_current_user_from_jwt)]


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
    body: Annotated[SignupRequest, Body(...)],
) -> SignupResponse:
    """
    Create a new user account.

    - **email**: Valid email address (will be lowercased)
    - **password**: At least 8 characters
    - **name**: Optional display name
    - **turnstile_token**: Cloudflare Turnstile token (or the ``CF-Turnstile-Response``
      header) — required when Turnstile is enabled on this server

    Hardening: 3 signups/hour per client /24, 20/day per email domain (charged
    after Turnstile passes). Once ``unverified_credit_cap_effective_at`` is set,
    new Free accounts get 25 smart credits until the email is verified.
    """
    await guard_signup(
        client_ip=get_client_ip(request),
        email=str(body.email),
        turnstile_token=body.turnstile_token or request.headers.get(TURNSTILE_HEADER),
    )
    user_manager = await get_user_manager(request)

    user, error = await user_manager.create_user(
        email=body.email,
        password=body.password,
        name=body.name,
    )

    if error == "Email already registered":
        # Do not reveal whether an address has an account: answer exactly like a
        # successful signup (same status, shape and roughly the same latency).
        user_manager.hash_password(body.password)
        log.info("signup_existing_email_suppressed")
        return SignupResponse(
            id=UserManager.generate_user_id(),
            email=body.email.lower().strip(),
            name=body.name,
        )
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )
    assert user is not None  # create_user returns a user whenever error is falsy

    # Send welcome email with API key (fire-and-forget, don't block signup)
    if EMAIL_AVAILABLE:
        try:
            settings = get_settings()
            if settings.resend_api_key:
                # Create an API key for the new user
                key_manager = request.app.state.api_key_manager
                api_key = await key_manager.create_key(
                    user_id=user.id,
                    name="Default Key",
                )

                email_service = EmailService.create(provider=EmailProvider.RESEND)
                asyncio.create_task(
                    email_service.send_welcome_email(
                        to=user.email,
                        api_key=api_key.key,  # Pass just the key string, not the full object
                        user_id=user.id,
                        plan="Free",
                    )
                )
                log.info("welcome_email_queued", user_id=user.id)
        except Exception as e:
            # Don't fail signup if email fails
            log.warning("welcome_email_failed", user_id=user.id, error_type=type(e).__name__)

    await _send_signup_verification(user_manager, user.id, user.email)

    return SignupResponse(
        id=user.id,
        email=user.email,
        name=user.name,
    )


async def _send_signup_verification(user_manager: UserManager, user_id: str, email: str) -> None:
    """Email the verification link right after signup (best effort; never fails the signup)."""
    if not EMAIL_AVAILABLE or not get_settings().resend_api_key:
        return
    try:
        token = await security_state.create_email_verification(user_manager.db, user_id, email)
        email_service = EmailService.create(provider=EmailProvider.RESEND)
        result = await email_service.send_email_verification_email(
            to=email, verify_url=dashboard_link(f"/verify-email?token={token}")
        )
        if not result.success:
            log.warning("signup_verification_email_failed", user_id=user_id)
    except Exception as e:
        log.warning("signup_verification_email_failed", user_id=user_id, error_type=type(e).__name__)


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse}},
)
@limiter.limit("10/minute")  # Prevent brute force
async def login(
    request: Request,
    body: Annotated[LoginRequest, Body(...)],
) -> LoginResponse:
    """
    Authenticate and get an access token.

    - **email**: Registered email address
    - **password**: Account password
    - **totp_code**: 6-digit TOTP code (required if 2FA is enabled)

    Returns a JWT access token valid for 7 days.
    If 2FA is enabled and no totp_code provided, returns requires_2fa=true.
    """
    user_manager = await get_user_manager(request)
    db = user_manager.db

    # Per-account lockout (independent of client IP) stops distributed
    # password / TOTP guessing against a single account.
    lock_key = security_state.account_key("login", body.email)
    remaining = await security_state.lockout_remaining_seconds(db, lock_key)
    if remaining:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(remaining)},
        )

    user, token, error = await user_manager.authenticate(
        email=body.email,
        password=body.password,
    )

    if error:
        await security_state.record_failure(db, lock_key)
        # Same message for unknown email / wrong password / inactive account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    assert user is not None  # authenticate returns a user whenever error is falsy

    # Check if 2FA is enabled
    if await user_manager.is_totp_enabled(user.id):
        if not body.totp_code:
            # Password correct but need 2FA code
            return LoginResponse(
                requires_2fa=True,
                message="2FA code required",
            )

        # Verify TOTP code (single-use; failures count toward lockout)
        if not await user_manager.verify_totp(user.id, body.totp_code):
            await security_state.record_failure(db, lock_key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid 2FA code",
            )

    await security_state.clear_failures(db, lock_key)

    is_admin = account_is_owner(await db.get_user_by_id(user.id))

    return LoginResponse(
        access_token=token,
        user={
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "email_verified": user.email_verified,
            "is_admin": is_admin,
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
    body: Annotated[ForgotPasswordRequest, Body(...)],
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

    # Send password reset email if token was generated
    if reset_token:
        # SECURITY: Never log reset tokens - they grant account access
        log.info("password_reset_token_generated")

        # Send the password reset email
        try:
            from remembra.cloud.email import EmailProvider, EmailService

            email_service = EmailService.create(provider=EmailProvider.RESEND)
            reset_url = f"https://app.remembra.dev/reset-password?token={reset_token}&email={body.email}"

            result = await email_service.send_password_reset_email(
                to=body.email,
                reset_url=reset_url,
                expires_in="1 hour",
            )

            if result.success:
                log.info("password_reset_email_sent")
            else:
                log.warning("password_reset_email_failed", error=result.error)
        except Exception as e:
            # Don't fail the request if email sending fails
            log.error("password_reset_email_error", error_type=type(e).__name__)

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
    body: Annotated[ResetPasswordRequest, Body(...)],
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

        # Superadmin only for verified owner accounts (never an unverified email claim)
        is_admin = account_is_owner(await user_manager.db.get_user_by_id(user.id))

        return UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
            email_verified=bool(user.email_verified),  # Ensure boolean
            is_active=bool(user.is_active),  # Ensure boolean
            is_admin=is_admin,
            created_at=user.created_at.isoformat() if user.created_at else utcnow().isoformat(),
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error("get_me_failed", user_id=current_user.get("id"), error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user profile",
        ) from e


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
    assert user is not None  # update_profile returns a user whenever error is falsy

    return UpdateProfileResponse(
        id=user.id,
        email=user.email,
        name=user.name,
    )


class ChangePasswordRequest(BaseModel):
    """Request body for changing password."""

    current_password: str = Field(description="Current password for verification")
    new_password: str = Field(min_length=8, description="New password with complexity requirements")

    @field_validator("new_password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        """Enforce password complexity requirements."""
        import re

        errors = []
        if len(v) < 8:
            errors.append("at least 8 characters")
        if not re.search(r"[A-Z]", v):
            errors.append("one uppercase letter")
        if not re.search(r"[a-z]", v):
            errors.append("one lowercase letter")
        if not re.search(r"\d", v):
            errors.append("one number")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~]', v):
            errors.append("one special character (!@#$%^&*...)")
        if errors:
            raise ValueError(f"Password must contain: {', '.join(errors)}")
        return v


class ChangePasswordResponse(BaseModel):
    """Response for successful password change."""

    message: str = "Password changed successfully"


@router.post(
    "/change-password",
    response_model=ChangePasswordResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")  # password / TOTP guessing with a stolen session
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
@limiter.limit("5/minute")  # password / TOTP guessing with a stolen session
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

    A paid subscription is cancelled first (at the end of the period already
    paid for): a deactivated account can no longer reach Billing to cancel
    it. When it cannot be cancelled, the account is not deactivated (400).

    Requires a valid Bearer token in the Authorization header.
    """
    from remembra.api.v1.billing import end_subscription_before_account_deletion

    user_manager = await get_user_manager(request)

    async def end_billing() -> str | None:
        return await end_subscription_before_account_deletion(request, current_user["id"])

    success, error = await user_manager.delete_account(
        user_id=current_user["id"],
        password=body.password,
        before_deactivate=end_billing,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Failed to deactivate account",
        )

    # Invalidate current token
    await user_manager.invalidate_token(current_user["id"], current_user["token"])

    return DeleteAccountResponse()


# ---------------------------------------------------------------------------
# Two-Factor Authentication (2FA) Endpoints
# ---------------------------------------------------------------------------


class TotpSetupResponse(BaseModel):
    """Response for TOTP setup initiation."""

    secret: str
    provisioning_uri: str
    message: str = "Scan the QR code with your authenticator app, then verify"


class TotpVerifyRequest(BaseModel):
    """Request body for TOTP verification."""

    code: str = Field(min_length=6, max_length=6, description="6-digit TOTP code")


class TotpVerifyResponse(BaseModel):
    """Response for TOTP verification/enable."""

    message: str = "Two-factor authentication enabled successfully"


class TotpDisableRequest(BaseModel):
    """Request body for TOTP disable."""

    password: str = Field(description="Password for confirmation")


class TotpDisableResponse(BaseModel):
    """Response for TOTP disable."""

    message: str = "Two-factor authentication disabled"


class TotpStatusResponse(BaseModel):
    """Response for TOTP status check."""

    enabled: bool
    message: str


@router.post(
    "/2fa/setup",
    response_model=TotpSetupResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def setup_totp(
    request: Request,
    current_user: CurrentUser,
) -> TotpSetupResponse:
    """
    Initiate 2FA setup.

    Returns a secret and provisioning URI for QR code generation.
    User must verify with a code before 2FA is enabled.

    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)

    secret, provisioning_uri, error = await user_manager.setup_totp(current_user["id"])

    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    return TotpSetupResponse(
        secret=secret,
        provisioning_uri=provisioning_uri,
    )


@router.post(
    "/2fa/enable",
    response_model=TotpVerifyResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")  # password / TOTP guessing with a stolen session
async def enable_totp(
    request: Request,
    body: TotpVerifyRequest,
    current_user: CurrentUser,
) -> TotpVerifyResponse:
    """
    Enable 2FA by verifying a TOTP code.

    Must call /2fa/setup first to get the secret.

    - **code**: 6-digit code from authenticator app

    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)

    success, error = await user_manager.enable_totp(current_user["id"], body.code)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Failed to enable 2FA",
        )

    return TotpVerifyResponse()


@router.post(
    "/2fa/disable",
    response_model=TotpDisableResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
@limiter.limit("5/minute")  # password / TOTP guessing with a stolen session
async def disable_totp(
    request: Request,
    body: TotpDisableRequest,
    current_user: CurrentUser,
) -> TotpDisableResponse:
    """
    Disable 2FA for the current user.

    - **password**: Password for security confirmation

    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)

    success, error = await user_manager.disable_totp(current_user["id"], body.password)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or "Failed to disable 2FA",
        )

    return TotpDisableResponse()


@router.get(
    "/2fa/status",
    response_model=TotpStatusResponse,
    responses={401: {"model": ErrorResponse}},
)
async def get_totp_status(
    request: Request,
    current_user: CurrentUser,
) -> TotpStatusResponse:
    """
    Check if 2FA is enabled for the current user.

    Requires a valid Bearer token in the Authorization header.
    """
    user_manager = await get_user_manager(request)

    enabled = await user_manager.is_totp_enabled(current_user["id"])

    return TotpStatusResponse(
        enabled=enabled,
        message="2FA is enabled" if enabled else "2FA is not enabled",
    )


# ---------------------------------------------------------------------------
# API Key Verification Endpoint
# ---------------------------------------------------------------------------


class VerifyKeyResponse(BaseModel):
    """Response for API key verification."""

    valid: bool
    key_id: str | None = None
    user_id: str | None = None
    role: str | None = None
    rate_limit_tier: str | None = None
    message: str


@router.get(
    "/verify",
    response_model=VerifyKeyResponse,
    summary="Verify API key validity",
)
@limiter.limit("30/minute")
async def verify_api_key(
    request: Request,
) -> VerifyKeyResponse:
    """
    Verify if the provided API key is valid and active.

    Provide the API key in the `X-API-Key` header.

    This endpoint:
    - Confirms the key is valid and not revoked
    - Returns key metadata (role, rate limit tier)
    - Does NOT update last_used_at (test only)

    Use this to test if a key works before using it in production.
    """
    from remembra.auth.keys import APIKeyManager
    from remembra.auth.rbac import RoleManager

    api_key = request.headers.get("X-API-Key")

    if not api_key:
        return VerifyKeyResponse(
            valid=False,
            message="No API key provided. Use X-API-Key header.",
        )

    # Get API key manager from app state
    key_manager: APIKeyManager = request.app.state.api_key_manager
    role_manager: RoleManager = request.app.state.role_manager

    # Validate the key (this also updates last_used_at as a side effect)
    key_info = await key_manager.validate_key(api_key)

    if not key_info:
        return VerifyKeyResponse(
            valid=False,
            message="Invalid or revoked API key.",
        )

    # Get role info
    key_role = await role_manager.get_role(key_info["id"])

    return VerifyKeyResponse(
        valid=True,
        key_id=key_info["id"],
        user_id=key_info["user_id"],
        role=key_role.role.value,
        rate_limit_tier=key_info.get("rate_limit_tier", "standard"),
        message="API key is valid and active.",
    )


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


class VerifyEmailConfirmRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class VerifyEmailResponse(BaseModel):
    message: str
    email_verified: bool


@router.post(
    "/verify-email/request",
    response_model=VerifyEmailResponse,
    summary="Send an email-verification link to the account address",
)
@limiter.limit("3/minute")
async def request_email_verification(
    request: Request,
    current_user: CurrentUser,
) -> VerifyEmailResponse:
    """Email a single-use verification link (valid 24h) to the account's address."""
    user_manager = await get_user_manager(request)
    user_row = await user_manager.db.get_user_by_id(current_user["id"])
    if not user_row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user_row.get("email_verified"):
        return VerifyEmailResponse(message="Email already verified", email_verified=True)
    if not EMAIL_AVAILABLE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email delivery is not configured")

    token = await security_state.create_email_verification(user_manager.db, user_row["id"], user_row["email"])
    try:
        email_service = EmailService.create(provider=EmailProvider.RESEND)
        result = await email_service.send_email_verification_email(
            to=user_row["email"],
            verify_url=dashboard_link(f"/verify-email?token={token}"),
        )
    except Exception as e:
        log.error("verification_email_error", error_type=type(e).__name__)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email delivery is not configured") from e
    if not result.success:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not send verification email")
    return VerifyEmailResponse(message="Verification email sent", email_verified=False)


@router.post(
    "/verify-email/confirm",
    response_model=VerifyEmailResponse,
    summary="Confirm the account email with the emailed token",
)
@limiter.limit("10/minute")
async def confirm_email_verification(
    request: Request,
    body: VerifyEmailConfirmRequest,
    current_user: CurrentUser,
) -> VerifyEmailResponse:
    """Mark the signed-in account's email verified if the token matches."""
    user_manager = await get_user_manager(request)
    user_row = await user_manager.db.get_user_by_id(current_user["id"])
    if not user_row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    found = await security_state.find_email_verification(user_manager.db, body.token)
    if found is None or found != (user_row["id"], str(user_row["email"]).strip().lower()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired verification token")
    # One free account per verified email: an API-signup tenant (or another
    # dashboard account) may already have verified this address.
    if await email_verified_on_another_account(user_manager.db, user_row["email"], exclude_user_id=user_row["id"]):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email address is already verified on another Remembra account.",
        )
    ok = await security_state.consume_email_verification(user_manager.db, user_row["id"], user_row["email"], body.token)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired verification token")
    await user_manager.db.update_user_email_verified(user_row["id"], True)
    log.info("email_verified", user_id=user_row["id"])
    return VerifyEmailResponse(message="Email verified", email_verified=True)
