"""
Plan limit enforcement for Remembra Cloud.

Every write path calls :func:`gate_write` BEFORE any LLM work. It enforces the
plan's input limits and memory cap, then decides whether the write may be
*enriched* (LLM fact extraction, consolidation, entity resolution):

* enrichment needs a credit reservation of 16 credits per 8,000-character
  chunk (the whole request's estimate, taken atomically up front). The
  reservation is reconciled from the real OpenAI usage once all background
  work of the write has finished and the unused part is refunded
  (:mod:`remembra.core.ai_spend`);
* if the reservation does not fit, or the global free-tier breaker is open,
  the write is **degraded**: it is stored atomically (no extraction, no entity
  resolution), never rejected. Responses carry
  ``X-Remembra-Enrichment: full|degraded|atomic`` and
  ``X-Remembra-Credits-Remaining``;
* relay writes (handoff / checkpoint / status / inbox) and explicit atomic
  stores never touch credits;
* an enriched write's spend job carries the reservation as a hard AI budget:
  once it is used up, the rest of the write falls back to atomic
  (:mod:`remembra.core.ai_spend`).

A write is rejected (429) only at the memory cap or, on Free, past the daily
cap on unenriched writes (atomic, degraded and relay stores: 300/day), which
bounds the embedding spend nothing else meters. Recalls have their own
monthly limit and per-plan burst limit; relay events have a per-plan burst
limit and a soft monthly cap that is reported, not enforced.

Team members of a pooled plan (Team, legacy Pro/Team, Enterprise) are metered
against the owner's account: shared ledger, limits and memory cap.

All of this is a no-op when cloud features are disabled (self-hosted).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, Request, Response, status

from remembra.auth.middleware import (
    AuthenticatedUser,
    get_current_user,
    get_user_from_jwt_or_api_key,
)
from remembra.cloud.metering import AccountState, CreditPeriod, UsageMeter, now_utc
from remembra.cloud.plans import (
    CREDIT_USD,
    OUT_OF_CREDITS_HINT,
    RESERVE_CREDITS_PER_CHUNK,
    PlanTier,
    estimate_chunks,
)
from remembra.cloud.ratelimit import get_cloud_rate_limiter, rate_limits_enabled
from remembra.core import ai_spend

logger = structlog.get_logger(__name__)

ENRICHMENT_HEADER = "X-Remembra-Enrichment"
CREDITS_HEADER = "X-Remembra-Credits-Remaining"
PLAN_HEADER = "X-Remembra-Plan"
RELAY_SOFT_CAP_HEADER = "X-Remembra-Relay-Soft-Cap"

# Memory types that are relay events: free on every plan, never enriched.
RELAY_MEMORY_TYPES = frozenset({"handoff", "checkpoint", "status"})

# Track which users have received warning emails (reset on app restart)
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


def _spawn_email(request: Request, coro: Any) -> None:
    tasks = getattr(request.app.state, "tasks", None)
    if tasks is not None:
        tasks.spawn(coro, name="usage_email")
    else:
        asyncio.get_running_loop().create_task(coro)


async def _user_email(request: Request, user_id: str) -> str | None:
    db = getattr(request.app.state, "db", None)
    if db is None:
        return None
    user_data = await db.get_user_by_id(user_id)
    return user_data.get("email") if user_data else None


def _quota_exceeded(reason: str, limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=reason,
        headers={"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"},
    )


async def _memory_cap_guard(
    request: Request,
    response: Response | None,
    meter: UsageMeter,
    account: AccountState,
    adding: int,
) -> None:
    """Usage headers + warning emails; 429 when ``adding`` memories would pass the cap."""
    user_id = account.user_id
    stored = await meter.count_pool_memories(account)
    cap = max(1, account.memory_cap)
    usage_percent = round(stored / cap * 100, 1)

    if response is not None:
        response.headers["X-Remembra-Usage-Percent"] = str(usage_percent)
        response.headers[PLAN_HEADER] = account.tier.value
        if usage_percent > 60:
            response.headers["X-Remembra-Upgrade-URL"] = "https://remembra.dev/pricing"
    request.state.usage_warning = get_usage_warning(usage_percent, account.tier.value)

    if usage_percent >= 80 and user_id not in _warned_users_80:
        try:
            email = await _user_email(request, user_id)
            if email:
                _spawn_email(
                    request,
                    _send_usage_warning_email(user_id, email, usage_percent, stored, cap, account.tier.value),
                )
        except Exception as e:
            logger.warning("failed_to_queue_warning_email", error=str(e))

    if adding > 0 and stored + adding > cap:
        logger.warning("memory_cap_reached", user_id=user_id, plan=account.tier.value, requested=adding)
        if user_id not in _warned_users_limit:
            try:
                email = await _user_email(request, user_id)
                if email:
                    _spawn_email(request, _send_limit_exceeded_email(user_id, email, stored, cap, account.tier.value))
            except Exception as e:
                logger.warning("failed_to_queue_limit_email", error=str(e))
        detail = f"Memory limit reached ({cap:,} memories)"
        if account.tier == PlanTier.FREE:
            detail += " — Upgrade to Solo for 50,000 memories ($12/mo)."
        raise _quota_exceeded(detail, cap)


def _burst_or_429(namespace: str, user_id: str, per_minute: int, what: str) -> None:
    if not rate_limits_enabled():
        return
    if not get_cloud_rate_limiter().hit(namespace, user_id, f"{per_minute}/minute"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{what} rate limit exceeded ({per_minute}/minute on your plan). Retry shortly.",
            headers={"Retry-After": "60", "X-RateLimit-Limit": str(per_minute), "X-RateLimit-Remaining": "0"},
        )


async def _recall_guard(request: Request, response: Response | None, user_id: str, count: int) -> None:
    meter = _get_meter_or_none(request)
    if meter is None or count <= 0:
        return
    account = await meter.get_account(user_id)
    limits = account.limits
    if count > limits.max_batch_recall_queries and count > 1:
        raise HTTPException(
            status_code=422,
            detail=f"Your plan allows up to {limits.max_batch_recall_queries} queries per batch recall.",
        )
    month = await meter.get_period_counters(account.pool, _calendar_month_start())
    used = month["recalls"]
    usage_percent = round(used / max(1, limits.max_recalls_per_month) * 100, 1)
    if response is not None:
        response.headers["X-Remembra-Usage-Percent"] = str(usage_percent)
        response.headers[PLAN_HEADER] = account.tier.value
        if usage_percent > 60:
            response.headers["X-Remembra-Upgrade-URL"] = "https://remembra.dev/pricing"
    request.state.usage_warning = get_usage_warning(usage_percent, account.tier.value)
    if used + count > limits.max_recalls_per_month:
        logger.warning("recall_quota_exceeded", user_id=user_id, requested=count, plan=account.tier.value)
        raise _quota_exceeded(f"Monthly recall limit reached ({limits.max_recalls_per_month:,}/mo)", limits.max_recalls_per_month)
    _burst_or_429("recall", user_id, limits.recall_burst_per_min, "Recall")


def _calendar_month_start() -> datetime:
    """Recall and relay counters are calendar-monthly, even on annual plans."""
    return CreditPeriod.monthly(now_utc()).start


async def enforce_recall_limit(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Dependency: monthly recall limit + per-plan recall burst. Recalls never use credits."""
    await _recall_guard(request, response, current_user.user_id, 1)


