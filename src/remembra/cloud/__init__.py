"""
Remembra Cloud — Billing, metering, and plan enforcement.

Provides:
  - Paddle billing integration (subscriptions, seats, annual plans)
  - Plan-based limits and smart-credit metering (degrade, never reject)
  - Usage tracking and enforcement
  - Tenant provisioning (signup → API key → ready)
  - Promotional codes (trials, discounts)
"""

from remembra.cloud.promocodes import PromoCode, PromoCodeManager, PromoType

__all__ = ["PromoCodeManager", "PromoCode", "PromoType"]
