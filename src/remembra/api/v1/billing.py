"""Billing endpoints – /api/v1/billing.

Paddle is the sole billing provider.
"""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter

router = APIRouter(prefix="/billing", tags=["billing"])

log = structlog.get_logger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class PlanInfo(BaseModel):
    """Plan information."""

    id: str
    name: str
    price_monthly: int = Field(description="Price in cents")
    price_yearly: int | None = Field(None, description="Yearly price in cents")
    features: list[str]
    limits: dict[str, Any]


class PlansResponse(BaseModel):
    """Available plans."""

    plans: list[PlanInfo]
    provider: str = Field(description="Billing provider: 'paddle'")


class CheckoutRequest(BaseModel):
    """Checkout request."""

    plan: str = Field(description="Plan ID: 'pro' or 'team'")
    billing_cycle: str = Field(default="monthly", description="'monthly' or 'yearly'")


class CheckoutResponse(BaseModel):
    """Checkout response."""

    checkout_url: str | None = Field(None, description="Redirect URL for hosted checkout")
    client_token: str | None = Field(None, description="Client token for overlay checkout (Paddle)")
    transaction_id: str | None = Field(None, description="Transaction ID for overlay checkout (Paddle)")
    provider: str


class PortalResponse(BaseModel):
    """Customer portal response."""

    portal_url: str


# ---------------------------------------------------------------------------
# Billing Provider Detection
# ---------------------------------------------------------------------------


def get_billing_provider(settings: Settings) -> str:
    """Return the active billing provider. Paddle is the only supported provider."""
    if getattr(settings, "paddle_api_key", None):
        return "paddle"
    return "none"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# Plan metadata (not in PlanLimits dataclass)
PLAN_METADATA = {
    "pro": {
        "name": "Pro",
        "price_monthly": 4900,  # $49 in cents
        "price_yearly": 49900,  # $499 in cents
        "features": [
            "500K memories",
            "1M recalls/month",
            "5 team members",
            "Webhooks",
            "Observability",
            "Priority support",
        ],
    },
    "team": {
        "name": "Team",
        "price_monthly": 19900,  # $199 in cents
        "price_yearly": 199900,  # $1999 in cents
        "features": [
            "2M memories",
            "5M recalls/month",
            "25 team members",
            "SSO",
            "Webhooks",
            "Observability",
            "Priority support",
            "Dedicated support",
        ],
    },
}


@router.get(
    "/plans",
    response_model=PlansResponse,
    summary="Get available plans",
)
@limiter.limit("60/minute")
async def get_plans(
    request: Request,
    settings: SettingsDep,
) -> PlansResponse:
    """Get available subscription plans.

    Returns plan details including pricing and features.
    Does not require authentication.
    """
    provider = get_billing_provider(settings)

    if provider == "paddle":
        from remembra.cloud.plans_paddle import PLANS, PlanTier

        plans = []
        for tier in [PlanTier.PRO, PlanTier.TEAM]:
            plan = PLANS[tier]
            meta = PLAN_METADATA.get(tier.value, {})
            plans.append(
                PlanInfo(
                    id=tier.value,
                    name=meta.get("name", tier.value.title()),
                    price_monthly=meta.get("price_monthly", 0),
                    price_yearly=meta.get("price_yearly"),
                    features=meta.get("features", []),
                    limits={
                        "max_memories": plan.max_memories,
                        "max_stores_per_month": plan.max_stores_per_month,
                        "max_recalls_per_month": plan.max_recalls_per_month,
                        "max_api_keys": plan.max_api_keys,
                        "max_users": plan.max_users,
                    },
                )
            )

        return PlansResponse(plans=plans, provider="paddle")

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Billing is not configured on this instance.",
    )


class ClientConfigResponse(BaseModel):
    """Client-side config for Paddle.js checkout."""

    provider: str
    client_token: str | None = None
    prices: dict[str, str] = Field(default_factory=dict, description="Plan -> price_id mapping")
    success_url: str = "https://remembra.dev/dashboard?checkout=success"


@router.get(
    "/client-config",
    response_model=ClientConfigResponse,
    summary="Get client-side checkout config",
)
@limiter.limit("60/minute")
async def get_client_config(
    request: Request,
    settings: SettingsDep,
) -> ClientConfigResponse:
    """Get config needed for client-side Paddle.js overlay checkout.

    Returns price IDs and client token so the frontend can open
    Paddle.Checkout.open() with items directly (no server-side transaction needed).
    """
    provider = get_billing_provider(settings)

    if provider == "paddle":
        from remembra.cloud.paddle_config import get_paddle_settings

        paddle_settings = get_paddle_settings()
        config = paddle_settings.config

        return ClientConfigResponse(
            provider="paddle",
            client_token=paddle_settings.client_token,
            prices={
                "pro": config.pro_prices.monthly,
                "team": config.team_prices.monthly,
            },
            success_url="https://remembra.dev/dashboard?checkout=success",
        )

    return ClientConfigResponse(provider=provider)


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    summary="Create checkout session",
)
@limiter.limit("10/minute")
async def create_checkout(
    request: Request,
    body: Annotated[CheckoutRequest, Body(...)],
    current_user: CurrentUser,
    settings: SettingsDep,
) -> CheckoutResponse:
    """Create a checkout session for plan upgrade.

    Returns a Paddle client token for overlay checkout.
    """
    provider = get_billing_provider(settings)

    if provider == "paddle":
        from remembra.cloud.billing_paddle import PaddleBillingManager
        from remembra.cloud.paddle_config import get_paddle_settings
        from remembra.cloud.plans_paddle import PlanTier

        paddle_settings = get_paddle_settings()
        billing = PaddleBillingManager(
            api_key=paddle_settings.api_key,
            webhook_secret=paddle_settings.webhook_secret or "",
            sandbox=paddle_settings.sandbox,
        )

        try:
            plan_tier = PlanTier(body.plan.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid plan: {body.plan}. Choose 'pro' or 'team'.",
            )

        # Fetch user email from database (AuthenticatedUser doesn't have email)
        db = request.app.state.db
        user_data = await db.get_user_by_id(current_user.user_id)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        user_email = user_data.get("email")
        if not user_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User email not found. Please update your profile.",
            )

        result = await billing.create_checkout_session(
            customer_id=None,  # Will be created
            plan=plan_tier,
            user_id=current_user.user_id,
            email=user_email,
        )

        return CheckoutResponse(
            checkout_url=result.get("checkout_url"),
            client_token=result.get("client_token"),
            transaction_id=result.get("transaction_id"),
            provider="paddle",
        )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Billing is not configured on this instance.",
    )


