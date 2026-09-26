"""Billing endpoints – /api/v1/billing.

Paddle is the sole billing provider.
"""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, JWTOrAPIKeyUser
from remembra.cloud import notify
from remembra.cloud.paddle_config import CheckoutUnavailableError, get_paddle_config
from remembra.cloud.plans import (
    FOUNDING_ANNUAL_PRICE_CENTS,
    FOUNDING_MAX_REDEMPTIONS,
    SELF_SERVE_TIERS,
    BillingInterval,
    PlanTier,
    get_plan,
)
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
    price_monthly: int = Field(description="Price in cents (per seat for Team)")
    price_yearly: int | None = Field(None, description="Yearly price in cents (per seat for Team)")
    per_seat: bool = False
    min_seats: int = 1
    available_monthly: bool = Field(False, description="Self-serve checkout configured for monthly billing")
    available_yearly: bool = Field(False, description="Self-serve checkout configured for yearly billing")
    features: list[str]
    limits: dict[str, Any]


class FoundingOffer(BaseModel):
    """Founding 100: Solo at $108/yr, price locked for life, annual only."""

    plan: str = "solo"
    price_yearly: int = FOUNDING_ANNUAL_PRICE_CENTS
    max_redemptions: int = FOUNDING_MAX_REDEMPTIONS
    remaining: int | None = Field(None, description="Seats left (None when metering is unavailable)")
    available: bool = False


class PlansResponse(BaseModel):
    """Available plans."""

    plans: list[PlanInfo]
    founding: FoundingOffer
    provider: str = Field(description="Billing provider: 'paddle' or 'none'")


class CheckoutRequest(BaseModel):
    """Checkout request."""

    plan: str = Field(description="Plan ID: 'solo', 'pro', 'team' or 'founding' (Solo annual, first 100)")
    billing_cycle: str = Field(default="monthly", description="'monthly' or 'yearly'")
    seats: int | None = Field(default=None, ge=1, le=1000, description="Team seats (minimum 3)")


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


# Marketing copy per plan (limits come from the plan catalog).
PLAN_FEATURES: dict[PlanTier, list[str]] = {
    PlanTier.SOLO: [
        "2,200 smart credits/mo",
        "50K memories",
        "Relay, pickups, inbox and trail free",
        "Unlimited projects",
        "Webhooks",
        "Email support",
    ],
    PlanTier.PRO: [
        "5,000 smart credits/mo",
        "125K memories",
        "250K recalls/mo",
        "Observability and traces",
        "Priority email support",
    ],
    PlanTier.TEAM: [
        "Per seat: 2,200 smart credits, 50K memories, pooled",
        "Shared projects and team inbox",
        "Roles (owner, member, viewer)",
        "Priority support",
    ],
}


