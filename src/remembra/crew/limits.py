"""Crew rate limits, plan limits and soft caps (spec §11.2 rate table, §12, §4.5).

Rate limits
    The existing limiter keys every account on ``user:{id}`` (``core.limiter``), so
    one chatty agent could push every other agent of the same user into 429 and,
    through D11, into fail-open. Crew routes add:

    * a **per-session** limit keyed on a hash of the session token,
    * a **per-host** limit for heartbeats keyed on a hash of the host token, and
    * a **per-user** limit in a namespace of its own for each bucket.

    Every bucket lives in its own namespace, so no bucket can drain another. The
    ``guard`` and ``claims`` buckets are additionally **reserved**: their routes
    must not also carry the shared per-user slowapi limit, so no other traffic of
    the same account can starve them (``RESERVED_BUCKETS``). Tokens are hashed
    before they reach the storage backend; a raw token is never a storage key.

Plan limits
    ``PlanLimits`` (``cloud.plans``) carries the per-crew numbers; they apply to the
    crew **owner's** plan and are never seat-scaled. :func:`crew_limits` turns a
    plan into a :class:`CrewLimits` with the full §4.5 retention table.

Soft caps (§12) never reduce protection:

    * over the live-session cap, ``join`` still succeeds, **observe-only**;
    * over the daily event soft cap, only ``activity.burst`` coalesces (5-min
      windows instead of 60 s); nothing else is ever dropped; ``budget.warning`` at
      80 % and ``budget.cap_reached`` at 100 % are emitted once each per day;
    * over the promotion cap, promotions are deferred to the next UTC day, except
      ``quota`` and ``lost`` handoffs, which always promote (and still count);
    * the zone cap refuses *new* zones (409 ``zone_cap``); existing zones and the
      built-in ``crew-policy`` zone are never removed or counted.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from fastapi import status
from limits import RateLimitItem, parse
from limits.storage import Storage, storage_from_string
from limits.strategies import MovingWindowRateLimiter

from remembra.cloud.plans import PLANS, SELF_SERVE_TIERS, PlanLimits, PlanTier, get_plan
from remembra.crew.access import crew_error

# ---------------------------------------------------------------------------
# Rate limits (§11.2)
# ---------------------------------------------------------------------------

SCOPES: Final = ("session", "user", "host")


@dataclass(frozen=True)
class BucketSpec:
    """Limits for one bucket, as ``limits`` strings; None = that scope is not limited."""

    per_session: str | None = None
    per_user: str | None = None
    per_host: str | None = None
    reserved: bool = False  # routes in this bucket must not also use the shared per-user limit

    def limit_for(self, scope: str) -> str | None:
        return {"session": self.per_session, "user": self.per_user, "host": self.per_host}[scope]


CREW_RATE_BUCKETS: Final[Mapping[str, BucketSpec]] = {
    "heartbeat": BucketSpec(per_host="2/minute"),
    "events": BucketSpec(per_session="30/minute", per_user="120/minute"),
    "guard": BucketSpec(per_session="300/minute", per_user="1200/minute", reserved=True),
    "claims": BucketSpec(per_session="60/minute", per_user="240/minute", reserved=True),
    "adopt": BucketSpec(per_session="6/hour", per_user="30/hour"),
    "messages": BucketSpec(per_session="20/minute", per_user="60/minute"),
    # Agent-originated Needs-you items (WP-7 applies it when an agent raises one).
    "needs_you": BucketSpec(per_session="6/hour", per_user="30/hour"),
    "tasks": BucketSpec(per_session="60/minute", per_user="120/minute"),
    "snapshot": BucketSpec(per_session="1 per 10 seconds"),
    "events_poll": BucketSpec(per_session="20/minute"),
    "join": BucketSpec(per_user="30/minute"),  # join and leave (and host registration)
    "ws_replay": BucketSpec(per_user="30/minute"),  # WebSocket subscribes with since_seq (WP-2)
    "default": BucketSpec(),  # no crew-specific limit; the app's per-user limiter applies
}
RESERVED_BUCKETS: Final = frozenset(name for name, spec in CREW_RATE_BUCKETS.items() if spec.reserved)


def token_key(token: str) -> str:
    """Storage key for a session or host token (sha256; the raw token never reaches storage)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    bucket: str
    scope: str | None = None  # the scope that refused (session | user | host)
    limit: str | None = None
    retry_after_s: int = 0


