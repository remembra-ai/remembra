"""
Plan catalog and limit checks for Remembra Cloud (the ONE plan module).

Owner-approved prices (2026-09):

  - free:        $0            relay free, 500 smart credits/mo (25 until email verified)
  - solo:        $12/mo  or $120/yr   2,200 credits/mo, 50K memories
  - founding:    Solo at $108/yr, price locked for life, annual only, first 100
  - pro:         $29/mo  or $290/yr   5,000 credits/mo, 125K memories
  - team:        $15/seat/mo or $150/seat/yr, 3-seat minimum, limits pooled per seat
  - enterprise:  custom contract ($399/mo floor)

Grandfathered (existing subscribers only, never sold):

  - legacy_pro_49:    the old $49 Pro  — 12,000 credits ($30 AI ceiling), 250K memories
  - legacy_team_199:  the old $199 Team — 60,000 credits ($150 AI ceiling), 600K memories

Smart credits are the only metered unit. One credit is $0.0025 of AI spend;
an enriched store costs ``max(ceil(chars / 8000), actual LLM $ / 0.0025)``.
Relay events (handoff / checkpoint / status / inbox), pickup briefs, trail
reads and recalls never consume credits. When credits run out, stores degrade
to atomic (no extraction, no entity resolution) instead of being rejected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

# One smart credit buys this much AI spend (USD).
CREDIT_USD = 0.0025
# Content is metered per chunk of this many characters.
CREDIT_CHUNK_CHARS = 8000
# Credits reserved per chunk before enrichment runs ($0.04) — the worst case
# a single chunk may spend; the difference is refunded when the work settles.
RESERVE_CREDITS_PER_CHUNK = 16
# Annual plans get the whole year's credits up front.
MONTHS_PER_YEAR = 12

OUT_OF_CREDITS_HINT = "You're out of smart credits; stores still save without enrichment. Solo gives 2,200/mo for $12."


class PlanTier(StrEnum):
    FREE = "free"
    SOLO = "solo"
    PRO = "pro"
    TEAM = "team"
    ENTERPRISE = "enterprise"
    # Grandfathered subscribers of the pre-2026-09 catalog. Not purchasable.
    LEGACY_PRO = "legacy_pro_49"
    LEGACY_TEAM = "legacy_team_199"


class BillingInterval(StrEnum):
    MONTH = "month"
    YEAR = "year"

    @classmethod
    def parse(cls, value: str | None) -> BillingInterval:
        """Accept the spellings clients use: monthly/month, yearly/annual/year."""
        normalized = (value or "month").strip().lower()
        if normalized in ("year", "yearly", "annual", "annually"):
            return cls.YEAR
        if normalized in ("month", "monthly"):
            return cls.MONTH
        raise ValueError(f"Unknown billing interval: {value!r}")


# Tiers a customer can buy through self-serve checkout.
SELF_SERVE_TIERS: tuple[PlanTier, ...] = (PlanTier.SOLO, PlanTier.PRO, PlanTier.TEAM)
LEGACY_TIERS: frozenset[PlanTier] = frozenset({PlanTier.LEGACY_PRO, PlanTier.LEGACY_TEAM})
# Founding 100: Solo, annual only, price locked for life.
FOUNDING_MAX_REDEMPTIONS = 100
FOUNDING_ANNUAL_PRICE_CENTS = 10_800


@dataclass(frozen=True)
class PlanLimits:
    """Enforced limits for one plan tier.

    For ``per_seat`` plans (Team) every pooled field holds the PER-SEAT value;
    :meth:`scaled` multiplies them by the billed seat count.
    """

    tier: PlanTier
    display_name: str

    # Storage
    max_memories: int
    max_storage_mb: int

    # Metered usage (per month; annual plans bank 12x credits up front)
    max_smart_credits_per_month: int
    max_recalls_per_month: int
    max_relay_events_per_month: int  # soft cap: surfaced, never rejected

    # Input limits
    max_content_chars: int
    max_batch_items: int
    max_batch_recall_queries: int

    # Burst limits (per minute, per account)
    recall_burst_per_min: int
    relay_burst_per_min: int

    # Access
    max_api_keys: int
    max_users: int  # seats
    max_projects: int

    # Background enrichment jobs allowed to run at once for this account
    enrichment_concurrency: int

    retention_days: int | None = None  # None = unlimited

    # Email verification gate (Free): credits available before the address is verified
    unverified_credit_cap: int | None = None

    # Memory cap before the 30-day notice of a cap reduction takes effect
    # (None = the cap did not shrink for this tier).
    pre_notice_max_memories: int | None = None

    per_seat: bool = False
    min_seats: int = 1

    # List prices in USD cents (None = not sold / custom)
    price_monthly_cents: int | None = None
    price_annual_cents: int | None = None

    # Features
    has_hybrid_search: bool = True
    has_entity_resolution: bool = True
    has_temporal_decay: bool = True
    has_reranking: bool = True
    has_graph_retrieval: bool = True
    has_webhooks: bool = False
    has_sso: bool = False
    has_observability: bool = False
    has_priority_support: bool = False

    @property
    def is_legacy(self) -> bool:
        return self.tier in LEGACY_TIERS

    @property
    def llm_ceiling_usd_month(self) -> float:
        """Hard monthly AI-spend ceiling implied by the credit allowance."""
        return round(self.max_smart_credits_per_month * CREDIT_USD, 4)

    def credit_allowance(self, interval: BillingInterval) -> int:
        """Credits available per billing period: a month, or a yearly bank of 12 months."""
        if interval == BillingInterval.YEAR:
            return self.max_smart_credits_per_month * MONTHS_PER_YEAR
        return self.max_smart_credits_per_month

    def memory_cap(self, now: datetime, notice_effective_at: datetime | None) -> int:
        """The memory cap in force at ``now``.

        A reduced cap only applies once the written-notice date has passed; until
        the owner sets that date (``memory_cap_notice_effective_at``) the previous
        cap stays in force.
        """
        if self.pre_notice_max_memories is None:
            return self.max_memories
        if notice_effective_at is None or _naive(now) < _naive(notice_effective_at):
            return self.pre_notice_max_memories
        return self.max_memories

    def scaled(self, seats: int) -> PlanLimits:
        """Pooled limits for ``seats`` billed seats (identity for non-seat plans)."""
        if not self.per_seat:
            return self
        seats = max(self.min_seats, int(seats or 0))
        return replace(
            self,
            max_memories=self.max_memories * seats,
            max_storage_mb=self.max_storage_mb * seats,
            max_smart_credits_per_month=self.max_smart_credits_per_month * seats,
            max_recalls_per_month=self.max_recalls_per_month * seats,
            max_relay_events_per_month=self.max_relay_events_per_month * seats,
            max_api_keys=self.max_api_keys * seats,
            max_users=seats,
            per_seat=False,
            min_seats=seats,
        )


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

_UNLIMITED_PROJECTS = 1_000

PLANS: dict[PlanTier, PlanLimits] = {
    PlanTier.FREE: PlanLimits(
        tier=PlanTier.FREE,
        display_name="Relay Free",
        max_memories=10_000,
        pre_notice_max_memories=25_000,  # previous Free cap, until notice
        max_storage_mb=250,
        max_smart_credits_per_month=500,  # $1.25 AI ceiling
        unverified_credit_cap=25,
        max_recalls_per_month=10_000,
        max_relay_events_per_month=5_000,
        max_content_chars=8_000,
        max_batch_items=10,
        max_batch_recall_queries=5,
        recall_burst_per_min=20,
        relay_burst_per_min=30,
        max_api_keys=3,
        max_users=1,
        max_projects=3,
        enrichment_concurrency=2,
        price_monthly_cents=0,
        price_annual_cents=0,
    ),
    PlanTier.SOLO: PlanLimits(
        tier=PlanTier.SOLO,
        display_name="Solo",
        max_memories=50_000,
        max_storage_mb=2_500,
        max_smart_credits_per_month=2_200,  # $5.50 AI ceiling
        max_recalls_per_month=50_000,
        max_relay_events_per_month=25_000,
        max_content_chars=50_000,
        max_batch_items=100,
        max_batch_recall_queries=20,
        recall_burst_per_min=60,
        relay_burst_per_min=60,
        max_api_keys=10,
        max_users=1,
        max_projects=_UNLIMITED_PROJECTS,
        enrichment_concurrency=4,
        price_monthly_cents=1_200,
        price_annual_cents=12_000,
        has_webhooks=True,
    ),
    PlanTier.PRO: PlanLimits(
        tier=PlanTier.PRO,
        display_name="Pro",
        max_memories=125_000,
        max_storage_mb=6_250,
        max_smart_credits_per_month=5_000,  # $12.50 AI ceiling
        max_recalls_per_month=250_000,
        max_relay_events_per_month=100_000,
        max_content_chars=50_000,
        max_batch_items=100,
        max_batch_recall_queries=20,
        recall_burst_per_min=120,
        relay_burst_per_min=120,
        max_api_keys=25,
        max_users=1,
        max_projects=_UNLIMITED_PROJECTS,
        enrichment_concurrency=8,
        price_monthly_cents=2_900,
        price_annual_cents=29_000,
        has_webhooks=True,
        has_observability=True,
        has_priority_support=True,
    ),
    PlanTier.TEAM: PlanLimits(
        tier=PlanTier.TEAM,
        display_name="Team",
        # Per seat; pooled across the team via scaled(seats).
        max_memories=50_000,
        max_storage_mb=2_500,
        max_smart_credits_per_month=2_200,
        max_recalls_per_month=50_000,
        max_relay_events_per_month=25_000,
        max_content_chars=50_000,
        max_batch_items=100,
        max_batch_recall_queries=20,
        recall_burst_per_min=120,
        relay_burst_per_min=120,
        max_api_keys=10,
        max_users=1,
        max_projects=_UNLIMITED_PROJECTS,
        enrichment_concurrency=8,
        per_seat=True,
        min_seats=3,
        price_monthly_cents=1_500,  # per seat
        price_annual_cents=15_000,  # per seat
        has_webhooks=True,
        has_observability=True,
        has_priority_support=True,
    ),
    PlanTier.ENTERPRISE: PlanLimits(
        tier=PlanTier.ENTERPRISE,
        display_name="Enterprise",
        max_memories=10_000_000,
        max_storage_mb=100_000,
        # Default contract ceiling ($150/mo); every contract carries an explicit one.
        max_smart_credits_per_month=60_000,
        max_recalls_per_month=5_000_000,
        max_relay_events_per_month=1_000_000,
        max_content_chars=50_000,
        max_batch_items=100,
        max_batch_recall_queries=20,
        recall_burst_per_min=600,
        relay_burst_per_min=600,
        max_api_keys=100,
        max_users=1_000,
        max_projects=10_000,
        enrichment_concurrency=8,
        price_monthly_cents=None,  # custom, $399/mo floor
        has_webhooks=True,
        has_sso=True,
        has_observability=True,
        has_priority_support=True,
    ),
    PlanTier.LEGACY_PRO: PlanLimits(
        tier=PlanTier.LEGACY_PRO,
        display_name="Pro (legacy $49)",
        max_memories=250_000,
        pre_notice_max_memories=500_000,
        max_storage_mb=5_000,
        max_smart_credits_per_month=12_000,  # $30 AI ceiling
        max_recalls_per_month=1_000_000,
        max_relay_events_per_month=100_000,
        max_content_chars=50_000,
        max_batch_items=100,
        max_batch_recall_queries=20,
        recall_burst_per_min=120,
        relay_burst_per_min=120,
        max_api_keys=10,
        max_users=5,
        max_projects=_UNLIMITED_PROJECTS,
        enrichment_concurrency=8,
        price_monthly_cents=4_900,
        has_webhooks=True,
        has_observability=True,
    ),
    PlanTier.LEGACY_TEAM: PlanLimits(
        tier=PlanTier.LEGACY_TEAM,
        display_name="Team (legacy $199)",
        max_memories=600_000,
        pre_notice_max_memories=2_000_000,
        max_storage_mb=20_000,
        max_smart_credits_per_month=60_000,  # $150 AI ceiling
        max_recalls_per_month=5_000_000,
        max_relay_events_per_month=500_000,
        max_content_chars=50_000,
        max_batch_items=100,
        max_batch_recall_queries=20,
        recall_burst_per_min=240,
        relay_burst_per_min=240,
        max_api_keys=50,
        max_users=25,
        max_projects=_UNLIMITED_PROJECTS,
        enrichment_concurrency=8,
        price_monthly_cents=19_900,
        has_webhooks=True,
        has_observability=True,
        has_priority_support=True,
    ),
}


def get_plan(tier: PlanTier | str) -> PlanLimits:
    """Get the catalog limits for a tier (per-seat values for Team)."""
    if isinstance(tier, str) and not isinstance(tier, PlanTier):
        tier = PlanTier(tier.lower())
    return PLANS[PlanTier(tier)]


def chunk_credits(content: str) -> int:
    """Minimum credits for one item: one per started 8,000-character chunk."""
    return max(1, math.ceil(len(content or "") / CREDIT_CHUNK_CHARS))


def estimate_chunks(contents: list[str]) -> int:
    """Chunk count for a write: the sum over items of ceil(len / 8000)."""
    return sum(chunk_credits(c) for c in contents)


def credits_for_usd(usd: float) -> int:
    """Credits that ``usd`` of AI spend consumes (rounded up to whole credits)."""
    if usd <= 0:
        return 0
    # Guard float noise: 0.0075 / 0.0025 must be 3, not 4.
    return math.ceil(round(usd / CREDIT_USD, 6))


def charge_for(min_credits: int, actual_usd: float) -> int:
    """Credits charged for a settled enrichment: max(chunk minimum, actual spend)."""
    return max(int(min_credits), credits_for_usd(actual_usd))


@dataclass
class UsageSnapshot:
    """Current usage counters for a tenant."""

    user_id: str
    plan: PlanTier
    memories_stored: int = 0
    recalls_this_month: int = 0
    stores_this_month: int = 0
    api_keys_active: int = 0
    storage_mb: float = 0.0
    # Effective limits (seat-scaled, notice-aware); defaults to the catalog.
    limits: PlanLimits | None = None
    max_memories: int | None = None

    def _limits(self) -> PlanLimits:
        return self.limits or get_plan(self.plan)

    def memory_cap(self) -> int:
        return self.max_memories if self.max_memories is not None else self._limits().max_memories

    def check_limit(self, action: str) -> LimitCheckResult:
        """Check whether ``action`` ("store", "recall", "create_key") is within the plan.

        Stores are only ever rejected at the memory cap; running out of smart
        credits degrades enrichment instead (see ``remembra.cloud.limits``).
        """
        limits = self._limits()

        if action == "store":
            cap = self.memory_cap()
            if self.memories_stored >= cap:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Memory limit reached ({cap:,} memories)",
                    limit=cap,
                    current=self.memories_stored,
                    upgrade_hint=("Upgrade to Solo for 50,000 memories ($12/mo)." if self.plan == PlanTier.FREE else None),
                )
            return LimitCheckResult(allowed=True)

        if action == "recall":
            if self.recalls_this_month >= limits.max_recalls_per_month:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Monthly recall limit reached ({limits.max_recalls_per_month:,}/mo)",
                    limit=limits.max_recalls_per_month,
                    current=self.recalls_this_month,
                )
            return LimitCheckResult(allowed=True)

        if action == "create_key":
            if self.api_keys_active >= limits.max_api_keys:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"API key limit reached ({limits.max_api_keys} keys)",
                    limit=limits.max_api_keys,
                    current=self.api_keys_active,
                )
            return LimitCheckResult(allowed=True)

        return LimitCheckResult(allowed=True)


@dataclass
class LimitCheckResult:
    """Result of a plan limit check."""

    allowed: bool
    reason: str | None = None
    limit: int | None = None
    current: int | None = None
    upgrade_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"allowed": self.allowed}
        if self.reason:
            d["reason"] = self.reason
        if self.limit is not None:
            d["limit"] = self.limit
            d["current"] = self.current
        if self.upgrade_hint:
            d["upgrade_hint"] = self.upgrade_hint
        return d
