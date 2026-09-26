"""Cloud billing, usage, and subscription endpoints – /api/v1/cloud."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from remembra.auth.middleware import CurrentUser, RequireMasterKey
from remembra.cloud.metering import AccountState, CreditPeriod, UsageMeter, now_utc
from remembra.cloud.plans import CREDIT_USD, RESERVE_CREDITS_PER_CHUNK, PlanLimits, PlanTier, get_plan
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
    meter: UsageMeter | None = getattr(request.app.state, "usage_meter", None)
    if meter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud features are not enabled on this instance.",
        )
    return meter


UsageMeterDep = Annotated[UsageMeter, Depends(get_usage_meter)]


def get_team_manager(request: Request) -> TeamManager | None:
    """Dependency to get TeamManager from app state (optional)."""
    manager: TeamManager | None = getattr(request.app.state, "team_manager", None)
    return manager


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


class DailyUsageResponse(BaseModel):
    user_id: str
    days: list[dict[str, Any]]


class SignupRequest(BaseModel):
    email: str = Field(description="User's email address")
    name: str | None = Field(None, description="Display name")
    user_id: str | None = Field(None, description="Custom user ID (auto-generated if omitted)")
    turnstile_token: str | None = Field(
        None, max_length=4096, description="Cloudflare Turnstile token (required when Turnstile is enabled)"
    )
    client_ip: str | None = Field(
        None,
        description="End user's IP as seen by the calling signup backend (used for the per-network signup limit)",
    )

    @field_validator("client_ip")
    @classmethod
    def valid_ip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import ipaddress

        return str(ipaddress.ip_address(v.strip()))


class SignupResponse(BaseModel):
    user_id: str
    api_key: str = Field(description="Your API key — store it securely! Shown only once.")
    api_key_id: str
    plan: str
    message: str
    email_verification_sent: bool = Field(
        False, description="A verification link was emailed (verify to lift the unverified-email credit hold)"
    )


class TenantVerifyEmailConfirmRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256, description="Token from the emailed verification link")


class TenantVerifyEmailResponse(BaseModel):
    message: str
    email_verified: bool


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


class UsagePeriod(BaseModel):
    key: str
    type: Literal["month", "year"]
    start: str
    end: str


class CreditsUsage(BaseModel):
    limit: int = Field(description="Credits in this period (monthly allowance or the yearly bank)")
    used: int
    reserved: int = Field(description="Held for enrichment still running; refunded when it settles")
    remaining: int
    bank: Literal["monthly", "yearly"]
    llm_usd_used: float = Field(description="Actual AI spend this period (USD)")
    ceiling_usd: float = Field(description="Hard AI-spend ceiling for this period (USD)")
    unverified_cap_applied: bool = Field(description="Free credits held at the unverified-email cap")


class EnrichmentStatus(BaseModel):
    status: Literal["full", "degraded"]
    reason: str | None = Field(None, description="credits_exhausted | free_breaker_open")


class RelayUsage(BaseModel):
    this_month: int
    soft_cap: int
    over_soft_cap: bool
    burst_per_min: int
    free: bool = Field(True, description="Relay events never consume smart credits")


class RecallUsage(BaseModel):
    this_month: int
    limit: int
    burst_per_min: int


class MemoryUsage(BaseModel):
    stored: int = Field(description="Memories counted toward the cap (session handoffs written by the relay are excluded)")
    cap: int
    handoffs: int = Field(
        default=0, description="Session handoffs written by the relay (every version kept); free, not counted toward the cap"
    )


class StoreUsage(BaseModel):
    this_month: int
    degraded_this_month: int = Field(description="Stores saved without enrichment (out of credits / paused)")


class UsageSummaryResponse(BaseModel):
    plan: str
    plan_name: str
    interval: Literal["month", "year"]
    seats: int
    founding: bool
    email_verified: bool
    period: UsagePeriod
    credits: CreditsUsage
    enrichment: EnrichmentStatus
    relay_events: RelayUsage
    recalls: RecallUsage
    memories: MemoryUsage
    stores: StoreUsage
    subscription_active: bool = Field(
        False, description="The account holds an active paid subscription: change plans in the billing portal"
    )


def _catalog_limits_dict(plan_limits: PlanLimits) -> dict[str, Any]:
    return {
        "max_memories": plan_limits.max_memories,
        "smart_credits_per_month": plan_limits.max_smart_credits_per_month,
        "llm_ceiling_usd_month": plan_limits.llm_ceiling_usd_month,
        "max_recalls_per_month": plan_limits.max_recalls_per_month,
        "relay_events_soft_cap": plan_limits.max_relay_events_per_month,
        "max_content_chars": plan_limits.max_content_chars,
        "max_batch_items": plan_limits.max_batch_items,
        "max_api_keys": plan_limits.max_api_keys,
        "max_users": plan_limits.max_users,
        "max_projects": plan_limits.max_projects,
        "max_storage_mb": plan_limits.max_storage_mb,
        "retention_days": plan_limits.retention_days,
        "has_webhooks": plan_limits.has_webhooks,
        "has_sso": plan_limits.has_sso,
        "has_observability": plan_limits.has_observability,
    }


def _limits_dict(account: AccountState) -> dict[str, Any]:
    """Effective limits for an account (seat-scaled, notice-aware memory cap, period credits)."""
    return {
        **_catalog_limits_dict(account.limits),
        "max_memories": account.memory_cap,
        "smart_credits": account.credit_limit,
        "credit_bank": "yearly" if account.interval.value == "year" else "monthly",
    }


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

    Hardening: 3 signups/hour per client /24 and 20/day per email domain
    (``client_ip`` from the calling backend, else the connection's IP), and a
    Cloudflare Turnstile check when ``REMEMBRA_TURNSTILE_SECRET`` is set.
    Tenants created here have no dashboard user record: a verification link is
    emailed to them (``/cloud/verify-email``), and once
    ``unverified_credit_cap_effective_at`` is set they are held at the
    unverified-email credit cap until they confirm it.

    Returns the API key — it is only shown once.
    """
    from remembra.auth.middleware import get_client_ip
    from remembra.cloud.provisioning import TenantProvisioner
    from remembra.cloud.signup_guard import guard_signup

    client_ip = body.client_ip or get_client_ip(request)
    await guard_signup(client_ip=client_ip, email=body.email, turnstile_token=body.turnstile_token)

    key_manager = request.app.state.api_key_manager

    # Email service for the welcome and verification emails (None when email is not configured)
    from remembra.cloud.email import email_service_or_none

    email_service = email_service_or_none()

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

    await meter.mark_tenant_signup(result.user_id)
    verification_sent = False
    if email_service is not None and body.email and "@" in body.email:
        from remembra.security import state as security_state

        try:
            token = await security_state.create_email_verification(request.app.state.db, result.user_id, body.email)
            verification_sent = await _send_tenant_verification(email_service, body.email, token)
        except Exception as e:  # never fail a completed signup over the email
            import logging

            logging.getLogger(__name__).warning("Tenant verification email not sent: %s", type(e).__name__)

    return SignupResponse(
        user_id=result.user_id,
        api_key=result.api_key,
        api_key_id=result.api_key_id,
        plan=result.plan.value,
        message="Store the API key securely — it cannot be retrieved again.",
        email_verification_sent=verification_sent,
    )