def _limits_payload(tier: PlanTier) -> dict[str, Any]:
    plan = get_plan(tier)
    return {
        "max_memories": plan.max_memories,
        "smart_credits_per_month": plan.max_smart_credits_per_month,
        "llm_ceiling_usd_month": plan.llm_ceiling_usd_month,
        "max_recalls_per_month": plan.max_recalls_per_month,
        "relay_events_soft_cap": plan.max_relay_events_per_month,
        "max_content_chars": plan.max_content_chars,
        "max_batch_items": plan.max_batch_items,
        "max_api_keys": plan.max_api_keys,
        "max_projects": plan.max_projects,
        "max_users": plan.max_users,
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
    """Self-serve plans with prices, limits and whether checkout is configured.

    Does not require authentication. Prices are USD cents, tax exclusive.
    """
    provider = get_billing_provider(settings)
    config = get_paddle_config() if provider == "paddle" else None

    plans = []
    for tier in SELF_SERVE_TIERS:
        plan = get_plan(tier)
        plans.append(
            PlanInfo(
                id=tier.value,
                name=plan.display_name,
                price_monthly=plan.price_monthly_cents or 0,
                price_yearly=plan.price_annual_cents,
                per_seat=plan.per_seat,
                min_seats=plan.min_seats,
                available_monthly=bool(config and config.price_for(tier, BillingInterval.MONTH)),
                available_yearly=bool(config and config.price_for(tier, BillingInterval.YEAR)),
                features=PLAN_FEATURES.get(tier, []),
                limits=_limits_payload(tier),
            )
        )

    meter = getattr(request.app.state, "usage_meter", None)
    remaining = None
    if meter is not None:
        remaining = max(0, FOUNDING_MAX_REDEMPTIONS - await meter.founding_redemptions())
    founding = FoundingOffer(
        remaining=remaining,
        available=bool(config and config.founding_price_id) and remaining is not None and remaining > 0,
    )
    return PlansResponse(plans=plans, founding=founding, provider=provider)


class ClientConfigResponse(BaseModel):
    """Client-side config for Paddle.js checkout."""

    provider: str
    client_token: str | None = None
    prices: dict[str, str] = Field(default_factory=dict, description="Plan -> price_id mapping")
    success_url: str = "https://remembra.dev/dashboard?checkout=success"
    checkout_binding: str | None = Field(
        None,
        description=(
            "Signed-in only: put in customData as remembra_binding (with remembra_user_id). Webhooks ignore a "
            "client-side purchase for an account without it."
        ),
    )
    has_subscription: bool = Field(
        False, description="Signed-in account already holds an active subscription: no prices; use the portal"
    )


@router.get(
    "/client-config",
    response_model=ClientConfigResponse,
    summary="Get client-side checkout config",
)
@limiter.limit("60/minute")
async def get_client_config(
    request: Request,
    settings: SettingsDep,
    current_user: JWTOrAPIKeyUser,
) -> ClientConfigResponse:
    """Get config needed for client-side Paddle.js overlay checkout.

    Returns price IDs and client token so the frontend can open
    Paddle.Checkout.open() with items directly (no server-side transaction needed).

    Only single-quantity plans are listed. Team (per seat, 3-seat minimum) and
    the Founding 100 price (capped, one per account) are sold only through
    server-created transactions (``POST /billing/checkout``), where the seat
    minimum and the redemption cap are enforced; the dashboard falls back to
    that path when a plan has no client price.

    Signed in, the response carries ``checkout_binding`` (the server's
    signature over the account id, required in ``customData`` for the webhook
    to credit a client-side purchase to the account); an account that already
    holds an active subscription gets no prices and ``has_subscription``: plan
    changes go through the Paddle portal instead of a second subscription.
    """
    provider = get_billing_provider(settings)

    if provider == "paddle":
        from remembra.cloud.billing_paddle import checkout_binding
        from remembra.cloud.paddle_config import get_paddle_settings

        binding: str | None = None
        if current_user is not None and current_user.user_id:
            meter = getattr(request.app.state, "usage_meter", None)
            tenant = await meter.get_tenant(current_user.user_id) if meter is not None else None
            if meter is not None and meter.active_subscription_id(tenant):
                return ClientConfigResponse(provider="paddle", has_subscription=True)
            binding = checkout_binding(current_user.user_id)

        paddle_settings = get_paddle_settings()
        config = paddle_settings.config

        # "<plan>" is the monthly price, "<plan>_annual" the yearly one; only
        # configured prices are listed (a missing key = not purchasable yet).
        prices: dict[str, str] = {}
        for tier in SELF_SERVE_TIERS:
            if get_plan(tier).per_seat:
                continue  # quantity is client-controlled in Paddle.js: server checkout only
            monthly = config.price_for(tier, BillingInterval.MONTH)
            annual = config.price_for(tier, BillingInterval.YEAR)
            if monthly:
                prices[tier.value] = monthly
            if annual:
                prices[f"{tier.value}_annual"] = annual

        return ClientConfigResponse(
            provider="paddle",
            client_token=paddle_settings.client_token,
            prices=prices,
            success_url="https://remembra.dev/dashboard?checkout=success",
            checkout_binding=binding,
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

        paddle_settings = get_paddle_settings()
        billing = PaddleBillingManager(
            api_key=paddle_settings.api_key,
            webhook_secret=paddle_settings.webhook_secret or "",
            sandbox=paddle_settings.sandbox,
        )

        plan_name = body.plan.strip().lower()
        founding = plan_name == "founding"
        try:
            plan_tier = PlanTier.SOLO if founding else PlanTier(plan_name)
            interval = BillingInterval.parse(body.billing_cycle)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid plan or billing cycle: {body.plan}/{body.billing_cycle}. "
                "Choose 'solo', 'pro', 'team' or 'founding', billed 'monthly' or 'yearly'.",
            ) from None
        if plan_tier not in SELF_SERVE_TIERS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The {plan_tier.value} plan is not available through self-serve checkout.",
            )

        meter = getattr(request.app.state, "usage_meter", None)
        if meter is not None:
            tenant = await meter.get_tenant(current_user.user_id)
            if meter.active_subscription_id(tenant):
                # A second subscription would bill twice, and cancelling either
                # one used to drop the account to Free. Plan changes go through
                # the Paddle portal, which changes the held subscription.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This account already has an active subscription. Change or cancel it from "
                    "Manage subscription instead of buying a second one.",
                )

        plan_limits = get_plan(plan_tier)
        quantity = 1
        if plan_limits.per_seat:
            quantity = body.seats if body.seats is not None else plan_limits.min_seats
            if quantity < plan_limits.min_seats:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Team requires at least {plan_limits.min_seats} seats.",
                )

        if founding:
            if interval != BillingInterval.YEAR:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Founding 100 is billed annually only ($108/yr).",
                )
            if meter is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Founding 100 checkout needs usage metering; retry later.",
                )
            tenant = await meter.get_tenant(current_user.user_id)
            if tenant and tenant.get("founding"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This account already holds a Founding 100 price.",
                )
            if await meter.founding_redemptions() >= FOUNDING_MAX_REDEMPTIONS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Founding 100 is sold out. Solo is $12/mo or $120/yr.",
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

        try:
            result = await billing.create_checkout_session(
                customer_id=None,  # Will be created
                plan=plan_tier,
                user_id=current_user.user_id,
                email=user_email,
                interval=interval,
                quantity=quantity,
                founding=founding,
            )
        except CheckoutUnavailableError as e:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e

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
    meter = getattr(request.app.state, "usage_meter", None)
    if meter is not None and result is not None and result.revenue_usd is not None and result.transaction_id:
        # Net revenue feeds the free-tier breaker budget; idempotent per transaction.
        await meter.record_revenue(result.transaction_id, result.revenue_usd)

    if result is None or result.action in ("ignored", "payment_failed", "payment_issue"):
        if result is not None and result.action in ("payment_failed", "payment_issue"):
            log.warning("paddle_payment_problem", user_id=result.user_id, action=result.action)
        if result is not None and result.action == "payment_failed" and meter is not None:
            await _notify_payment_failed(request, meter, result)
        return "no_change"

    if meter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Usage metering is not available; retry later.",
        )

    if result.action in ("refund_downgrade", "refund_partial"):
        return await _apply_paddle_refund(request, meter, result)

    user_id = await _resolve_paddle_account(request, meter, result)
    if user_id is None:
        return "unmatched"

    plan = PlanTier.FREE if result.action == "cancel_subscription" else result.plan
    if plan is None:
        return "no_change"

    # An event only changes the plan of the subscription the account holds.
    # Another subscription (a second purchase, or one bought by someone else
    # naming this account) must neither cancel nor re-plan it.
    tenant = await meter.get_tenant(user_id)
    held = meter.active_subscription_id(tenant)
    event_sub = result.paddle_subscription_id
    if result.action == "cancel_subscription":
        recorded = (tenant or {}).get("stripe_subscription_id")
        # Accounts upgraded before subscription ids were recorded: the cancel
        # counts when it is for this account's own Paddle customer.
        own_customer = bool(result.paddle_customer_id) and (tenant or {}).get("stripe_customer_id") == result.paddle_customer_id
        if not (event_sub and event_sub == recorded) and not (recorded is None and own_customer):
            log.info("paddle_cancel_for_other_subscription", user_id=user_id, has_held=bool(held))
            return "no_change"
        if (tenant or {}).get("billing_flag") == "second_subscription_review":
            # The flagged second subscription may still be billing: the account
            # now shows Free until its next renewal re-applies it.
            await _operator_alert(
                request,
                f"paddle_second_subscription:{user_id}:cancel",
                "An account flagged for a second Paddle subscription cancelled the one it held and is now on Free. "
                "If the second subscription is still active, apply its plan by hand.",
                {"user_id": user_id, "canceled_subscription_id": event_sub},
            )
    elif held and event_sub != held:
        if result.action == "update_subscription":
            log.info("paddle_update_for_other_subscription", user_id=user_id)
            return "no_change"
        # A second paid subscription while one is active: never overwrite the
        # held plan (renewals of either would flip it). The customer may be
        # paying twice: flag it and tell the operator.
        await meter.set_billing_flag(user_id, "second_subscription_review")
        log.error("paddle_second_subscription", user_id=user_id, plan=plan.value, transaction_id=result.transaction_id)
        await _operator_alert(
            request,
            f"paddle_second_subscription:{user_id}",
            "A Paddle payment arrived for a second subscription on an account that already holds an active one. "
            "The held plan was kept; review the account and refund or cancel the duplicate in Paddle.",
            {"user_id": user_id, "plan": plan.value, "transaction_id": result.transaction_id},
        )
        return "flagged"

    before = dict(tenant) if tenant else None
    await meter.apply_subscription(
        user_id,
        plan,
        interval=result.interval,
        seats=result.seats,
        period_anchor=result.period_anchor,
        founding=False,  # claimed below, against the cap
        customer_id=result.paddle_customer_id,
        subscription_id=result.paddle_subscription_id,
        email=result.customer_email,
        name=result.customer_name,
    )
    if result.founding and not await meter.claim_founding(user_id):
        # Over the Founding 100 cap (the price was bought client-side or raced
        # the last seat): the account gets plain Solo annual and the charge is
        # flagged for a refund of the difference.
        await meter.set_billing_flag(user_id, "founding_over_cap_refund_due")
        log.error("paddle_founding_over_cap", user_id=user_id, transaction_id=result.transaction_id)
    if result.seats_below_minimum:
        await meter.set_billing_flag(user_id, "team_seats_below_minimum")

    team_manager = getattr(request.app.state, "team_manager", None)
    if team_manager is not None:
        try:
            account = await meter.get_account(user_id)
            await team_manager.update_owner_teams_plan(owner_id=user_id, plan=plan.value, max_seats=account.limits.max_users)
        except Exception as e:  # team sync is best-effort; the tenant plan is authoritative
            log.warning("paddle_team_plan_sync_failed", user_id=user_id, error_type=type(e).__name__)

    log.info("paddle_plan_applied", user_id=user_id, plan=plan.value, action=result.action)
    # Tell the customer when the plan, interval or seats changed (renewals send nothing).
    notify.notify_billing_change(request.app.state, user_id, before, cancelled=result.action == "cancel_subscription")
    return "applied"


