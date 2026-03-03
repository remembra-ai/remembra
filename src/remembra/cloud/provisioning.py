"""
Tenant provisioning for Remembra Cloud.

Handles the signup → API key → ready flow:
  1. Register tenant in cloud_tenants table
  2. Create Stripe customer (if billing enabled)
  3. Generate initial API key
  4. Send welcome email
  5. Return credentials to the user

Also handles post-checkout activation (Stripe webhook → upgrade plan).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from remembra.auth.keys import APIKeyManager
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier

logger = logging.getLogger(__name__)

# Import email service if available
try:
    from remembra.cloud.email import EmailService  # noqa: F401
    EMAIL_AVAILABLE = True
except ImportError:
    EMAIL_AVAILABLE = False
    logger.warning("Email service not available - emails will not be sent")


@dataclass
class ProvisionResult:
    """Result of provisioning a new tenant."""

    user_id: str
    api_key: str
    api_key_id: str
    plan: PlanTier
    stripe_customer_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "api_key": self.api_key,
            "api_key_id": self.api_key_id,
            "plan": self.plan.value,
            "stripe_customer_id": self.stripe_customer_id,
            "message": "Store the API key securely — it cannot be retrieved again.",
        }


class TenantProvisioner:
    """Handles new tenant signup and provisioning.

    Args:
        meter: UsageMeter for tenant registration and plan tracking.
        key_manager: APIKeyManager for generating API keys.
        email_service: Optional EmailService for sending transactional emails.
    """

    def __init__(
        self,
        meter: UsageMeter,
        key_manager: APIKeyManager,
        email_service: Any | None = None,
    ) -> None:
        self._meter = meter
        self._key_manager = key_manager
        self._email_service = email_service

    async def provision(
        self,
        user_id: str | None = None,
        email: str | None = None,
        name: str | None = None,
        plan: PlanTier = PlanTier.FREE,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
    ) -> ProvisionResult:
        """Provision a new tenant.

        Steps:
        1. Generate a unique user_id if not provided
        2. Register in cloud_tenants table
        3. Create initial API key
        4. Return credentials

        Args:
            user_id: Optional user identifier. Auto-generated if not provided.
            email: User's email (stored in Stripe, not locally).
            name: User's display name.
            plan: Initial plan tier (default: FREE).
            stripe_customer_id: Stripe customer ID if already created.
            stripe_subscription_id: Stripe subscription ID if already created.

        Returns:
            ProvisionResult with API key and tenant details.
        """
        # Generate user_id if not provided
        if not user_id:
            user_id = f"user_{secrets.token_hex(12)}"

        # Check if tenant already exists
        existing = await self._meter.get_tenant(user_id)
        if existing:
            raise ValueError(f"Tenant {user_id} already exists")

        # Register tenant
        await self._meter.register_tenant(
            user_id=user_id,
            plan=plan,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        )

        # Create initial API key
        api_key_result = await self._key_manager.create_key(
            user_id=user_id,
            name=f"Default key for {name or user_id}",
            rate_limit_tier="standard",
        )

        logger.info(
            "Provisioned tenant: user_id=%s plan=%s",
            user_id,
            plan.value,
        )

        # Send welcome email if email service is available and email provided
        if self._email_service and email:
            try:
                await self._email_service.send_welcome_email(
                    to=email,
                    api_key=api_key_result.key,
                    user_id=user_id,
                    plan=plan.value,
                )
                logger.info("Welcome email sent to %s", email)
            except Exception as e:
                # Don't fail provisioning if email fails
                logger.error("Failed to send welcome email to %s: %s", email, str(e))

        return ProvisionResult(
            user_id=user_id,
            api_key=api_key_result.key,
            api_key_id=api_key_result.id,
            plan=plan,
            stripe_customer_id=stripe_customer_id,
        )

    async def activate_subscription(
        self,
        user_id: str,
        plan: PlanTier,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
    ) -> None:
        """Activate a paid subscription for an existing tenant.

        Called after Stripe checkout.session.completed webhook.
        """
        tenant = await self._meter.get_tenant(user_id)

        if tenant is None:
            # Auto-register if tenant doesn't exist yet
            await self._meter.register_tenant(
                user_id=user_id,
                plan=plan,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
            )
        else:
            await self._meter.update_plan(
                user_id=user_id,
                plan=plan,
                stripe_subscription_id=stripe_subscription_id,
            )

        logger.info(
            "Subscription activated: user_id=%s plan=%s",
            user_id,
            plan.value,
        )

    async def deactivate_subscription(self, user_id: str) -> None:
        """Downgrade a tenant to free plan.

        Called after Stripe subscription cancellation.
        """
        await self._meter.update_plan(
            user_id=user_id,
            plan=PlanTier.FREE,
        )
        logger.info("Subscription deactivated: user_id=%s", user_id)