async def _send_tenant_verification(email_service: Any, email: str, token: str) -> bool:
    from urllib.parse import urlencode

    from remembra.api.v1.auth import dashboard_link

    url = dashboard_link("/verify-email") + "?" + urlencode({"token": token, "account": "api"})
    result = await email_service.send_email_verification_email(to=email, verify_url=url)
    return bool(result.success)


# ---------------------------------------------------------------------------
# Email verification for API-signup tenants (no dashboard user record)
# ---------------------------------------------------------------------------


@router.post(
    "/verify-email/request",
    response_model=TenantVerifyEmailResponse,
    summary="Email a verification link to an API-signup account",
)
@limiter.limit("3/minute")
async def request_tenant_email_verification(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
) -> TenantVerifyEmailResponse:
    """For accounts created by ``POST /cloud/signup`` (authenticate with the account's API key).

    Dashboard accounts use ``POST /api/v1/auth/verify-email/request`` instead.
    The link (valid 24h, single use) opens the dashboard's /verify-email page.
    """
    from remembra.security import state as security_state

    db = request.app.state.db
    if await db.get_user_by_id(current_user.user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account signs in to the dashboard: use POST /api/v1/auth/verify-email/request.",
        )
    tenant = await meter.get_tenant(current_user.user_id)
    email = str((tenant or {}).get("email") or "").strip()
    if not tenant or "@" not in email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This account has no email address to verify.")
    if tenant.get("email_verified"):
        return TenantVerifyEmailResponse(message="Email already verified", email_verified=True)
    try:
        from remembra.cloud.email import EmailProvider, EmailService

        email_service = EmailService.create(provider=EmailProvider.RESEND)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email delivery is not configured") from e
    token = await security_state.create_email_verification(db, current_user.user_id, email)
    try:
        sent = await _send_tenant_verification(email_service, email, token)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not send verification email") from e
    if not sent:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not send verification email")
    return TenantVerifyEmailResponse(message="Verification email sent", email_verified=False)


