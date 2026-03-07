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

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status

from remembra.auth.middleware import (
    AuthenticatedUser,
    get_current_user,
    get_user_from_jwt_or_api_key,
)
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import get_plan

logger = logging.getLogger(__name__)


def get_usage_warning(usage_percent: float, plan: str) -> dict | None:
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
    usage_percent = round(
        (snapshot.memories_stored / plan_limits.max_memories) * 100, 1
    )

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
            "store_limit_exceeded user=%s plan=%s reason=%s",
            current_user.user_id,
            snapshot.plan.value,
            check.reason,
        )
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
    usage_percent = round(
        (snapshot.recalls_this_month / plan_limits.max_recalls_per_month) * 100, 1
    )

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
            "recall_limit_exceeded user=%s plan=%s reason=%s",
            current_user.user_id,
            snapshot.plan.value,
            check.reason,
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


async def record_store_usage(request: Request, user_id: str) -> None:
    """Record a store event in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None:
        await meter.record_store(user_id)


async def record_recall_usage(request: Request, user_id: str) -> None:
    """Record a recall event in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None:
        await meter.record_recall(user_id)


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