async def _notify_payment_failed(request: Request, meter: Any, result: Any) -> None:
    """Email the account holding the past-due subscription (Paddle sends no custom_data we trust here)."""
    if not result.paddle_subscription_id:
        return
    user_id = await meter.find_tenant_by_billing_ids(subscription_id=result.paddle_subscription_id, customer_id=None)
    if user_id is None:
        log.warning("paddle_past_due_unmatched")
        return
    if meter.active_subscription_id(await meter.get_tenant(user_id)) != result.paddle_subscription_id:
        return  # not the subscription this account's plan comes from
    notify.notify_payment_failed(request.app.state, str(user_id), result.paddle_subscription_id)


async def _resolve_paddle_account(request: Request, meter: Any, result: Any) -> str | None:
    """The account a subscription event belongs to, or None.

    In order: the account that already holds the event's subscription (renewals
    and changes of a known subscription, whatever custom_data says); the
    custom_data user when the server signed it (server checkout, or the
    account's own client config); the custom_data user when the event's Paddle
    customer is the one recorded for that account; the account recorded for
    that Paddle customer. A bare, unsigned custom_data user id is never enough:
    the browser writes it in an overlay checkout.
    """
    if result.paddle_subscription_id:
        holder = await meter.find_tenant_by_billing_ids(subscription_id=result.paddle_subscription_id, customer_id=None)
        if holder is not None:
            return str(holder)
    claimed = result.user_id
    db = request.app.state.db
    if claimed and (await db.get_user_by_id(claimed) is not None or await meter.get_tenant(claimed) is not None):
        if result.user_verified:
            return str(claimed)
        tenant = await meter.get_tenant(claimed) or {}
        if result.paddle_customer_id and tenant.get("stripe_customer_id") == result.paddle_customer_id:
            return str(claimed)
    if result.paddle_customer_id:
        by_customer = await meter.find_tenant_by_billing_ids(subscription_id=None, customer_id=result.paddle_customer_id)
        if by_customer is not None:
            return str(by_customer)
    if claimed and result.action != "cancel_subscription":
        # Possibly a real payment we cannot attribute safely: a human decides.
        log.error("paddle_event_unverified_account", action=result.action, transaction_id=result.transaction_id)
        await _operator_alert(
            request,
            f"paddle_unverified_account:{result.paddle_subscription_id or result.transaction_id}",
            "A Paddle subscription event named an account without the server's checkout signature and matched no "
            "known subscription or customer. Nothing was applied; check the payment in Paddle.",
            {
                "claimed_user_id": claimed,
                "subscription_id": result.paddle_subscription_id,
                "transaction_id": result.transaction_id,
            },
        )
    else:
        log.warning("paddle_event_unmatched_user", action=result.action)
    return None