@router.post(
    "/verify-email/confirm",
    response_model=TenantVerifyEmailResponse,
    summary="Confirm an API-signup account's email with the emailed token",
)
@limiter.limit("10/minute")
async def confirm_tenant_email_verification(
    request: Request,
    body: Annotated[TenantVerifyEmailConfirmRequest, Body(...)],
    meter: UsageMeterDep,
) -> TenantVerifyEmailResponse:
    """Token-only (the account has no dashboard session). The token is single use and bound
    to the account and the address it was sent to. Refused when another account already
    verified the same address (one free account per verified email)."""
    from remembra.security import state as security_state

    db = request.app.state.db
    invalid = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired verification token")
    found = await security_state.find_email_verification(db, body.token)
    if found is None:
        raise invalid
    user_id, email = found
    if await db.get_user_by_id(user_id):
        raise invalid  # dashboard accounts confirm while signed in (/auth/verify-email/confirm)
    tenant = await meter.get_tenant(user_id)
    if not tenant or str(tenant.get("email") or "").strip().lower() != email:
        raise invalid
    if tenant.get("email_verified"):
        return TenantVerifyEmailResponse(message="Email already verified", email_verified=True)
    if await meter.email_has_verified_account(email, exclude_user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email address is already verified on another Remembra account.",
        )
    if not await security_state.consume_email_verification(db, user_id, email, body.token):
        raise invalid
    await meter.set_tenant_email_verified(user_id)
    return TenantVerifyEmailResponse(message="Email verified", email_verified=True)


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
    """Get usage statistics for the current calendar month."""
    monthly = await meter.get_monthly_usage(current_user.user_id)
    account = await meter.get_account(current_user.user_id)

    return UsageResponse(
        user_id=current_user.user_id,
        plan=account.tier.value,
        period=monthly["period"],
        stores=monthly["stores"],
        recalls=monthly["recalls"],
        deletes=monthly["deletes"],
        active_days=monthly["active_days"],
        limits=_limits_dict(account),
    )


