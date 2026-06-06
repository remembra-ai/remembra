"""Cloud billing, usage, and subscription endpoints – /api/v1/cloud."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, RequireMasterKey
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier, get_plan
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter
from remembra.teams.manager import TeamManager

router = APIRouter(prefix="/cloud", tags=["cloud"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_usage_meter(request: Request) -> UsageMeter:
    """Dependency to get UsageMeter from app state."""
    meter = getattr(request.app.state, "usage_meter", None)
    if meter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud features are not enabled on this instance.",
        )
    return meter


UsageMeterDep = Annotated[UsageMeter, Depends(get_usage_meter)]


def get_team_manager(request: Request) -> TeamManager | None:
    """Dependency to get TeamManager from app state (optional)."""
    return getattr(request.app.state, "team_manager", None)


TeamManagerDep = Annotated[TeamManager | None, Depends(get_team_manager)]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class UsageResponse(BaseModel):
    user_id: str
    plan: str
    period: str
    stores: int
    recalls: int
    deletes: int
    active_days: int
    limits: dict[str, Any]


class PlanInfoResponse(BaseModel):
    plan: str
    limits: dict[str, Any]
    usage: dict[str, Any]
    limit_checks: dict[str, Any]


class CheckoutRequest(BaseModel):
    plan: str = Field(description="Target plan: 'pro' or 'enterprise'")


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalResponse(BaseModel):
    portal_url: str


class DailyUsageResponse(BaseModel):
    user_id: str
    days: list[dict[str, Any]]


class SignupRequest(BaseModel):
    email: str = Field(description="User's email address")
    name: str | None = Field(None, description="Display name")
    user_id: str | None = Field(None, description="Custom user ID (auto-generated if omitted)")


class SignupResponse(BaseModel):
    user_id: str
    api_key: str = Field(description="Your API key — store it securely! Shown only once.")
    api_key_id: str
    plan: str
    message: str


class BillingContextResponse(BaseModel):
    """Billing context for determining what the user should see.

    Following industry standards (Claude Teams, Slack, Linear, GitHub):
    - Team members see team context, not individual billing
    - Only owners can manage billing
    - Members see "You're on X plan via Team Y"
    """

    context: Literal["personal", "team"] = Field(description="Whether user is viewing personal or team billing context")
    # Personal context fields
    plan: str | None = Field(None, description="Plan tier (free/pro/team/enterprise)")
    # Team context fields
    team_id: str | None = Field(None, description="Team ID if in team context")
    team_name: str | None = Field(None, description="Team name for display")
    team_plan: str | None = Field(None, description="Team's plan tier")
    role: str | None = Field(None, description="User's role in team (owner/admin/member/viewer)")
    can_manage_billing: bool = Field(False, description="Whether user can access billing management")
    owner_email: str | None = Field(None, description="Team owner's email (for 'contact admin' messaging)")
    # Limits (from team plan or personal plan)
    limits: dict[str, Any] = Field(default_factory=dict)
    # Usage (personal contribution or team aggregate)
    usage: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Signup / Provisioning
# ---------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tenant account",
)
@limiter.limit("5/minute")
async def signup(
    request: Request,
    body: Annotated[SignupRequest, Body(...)],
    meter: UsageMeterDep,
    settings: SettingsDep,
    _: RequireMasterKey,
) -> SignupResponse:
    """Create a new tenant account with an API key.

    **Requires master key** (used by the signup page backend).

    Returns the API key — it is only shown once.
    """
    from remembra.cloud.provisioning import TenantProvisioner

    key_manager = request.app.state.api_key_manager

    # Initialize email service for welcome email
    email_service = None
    try:
        from remembra.cloud.email import EmailProvider, EmailService

        email_service = EmailService.create(provider=EmailProvider.RESEND)
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("Email service not available, skipping welcome email: %s", str(e))

    provisioner = TenantProvisioner(
        meter=meter,
        key_manager=key_manager,
        email_service=email_service,
    )

    try:
        result = await provisioner.provision(
            user_id=body.user_id,
            email=body.email,
            name=body.name,
            plan=PlanTier.FREE,
            stripe_customer_id=None,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    return SignupResponse(
        user_id=result.user_id,
        api_key=result.api_key,
        api_key_id=result.api_key_id,
        plan=result.plan.value,
        message="Store the API key securely — it cannot be retrieved again.",
    )


# ---------------------------------------------------------------------------
# Usage endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="Get current month's usage",
)
@limiter.limit("60/minute")
async def get_usage(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
) -> UsageResponse:
    """Get usage statistics for the current billing period."""
    monthly = await meter.get_monthly_usage(current_user.user_id)
    plan = await meter.get_tenant_plan(current_user.user_id)
    plan_limits = get_plan(plan)

    return UsageResponse(
        user_id=current_user.user_id,
        plan=plan.value,
        period=monthly["period"],
        stores=monthly["stores"],
        recalls=monthly["recalls"],
        deletes=monthly["deletes"],
        active_days=monthly["active_days"],
        limits={
            "max_memories": plan_limits.max_memories,
            "max_stores_per_month": plan_limits.max_stores_per_month,
            "max_recalls_per_month": plan_limits.max_recalls_per_month,
            "max_api_keys": plan_limits.max_api_keys,
            "max_storage_mb": plan_limits.max_storage_mb,
        },
    )


@router.get(
    "/usage/daily",
    response_model=DailyUsageResponse,
    summary="Get daily usage breakdown",
)
@limiter.limit("30/minute")
async def get_daily_usage(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
    days: int = 30,
) -> DailyUsageResponse:
    """Get daily usage breakdown for the last N days (default: 30)."""
    daily = await meter.get_daily_usage(current_user.user_id, days=min(days, 90))
    return DailyUsageResponse(
        user_id=current_user.user_id,
        days=daily,
    )


@router.get(
    "/plan",
    response_model=PlanInfoResponse,
    summary="Get current plan and limit status",
)
@limiter.limit("60/minute")
async def get_plan_info(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
) -> PlanInfoResponse:
    """Get current plan details, usage snapshot, and limit check results."""
    snapshot = await meter.get_usage_snapshot(current_user.user_id)
    plan_limits = get_plan(snapshot.plan)

    store_check = snapshot.check_limit("store")
    recall_check = snapshot.check_limit("recall")
    key_check = snapshot.check_limit("create_key")

    return PlanInfoResponse(
        plan=snapshot.plan.value,
        limits={
            "max_memories": plan_limits.max_memories,
            "max_stores_per_month": plan_limits.max_stores_per_month,
            "max_recalls_per_month": plan_limits.max_recalls_per_month,
            "max_api_keys": plan_limits.max_api_keys,
            "max_users": plan_limits.max_users,
            "max_projects": plan_limits.max_projects,
            "retention_days": plan_limits.retention_days,
            "has_webhooks": plan_limits.has_webhooks,
            "has_sso": plan_limits.has_sso,
            "has_observability": plan_limits.has_observability,
        },
        usage={
            "memories_stored": snapshot.memories_stored,
            "stores_this_month": snapshot.stores_this_month,
            "recalls_this_month": snapshot.recalls_this_month,
            "api_keys_active": snapshot.api_keys_active,
        },
        limit_checks={
            "store": store_check.to_dict(),
            "recall": recall_check.to_dict(),
            "create_key": key_check.to_dict(),
        },
    )


@router.get(
    "/context",
    response_model=BillingContextResponse,
    summary="Get billing context (personal vs team)",
)
@limiter.limit("60/minute")
async def get_billing_context(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
    team_manager: TeamManagerDep,
) -> BillingContextResponse:
    """Get the user's billing context — determines what they should see.

    Following industry standards (Claude Teams, Slack, Linear, Figma, GitHub):
    - If user is on a team: Show team context, hide billing management for non-owners
    - If user is not on a team: Show personal plan and billing

    This endpoint should be called before rendering the billing page to
    determine which view to show.
    """
    # Check if user is on any team
    teams = []
    if team_manager:
        try:
            teams = await team_manager.list_user_teams(current_user.user_id)
        except Exception:
            teams = []

    if teams:
        # User is on at least one team — use primary team (first one)
        team = teams[0]
        team_plan = team.get("plan", "pro")
        role = team.get("role", "member")

        # Only owners can manage billing (industry standard)
        can_manage = role == "owner"

        # Get team plan limits
        try:
            plan_limits = get_plan(PlanTier(team_plan))
        except ValueError:
            plan_limits = get_plan(PlanTier.PRO)

        # Get owner email for "contact admin" messaging
        owner_email = None
        if not can_manage:
            try:
                # Fetch owner info
                owner_id = team.get("owner_id")
                if owner_id:
                    tenant = await meter.get_tenant(owner_id)
                    if tenant:
                        owner_email = tenant.get("email")
            except Exception:
                pass

        # Get user's personal usage (their contribution to team)
        try:
            monthly = await meter.get_monthly_usage(current_user.user_id)
            usage = {
                "stores_this_month": monthly.get("stores", 0),
                "recalls_this_month": monthly.get("recalls", 0),
            }
        except Exception:
            usage = {}

        return BillingContextResponse(
            context="team",
            team_id=team["id"],
            team_name=team["name"],
            team_plan=team_plan,
            role=role,
            can_manage_billing=can_manage,
            owner_email=owner_email,
            limits={
                "max_memories": plan_limits.max_memories,
                "max_stores_per_month": plan_limits.max_stores_per_month,
                "max_recalls_per_month": plan_limits.max_recalls_per_month,
                "max_api_keys": plan_limits.max_api_keys,
                "max_users": plan_limits.max_users,
                "max_projects": plan_limits.max_projects,
                "has_webhooks": plan_limits.has_webhooks,
                "has_sso": plan_limits.has_sso,
            },
            usage=usage,
        )

    # No team — personal context
    snapshot = await meter.get_usage_snapshot(current_user.user_id)
    plan_limits = get_plan(snapshot.plan)

    return BillingContextResponse(
        context="personal",
        plan=snapshot.plan.value,
        can_manage_billing=True,  # Personal accounts always manage their own billing
        limits={
            "max_memories": plan_limits.max_memories,
            "max_stores_per_month": plan_limits.max_stores_per_month,
            "max_recalls_per_month": plan_limits.max_recalls_per_month,
            "max_api_keys": plan_limits.max_api_keys,
            "max_users": plan_limits.max_users,
            "max_projects": plan_limits.max_projects,
            "has_webhooks": plan_limits.has_webhooks,
            "has_sso": plan_limits.has_sso,
        },
        usage={
            "memories_stored": snapshot.memories_stored,
            "stores_this_month": snapshot.stores_this_month,
            "recalls_this_month": snapshot.recalls_this_month,
            "api_keys_active": snapshot.api_keys_active,
        },
    )




# ---------------------------------------------------------------------------
# Promo Codes
# ---------------------------------------------------------------------------


class PromoValidateRequest(BaseModel):
    code: str = Field(description="Promo code to validate")


class PromoRedeemRequest(BaseModel):
    code: str = Field(description="Promo code to redeem")


class PromoResponse(BaseModel):
    success: bool
    error: str | None = None
    plan: str | None = None
    duration_days: int | None = None
    expires_at: str | None = None
    message: str | None = None


class PromoListResponse(BaseModel):
    codes: list[dict]


@router.post(
    "/promo/validate",
    response_model=PromoResponse,
    summary="Validate a promo code",
    description="Check if a promo code is valid and see what benefits it provides.",
)
@limiter.limit("10/minute")
async def validate_promo_code(
    request: Request,
    body: PromoValidateRequest,
    user: CurrentUser,
) -> PromoResponse:
    """Validate a promo code without redeeming it."""
    from remembra.cloud.promocodes import PromoCodeManager

    manager = PromoCodeManager()
    result = await manager.validate(body.code, user.user_id)

    return PromoResponse(
        success=result.success,
        error=result.error,
        plan=result.plan_tier.value if result.plan_tier else None,
        duration_days=result.duration_days if result.success else None,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        message=result.message,
    )


@router.post(
    "/promo/redeem",
    response_model=PromoResponse,
    summary="Redeem a promo code",
    description="Apply a promo code to get free access or a discount.",
)
@limiter.limit("5/minute")
async def redeem_promo_code(
    request: Request,
    body: PromoRedeemRequest,
    user: CurrentUser,
    meter: UsageMeterDep,
) -> PromoResponse:
    """Redeem a promo code for the current user."""
    from remembra.cloud.promocodes import PromoCodeManager

    manager = PromoCodeManager()

    # Get user's Stripe customer ID if they have one
    tenant_info = await meter.get_tenant(user.user_id)
    stripe_customer_id = tenant_info.get("stripe_customer_id") if tenant_info else None

    result = await manager.redeem(
        code=body.code,
        user_id=user.user_id,
        email=user.email,
        stripe_customer_id=stripe_customer_id,
    )

    if result.success and result.plan_tier:
        # Update user's plan in the metering system
        await meter.update_plan(
            user_id=user.user_id,
            plan=result.plan_tier,
            promo_expires_at=result.expires_at,
        )

    return PromoResponse(
        success=result.success,
        error=result.error,
        plan=result.plan_tier.value if result.plan_tier else None,
        duration_days=result.duration_days if result.success else None,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        message=result.message,
    )


@router.get(
    "/promo/list",
    response_model=PromoListResponse,
    summary="List active promo codes",
    description="Admin endpoint: List all active promotional codes with stats.",
    dependencies=[Depends(RequireMasterKey)],
)
async def list_promo_codes(request: Request) -> PromoListResponse:
    """List all active promo codes (admin only)."""
    from remembra.cloud.promocodes import PromoCodeManager

    manager = PromoCodeManager()
    codes = manager.list_active_codes()

    return PromoListResponse(codes=codes)


@router.get(
    "/promo/{code}/stats",
    summary="Get promo code stats",
    description="Admin endpoint: Get redemption stats for a specific promo code.",
    dependencies=[Depends(RequireMasterKey)],
)
async def get_promo_stats(request: Request, code: str) -> dict:
    """Get stats for a specific promo code (admin only)."""
    from remembra.cloud.promocodes import PromoCodeManager

    manager = PromoCodeManager()
    stats = manager.get_stats(code)

    if not stats:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Promo code '{code}' not found",
        )

    return stats
