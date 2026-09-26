"""Billing endpoints – /api/v1/billing.

Paddle is the sole billing provider.
"""

from datetime import timedelta
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser, JWTOrAPIKeyUser
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


class FoundingSeatsResponse(BaseModel):
    """Founding 100 seats, for the pricing page (public)."""

    max_redemptions: int = FOUNDING_MAX_REDEMPTIONS
    taken: int | None = Field(None, description="Seats held by founders, open checkouts and founders in the 14-day lapse grace")
    remaining: int | None = None
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

    seats = await _founding_seats(request, config)
    founding = FoundingOffer(remaining=seats.remaining, available=seats.available)
    return PlansResponse(plans=plans, founding=founding, provider=provider)


async def _founding_seats(request: Request, config: Any) -> FoundingSeatsResponse:
    meter = getattr(request.app.state, "usage_meter", None)
    if meter is None:
        return FoundingSeatsResponse()
    taken = await meter.founding_seats_taken()
    remaining = max(0, FOUNDING_MAX_REDEMPTIONS - taken)
    return FoundingSeatsResponse(
        taken=taken, remaining=remaining, available=bool(config and config.founding_price_id) and remaining > 0
    )


@router.get(
    "/founding",
    response_model=FoundingSeatsResponse,
    summary="Founding 100 seats left",
)
@limiter.limit("120/minute")
async def get_founding_seats(request: Request, settings: SettingsDep) -> FoundingSeatsResponse:
    """Seats left in the Founding 100 (no authentication; the pricing page reads it).

    A seat is taken by a founder holding the price, by a founding checkout in
    progress (2 hours), and by a founder whose subscription ended less than 14
    days ago. ``available`` is false once all 100 are taken.
    """
    config = get_paddle_config() if get_billing_provider(settings) == "paddle" else None
    return await _founding_seats(request, config)


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

        seat_hold = None
        if founding and meter is not None:
            # Taken before the buyer pays, so seat 101 is refused here, not after payment.
            seat_hold = await meter.hold_founding_seat(current_user.user_id)
            if seat_hold is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Founding 100 is sold out. Solo is $12/mo or $120/yr.",
                )
        try:
            if founding:
                await _reopen_founding_price(request, billing)
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
            if seat_hold is not None and meter is not None:
                await meter.release_founding_hold(seat_hold)  # a lapsed founder keeps the 14-day grace
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
        except httpx.HTTPError as e:
            if seat_hold is not None and meter is not None:
                await meter.release_founding_hold(seat_hold)
            raise _provider_unavailable("checkout", e) from e
        if founding and meter is not None and result.get("transaction_id"):
            await meter.set_founding_hold_transaction(current_user.user_id, str(result["transaction_id"]))

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

        # The Paddle customer recorded for the account, else a lookup by email.
        meter = getattr(request.app.state, "usage_meter", None)
        tenant = await meter.get_tenant(current_user.user_id) if meter is not None else None
        customer_id = (tenant or {}).get("stripe_customer_id")
        try:
            if customer_id:
                url: str | None = await billing.create_portal_session(str(customer_id))
            else:
                url = await billing.create_portal_session_by_email(user_email)
        except httpx.HTTPError as e:
            raise _provider_unavailable("portal", e) from e

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


def _provider_unavailable(operation: str, error: httpx.HTTPError) -> HTTPException:
    """502 for a Paddle API failure (down, timeout or a refused call): a clean JSON error, never a bare 500."""
    status_code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    log.error("paddle_api_error", operation=operation, error_type=type(error).__name__, status_code=status_code)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Our billing provider did not respond as expected. Nothing was charged or changed; please try again in a minute.",
    )


# cloud_migrations marker: the Founding price is archived in Paddle (all 100 seats taken).
_FOUNDING_ARCHIVED_MARKER = "state:founding_price_archived"


async def _founding_price_archived(db: Any) -> bool:
    cursor = await db.conn.execute("SELECT 1 FROM cloud_migrations WHERE name = ?", (_FOUNDING_ARCHIVED_MARKER,))
    return await cursor.fetchone() is not None


async def _reopen_founding_price(request: Request, billing: Any) -> None:
    """A seat came free after the price was archived at 100: make it buyable again (raises httpx.HTTPError)."""
    db = request.app.state.db
    if not await _founding_price_archived(db):
        return
    from remembra.cloud.paddle_config import get_paddle_settings

    price_id = get_paddle_settings().config.founding_price_id
    if price_id:
        await billing.set_price_status(price_id, "active")
    await db.conn.execute("DELETE FROM cloud_migrations WHERE name = ?", (_FOUNDING_ARCHIVED_MARKER,))
    await db.conn.commit()
    log.info("founding_price_reopened")