class CrewRateLimiter:
    """Moving-window limiter for the crew buckets over a ``limits`` storage backend.

    ``storage_uri`` follows ``REMEMBRA_RATE_LIMIT_STORAGE``: ``memory://`` (per
    process; production runs one worker) or ``redis://…`` (shared).
    """

    def __init__(self, storage_uri: str = "memory://", *, clock: Callable[[], float] = time.time) -> None:
        storage = storage_from_string(storage_uri)
        if not isinstance(storage, Storage):
            raise ValueError(f"Rate limit storage {storage_uri!r} is async-only; use memory:// or redis://")
        self.storage_uri = storage_uri
        self._storage = storage
        self._limiter = MovingWindowRateLimiter(storage)
        self._clock = clock
        self._items: dict[str, RateLimitItem] = {}

    def _item(self, limit: str) -> RateLimitItem:
        item = self._items.get(limit)
        if item is None:
            item = self._items[limit] = parse(limit)
        return item

    def check(
        self,
        bucket: str,
        *,
        user_id: str | None = None,
        session_token: str | None = None,
        host_token: str | None = None,
    ) -> RateDecision:
        """Consume one request of ``bucket`` for every scope given; nothing is consumed when any scope refuses.

        ``session_token`` / ``host_token`` may be the raw token or the stored token
        hash (anything stable per session / host); either way it is hashed again
        before use. A scope whose key is not supplied is skipped (e.g. a dashboard
        caller has no session), except ``per_host``, which is mandatory.
        """
        spec = CREW_RATE_BUCKETS.get(bucket)
        if spec is None:
            raise ValueError(f"unknown crew rate bucket {bucket!r}")
        if spec.per_host and not host_token:
            raise ValueError(f"bucket {bucket!r} is keyed on the host token; host_token is required")
        keys = {
            "session": token_key(session_token) if session_token else None,
            "user": user_id or None,
            "host": token_key(host_token) if host_token else None,
        }
        planned: list[tuple[str, str, RateLimitItem, str]] = []
        for scope in SCOPES:
            limit = spec.limit_for(scope)
            key = keys[scope]
            if limit and key:
                planned.append((scope, limit, self._item(limit), key))
        namespace = f"crew:{bucket}"
        for scope, limit, item, key in planned:
            if not self._limiter.test(item, f"{namespace}:{scope}", key):
                return RateDecision(False, bucket, scope, limit, self._retry_after(item, f"{namespace}:{scope}", key))
        for scope, limit, item, key in planned:
            if not self._limiter.hit(item, f"{namespace}:{scope}", key):
                # Lost a race with a concurrent request between test and hit.
                return RateDecision(False, bucket, scope, limit, self._retry_after(item, f"{namespace}:{scope}", key))
        return RateDecision(True, bucket)

    def _retry_after(self, item: RateLimitItem, namespace: str, key: str) -> int:
        stats = self._limiter.get_window_stats(item, namespace, key)
        return max(1, math.ceil(stats.reset_time - self._clock()))

    def reset(self) -> None:
        self._storage.reset()


_crew_limiter: CrewRateLimiter | None = None


def get_crew_rate_limiter() -> CrewRateLimiter:
    """The process-wide crew limiter, on the same backend setting as the app limiter."""
    global _crew_limiter
    if _crew_limiter is None:
        from remembra.cloud.ratelimit import storage_uri_from_setting
        from remembra.config import get_settings

        _crew_limiter = CrewRateLimiter(storage_uri_from_setting(get_settings().rate_limit_storage))
    return _crew_limiter


def set_crew_rate_limiter(limiter: CrewRateLimiter | None) -> None:
    global _crew_limiter
    _crew_limiter = limiter


def enforce_rate_limit(
    bucket: str,
    *,
    user_id: str | None = None,
    session_token: str | None = None,
    host_token: str | None = None,
    limiter: CrewRateLimiter | None = None,
) -> None:
    """Raise 429 ``rate_limited`` (``retry_after_s`` + ``Retry-After``) when ``bucket`` is exhausted.

    A no-op when rate limiting is disabled (``REMEMBRA_RATE_LIMIT_ENABLED=false``)
    and no explicit ``limiter`` is passed.
    """
    if limiter is None:
        from remembra.config import get_settings

        if not get_settings().rate_limit_enabled:
            return
        limiter = get_crew_rate_limiter()
    decision = limiter.check(bucket, user_id=user_id, session_token=session_token, host_token=host_token)
    if not decision.allowed:
        raise crew_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            f"Too many {bucket} requests for this {decision.scope} ({decision.limit}). Retry in {decision.retry_after_s}s.",
            headers={"Retry-After": str(decision.retry_after_s)},
            retry_after_s=decision.retry_after_s,
        )


