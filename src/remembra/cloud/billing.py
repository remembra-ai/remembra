"""
Stripe billing integration for Remembra Cloud.

Handles:
  - Creating Stripe customers and checkout sessions
  - Processing subscription webhooks
  - Managing plan changes (upgrade/downgrade)
  - Subscription portal sessions

Requires: pip install stripe
"""

from __future__ import annotations

import logging
from typing import Any

import stripe

from remembra.cloud.plans import PlanTier, get_plan

logger = logging.getLogger(__name__)


class BillingManager:
    """Manages Stripe billing for Remembra Cloud.

    Args:
        stripe_secret_key: Stripe secret key (sk_live_xxx or sk_test_xxx)
        stripe_webhook_secret: Webhook signing secret (whsec_xxx)
        success_url: URL to redirect after successful checkout
        cancel_url: URL to redirect on checkout cancellation
    """

    def __init__(
        self,
        stripe_secret_key: str,
        stripe_webhook_secret: str,
        success_url: str = "https://remembra.dev/dashboard?checkout=success",
        cancel_url: str = "https://remembra.dev/pricing?checkout=cancelled",
    ) -> None:
        stripe.api_key = stripe_secret_key
        self._webhook_secret = stripe_webhook_secret
        self._success_url = success_url
        self._cancel_url = cancel_url

    # -----------------------------------------------------------------------
    # Customer management
    # -----------------------------------------------------------------------

    async def create_customer(
        self,
        user_id: str,
        email: str,
        name: str | None = None,
    ) -> str:
        """Create a Stripe customer and return the customer ID.

        The user_id is stored as Stripe metadata for webhook correlation.
        """
        customer = stripe.Customer.create(
            email=email,
            name=name,
            metadata={
                "remembra_user_id": user_id,
            },
        )
        logger.info("Created Stripe customer %s for user %s", customer.id, user_id)
        return customer.id

    async def get_customer(self, stripe_customer_id: str) -> dict[str, Any]:
        """Get Stripe customer details."""
        customer = stripe.Customer.retrieve(stripe_customer_id)
        return {
            "id": customer.id,
            "email": customer.email,
            "name": customer.name,
            "metadata": dict(customer.metadata),
        }

    # -----------------------------------------------------------------------
    # Checkout
    # -----------------------------------------------------------------------

    async def create_checkout_session(
        self,
        stripe_customer_id: str,
        plan: PlanTier,
        user_id: str,
    ) -> str:
        """Create a Stripe Checkout session for plan subscription.

        Returns the checkout URL to redirect the user to.
        """
        plan_limits = get_plan(plan)
        if not plan_limits.stripe_price_id:
            raise ValueError(f"No Stripe price configured for {plan.value} plan")

        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            mode="subscription",
            line_items=[
                {
                    "price": plan_limits.stripe_price_id,
                    "quantity": 1,
                },
            ],
            success_url=self._success_url + "&session_id={CHECKOUT_SESSION_ID}",
            cancel_url=self._cancel_url,
            metadata={
                "remembra_user_id": user_id,
                "plan": plan.value,
            },
            subscription_data={
                "metadata": {
                    "remembra_user_id": user_id,
                    "plan": plan.value,
                },
            },
        )

        logger.info(
            "Created checkout session %s for user %s (plan: %s)",
            session.id,
            user_id,
            plan.value,
        )
        return session.url

    # -----------------------------------------------------------------------
    # Portal
    # -----------------------------------------------------------------------

    async def create_portal_session(
        self,
        stripe_customer_id: str,
        return_url: str = "https://remembra.dev/dashboard",
    ) -> str:
        """Create a Stripe Billing Portal session.

        Users can manage their subscription, update payment methods,
        and view invoices through the portal.

        Returns the portal URL.
        """
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        return session.url

    # -----------------------------------------------------------------------
    # Subscription management
    # -----------------------------------------------------------------------

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Get subscription details from Stripe."""
        sub = stripe.Subscription.retrieve(subscription_id)
        return {
            "id": sub.id,
            "status": sub.status,
            "plan": sub.metadata.get("plan", "unknown"),
            "current_period_start": sub.current_period_start,
            "current_period_end": sub.current_period_end,
            "cancel_at_period_end": sub.cancel_at_period_end,
        }

    async def cancel_subscription(
        self,
        subscription_id: str,
        at_period_end: bool = True,
    ) -> None:
        """Cancel a subscription.

        Args:
            subscription_id: The Stripe subscription ID.
            at_period_end: If True, cancel at end of billing period.
                          If False, cancel immediately (triggers prorated refund).
        """
        if at_period_end:
            stripe.Subscription.modify(
                subscription_id,
                cancel_at_period_end=True,
            )
            logger.info("Scheduled cancellation for subscription %s", subscription_id)
        else:
            stripe.Subscription.cancel(subscription_id)
            logger.info("Cancelled subscription %s immediately", subscription_id)

    # -----------------------------------------------------------------------
    # Webhooks
    # -----------------------------------------------------------------------

    def verify_webhook(
        self,
        payload: bytes,
        signature: str,
    ) -> dict[str, Any]:
        """Verify and parse a Stripe webhook event.

        Args:
            payload: Raw request body bytes.
            signature: Stripe-Signature header value.

        Returns:
            Parsed event dict.

        Raises:
            ValueError: If signature verification fails.
        """
        try:
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                self._webhook_secret,
            )
            return dict(event)
        except stripe.error.SignatureVerificationError:
            raise ValueError("Invalid webhook signature")

    async def handle_webhook_event(
        self,
        event: dict[str, Any],
    ) -> WebhookResult:
        """Process a verified Stripe webhook event.

        Handles:
        - checkout.session.completed → activate subscription
        - customer.subscription.updated → plan change
        - customer.subscription.deleted → cancel subscription
        - invoice.payment_failed → handle payment failure

        Returns:
            WebhookResult with action taken and user_id affected.
        """
        event_type = event.get("type", "")
        data = event.get("data", {}).get("object", {})

        if event_type == "checkout.session.completed":
            user_id = data.get("metadata", {}).get("remembra_user_id")
            plan = data.get("metadata", {}).get("plan", "pro")
            subscription_id = data.get("subscription")
            customer_id = data.get("customer")
            
            # For payment link flow: no user_id, but we have customer email
            customer_email = data.get("customer_email") or data.get("customer_details", {}).get("email")
            customer_name = data.get("customer_details", {}).get("name")

            logger.info(
                "Checkout completed: user=%s email=%s plan=%s sub=%s",
                user_id,
                customer_email,
                plan,
                subscription_id,
            )
            return WebhookResult(
                action="activate_subscription",
                user_id=user_id,
                plan=PlanTier(plan),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                customer_email=customer_email,
                customer_name=customer_name,
            )

        if event_type == "customer.subscription.updated":
            metadata = data.get("metadata", {})
            user_id = metadata.get("remembra_user_id")
            status = data.get("status")
            plan = metadata.get("plan", "pro")

            if status == "active":
                return WebhookResult(
                    action="update_subscription",
                    user_id=user_id,
                    plan=PlanTier(plan),
                    stripe_subscription_id=data.get("id"),
                )
            elif status in ("past_due", "unpaid"):
                return WebhookResult(
                    action="payment_issue",
                    user_id=user_id,
                    plan=PlanTier(plan),
                )

        if event_type == "customer.subscription.deleted":
            metadata = data.get("metadata", {})
            user_id = metadata.get("remembra_user_id")

            logger.info("Subscription deleted for user %s", user_id)
            return WebhookResult(
                action="cancel_subscription",
                user_id=user_id,
                plan=PlanTier.FREE,
            )

        if event_type == "invoice.payment_failed":
            customer_id = data.get("customer")
            # Look up customer to get user_id
            try:
                customer = stripe.Customer.retrieve(customer_id)
                user_id = customer.metadata.get("remembra_user_id")
            except Exception:
                user_id = None

            logger.warning("Payment failed for customer %s (user %s)", customer_id, user_id)
            return WebhookResult(
                action="payment_failed",
                user_id=user_id,
            )

        # Unhandled event type
        return WebhookResult(action="ignored", event_type=event_type)


class WebhookResult:
    """Result of processing a Stripe webhook event."""

    def __init__(
        self,
        action: str,
        user_id: str | None = None,
        plan: PlanTier | None = None,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
        event_type: str | None = None,
        customer_email: str | None = None,
        customer_name: str | None = None,
    ) -> None:
        self.action = action
        self.user_id = user_id
        self.plan = plan
        self.stripe_customer_id = stripe_customer_id
        self.stripe_subscription_id = stripe_subscription_id
        self.event_type = event_type
        self.customer_email = customer_email
        self.customer_name = customer_name

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action}
        if self.user_id:
            d["user_id"] = self.user_id
        if self.plan:
            d["plan"] = self.plan.value
        if self.event_type:
            d["event_type"] = self.event_type
        if self.customer_email:
            d["customer_email"] = self.customer_email
        return d