async def _close_founding_if_full(request: Request, meter: Any) -> None:
    """Seat 100 was just taken: archive the Founding price in Paddle and tell the owner."""
    holders = await meter.founding_redemptions()
    if holders < FOUNDING_MAX_REDEMPTIONS:
        return
    db = request.app.state.db
    if await _founding_price_archived(db):
        return
    from remembra.cloud.billing_paddle import PaddleBillingManager
    from remembra.cloud.paddle_config import get_paddle_settings

    paddle = get_paddle_settings()
    price_id = paddle.config.founding_price_id
    billing = PaddleBillingManager(api_key=paddle.api_key, webhook_secret=paddle.webhook_secret or "", sandbox=paddle.sandbox)
    archived = False
    if price_id:
        try:
            await billing.set_price_status(price_id, "archived")
            archived = True
        except httpx.HTTPError as e:
            log.error("founding_price_archive_failed", error_type=type(e).__name__)
    if archived:
        from remembra.cloud.metering import now_utc

        await db.conn.execute(
            "INSERT OR IGNORE INTO cloud_migrations (name, applied_at) VALUES (?, ?)",
            (_FOUNDING_ARCHIVED_MARKER, now_utc().isoformat()),
        )
        await db.conn.commit()
    await _operator_alert(
        request,
        "founding_100_full",
        "All 100 Founding seats are taken. "
        + (
            "The Founding price was archived in Paddle, so it cannot be bought any more."
            if archived
            else "Archiving the Founding price in Paddle FAILED: archive it by hand (Catalog > Prices)."
        ),
        {"holders": holders, "price_id": price_id, "archived": archived},
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
        if result is not None and getattr(result, "unknown_price_ids", None):
            # Paid for something the catalog does not know: nothing was applied.
            await _operator_alert(
                request,
                f"paddle_unknown_price:{result.transaction_id or result.paddle_subscription_id}",
                "A Paddle payment used a price that is not in the configured catalog, so no plan was applied. "
                "Check the price IDs in the Paddle settings and the payment in Paddle.",
                {
                    "prices": result.unknown_price_ids,
                    "transaction_id": result.transaction_id,
                    "subscription_id": result.paddle_subscription_id,
                },
            )
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
        log.error("paddle_second_subscription", user_id=user_id, plan=plan.value, transaction_id=result.transaction_id)
        await _flag_account(
            request,
            meter,
            user_id,
            "second_subscription_review",
            "A Paddle payment arrived for a second subscription on an account that already holds an active one. "
            "The held plan was kept; review the account and refund or cancel the duplicate in Paddle.",
            {"plan": plan.value, "transaction_id": result.transaction_id, "subscription_id": event_sub},
            event=f"paddle_second_subscription:{user_id}",
        )
        return "flagged"

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
        bank_unlock_at=_new_bank_unlock(tenant, result, plan),
    )
    if not result.founding and (tenant or {}).get("founding") and result.plan_from_price:
        # The held subscription moved off the Founding price (a plan change in
        # the portal): the lock ends, with the same 14-day grace as a lapse.
        await meter.end_founding(user_id)
    if result.founding:
        if await meter.claim_founding(user_id):
            await _close_founding_if_full(request, meter)
        else:
            # Past the Founding 100 cap (the price was bought client-side, or a
            # checkout hold expired while all seats filled): the account gets
            # plain Solo annual and the owner decides whether to honor $108 or
            # move the subscription to the $120 Solo annual price with notice.
            log.error("paddle_founding_over_cap", user_id=user_id, transaction_id=result.transaction_id)
            await _flag_account(
                request,
                meter,
                user_id,
                "founding_over_cap",
                "A Founding 100 payment arrived after all 100 seats were taken. The account got plain Solo annual. "
                "Decide in Paddle: honor the $108 price, or move the subscription to the Solo annual price with "
                "notice before it renews. Then clear the flag.",
                {"transaction_id": result.transaction_id, "subscription_id": result.paddle_subscription_id},
            )
    if result.seats_below_minimum:
        await _flag_account(
            request,
            meter,
            user_id,
            "team_seats_below_minimum",
            "A Team subscription was paid for fewer seats than the 3-seat minimum; the paid seats were granted. "
            "Set quantity.minimum on the Paddle price and adjust the subscription.",
            {"seats": result.seats, "subscription_id": result.paddle_subscription_id},
        )

    team_manager = getattr(request.app.state, "team_manager", None)
    if team_manager is not None:
        try:
            account = await meter.get_account(user_id)
            await team_manager.update_owner_teams_plan(owner_id=user_id, plan=plan.value, max_seats=account.limits.max_users)
        except Exception as e:  # team sync is best-effort; the tenant plan is authoritative
            log.warning("paddle_team_plan_sync_failed", user_id=user_id, error_type=type(e).__name__)

    log.info("paddle_plan_applied", user_id=user_id, plan=plan.value, action=result.action)
    return "applied"


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
        if result.action != "cancel_subscription":
            # A payment that names no account and matches no known customer.
            await _operator_alert(
                request,
                f"paddle_unmatched_purchase:{result.paddle_subscription_id or result.transaction_id}",
                "A Paddle payment matched no Remembra account (no signed account id, unknown subscription and "
                "customer). Nothing was applied; find the buyer in Paddle and apply or refund it.",
                {
                    "action": result.action,
                    "subscription_id": result.paddle_subscription_id,
                    "customer_id": result.paddle_customer_id,
                    "transaction_id": result.transaction_id,
                },
            )
    return None