async def enforce_key_limit(
    request: Request,
    current_user: AuthenticatedUser | None = Depends(get_user_from_jwt_or_api_key),
) -> None:
    """Dependency that enforces API key creation limits (no-op without cloud)."""
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
            "key_limit_exceeded",
            user_id=current_user.user_id,
            plan=snapshot.plan.value,
            reason=check.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=check.reason or "API key limit exceeded",
        )


# -----------------------------------------------------------------------
# Write gate: input limits, memory cap, credit reservation / degrade
# -----------------------------------------------------------------------


@dataclass
class EnrichmentGrant:
    """Outcome of :func:`gate_write` for one request."""

    mode: str  # "full" | "degraded" | "atomic" | "unmetered"
    reason: str | None = None
    job: ai_spend.SpendJob | None = None
    credits_remaining: int | None = None
    reserved_credits: int = 0
    min_credits: int = 0
    user_id: str = ""
    meter: UsageMeter | None = None

    @property
    def enrich(self) -> bool:
        """May this write run LLM enrichment?"""
        return self.mode in ("full", "unmetered")

    @property
    def degraded(self) -> bool:
        return self.mode == "degraded"

    def activate(self) -> AbstractContextManager[ai_spend.SpendJob | None]:
        """Publish the spend job while the write runs (settles when all its work is done)."""
        return ai_spend.activate(self.job)

    async def record_degraded(self, stored: int) -> None:
        """Count stores that were saved atomically because enrichment was unavailable."""
        if self.degraded and self.meter is not None and stored > 0:
            await self.meter.record_degraded_store(self.user_id, stored)