@router.post(
    "/portal",
    response_model=PortalResponse,
    summary="Get customer portal URL",
)
@limiter.limit("10/minute")
async def get_portal(
    request: Request,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> PortalResponse:
    """Get URL to customer billing portal.

    Users can manage subscriptions, update payment methods, view invoices.
    """
    provider = get_billing_provider(settings)

    if provider == "paddle":
        from remembra.cloud.billing_paddle import PaddleBillingManager
        from remembra.cloud.paddle_config import get_paddle_settings

        paddle_settings = get_paddle_settings()
        billing = PaddleBillingManager(
            api_key=paddle_settings.api_key,
            webhook_secret=paddle_settings.webhook_secret or "",
            sandbox=paddle_settings.sandbox,
        )

        # Fetch user email from database (AuthenticatedUser doesn't have email)
        db = request.app.state.db
        user_data = await db.get_user_by_id(current_user.user_id)
        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        user_email = user_data.get("email")
        if not user_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User email not found. Please update your profile.",
            )

        # Look up customer by email address
        url = await billing.create_portal_session_by_email(user_email)

        if not url:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No billing account found. Please contact support if you have an active subscription.",
            )

        return PortalResponse(portal_url=url)

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Billing is not configured on this instance.",
    )


@router.post(
    "/webhook/paddle",
    summary="Paddle webhook handler",
    include_in_schema=False,
)
async def paddle_webhook(request: Request) -> dict[str, str]:
    """Process Paddle webhook events.

    Handles subscription lifecycle events.
    Validates via Paddle webhook signature.
    """
    settings = get_settings()

    provider = get_billing_provider(settings)
    if provider != "paddle":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paddle billing is not configured.",
        )

    from remembra.cloud.billing_paddle import PaddleBillingManager
    from remembra.cloud.paddle_config import get_paddle_settings

    paddle_settings = get_paddle_settings()
    if not paddle_settings.webhook_secret:
        # Fail closed: without a secret the signature check is meaningless.
        log.error("paddle_webhook_secret_missing")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paddle webhook verification is not configured.",
        )
    billing = PaddleBillingManager(
        api_key=paddle_settings.api_key,
        webhook_secret=paddle_settings.webhook_secret,
        sandbox=paddle_settings.sandbox,
    )

    # Get raw body and signature
    payload = await request.body()
    signature = request.headers.get("paddle-signature", "")

    try:
        event = billing.verify_webhook(payload, signature)
    except ValueError as e:
        log.warning("paddle_webhook_rejected", reason=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        ) from e

    # Process the event and apply it to the tenant's plan.
    result = await billing.handle_webhook_event(event)
    applied = await _apply_paddle_result(request, result)

    return {"status": "ok", "action": result.action if result else "ignored", "applied": applied}


async def _apply_paddle_result(request: Request, result: Any) -> str:
    """Persist a verified Paddle lifecycle event to the tenant record.

    Returns a short status: ``applied``, ``no_change`` or ``unmatched``.
    Raises 503 when metering is unavailable so Paddle retries later.
    """
    if result is None or result.action in ("ignored", "payment_failed", "payment_issue"):
        if result is not None and result.action in ("payment_failed", "payment_issue"):
            log.warning("paddle_payment_problem", user_id=result.user_id, action=result.action)
        return "no_change"

    meter = getattr(request.app.state, "usage_meter", None)
    if meter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Usage metering is not available; retry later.",
        )

    from remembra.cloud.plans import PlanTier as CloudPlanTier

    user_id = result.user_id
    db = request.app.state.db
    known = bool(user_id) and (await db.get_user_by_id(user_id) is not None or await meter.get_tenant(user_id) is not None)
    if not known:
        log.warning("paddle_event_unmatched_user", action=result.action)
        return "unmatched"

    plan = CloudPlanTier.FREE if result.action == "cancel_subscription" else CloudPlanTier(result.plan.value)
    await meter.register_tenant(
        user_id,
        plan=plan,
        stripe_customer_id=result.paddle_customer_id,
        stripe_subscription_id=result.paddle_subscription_id,
        email=result.customer_email,
        name=result.customer_name,
    )

    team_manager = getattr(request.app.state, "team_manager", None)
    if team_manager is not None:
        try:
            from remembra.cloud.plans import get_plan as get_cloud_plan

            await team_manager.update_owner_teams_plan(
                owner_id=user_id, plan=plan.value, max_seats=get_cloud_plan(plan).max_users
            )
        except Exception as e:  # team sync is best-effort; the tenant plan is authoritative
            log.warning("paddle_team_plan_sync_failed", user_id=user_id, error_type=type(e).__name__)

    log.info("paddle_plan_applied", user_id=user_id, plan=plan.value, action=result.action)
    return "applied"
