"""
Promotional code system for Remembra Cloud.

Supports:
  - Fixed discount codes (e.g., LAUNCH100 = first 100 get Pro free for 30 days)
  - Partner codes (e.g., LANGCHAIN = 20% off forever)
  - Time-limited campaigns (e.g., MARCH2026 expires end of month)

Usage:
    promo_manager = PromoCodeManager()
    result = await promo_manager.redeem("LAUNCH100", user_id, email)
    if result.success:
        # User now has Pro access for result.duration_days
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from remembra.cloud.plans import PlanTier
from remembra.core.time import utcnow

logger = logging.getLogger(__name__)


class PromoType(StrEnum):
    """Types of promotional offers."""

    TRIAL = "trial"  # Free trial of paid plan
    DISCOUNT = "discount"  # Percentage off
    EXTENDED = "extended"  # Extra features/limits


@dataclass
class PromoCode:
    """Definition of a promotional code."""

    code: str  # The code users enter (uppercase)
    promo_type: PromoType  # Type of promotion
    plan_tier: PlanTier  # Plan tier to grant
    duration_days: int  # How long the promo lasts (0 = permanent)
    max_redemptions: int | None  # Max total uses (None = unlimited)
    expires_at: datetime | None  # When code expires (None = never)
    discount_percent: int = 0  # For DISCOUNT type (0-100)
    description: str = ""  # Human-readable description

    # Tracking (updated on redemption)
    redemption_count: int = 0
    redeemed_by: list[str] = field(default_factory=list)  # List of user_ids

    def is_valid(self) -> tuple[bool, str | None]:
        """Check if the promo code can still be redeemed.

        Returns:
            (is_valid, error_message)
        """
        # Check expiration
        if self.expires_at and utcnow() > self.expires_at:
            return False, "This promo code has expired"

        # Check redemption limit
        if self.max_redemptions and self.redemption_count >= self.max_redemptions:
            return False, f"This promo code has reached its limit ({self.max_redemptions} redemptions)"

        return True, None

    def can_user_redeem(self, user_id: str) -> tuple[bool, str | None]:
        """Check if a specific user can redeem this code.

        Returns:
            (can_redeem, error_message)
        """
        # First check if code is still valid
        valid, error = self.is_valid()
        if not valid:
            return False, error

        # Check if user already redeemed
        if user_id in self.redeemed_by:
            return False, "You've already redeemed this promo code"

        return True, None


@dataclass
class RedemptionResult:
    """Result of attempting to redeem a promo code."""

    success: bool
    error: str | None = None
    plan_tier: PlanTier | None = None
    duration_days: int = 0
    expires_at: datetime | None = None
    stripe_coupon_id: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Active promo codes (could move to database later)
# ---------------------------------------------------------------------------

PROMO_CODES: dict[str, PromoCode] = {
    # Launch campaign - first 100 users get Pro free for 30 days
    "LAUNCH100": PromoCode(
        code="LAUNCH100",
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=30,
        max_redemptions=100,
        expires_at=datetime(2026, 4, 30, 23, 59, 59),  # End of April 2026
        description="Launch special: 30 days of Pro free (first 100 users)",
    ),
    # Early adopter code - capped (was unlimited), 14 day trial
    "EARLYADOPTER": PromoCode(
        code="EARLYADOPTER",
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=14,
        max_redemptions=1000,
        expires_at=datetime(2026, 6, 30, 23, 59, 59),  # End of June 2026
        description="Early adopter: 14 days of Pro free",
    ),
    # Hacker News special
    "HACKERNEWS": PromoCode(
        code="HACKERNEWS",
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=30,
        max_redemptions=500,
        expires_at=datetime(2026, 5, 31, 23, 59, 59),
        description="Hacker News special: 30 days of Pro free",
    ),
    # Product Hunt launch
    "PRODUCTHUNT": PromoCode(
        code="PRODUCTHUNT",
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=30,
        max_redemptions=200,
        expires_at=datetime(2026, 5, 31, 23, 59, 59),
        description="Product Hunt launch: 30 days of Pro free",
    ),
    # Partner codes
    "LANGCHAIN": PromoCode(
        code="LANGCHAIN",
        promo_type=PromoType.DISCOUNT,
        plan_tier=PlanTier.PRO,
        duration_days=0,  # Permanent discount
        max_redemptions=None,
        expires_at=None,
        discount_percent=20,
        description="LangChain community: 20% off Pro forever",
    ),
    "CLAUDEDEV": PromoCode(
        code="CLAUDEDEV",
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=30,
        max_redemptions=300,
        expires_at=datetime(2026, 6, 30, 23, 59, 59),
        description="Claude developers: 30 days of Pro free",
    ),
}


_REDEMPTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS promo_redemptions (
    code TEXT NOT NULL,
    user_id TEXT NOT NULL,
    redeemed_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (code, user_id)
);
CREATE INDEX IF NOT EXISTS idx_promo_redemptions_code ON promo_redemptions(code);
"""