def _set_enrichment_headers(response: Response | None, grant: EnrichmentGrant, tier: str | None) -> None:
    if response is None:
        return
    response.headers[ENRICHMENT_HEADER] = grant.mode if grant.mode != "unmetered" else "full"
    if grant.credits_remaining is not None:
        response.headers[CREDITS_HEADER] = str(grant.credits_remaining)
    if tier:
        response.headers[PLAN_HEADER] = tier


async def _project_guard(meter: UsageMeter, account: AccountState, writer_id: str, project_ids: Iterable[str]) -> None:
    wanted = {p for p in project_ids if p}
    if not wanted:
        return
    new = [p for p in wanted if not await meter.project_exists(writer_id, p)]
    if not new:
        return
    existing = await meter.count_projects(writer_id)
    if existing + len(new) > account.limits.max_projects:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Project limit reached ({account.limits.max_projects} projects on the "
                f"{account.limits.display_name} plan). Existing projects keep working; "
                "Solo includes unlimited projects."
            ),
        )


async def _unenriched_guard(meter: UsageMeter, account: AccountState, texts: Sequence[str]) -> None:
    """Free: 429 past the daily cap on unenriched writes; their embedding cost feeds the free breaker."""
    if not texts:
        return
    cap = account.limits.max_unenriched_writes_per_day
    if cap is not None and account.free_group and not await meter.take_unenriched_writes(account, len(texts)):
        logger.warning("unenriched_daily_cap_reached", user_id=account.user_id, plan=account.tier.value, requested=len(texts))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily limit reached: {cap:,} stores without enrichment per day on the "
                f"{account.limits.display_name} plan. It resets at 00:00 UTC; Solo has no daily limit."
            ),
            headers={"Retry-After": "3600", "X-RateLimit-Limit": str(cap), "X-RateLimit-Remaining": "0"},
        )
    if account.free_group:
        await meter.record_embedding_spend(account, list(texts))


async def relay_guard(request: Request, response: Response | None, user_id: str) -> None:
    """Per-plan relay burst limit + soft monthly cap header. Relay never uses credits."""
    meter = _get_meter_or_none(request)
    if meter is None:
        return
    account = await meter.get_account(user_id)
    _burst_or_429("relay", user_id, account.limits.relay_burst_per_min, "Relay")
    month = await meter.get_period_counters(user_id, _calendar_month_start())
    over = month["relay_events"] >= account.limits.max_relay_events_per_month
    if response is not None:
        response.headers[PLAN_HEADER] = account.tier.value
        if over:
            response.headers[RELAY_SOFT_CAP_HEADER] = "exceeded"
    if over:
        logger.info("relay_soft_cap_exceeded", user_id=user_id, plan=account.tier.value)


