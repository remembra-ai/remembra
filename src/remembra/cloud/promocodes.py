"""
Promotional code system for Remembra Cloud.

Supports:
  - Fixed discount codes (e.g., LAUNCH100 = first 100 get Pro free for 30 days)
  - Partner codes (e.g., LANGCHAIN = 20% off forever)
  - Time-limited campaigns (e.g., MARCH2026 expires end of month)

Usage:
    promo_manager = PromoCodeManager(stripe_secret_key)
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

import stripe

from remembra.cloud.plans import PlanTier

logger = logging.getLogger(__name__)


class PromoType(StrEnum):
    """Types of promotional offers."""
    TRIAL = "trial"           # Free trial of paid plan
    DISCOUNT = "discount"     # Percentage off
    EXTENDED = "extended"     # Extra features/limits


@dataclass
class PromoCode:
    """Definition of a promotional code."""
    
    code: str                           # The code users enter (uppercase)
    promo_type: PromoType               # Type of promotion
    plan_tier: PlanTier                 # Plan tier to grant
    duration_days: int                  # How long the promo lasts (0 = permanent)
    max_redemptions: int | None         # Max total uses (None = unlimited)
    expires_at: datetime | None         # When code expires (None = never)
    discount_percent: int = 0           # For DISCOUNT type (0-100)
    description: str = ""               # Human-readable description
    
    # Tracking (updated on redemption)
    redemption_count: int = 0
    redeemed_by: list[str] = field(default_factory=list)  # List of user_ids
    
    def is_valid(self) -> tuple[bool, str | None]:
        """Check if the promo code can still be redeemed.
        
        Returns:
            (is_valid, error_message)
        """
        # Check expiration
        if self.expires_at and datetime.utcnow() > self.expires_at:
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
    
    # Early adopter code - unlimited, 14 day trial
    "EARLYADOPTER": PromoCode(
        code="EARLYADOPTER",
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=14,
        max_redemptions=None,  # Unlimited
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


class PromoCodeManager:
    """Manages promotional code redemption and Stripe integration."""
    
    def __init__(self, stripe_secret_key: str | None = None) -> None:
        if stripe_secret_key:
            stripe.api_key = stripe_secret_key
        self._codes = PROMO_CODES.copy()
    
    def get_code(self, code: str) -> PromoCode | None:
        """Get a promo code by its code string (case-insensitive)."""
        return self._codes.get(code.upper())
    
    def list_active_codes(self) -> list[dict[str, Any]]:
        """List all active promo codes with stats."""
        active = []
        for code in self._codes.values():
            valid, _ = code.is_valid()
            if valid:
                active.append({
                    "code": code.code,
                    "type": code.promo_type.value,
                    "plan": code.plan_tier.value,
                    "duration_days": code.duration_days,
                    "discount_percent": code.discount_percent,
                    "redemptions": code.redemption_count,
                    "max_redemptions": code.max_redemptions,
                    "remaining": (
                        code.max_redemptions - code.redemption_count
                        if code.max_redemptions else "unlimited"
                    ),
                    "expires_at": code.expires_at.isoformat() if code.expires_at else None,
                    "description": code.description,
                })
        return active
    
    async def validate(self, code: str, user_id: str) -> RedemptionResult:
        """Validate a promo code without redeeming it.
        
        Use this to show the user what they'll get before applying.
        """
        promo = self.get_code(code)
        if not promo:
            return RedemptionResult(
                success=False,
                error="Invalid promo code",
            )
        
        can_redeem, error = promo.can_user_redeem(user_id)
        if not can_redeem:
            return RedemptionResult(
                success=False,
                error=error,
            )
        
        # Calculate expiration
        expires_at = None
        if promo.duration_days > 0:
            expires_at = datetime.utcnow() + timedelta(days=promo.duration_days)
        
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
        """Redeem a promo code for a user.
        
        For TRIAL codes: Grants plan access for duration_days.
        For DISCOUNT codes: Creates/applies Stripe coupon.
        
        Args:
            code: The promo code to redeem
            user_id: The user's ID
            email: User's email (for logging)
            stripe_customer_id: Stripe customer ID (for discount codes)
        
        Returns:
            RedemptionResult with success status and details.
        """
        # Validate first
        result = await self.validate(code, user_id)
        if not result.success:
            return result
        
        promo = self.get_code(code)
        if not promo:
            return RedemptionResult(success=False, error="Invalid promo code")
        
        # Handle different promo types
        if promo.promo_type == PromoType.TRIAL:
            # Grant plan access directly (no Stripe needed)
            expires_at = datetime.utcnow() + timedelta(days=promo.duration_days)
            
            # Record redemption
            promo.redemption_count += 1
            promo.redeemed_by.append(user_id)
            
            logger.info(
                "Promo redeemed: code=%s user=%s email=%s plan=%s days=%d",
                code,
                user_id,
                email,
                promo.plan_tier.value,
                promo.duration_days,
            )
            
            return RedemptionResult(
                success=True,
                plan_tier=promo.plan_tier,
                duration_days=promo.duration_days,
                expires_at=expires_at,
                message=f"🎉 Success! You now have {promo.plan_tier.value.title()} access for {promo.duration_days} days!",
            )
        
        elif promo.promo_type == PromoType.DISCOUNT:
            # Create Stripe coupon and apply to customer
            if not stripe_customer_id:
                return RedemptionResult(
                    success=False,
                    error="Discount codes require an active subscription. Please subscribe first.",
                )
            
            try:
                # Create coupon if it doesn't exist
                coupon_id = f"PROMO_{promo.code}"
                try:
                    stripe.Coupon.retrieve(coupon_id)
                except stripe.error.InvalidRequestError:
                    stripe.Coupon.create(
                        id=coupon_id,
                        percent_off=promo.discount_percent,
                        duration="forever",
                        name=promo.description,
                    )
                
                # Apply to customer's subscription
                subscriptions = stripe.Subscription.list(customer=stripe_customer_id, limit=1)
                if subscriptions.data:
                    stripe.Subscription.modify(
                        subscriptions.data[0].id,
                        coupon=coupon_id,
                    )
                
                # Record redemption
                promo.redemption_count += 1
                promo.redeemed_by.append(user_id)
                
                logger.info(
                    "Discount applied: code=%s user=%s discount=%d%%",
                    code,
                    user_id,
                    promo.discount_percent,
                )
                
                return RedemptionResult(
                    success=True,
                    plan_tier=promo.plan_tier,
                    duration_days=0,  # Permanent
                    stripe_coupon_id=coupon_id,
                    message=f"🎉 Success! {promo.discount_percent}% discount applied to your subscription!",
                )
                
            except stripe.error.StripeError as e:
                logger.error("Stripe error applying promo: %s", e)
                return RedemptionResult(
                    success=False,
                    error=f"Failed to apply discount: {str(e)}",
                )
        
        return RedemptionResult(success=False, error="Unknown promo type")
    
    def add_code(self, promo: PromoCode) -> None:
        """Add a new promo code (for dynamic creation)."""
        self._codes[promo.code.upper()] = promo
        logger.info("Added promo code: %s", promo.code)
    
    def get_stats(self, code: str) -> dict[str, Any] | None:
        """Get redemption stats for a promo code."""
        promo = self.get_code(code)
        if not promo:
            return None
        
        return {
            "code": promo.code,
            "redemption_count": promo.redemption_count,
            "max_redemptions": promo.max_redemptions,
            "remaining": (
                promo.max_redemptions - promo.redemption_count
                if promo.max_redemptions else "unlimited"
            ),
            "redeemed_by_count": len(promo.redeemed_by),
            "expires_at": promo.expires_at.isoformat() if promo.expires_at else None,
            "is_valid": promo.is_valid()[0],
        }