# ---------------------------------------------------------------------------
# Plan limits (§12) and retention (§4.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrewRetention:
    """Retention per data class (§4.5). Moments, human actions, bypasses and policy changes are never deleted."""

    events_days: int
    activity_burst_days: int
    checkpoint_facts_days: int  # then headline + hash only
    ended_sessions_days: int
    brief_text_days: int
    footprints_after_end_days: int = 7
    idempotency_hours: int = 72
    baton_ref_days_after_close: int = 7  # after adopt or release


# (activity.burst, checkpoint facts, ended sessions, baton brief text) in days. Raw events come
# from PlanLimits.crew_event_retention_days. Solo is not in §4.5 (it predates the 2026-09 Solo
# tier) and sits between Free and Pro; Enterprise holds the contract defaults ("custom").
_RETENTION_DETAIL: Final[Mapping[PlanTier, tuple[int, int, int, int]]] = {
    PlanTier.FREE: (7, 14, 90, 30),
    PlanTier.SOLO: (14, 30, 180, 90),
    PlanTier.PRO: (30, 90, 365, 180),
    PlanTier.TEAM: (60, 180, 365, 365),
    PlanTier.ENTERPRISE: (90, 365, 730, 730),
    PlanTier.LEGACY_PRO: (30, 90, 365, 180),
    PlanTier.LEGACY_TEAM: (60, 180, 365, 365),
}


@dataclass(frozen=True)
class CrewLimits:
    """The crew-mode limits in force for one crew (from its owner's plan)."""

    tier: str
    max_sessions_live: int
    max_zones: int
    events_per_day_soft: int
    memory_promotions_per_day: int
    teammates: bool
    retention: CrewRetention


def crew_limits(plan: PlanLimits, *, tier: str | None = None) -> CrewLimits:
    burst, facts, ended, brief = _RETENTION_DETAIL[plan.tier]
    return CrewLimits(
        tier=tier or plan.tier.value,
        max_sessions_live=plan.max_crew_sessions_live,
        max_zones=plan.max_zones,
        events_per_day_soft=plan.crew_events_per_day_soft,
        memory_promotions_per_day=plan.crew_memory_promotions_per_day,
        teammates=plan.crew_teammates,
        retention=CrewRetention(
            events_days=plan.crew_event_retention_days,
            activity_burst_days=burst,
            checkpoint_facts_days=facts,
            ended_sessions_days=ended,
            brief_text_days=brief,
        ),
    )


def crew_limits_for_tier(tier: PlanTier | str) -> CrewLimits:
    return crew_limits(get_plan(tier))


# Self-hosted servers (no cloud metering) get the Enterprise contract defaults.
SELF_HOSTED_CREW_LIMITS: Final = crew_limits(PLANS[PlanTier.ENTERPRISE], tier="self_hosted")


async def crew_limits_for_owner(meter: Any, owner_user_id: str) -> CrewLimits:
    """Crew limits from the crew owner's plan (``UsageMeter.get_account``); self-hosted when ``meter`` is None."""
    if meter is None:
        return SELF_HOSTED_CREW_LIMITS
    account = await meter.get_account(owner_user_id)
    # account.limits is seat-scaled; crew fields are never scaled, so this is the catalog value.
    return crew_limits(account.limits)


def _upgrade_target(current_tier: str, field_name: str, current_value: int) -> PlanLimits | None:
    """The cheapest self-serve plan with a higher value of ``field_name`` (None at the top)."""
    if current_tier in (PlanTier.ENTERPRISE.value, "self_hosted"):
        return None
    for tier in SELF_SERVE_TIERS:
        plan = PLANS[tier]
        if getattr(plan, field_name) > current_value:
            return plan
    return PLANS[PlanTier.ENTERPRISE]


def _upgrade_hint(limits: CrewLimits, field_name: str, current_value: int, noun: str) -> str | None:
    target = _upgrade_target(limits.tier, field_name, current_value)
    if target is None:
        return None
    return f"{target.display_name} allows {getattr(target, field_name):,} {noun}."


@dataclass(frozen=True)
class SeatDecision:
    """Outcome of the live-session cap for one join (§12): never a refusal."""

    observe_only: bool
    live_sessions: int  # full-seat live sessions before this join
    limit: int
    upgrade_hint: str | None = None

    @property
    def can_claim(self) -> bool:
        return not self.observe_only