def _new_bank_unlock(tenant: dict[str, Any] | None, result: Any, plan: PlanTier) -> Any:
    """When a NEW yearly bank unlocks in full (R-27), or None to leave the account as it is.

    A yearly bank is new when the subscription is not the one the account
    already holds (a new purchase) or the account switches to yearly billing.
    Renewals and repeats of a held subscription never hold credits back.
    """
    from remembra.cloud.metering import now_utc

    days = get_settings().annual_credit_unlock_days
    if days <= 0 or plan == PlanTier.FREE or result.interval != BillingInterval.YEAR:
        return None
    tenant = tenant or {}
    held = tenant.get("stripe_subscription_id") if str(tenant.get("plan") or "free") != PlanTier.FREE.value else None
    new_subscription = bool(result.paddle_subscription_id) and result.paddle_subscription_id != held
    switched = tenant.get("billing_interval") != BillingInterval.YEAR.value
    if not (new_subscription or switched):
        return None
    return (result.period_anchor or now_utc()) + timedelta(days=days)


async def _flag_account(
    request: Request,
    meter: Any,
    user_id: str,
    flag: str,
    message: str,
    details: dict[str, Any],
    *,
    event: str | None = None,
) -> None:
    """Record a billing flag on the account (listed at GET /admin/billing-flags) and alert the owner."""
    await meter.set_billing_flag(user_id, flag)
    await _operator_alert(
        request, event or f"billing_flag:{flag}:{user_id}", message, {"user_id": user_id, "flag": flag, **details}
    )


async def _operator_alert(request: Request, event: str, message: str, details: dict[str, Any]) -> None:
    """Best-effort operator alert (webhook / email); never fails the webhook."""
    from remembra.account.deletion import notify_owner

    await notify_owner(request.app.state, event, message, details)


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
        await _operator_alert(
            request,
            f"paddle_refund_unmatched:{result.transaction_id}",
            "An approved Paddle refund or chargeback matched no Remembra account. Check it in Paddle.",
            {
                "adjustment": result.adjustment_action,
                "subscription_id": result.paddle_subscription_id,
                "customer_id": result.paddle_customer_id,
            },
        )
        return "unmatched"
    await _alert_refund_after_heavy_use(request, meter, user_id, result)
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
    await _flag_account(
        request,
        meter,
        user_id,
        f"{result.adjustment_action or 'refund'}_downgraded",
        f"An approved {result.adjustment_action or 'refund'} ended a paid plan; the account is back on Free.",
        {"subscription_id": result.paddle_subscription_id, "adjustment_id": result.transaction_id},
    )
    team_manager = getattr(request.app.state, "team_manager", None)
    if team_manager is not None:
        try:
            await team_manager.update_owner_teams_plan(owner_id=user_id, plan=PlanTier.FREE.value, max_seats=1)
        except Exception as e:
            log.warning("paddle_team_plan_sync_failed", user_id=user_id, error_type=type(e).__name__)
    log.warning("paddle_refund_downgraded", user_id=user_id)
    return "applied"


# A refund after spending more than this share of the period's credits is worth a look (R-27).
REFUND_HEAVY_USE_SHARE = 0.25


async def _alert_refund_after_heavy_use(request: Request, meter: Any, user_id: str, result: Any) -> None:
    """Tell the owner when a refunded account had already used over 25% of the credits it could spend.

    The bank is what the period released so far: during the first 14 days of a
    new yearly plan that is one month's credits (R-27), so a buyer who burns
    that and asks for a refund is caught too.
    """
    account = await meter.get_account(user_id, include_team=False)
    bank = account.credit_limit
    if bank <= 0:
        return
    balance = await meter.get_credit_balance(account)
    if balance.used <= bank * REFUND_HEAVY_USE_SHARE:
        return
    log.warning("paddle_refund_after_heavy_use", user_id=user_id, used=balance.used, bank=bank)
    await _operator_alert(
        request,
        f"paddle_refund_heavy_use:{user_id}",
        f"An approved {result.adjustment_action or 'refund'} arrived for an account that had used "
        f"{balance.used:,} of its {bank:,} credits ({balance.used / bank:.0%}) this period, "
        f"about ${balance.llm_usd:.2f} of AI spend. Review for refund abuse.",
        {
            "user_id": user_id,
            "credits_used": balance.used,
            "credit_bank": bank,
            "llm_usd": round(balance.llm_usd, 4),
            "adjustment_id": result.transaction_id,
        },
    )