@router.get(
    "/usage/summary",
    response_model=UsageSummaryResponse,
    summary="Plan, smart credits, relay, recalls and memories for the dashboard",
)
@limiter.limit("60/minute")
async def get_usage_summary(
    request: Request,
    current_user: CurrentUser,
    meter: UsageMeterDep,
) -> UsageSummaryResponse:
    """Everything the billing panel shows.

    Smart credits are reported for the current billing period: a calendar
    month, or the subscription year on annual plans (the whole year's credits
    are banked up front). Relay events, pickups, trail reads and recalls never
    consume credits. ``enrichment.status`` is ``degraded`` when new stores will
    be saved without AI enrichment (credits exhausted or the platform's free
    tier budget paused).
    """
    user_id = current_user.user_id
    account = await meter.get_account(user_id)
    balance = await meter.get_credit_balance(account)
    month = await meter.get_period_counters(account.pool, CreditPeriod.monthly(now_utc()).start)
    limits = account.limits

    reason: str | None = None
    if account.free_group and await meter.free_breaker_open():
        reason = "free_breaker_open"
    elif balance.remaining < RESERVE_CREDITS_PER_CHUNK:
        reason = "credits_exhausted"

    unverified_cap = limits.unverified_credit_cap
    return UsageSummaryResponse(
        plan=account.tier.value,
        plan_name=limits.display_name,
        interval=account.interval.value,
        seats=account.seats,
        founding=account.founding,
        email_verified=account.email_verified,
        period=UsagePeriod(
            key=account.period.key,
            type=account.period.interval.value,
            start=account.period.start.isoformat(),
            end=account.period.end.isoformat(),
        ),
        credits=CreditsUsage(
            limit=balance.limit,
            used=balance.used,
            reserved=balance.reserved,
            remaining=balance.remaining,
            bank="yearly" if account.interval.value == "year" else "monthly",
            llm_usd_used=round(balance.llm_usd, 4),
            ceiling_usd=round(balance.limit * CREDIT_USD, 2),
            unverified_cap_applied=bool(
                account.tier == PlanTier.FREE
                and not account.email_verified
                and unverified_cap is not None
                and balance.limit <= unverified_cap
            ),
        ),
        enrichment=EnrichmentStatus(status="degraded" if reason else "full", reason=reason),
        relay_events=RelayUsage(
            this_month=month["relay_events"],
            soft_cap=limits.max_relay_events_per_month,
            over_soft_cap=month["relay_events"] >= limits.max_relay_events_per_month,
            burst_per_min=limits.relay_burst_per_min,
        ),
        recalls=RecallUsage(
            this_month=month["recalls"],
            limit=limits.max_recalls_per_month,
            burst_per_min=limits.recall_burst_per_min,
        ),
        memories=MemoryUsage(
            stored=await meter.count_pool_memories(account),
            cap=account.memory_cap,
            handoffs=await meter.count_pool_relay_records(account),
        ),
        stores=StoreUsage(this_month=month["stores"], degraded_this_month=month["degraded_stores"]),
        subscription_active=meter.active_subscription_id(await meter.get_tenant(account.user_id)) is not None,
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
    account = await meter.get_account(current_user.user_id)
    balance = await meter.get_credit_balance(account)

    store_check = snapshot.check_limit("store")
    recall_check = snapshot.check_limit("recall")
    key_check = snapshot.check_limit("create_key")

    return PlanInfoResponse(
        plan=snapshot.plan.value,
        limits=_limits_dict(account),
        usage={
            "memories_stored": snapshot.memories_stored,
            "stores_this_month": snapshot.stores_this_month,
            "recalls_this_month": snapshot.recalls_this_month,
            "api_keys_active": snapshot.api_keys_active,
            "smart_credits_used": balance.used,
            "smart_credits_remaining": balance.remaining,
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

        # Team plan limits, pooled over the owner's billed seats
        try:
            plan_limits = get_plan(PlanTier(team_plan))
        except ValueError:
            plan_limits = get_plan(PlanTier.TEAM)
        owner_tenant = await meter.get_tenant(team.get("owner_id", "")) if team.get("owner_id") else None
        plan_limits = plan_limits.scaled(int((owner_tenant or {}).get("seats") or plan_limits.min_seats))

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
            limits=_catalog_limits_dict(plan_limits),
            usage=usage,
        )

    # No team — personal context
    snapshot = await meter.get_usage_snapshot(current_user.user_id)
    account = await meter.get_account(current_user.user_id)

    return BillingContextResponse(
        context="personal",
        plan=snapshot.plan.value,
        can_manage_billing=True,  # Personal accounts always manage their own billing
        limits=_limits_dict(account),
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
    codes: list[dict[str, Any]]


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

    manager = PromoCodeManager(request.app.state.db)
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

    db = request.app.state.db
    manager = PromoCodeManager(db)

    # Get user's Stripe customer ID if they have one
    tenant_info = await meter.get_tenant(user.user_id)
    stripe_customer_id = tenant_info.get("stripe_customer_id") if tenant_info else None

    # Fetch email from database (AuthenticatedUser doesn't carry email)
    user_data = await db.get_user_by_id(user.user_id)
    user_email = user_data.get("email") if user_data else None
    if tenant_info is None:
        # update_plan only updates existing tenant rows.
        await meter.register_tenant(user.user_id, plan=PlanTier.FREE, email=user_email)

    result = await manager.redeem(
        code=body.code,
        user_id=user.user_id,
        email=user_email,
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

    manager = PromoCodeManager(request.app.state.db)
    codes = await manager.list_active_codes()

    return PromoListResponse(codes=codes)


@router.get(
    "/promo/{code}/stats",
    summary="Get promo code stats",
    description="Admin endpoint: Get redemption stats for a specific promo code.",
    dependencies=[Depends(RequireMasterKey)],
)
async def get_promo_stats(request: Request, code: str) -> dict[str, Any]:
    """Get stats for a specific promo code (admin only)."""
    from remembra.cloud.promocodes import PromoCodeManager

    manager = PromoCodeManager(request.app.state.db)
    stats = await manager.get_stats(code)

    if not stats:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Promo code '{code}' not found",
        )

    return stats