class PromoCodeManager:
    """Manages promotional code redemption (plan-access grants).

    Redemptions are persisted in SQLite (``promo_redemptions``), so limits and
    the one-redemption-per-user-per-code rule survive restarts and hold across
    workers. The check-and-insert is a single atomic statement.
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._codes = PROMO_CODES.copy()
        self._schema_ready = False

    async def _ensure_schema(self) -> None:
        if not self._schema_ready:
            await self._db.conn.executescript(_REDEMPTIONS_SCHEMA)
            await self._db.conn.commit()
            self._schema_ready = True

    async def redemption_count(self, code: str) -> int:
        await self._ensure_schema()
        cursor = await self._db.conn.execute("SELECT COUNT(*) FROM promo_redemptions WHERE code = ?", (code.upper(),))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def has_redeemed(self, code: str, user_id: str) -> bool:
        await self._ensure_schema()
        cursor = await self._db.conn.execute(
            "SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?", (code.upper(), user_id)
        )
        return await cursor.fetchone() is not None

    def get_code(self, code: str) -> PromoCode | None:
        """Get a promo code by its code string (case-insensitive)."""
        return self._codes.get(code.upper())

    async def _check(self, promo: PromoCode, user_id: str) -> str | None:
        if promo.expires_at and utcnow() > promo.expires_at:
            return "This promo code has expired"
        if promo.max_redemptions is not None and await self.redemption_count(promo.code) >= promo.max_redemptions:
            return f"This promo code has reached its limit ({promo.max_redemptions} redemptions)"
        if await self.has_redeemed(promo.code, user_id):
            return "You've already redeemed this promo code"
        return None

    async def list_active_codes(self) -> list[dict[str, Any]]:
        """List all active promo codes with persisted redemption stats."""
        active = []
        for code in self._codes.values():
            if code.expires_at and utcnow() > code.expires_at:
                continue
            count = await self.redemption_count(code.code)
            if code.max_redemptions is not None and count >= code.max_redemptions:
                continue
            active.append(
                {
                    "code": code.code,
                    "type": code.promo_type.value,
                    "plan": code.plan_tier.value,
                    "duration_days": code.duration_days,
                    "discount_percent": code.discount_percent,
                    "redemptions": count,
                    "max_redemptions": code.max_redemptions,
                    "remaining": (code.max_redemptions - count if code.max_redemptions is not None else "unlimited"),
                    "expires_at": code.expires_at.isoformat() if code.expires_at else None,
                    "description": code.description,
                }
            )
        return active

    async def validate(self, code: str, user_id: str) -> RedemptionResult:
        """Validate a promo code without redeeming it.

        Use this to show the user what they'll get before applying.
        """
        promo = self.get_code(code)
        if not promo:
            return RedemptionResult(success=False, error="Invalid promo code")

        error = await self._check(promo, user_id)
        if error:
            return RedemptionResult(success=False, error=error)

        expires_at = utcnow() + timedelta(days=promo.duration_days) if promo.duration_days > 0 else None
        return RedemptionResult(
            success=True,
            plan_tier=promo.plan_tier,
            duration_days=promo.duration_days,
            expires_at=expires_at,
            message=promo.description,
        )

    async def redeem(
        self,
        code: str,
        user_id: str,
        email: str | None = None,
        stripe_customer_id: str | None = None,
    ) -> RedemptionResult:
        """Redeem a promo code for a user (at most once per user per code).

        For TRIAL codes: grants plan access for ``duration_days``.
        DISCOUNT codes are no longer supported.
        """
        promo = self.get_code(code)
        if not promo:
            return RedemptionResult(success=False, error="Invalid promo code")
        if promo.promo_type == PromoType.DISCOUNT:
            return RedemptionResult(
                success=False,
                error="Discount codes are no longer supported. Please use a plan-access code.",
            )
        if promo.promo_type != PromoType.TRIAL:
            return RedemptionResult(success=False, error="Unknown promo type")

        error = await self._check(promo, user_id)
        if error:
            return RedemptionResult(success=False, error=error)

        now = utcnow()
        expires_at = now + timedelta(days=promo.duration_days)
        # Atomic claim: succeeds only if this user has not redeemed the code and
        # the global cap still has room — no in-memory race, survives restarts.
        cursor = await self._db.conn.execute(
            """
            INSERT INTO promo_redemptions (code, user_id, redeemed_at, expires_at)
            SELECT ?, ?, ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?)
              AND (? IS NULL OR (SELECT COUNT(*) FROM promo_redemptions WHERE code = ?) < ?)
            """,
            (
                promo.code,
                user_id,
                now.isoformat(),
                expires_at.isoformat(),
                promo.code,
                user_id,
                promo.max_redemptions,
                promo.code,
                promo.max_redemptions,
            ),
        )
        await self._db.conn.commit()
        if cursor.rowcount != 1:
            error = await self._check(promo, user_id) or "This promo code can no longer be redeemed"
            return RedemptionResult(success=False, error=error)

        logger.info(
            "Promo redeemed: code=%s user=%s plan=%s days=%d",
            promo.code,
            user_id,
            promo.plan_tier.value,
            promo.duration_days,
        )
        return RedemptionResult(
            success=True,
            plan_tier=promo.plan_tier,
            duration_days=promo.duration_days,
            expires_at=expires_at,
            message=f"Success! You now have {promo.plan_tier.value.title()} access for {promo.duration_days} days.",
        )

    def add_code(self, promo: PromoCode) -> None:
        """Add a new promo code (for dynamic creation)."""
        self._codes[promo.code.upper()] = promo
        logger.info("Added promo code: %s", promo.code)

    async def get_stats(self, code: str) -> dict[str, Any] | None:
        """Get persisted redemption stats for a promo code."""
        promo = self.get_code(code)
        if not promo:
            return None
        count = await self.redemption_count(promo.code)
        return {
            "code": promo.code,
            "redemption_count": count,
            "max_redemptions": promo.max_redemptions,
            "remaining": (promo.max_redemptions - count if promo.max_redemptions is not None else "unlimited"),
            "redeemed_by_count": count,
            "expires_at": promo.expires_at.isoformat() if promo.expires_at else None,
            "is_valid": not (promo.expires_at and utcnow() > promo.expires_at)
            and (promo.max_redemptions is None or count < promo.max_redemptions),
        }
