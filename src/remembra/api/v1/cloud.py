"""Cloud billing, usage, and subscription endpoints – /api/v1/cloud."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    context: Literal["personal", "team"] = Field(
        description="Whether user is viewing personal or team billing context"
    )
    # Personal context fields
    plan: str | None = Field(None, description="Plan tier (free/pro/team/enterprise)")
    # Team context fields
    team_id: str | None = Field(None, description="Team ID if in team context")
    team_name: str | None = Field(None, description="Team name for display")
    team_plan: str | None = Field(None, description="Team's plan tier")
    role: str | None = Field(None, description="User's role in team (owner/admin/member/viewer)")
    can_manage_billing: bool = Field(
        False, description="Whether user can access billing management"
    )
    owner_email: str | None = Field(
        None, description="Team owner's email (for 'contact admin' messaging)"
    )
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
    body: SignupRequest,
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
        from remembra.cloud.email import EmailService, EmailProvider
        email_service = EmailService.create(provider=EmailProvider.RESEND)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Email service not available, skipping welcome email: %s", str(e)
        )
    
    provisioner = TenantProvisioner(
        meter=meter, 
        key_manager=key_manager,
        email_service=email_service,
    )

    # Optionally create Stripe customer
    stripe_customer_id = None
    if settings.stripe_secret_key and body.email:
        from remembra.cloud.billing import BillingManager

        billing = BillingManager(
            stripe_secret_key=settings.stripe_secret_key,
            stripe_webhook_secret=settings.stripe_webhook_secret or "",
        )
        stripe_customer_id = await billing.create_customer(
            user_id=body.user_id or "pending",
            email=body.email,
            name=body.name,
        )

    try:
        result = await provisioner.provision(
            user_id=body.user_id,
            email=body.email,
            name=body.name,
            plan=PlanTier.FREE,
            stripe_customer_id=stripe_customer_id,
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
# Billing endpoints (requires Stripe)
# ---------------------------------------------------------------------------


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    summary="Create a Stripe checkout session",
)
@limiter.limit("10/minute")
async def create_checkout(
    request: Request,
    body: CheckoutRequest,
    current_user: CurrentUser,
    meter: UsageMeterDep,
    settings: SettingsDep,
) -> CheckoutResponse:
    """Create a Stripe Checkout session for plan upgrade.

    Returns a checkout URL to redirect the user to.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured on this instance.",
        )

    try:
        target_plan = PlanTier(body.plan.lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid plan: {body.plan}. Choose 'pro' or 'enterprise'.",
        )

    if target_plan == PlanTier.FREE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot checkout to free plan. Use the billing portal to cancel.",
        )

    # Lazy import to avoid requiring stripe when cloud is off
    from remembra.cloud.billing import BillingManager

    billing = BillingManager(
        stripe_secret_key=settings.stripe_secret_key,
        stripe_webhook_secret=settings.stripe_webhook_secret or "",
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
    )

    # Ensure customer exists
    tenant = await meter.get_tenant(current_user.user_id)
    if tenant and tenant.get("stripe_customer_id"):
        customer_id = tenant["stripe_customer_id"]
    else:
        # Get user's actual email from database
        user_email = await meter.get_user_email(current_user.user_id)
        if not user_email:
            # Fallback: check tenant record
            user_email = tenant.get("email") if tenant else None
        if not user_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No email found for user. Please update your profile.",
            )
        
        customer_id = await billing.create_customer(
            user_id=current_user.user_id,
            email=user_email,
            name=current_user.name,
        )
        await meter.register_tenant(
            user_id=current_user.user_id,
            stripe_customer_id=customer_id,
            email=user_email,
            name=current_user.name,
        )

    url = await billing.create_checkout_session(
        stripe_customer_id=customer_id,
        plan=target_plan,
        user_id=current_user.user_id,
    )

    return CheckoutResponse(checkout_url=url)


