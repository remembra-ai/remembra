"""
Plan definitions and limit enforcement for Remembra Cloud.

Plans:
  - free:       50K memories, 1 project, community support
  - pro:        $49/mo — 500K memories, 5 projects, email support
  - team:       $99/mo — 2M memories, unlimited projects, priority support
  - enterprise: Custom pricing — unlimited everything, SLA, SSO
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PlanTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    TEAM = "team"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True)
class PlanLimits:
    """Enforced resource limits for a plan tier."""

    # Storage
    max_memories: int
    max_storage_mb: int

    # Rate limits (per month)
    max_recalls_per_month: int
    max_stores_per_month: int

    # Access
    max_api_keys: int
    max_users: int
    max_projects: int

    # Features
    retention_days: int | None  # None = unlimited
    has_hybrid_search: bool = True
    has_entity_resolution: bool = True
    has_temporal_decay: bool = True
    has_reranking: bool = True
    has_graph_retrieval: bool = True
    has_webhooks: bool = False
    has_sso: bool = False
    has_observability: bool = False
    has_priority_support: bool = False

    # Stripe
    stripe_price_id: str | None = None


# ---------------------------------------------------------------------------
# Plan definitions (matching PRODUCT-SPEC.md research)
# ---------------------------------------------------------------------------

PLANS: dict[PlanTier, PlanLimits] = {
    # Free: Indie devs, students
    PlanTier.FREE: PlanLimits(
        max_memories=50_000,        # 50K memories (research spec)
        max_storage_mb=500,
        max_recalls_per_month=100_000,
        max_stores_per_month=50_000,
        max_api_keys=3,
        max_users=1,
        max_projects=1,             # 1 project (research spec)
        retention_days=None,        # unlimited
        has_webhooks=False,
        has_sso=False,
        has_observability=False,
        has_priority_support=False,
        stripe_price_id=None,
    ),
    # Pro $49/mo: Startups, side projects
    PlanTier.PRO: PlanLimits(
        max_memories=500_000,       # 500K memories (research spec)
        max_storage_mb=5_000,
        max_recalls_per_month=1_000_000,
        max_stores_per_month=500_000,
        max_api_keys=10,
        max_users=5,
        max_projects=5,             # 5 projects (research spec)
        retention_days=365,
        has_webhooks=True,
        has_sso=False,
        has_observability=True,
        has_priority_support=False,
        stripe_price_id="price_1T6ZDAQ3CqXwAZA7jUWCVVF0",  # $49/mo (correct)
    ),
    # Team $99/mo: Growing companies
    PlanTier.TEAM: PlanLimits(
        max_memories=2_000_000,     # 2M memories (research spec)
        max_storage_mb=20_000,
        max_recalls_per_month=5_000_000,
        max_stores_per_month=2_000_000,
        max_api_keys=50,
        max_users=25,
        max_projects=100,           # unlimited-ish
        retention_days=None,        # unlimited
        has_webhooks=True,
        has_sso=False,
        has_observability=True,
        has_priority_support=True,
        stripe_price_id="price_1T7CdNQ3CqXwAZA7scUmw5XO",  # $99/mo
    ),
    # Enterprise: Large orgs, custom pricing
    PlanTier.ENTERPRISE: PlanLimits(
        max_memories=10_000_000,
        max_storage_mb=100_000,
        max_recalls_per_month=50_000_000,
        max_stores_per_month=10_000_000,
        max_api_keys=100,
        max_users=1000,
        max_projects=1000,
        retention_days=None,        # unlimited
        has_webhooks=True,
        has_sso=True,
        has_observability=True,
        has_priority_support=True,
        stripe_price_id=None,       # Custom pricing, contact sales
    ),
}


def get_plan(tier: PlanTier | str) -> PlanLimits:
    """Get plan limits for a given tier."""
    if isinstance(tier, str):
        tier = PlanTier(tier.lower())
    return PLANS[tier]


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

    def check_limit(self, action: str) -> LimitCheckResult:
        """Check if a specific action is within plan limits.

        Args:
            action: "store", "recall", "create_key", "create_project"

        Returns:
            LimitCheckResult with allowed status and details.
        """
        limits = get_plan(self.plan)

        if action == "store":
            if self.memories_stored >= limits.max_memories:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Memory limit reached ({limits.max_memories:,} memories)",
                    limit=limits.max_memories,
                    current=self.memories_stored,
                    upgrade_hint="Upgrade to Pro for 500K memories" if self.plan == PlanTier.FREE else None,
                )
            if self.stores_this_month >= limits.max_stores_per_month:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Monthly store limit reached ({limits.max_stores_per_month:,}/mo)",
                    limit=limits.max_stores_per_month,
                    current=self.stores_this_month,
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