def seat_for_join(live_sessions: int, limits: CrewLimits) -> SeatDecision:
    """``live_sessions`` = live full-seat sessions of the crew excluding the joiner (observe-only ones do not count)."""
    if live_sessions < limits.max_sessions_live:
        return SeatDecision(False, live_sessions, limits.max_sessions_live)
    hint = _upgrade_hint(limits, "max_crew_sessions_live", limits.max_sessions_live, "live sessions per crew")
    return SeatDecision(True, live_sessions, limits.max_sessions_live, hint)


def enforce_zone_capacity(existing_zones: int, adding: int, limits: CrewLimits) -> None:
    """409 ``zone_cap`` when ``adding`` new zones would pass the plan's zone cap.

    ``existing_zones`` counts live, non-builtin zones (``crew-policy`` never counts).
    Only additions are refused; zones that already exist are never archived or
    loosened because of the cap.
    """
    if adding <= 0:
        return
    if existing_zones + adding > limits.max_zones:
        hint = _upgrade_hint(limits, "max_zones", limits.max_zones, "zones per crew")
        raise crew_error(
            status.HTTP_409_CONFLICT,
            "zone_cap",
            f"This crew has {existing_zones} of {limits.max_zones} zones; {adding} more would pass the plan limit."
            + (f" {hint}" if hint else ""),
        )


# --- daily event soft cap ----------------------------------------------------

BURST_WINDOW_S: Final = 60
BURST_WINDOW_OVER_CAP_S: Final = 300
BUDGET_WARNING_FRACTION: Final = 0.8
COALESCIBLE_EVENT_TYPES: Final = frozenset({"activity.burst"})


def warning_threshold(soft_cap: int) -> int:
    return max(1, math.ceil(soft_cap * BUDGET_WARNING_FRACTION))


def over_event_cap(events_today: int, limits: CrewLimits) -> bool:
    return events_today >= limits.events_per_day_soft


def burst_window_s(events_today: int, limits: CrewLimits) -> int:
    """Minimum spacing of ``activity.burst`` events per session: 60 s, or 5 min over the soft cap."""
    return BURST_WINDOW_OVER_CAP_S if over_event_cap(events_today, limits) else BURST_WINDOW_S


def should_coalesce(event_type: str, seconds_since_last_burst: float | None, events_today: int, limits: CrewLimits) -> bool:
    """True when this event must be merged into the session's previous ``activity.burst``.

    Only ``activity.burst`` ever coalesces. Claims, guard decisions, collisions,
    tasks, reports, checkpoints, handoffs, batons, decisions, messages and moments
    are never dropped, whatever the count.
    """
    if event_type not in COALESCIBLE_EVENT_TYPES or seconds_since_last_burst is None:
        return False
    return seconds_since_last_burst < burst_window_s(events_today, limits)


def budget_signals(events_before: int, events_after: int, limits: CrewLimits) -> list[tuple[str, dict[str, Any]]]:
    """Server events to emit when the day's count moves from ``events_before`` to ``events_after``.

    Each threshold fires exactly once per day because the count only grows:
    ``budget.warning`` when it reaches 80 % of the soft cap, ``budget.cap_reached``
    (which also raises a Needs-you item) when it reaches the cap. Payloads follow
    ``schemas.EVENT_SPECS``.
    """
    cap = limits.events_per_day_soft
    out: list[tuple[str, dict[str, Any]]] = []
    for event_type, threshold in (("budget.warning", warning_threshold(cap)), ("budget.cap_reached", cap)):
        if events_before < threshold <= events_after:
            out.append((event_type, {"metric": "crew_events_per_day", "used": events_after, "limit": cap}))
    return out


# --- memory promotions ---------------------------------------------------------

ALWAYS_PROMOTE_TRIGGERS: Final = frozenset({"quota", "lost"})


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool  # False = defer to the next UTC day (queue it)
    counts: bool  # every promotion made counts against the daily cap
    reason: str


def promotion_decision(promotions_today: int, trigger: str, limits: CrewLimits) -> PromotionDecision:
    """Promote now or defer a checkpoint promotion (D15, §12). Quota and lost always promote."""
    if trigger in ALWAYS_PROMOTE_TRIGGERS:
        return PromotionDecision(True, True, "always_promote")
    if promotions_today < limits.memory_promotions_per_day:
        return PromotionDecision(True, True, "under_cap")
    return PromotionDecision(False, False, "cap_reached_deferred")
