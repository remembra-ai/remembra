"""
Plan limit enforcement for Remembra Cloud.

Provides FastAPI dependencies that check plan limits
before allowing store/recall/key-creation operations.

Usage in routes:
    @router.post("/memories")
    async def store_memory(
        ...,
        _limit: EnforceStoreLimit,
    ):
        ...
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, Request, Response, status

from remembra.auth.middleware import (
    AuthenticatedUser,
    get_current_user,
    get_user_from_jwt_or_api_key,
)
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import get_plan

logger = structlog.get_logger(__name__)

# Track which users have received warning emails (reset on app restart)
# In production, this should be stored in database
_warned_users_80: set[str] = set()
_warned_users_limit: set[str] = set()


async def _send_usage_warning_email(
    user_id: str,
    user_email: str,
    usage_percent: float,
    current_usage: int,
    limit: int,
    plan: str,
) -> None:
    """Send usage warning email (80% threshold) - fire and forget."""
    if user_id in _warned_users_80:
        return  # Already warned this session

    try:
        from remembra.cloud.email import EmailProvider, EmailService

        email_service = EmailService.create(provider=EmailProvider.RESEND)
        await email_service.send_usage_warning_email(
            to=user_email,
            usage_percent=int(usage_percent),
            current_usage=current_usage,
            limit=limit,
            plan=plan,
        )
        _warned_users_80.add(user_id)
        logger.info("usage_warning_email_sent", user_id=user_id, percent=usage_percent)
    except Exception as e:
        logger.warning("usage_warning_email_failed", user_id=user_id, error=str(e))


async def _send_limit_exceeded_email(
    user_id: str,
    user_email: str,
    current_usage: int,
    limit: int,
    plan: str,
) -> None:
    """Send limit exceeded email - fire and forget."""
    if user_id in _warned_users_limit:
        return  # Already notified this session

    try:
        from remembra.cloud.email import EmailProvider, EmailService

        email_service = EmailService.create(provider=EmailProvider.RESEND)
        await email_service.send_limit_exceeded_email(
            to=user_email,
            current_usage=current_usage,
            limit=limit,
            plan=plan,
        )
        _warned_users_limit.add(user_id)
        logger.info("limit_exceeded_email_sent", user_id=user_id)
    except Exception as e:
        logger.warning("limit_exceeded_email_failed", user_id=user_id, error=str(e))


def get_usage_warning(usage_percent: float, plan: str) -> dict[str, Any] | None:
    """Return a usage warning dict if threshold is crossed."""
    if usage_percent >= 95:
        return {
            "level": "critical",
            "message": "95% of plan used. Upgrade now to avoid interruption.",
            "usage_percent": usage_percent,
            "plan": plan,
            "upgrade_url": "https://remembra.dev/pricing",
        }
    elif usage_percent >= 80:
        return {
            "level": "warning",
            "message": "You're at 80% of your plan. Consider upgrading for uninterrupted service.",
            "usage_percent": usage_percent,
            "plan": plan,
            "upgrade_url": "https://remembra.dev/pricing",
        }
    elif usage_percent >= 60:
        return {
            "level": "info",
            "message": "You're using Remembra well! 60% of plan used.",
            "usage_percent": usage_percent,
            "plan": plan,
        }
    return None


def _get_meter_or_none(request: Request) -> UsageMeter | None:
    """Get the UsageMeter if cloud is enabled, else None."""
    return getattr(request.app.state, "usage_meter", None)


async def enforce_store_limit(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Dependency that enforces store limits on the current user.

    Raises 429 Too Many Requests if the user has exceeded their plan's
    memory storage or monthly store limit.

    Also sets usage headers and stores a usage_warning in request.state
    when cloud is enabled.

    This is a no-op when cloud features are disabled.
    """
    meter = _get_meter_or_none(request)
    if meter is None:
        return  # Cloud not enabled — no limits

    snapshot = await meter.get_usage_snapshot(current_user.user_id)
    check = snapshot.check_limit("store")

    # Compute usage percentage based on memory count vs plan max
    plan_limits = get_plan(snapshot.plan)
    usage_percent = round((snapshot.memories_stored / plan_limits.max_memories) * 100, 1)

    # Set usage headers on the response
    response.headers["X-Remembra-Usage-Percent"] = str(usage_percent)
    response.headers["X-Remembra-Plan"] = snapshot.plan.value
    if usage_percent > 60:
        response.headers["X-Remembra-Upgrade-URL"] = "https://remembra.dev/pricing"

    # Store usage warning in request.state for API endpoints to pick up
    warning = get_usage_warning(usage_percent, snapshot.plan.value)
    request.state.usage_warning = warning

    # Send usage warning email at 80%+ (fire-and-forget, don't block request)
    if usage_percent >= 80 and current_user.user_id not in _warned_users_80:
        try:
            # Get user email from database
            db = getattr(request.app.state, "db", None)
            if db:
                user_data = await db.get_user_by_id(current_user.user_id)
                if user_data and user_data.get("email"):
                    asyncio.create_task(
                        _send_usage_warning_email(
                            user_id=current_user.user_id,
                            user_email=user_data["email"],
                            usage_percent=usage_percent,
                            current_usage=snapshot.memories_stored,
                            limit=plan_limits.max_memories,
                            plan=snapshot.plan.value,
                        )
                    )
        except Exception as e:
            logger.warning("failed_to_queue_warning_email", error=str(e))

    if not check.allowed:
        logger.warning(
            "store_limit_exceeded",
            user_id=current_user.user_id,
            plan=snapshot.plan.value,
            reason=check.reason,
        )

        # Send limit exceeded email (fire-and-forget)
        if current_user.user_id not in _warned_users_limit:
            try:
                db = getattr(request.app.state, "db", None)
                if db:
                    user_data = await db.get_user_by_id(current_user.user_id)
                    if user_data and user_data.get("email"):
                        asyncio.create_task(
                            _send_limit_exceeded_email(
                                user_id=current_user.user_id,
                                user_email=user_data["email"],
                                current_usage=snapshot.memories_stored,
                                limit=plan_limits.max_memories,
                                plan=snapshot.plan.value,
                            )
                        )
            except Exception as e:
                logger.warning("failed_to_queue_limit_email", error=str(e))

        detail = check.reason or "Store limit exceeded"
        if check.upgrade_hint:
            detail += f" — {check.upgrade_hint}"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={
                "X-RateLimit-Limit": str(check.limit) if check.limit else "",
                "X-RateLimit-Remaining": "0",
            },
        )