@router.post(
    "/portal",
    response_model=PortalResponse,
    summary="Create a Stripe billing portal session",
)
@limiter.limit("10/minute")
async def create_portal(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
    settings: SettingsDep,
) -> PortalResponse:
    """Create a Stripe Billing Portal session.

    Users can manage subscriptions, update payment methods,
    and view invoices.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured on this instance.",
        )

    tenant = await meter.get_tenant(current_user.user_id)
    if not tenant or not tenant.get("stripe_customer_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No billing account found. Create a subscription first.",
        )

    from remembra.cloud.billing import BillingManager

    billing = BillingManager(
        stripe_secret_key=settings.stripe_secret_key,
        stripe_webhook_secret=settings.stripe_webhook_secret or "",
    )

    url = await billing.create_portal_session(
        stripe_customer_id=tenant["stripe_customer_id"],
        return_url=settings.billing_portal_return_url,
    )

    return PortalResponse(portal_url=url)


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------


@router.post(
    "/webhook/stripe",
    summary="Stripe webhook handler",
    include_in_schema=False,
)
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Process Stripe webhook events.

    Handles subscription lifecycle events:
    - checkout.session.completed → activate subscription
    - customer.subscription.updated → plan change
    - customer.subscription.deleted → cancel subscription
    - invoice.payment_failed → handle payment failure

    NOTE: This endpoint does NOT require API key auth —
    it validates via Stripe webhook signature instead.
    """
    settings = get_settings()

    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured.",
        )

    from remembra.cloud.billing import BillingManager

    billing = BillingManager(
        stripe_secret_key=settings.stripe_secret_key,
        stripe_webhook_secret=settings.stripe_webhook_secret,
    )

    # Get raw body and signature
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = billing.verify_webhook(payload, signature)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        )

    # Process the event
    meter = getattr(request.app.state, "usage_meter", None)
    if meter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud services unavailable.",
        )

    result = await billing.handle_webhook_event(event)

    # Apply the result to our system
    if result.action == "activate_subscription":
        user_id = result.user_id
        
        # Payment link flow: no existing user, create from email
        if not user_id and result.customer_email:
            from remembra.cloud.provisioning import TenantProvisioner
            
            key_manager = request.app.state.api_key_manager
            
            # Initialize email service for welcome email
            email_service = None
            try:
                from remembra.cloud.email import EmailService, EmailProvider
                email_service = EmailService.create(provider=EmailProvider.RESEND)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Email service not available, skipping welcome email: %s", str(e)
                )
            
            provisioner = TenantProvisioner(
                meter=meter, 
                key_manager=key_manager,
                email_service=email_service,
            )
            
            # Provision new account with their paid plan (sends welcome email)
            provision_result = await provisioner.provision(
                email=result.customer_email,
                name=result.customer_name,
                plan=result.plan or PlanTier.PRO,
                stripe_customer_id=result.stripe_customer_id,
            )
            user_id = provision_result.user_id
            
            import logging
            logging.getLogger(__name__).info(
                "New paid customer provisioned: email=%s user_id=%s plan=%s",
                result.customer_email,
                user_id,
                result.plan.value if result.plan else "pro",
            )
        
        if user_id:
            await meter.register_tenant(
                user_id=user_id,
                plan=result.plan or PlanTier.PRO,
                stripe_customer_id=result.stripe_customer_id,
                stripe_subscription_id=result.stripe_subscription_id,
            )
            # Sync existing team plans with new billing
            team_manager = getattr(request.app.state, "team_manager", None)
            if team_manager:
                plan = result.plan or PlanTier.PRO
                plan_limits = get_plan(plan)
                await team_manager.update_owner_teams_plan(
                    owner_id=user_id,
                    plan=plan.value,
                    max_seats=plan_limits.max_users,
                )

    elif result.action == "update_subscription":
        if result.user_id:
            await meter.update_plan(
                user_id=result.user_id,
                plan=result.plan or PlanTier.PRO,
                stripe_subscription_id=result.stripe_subscription_id,
            )
            # Sync team plans with billing
            team_manager = getattr(request.app.state, "team_manager", None)
            if team_manager:
                plan = result.plan or PlanTier.PRO
                plan_limits = get_plan(plan)
                await team_manager.update_owner_teams_plan(
                    owner_id=result.user_id,
                    plan=plan.value,
                    max_seats=plan_limits.max_users,
                )

    elif result.action == "cancel_subscription":
        if result.user_id:
            await meter.update_plan(
                user_id=result.user_id,
                plan=PlanTier.FREE,
            )
            # Downgrade team plans to free
            team_manager = getattr(request.app.state, "team_manager", None)
            if team_manager:
                plan_limits = get_plan(PlanTier.FREE)
                await team_manager.update_owner_teams_plan(
                    owner_id=result.user_id,
                    plan="free",
                    max_seats=plan_limits.max_users,
                )

    elif result.action == "payment_failed":
        # For now, just log — don't downgrade immediately.
        # Stripe retries before cancelling.
        pass

    # Send email notifications for billing events
    try:
        from remembra.cloud.email import EmailService, EmailProvider
        from remembra.cloud.webhook_email_integration import StripeWebhookEmailHandler
        
        email_service = EmailService.create(provider=EmailProvider.RESEND)
        email_handler = StripeWebhookEmailHandler(email_service)
        await email_handler.handle_event(event)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to send email notification for webhook event: %s", str(e)
        )

    return {"status": "ok", "action": result.action}