async def gate_write(
    request: Request,
    response: Response | None,
    user_id: str,
    contents: Sequence[str],
    *,
    atomic: Sequence[bool] | None = None,
    project_ids: Iterable[str] = (),
    memories_added: int | None = None,
    enforce_batch_limit: bool = True,
    enforce_content_limit: bool = True,
    relay: bool = False,
    count_unenriched: bool = True,
) -> EnrichmentGrant:
    """Gate one write request BEFORE any LLM call.

    Args:
        contents: the text of every item (for ingest: the transcript).
        atomic: per item, True when the caller asked for an atomic store
            (skip_extraction / relay memory types) — no credits needed.
        project_ids: projects written to (plan project cap).
        memories_added: rows this write may add (defaults to ``len(contents)``).
        enforce_batch_limit / enforce_content_limit: apply the plan's batch
            size and per-store character limits (imports/ingest are chunk
            metered instead).
        relay: the write is a relay event (per-plan relay burst limit).
        count_unenriched: count atomic / degraded items toward the plan's daily
            unenriched-write cap (False when nothing is embedded server side).

    Raises:
        413 content over the plan's per-store limit; 422 batch too large;
        403 new project over the plan's project cap; 429 memory cap, a
        relay burst, or (Free) the daily unenriched-write cap.
    """
    meter = _get_meter_or_none(request)
    if meter is None:
        return EnrichmentGrant(mode="unmetered", user_id=user_id)

    account = await meter.get_account(user_id)
    limits = account.limits
    flags = list(atomic) if atomic is not None else [False] * len(contents)

    if enforce_batch_limit and len(contents) > limits.max_batch_items:
        raise HTTPException(
            status_code=422,
            detail=f"Batch too large: your plan allows {limits.max_batch_items} items per request.",
        )
    if enforce_content_limit:
        too_long = [i for i, c in enumerate(contents) if len(c) > limits.max_content_chars]
        if too_long:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Content too long: your plan allows {limits.max_content_chars:,} characters per store (item {too_long[0]})."
                ),
            )

    await _memory_cap_guard(request, response, meter, account, len(contents) if memories_added is None else memories_added)
    await _project_guard(meter, account, user_id, project_ids)
    if relay:
        await relay_guard(request, response, user_id)

    enrichable = [c for c, is_atomic in zip(contents, flags, strict=False) if not is_atomic]
    atomic_items = [c for c, is_atomic in zip(contents, flags, strict=False) if is_atomic]
    if count_unenriched:
        await _unenriched_guard(meter, account, atomic_items)
    if not enrichable:
        balance = await meter.get_credit_balance(account)
        grant = EnrichmentGrant(mode="atomic", credits_remaining=balance.remaining, user_id=user_id, meter=meter)
        _set_enrichment_headers(response, grant, account.tier.value)
        return grant

    min_credits = estimate_chunks(enrichable)
    reason: str | None = None
    reservation_id: str | None = None
    if account.free_group and await meter.free_breaker_open():
        reason = "free_breaker_open"
    else:
        reservation_id = await meter.reserve_credits(account, min_credits * RESERVE_CREDITS_PER_CHUNK, min_credits=min_credits)
        if reservation_id is None:
            reason = "credits_exhausted"

    balance = await meter.get_credit_balance(account)
    if reservation_id is None:
        logger.info("enrichment_degraded", user_id=user_id, reason=reason, chunks=min_credits, plan=account.tier.value)
        if count_unenriched:
            await _unenriched_guard(meter, account, enrichable)
        grant = EnrichmentGrant(
            mode="degraded",
            reason=reason,
            credits_remaining=balance.remaining,
            min_credits=min_credits,
            user_id=user_id,
            meter=meter,
        )
        if response is not None and reason == "credits_exhausted":
            response.headers["X-Remembra-Upgrade-Hint"] = OUT_OF_CREDITS_HINT
        _set_enrichment_headers(response, grant, account.tier.value)
        return grant

    rid = reservation_id
    reserved = min_credits * RESERVE_CREDITS_PER_CHUNK
    if account.free_group:
        await meter.record_embedding_spend(account, enrichable)

    async def _settle(usd: float, enriched: bool) -> int:
        return await meter.settle_reservation(rid, usd, enriched=enriched)

    job = ai_spend.SpendJob(
        user_id=user_id,
        settle=_settle,
        concurrency=limits.enrichment_concurrency,
        label=request.url.path,
        # The reservation is a hard budget: no AI call may take the write past it.
        budget_usd=reserved * CREDIT_USD,
        reservation_id=rid,
    )
    grant = EnrichmentGrant(
        mode="full",
        job=job,
        credits_remaining=balance.remaining,
        reserved_credits=reserved,
        min_credits=min_credits,
        user_id=user_id,
        meter=meter,
    )
    _set_enrichment_headers(response, grant, account.tier.value)
    return grant


# -----------------------------------------------------------------------
# Usage recording helpers (called AFTER successful operations)
# -----------------------------------------------------------------------


async def record_store_usage(request: Request, user_id: str, count: int = 1) -> None:
    """Record ``count`` store events in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None and count > 0:
        await meter.record_store(user_id, count)


async def record_relay_usage(request: Request, user_id: str, count: int = 1) -> None:
    """Record relay events (handoff / checkpoint / status / inbox). Never consumes credits."""
    meter = _get_meter_or_none(request)
    if meter is not None and count > 0:
        await meter.record_relay_event(user_id, count)


async def record_recall_usage(request: Request, user_id: str, count: int = 1) -> None:
    """Record ``count`` recall events in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None and count > 0:
        await meter.record_recall(user_id, count)


async def enforce_recall_quota(request: Request, user_id: str, count: int = 1, response: Response | None = None) -> None:
    """Reject ``count`` recalls over the monthly limit (429), the burst limit (429) or the batch size (422)."""
    await _recall_guard(request, response, user_id, count)


async def record_delete_usage(request: Request, user_id: str) -> None:
    """Record a delete event in usage metering (no-op if cloud disabled)."""
    meter = _get_meter_or_none(request)
    if meter is not None:
        await meter.record_delete(user_id)


# -----------------------------------------------------------------------
# Type aliases for FastAPI Depends
# -----------------------------------------------------------------------

EnforceRecallLimit = Annotated[None, Depends(enforce_recall_limit)]
EnforceKeyLimit = Annotated[None, Depends(enforce_key_limit)]