async def enforce_recall_limit(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Dependency that enforces recall limits on the current user.

    Raises 429 Too Many Requests if the user has exceeded their plan's
    monthly recall limit.

    Also sets usage headers and stores a usage_warning in request.state
    when cloud is enabled.

    This is a no-op when cloud features are disabled.
    """
    meter = _get_meter_or_none(request)
    if meter is None:
        return

    snapshot = await meter.get_usage_snapshot(current_user.user_id)
    check = snapshot.check_limit("recall")

    # Compute usage percentage based on recalls this month vs plan max
    plan_limits = get_plan(snapshot.plan)
    usage_percent = round((snapshot.recalls_this_month / plan_limits.max_recalls_per_month) * 100, 1)

    # Set usage headers on the response
    response.headers["X-Remembra-Usage-Percent"] = str(usage_percent)
    response.headers["X-Remembra-Plan"] = snapshot.plan.value
    if usage_percent > 60:
        response.headers["X-Remembra-Upgrade-URL"] = "https://remembra.dev/pricing"

    # Store usage warning in request.state for API endpoints to pick up
    warning = get_usage_warning(usage_percent, snapshot.plan.value)
    request.state.usage_warning = warning

    if not check.allowed:
        logger.warning(
            "recall_limit_exceeded",
            user_id=current_user.user_id,
            plan=snapshot.plan.value,
            reason=check.reason,
        )
        detail = check.reason or "Recall limit exceeded"
        if check.upgrade_hint:
            detail += f" — {check.upgrade_hint}"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={
                "X-RateLimit-Limit": str(check.limit) if check.limit else "",
                "X-RateLimit-Remaining": "0",
            },
        )


async def enforce_key_limit(
    request: Request,
    current_user: AuthenticatedUser | None = Depends(get_user_from_jwt_or_api_key),
) -> None:
    """Dependency that enforces API key creation limits.

    Raises 429 if the user has reached their plan's max API keys.

    This is a no-op when cloud features are disabled or user is not authenticated.
    """
    meter = _get_meter_or_none(request)
    if meter is None:
        return

    # Skip limit check if user not authenticated (endpoint will handle auth)
    if current_user is None:
        return

    snapshot = await meter.get_usage_snapshot(current_user.user_id)
    check = snapshot.check_limit("create_key")

    if not check.allowed:
        logger.warning(
            "key_limit_exceeded user=%s plan=%s reason=%s",
            current_user.user_id,
            snapshot.plan.value,
            check.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=check.reason or "API key limit exceeded",
        )


# -----------------------------------------------------------------------
# Usage recording helpers (called AFTER successful operations)
# -----------------------------------------------------------------------


async def record_store_usage(request: Request, user_id: str, count: int = 1) -> None:
    """Record ``count`` store events in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None and count > 0:
        await meter.record_store(user_id, count)


async def record_recall_usage(request: Request, user_id: str, count: int = 1) -> None:
    """Record ``count`` recall events in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None and count > 0:
        await meter.record_recall(user_id, count)


def _quota_exceeded(reason: str, limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=reason,
        headers={"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"},
    )


async def enforce_store_quota(request: Request, user_id: str, count: int = 1) -> None:
    """Reject (429) a write of ``count`` memories that would exceed the plan.

    Used by every multi-item write path (batch, bulk, import, ingest) so they
    cannot bypass the per-request ``EnforceStoreLimit`` dependency.
    No-op when cloud features are disabled.
    """
    meter = _get_meter_or_none(request)
    if meter is None or count <= 0:
        return
    snapshot = await meter.get_usage_snapshot(user_id)
    plan_limits = get_plan(snapshot.plan)
    if snapshot.memories_stored + count > plan_limits.max_memories:
        logger.warning("store_quota_exceeded", user_id=user_id, requested=count, plan=snapshot.plan.value)
        raise _quota_exceeded(f"Memory limit reached ({plan_limits.max_memories:,} memories)", plan_limits.max_memories)
    if snapshot.stores_this_month + count > plan_limits.max_stores_per_month:
        logger.warning("monthly_store_quota_exceeded", user_id=user_id, requested=count, plan=snapshot.plan.value)
        raise _quota_exceeded(
            f"Monthly store limit reached ({plan_limits.max_stores_per_month:,}/mo)", plan_limits.max_stores_per_month
        )


async def enforce_recall_quota(request: Request, user_id: str, count: int = 1) -> None:
    """Reject (429) ``count`` recalls that would exceed the plan's monthly recall limit."""
    meter = _get_meter_or_none(request)
    if meter is None or count <= 0:
        return
    snapshot = await meter.get_usage_snapshot(user_id)
    plan_limits = get_plan(snapshot.plan)
    if snapshot.recalls_this_month + count > plan_limits.max_recalls_per_month:
        logger.warning("recall_quota_exceeded", user_id=user_id, requested=count, plan=snapshot.plan.value)
        raise _quota_exceeded(
            f"Monthly recall limit reached ({plan_limits.max_recalls_per_month:,}/mo)",
            plan_limits.max_recalls_per_month,
        )


async def record_delete_usage(request: Request, user_id: str) -> None:
    """Record a delete event in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None:
        await meter.record_delete(user_id)


# -----------------------------------------------------------------------
# Type aliases for FastAPI Depends
# -----------------------------------------------------------------------

EnforceStoreLimit = Annotated[None, Depends(enforce_store_limit)]
EnforceRecallLimit = Annotated[None, Depends(enforce_recall_limit)]
EnforceKeyLimit = Annotated[None, Depends(enforce_key_limit)]
