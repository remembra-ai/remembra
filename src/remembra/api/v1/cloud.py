"""Cloud billing, usage, and subscription endpoints – /api/v1/cloud."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, RequireMasterKey
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier, get_plan
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter

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
    provisioner = TenantProvisioner(meter=meter, key_manager=key_manager)

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
        customer_id = await billing.create_customer(
            user_id=current_user.user_id,
            email=f"{current_user.user_id}@remembra.dev",  # Placeholder
            name=current_user.name,
        )
        await meter.register_tenant(
            user_id=current_user.user_id,
            stripe_customer_id=customer_id,
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
            provisioner = TenantProvisioner(meter=meter, key_manager=key_manager)
            
            # Provision new account with their paid plan
            provision_result = await provisioner.provision(
                email=result.customer_email,
                name=result.customer_name,
                plan=result.plan or PlanTier.PRO,
                stripe_customer_id=result.stripe_customer_id,
            )
            user_id = provision_result.user_id
            
            # Send welcome email with API key
            # TODO: Integrate with email service to send credentials
            import logging
            logging.getLogger(__name__).info(
                "New paid customer provisioned: email=%s user_id=%s plan=%s api_key=%s",
                result.customer_email,
                user_id,
                result.plan.value if result.plan else "pro",
                provision_result.api_key[:8] + "...",  # Log partial key for debugging
            )
        
        if user_id:
            await meter.register_tenant(
                user_id=user_id,
                plan=result.plan or PlanTier.PRO,
                stripe_customer_id=result.stripe_customer_id,
                stripe_subscription_id=result.stripe_subscription_id,
            )

    elif result.action == "update_subscription":
        if result.user_id:
            await meter.update_plan(
                user_id=result.user_id,
                plan=result.plan or PlanTier.PRO,
                stripe_subscription_id=result.stripe_subscription_id,
            )

    elif result.action == "cancel_subscription":
        if result.user_id:
            await meter.update_plan(
                user_id=result.user_id,
                plan=PlanTier.FREE,
            )

    elif result.action == "payment_failed":
        # For now, just log — don't downgrade immediately.
        # Stripe retries before cancelling.
        pass

    return {"status": "ok", "action": result.action}