async def _operator_alert(request: Request, event: str, message: str, details: dict[str, Any]) -> None:
    """Best-effort operator alert (webhook / email); never fails the webhook."""
    alerts = getattr(request.app.state, "alerts", None)
    if alerts is None:
        return
    tasks = getattr(request.app.state, "tasks", None)
    coro = alerts.notify(event, message, details)
    try:
        if tasks is not None:
            tasks.spawn(coro, name=f"alert:{event}")
        else:
            await coro
    except Exception as e:
        coro.close()  # never scheduled (e.g. the registry is shutting down)
        log.warning("paddle_operator_alert_failed", error_type=type(e).__name__)


async def _apply_paddle_refund(request: Request, meter: Any, result: Any) -> str:
    """Approved refund / chargeback: a full refund or chargeback ends the paid plan at once.

    The subscription (preferred) or customer id finds the account; the webhook
    carries no custom_data for adjustments. Downgrading to Free also ends the
    annual credit bank, so credits that were paid for and refunded cannot be
    spent afterwards. Revenue was already reduced by the caller.
    """
    user_id = await meter.find_tenant_by_billing_ids(
        subscription_id=result.paddle_subscription_id, customer_id=result.paddle_customer_id
    )
    if user_id is None:
        log.warning("paddle_refund_unmatched", action=result.action)
        return "unmatched"
    if result.action != "refund_downgrade":
        log.info("paddle_partial_refund_recorded", user_id=user_id)
        return "no_change"
    tenant = await meter.get_tenant(user_id) or {}
    held = tenant.get("stripe_subscription_id")
    if result.paddle_subscription_id and held and held != result.paddle_subscription_id:
        # A refund of an older subscription the account no longer holds.
        log.info("paddle_refund_for_other_subscription", user_id=user_id)
        return "no_change"
    await meter.apply_subscription(user_id, PlanTier.FREE)
    await meter.set_billing_flag(user_id, f"{result.adjustment_action or 'refund'}_downgraded")
    notify.notify_billing_change(request.app.state, user_id, dict(tenant) if tenant else None)
    team_manager = getattr(request.app.state, "team_manager", None)
    if team_manager is not None:
        try:
            await team_manager.update_owner_teams_plan(owner_id=user_id, plan=PlanTier.FREE.value, max_seats=1)
        except Exception as e:
            log.warning("paddle_team_plan_sync_failed", user_id=user_id, error_type=type(e).__name__)
    log.warning("paddle_refund_downgraded", user_id=user_id)
    return "applied"
