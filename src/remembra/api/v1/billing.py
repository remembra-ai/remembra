"""Billing endpoints – /api/v1/billing.

Supports both Stripe and Paddle based on REMEMBRA_BILLING_PROVIDER config.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.config import Settings, get_settings
from remembra.core.limiter import limiter

router = APIRouter(prefix="/billing", tags=["billing"])

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
    provider: str = Field(description="Billing provider: 'stripe' or 'paddle'")


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
    """Determine which billing provider to use."""
    provider = getattr(settings, "billing_provider", None)
    if provider:
        return provider.lower()

    # Auto-detect based on configured keys
    if getattr(settings, "paddle_api_key", None):
        return "paddle"
    if getattr(settings, "stripe_secret_key", None):
        return "stripe"

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

    elif provider == "stripe":
        from remembra.cloud.plans import PLANS, PlanTier

        plans = []
        for tier in [PlanTier.PRO, PlanTier.ENTERPRISE]:
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

        return PlansResponse(plans=plans, provider="stripe")

    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured on this instance.",
        )


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

    For Paddle: Returns client token for overlay checkout.
    For Stripe: Returns redirect URL for hosted checkout.
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

    elif provider == "stripe":
        # Delegate to existing cloud checkout
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/api/v1/cloud/checkout"},
            detail="Use /api/v1/cloud/checkout for Stripe billing.",
        )

    else:
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

        # Look up customer by email address
        url = await billing.create_portal_session_by_email(current_user.email)

        if not url:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No billing account found. Please contact support if you have an active subscription.",
            )

        return PortalResponse(portal_url=url)

    elif provider == "stripe":
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/api/v1/cloud/portal"},
            detail="Use /api/v1/cloud/portal for Stripe billing.",
        )

    else:
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
    from remembra.cloud.paddle_config import get_paddle_config

    config = get_paddle_config()
    billing = PaddleBillingManager(
        api_key=config.api_key,
        webhook_secret=config.webhook_secret or "",
        sandbox=config.sandbox,
    )

    # Get raw body and signature
    payload = await request.body()
    signature = request.headers.get("paddle-signature", "")

    try:
        event = billing.verify_webhook(payload.decode(), signature)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid webhook signature: {e}",
        )

    # Process the event
    result = await billing.handle_webhook_event(event)

    # TODO: Apply result to metering system (similar to Stripe webhook)

    return {"status": "ok", "action": result.action if result else "ignored"}
