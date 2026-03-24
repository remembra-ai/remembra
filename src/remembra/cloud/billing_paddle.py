"""
Paddle billing integration for Remembra Cloud.

Handles:
  - Creating Paddle checkout sessions (overlay or hosted)
  - Processing subscription webhooks
  - Managing plan changes (upgrade/downgrade)
  - Customer portal access

Paddle is the Merchant of Record - they handle taxes, compliance, refunds.

Requires: pip install paddle-python-sdk
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from remembra.cloud.plans_paddle import PlanTier, get_plan

logger = logging.getLogger(__name__)

PADDLE_API_BASE = "https://api.paddle.com"
PADDLE_SANDBOX_API_BASE = "https://sandbox-api.paddle.com"


class PaddleBillingManager:
    """Manages Paddle billing for Remembra Cloud.

    Args:
        api_key: Paddle API key
        webhook_secret: Paddle webhook signing secret
        sandbox: If True, use sandbox environment
        success_url: URL to redirect after successful checkout
        cancel_url: URL to redirect on checkout cancellation (not used in overlay)
    """

    def __init__(
        self,
        api_key: str,
        webhook_secret: str,
        sandbox: bool = False,
        success_url: str = "https://remembra.dev/dashboard?checkout=success",
        cancel_url: str = "https://remembra.dev/pricing?checkout=cancelled",
    ) -> None:
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._sandbox = sandbox
        self._success_url = success_url
        self._cancel_url = cancel_url
        self._api_base = PADDLE_SANDBOX_API_BASE if sandbox else PADDLE_API_BASE

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Paddle API."""
        url = f"{self._api_base}{endpoint}"
        async with httpx.AsyncClient() as client:
            if method == "GET":
                response = await client.get(url, headers=self._headers())
            elif method == "POST":
                response = await client.post(
                    url, headers=self._headers(), json=data
                )
            elif method == "PATCH":
                response = await client.patch(
                    url, headers=self._headers(), json=data
                )
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

    # -----------------------------------------------------------------------
    # Customer management
    # -----------------------------------------------------------------------

    async def create_customer(
        self,
        user_id: str,
        email: str,
        name: str | None = None,
    ) -> str:
        """Create a Paddle customer and return the customer ID.

        The user_id is stored as custom_data for webhook correlation.
        """
        payload: dict[str, Any] = {
            "email": email,
            "custom_data": {
                "remembra_user_id": user_id,
            },
        }
        if name:
            payload["name"] = name

        result = await self._request("POST", "/customers", payload)
        customer_id = result["data"]["id"]
        logger.info("Created Paddle customer %s for user %s", customer_id, user_id)
        return customer_id

    async def get_customer(self, paddle_customer_id: str) -> dict[str, Any]:
        """Get Paddle customer details."""
        result = await self._request("GET", f"/customers/{paddle_customer_id}")
        data = result["data"]
        return {
            "id": data["id"],
            "email": data.get("email"),
            "name": data.get("name"),
            "custom_data": data.get("custom_data", {}),
        }

    async def update_customer_email(
        self,
        paddle_customer_id: str,
        email: str,
        name: str | None = None,
    ) -> None:
        """Update Paddle customer email (and optionally name)."""
        payload: dict[str, Any] = {"email": email}
        if name:
            payload["name"] = name
        await self._request("PATCH", f"/customers/{paddle_customer_id}", payload)
        logger.info(
            "Updated Paddle customer email: customer=%s email=%s",
            paddle_customer_id,
            email,
        )

    # -----------------------------------------------------------------------
    # Checkout - Transactions
    # -----------------------------------------------------------------------

    async def create_checkout_transaction(
        self,
        paddle_customer_id: str | None,
        plan: PlanTier,
        user_id: str,
        customer_email: str | None = None,
    ) -> dict[str, Any]:
        """Create a Paddle transaction for checkout.

        Returns transaction_id and checkout URL (for hosted checkout).
        For overlay checkout, use transaction_id with Paddle.js.
        """
        plan_limits = get_plan(plan)
        if not plan_limits.paddle_price_id:
            raise ValueError(f"No Paddle price configured for {plan.value} plan")

        payload: dict[str, Any] = {
            "items": [
                {
                    "price_id": plan_limits.paddle_price_id,
                    "quantity": 1,
                }
            ],
            "custom_data": {
                "remembra_user_id": user_id,
                "plan": plan.value,
            },
            "checkout": {
                "url": self._success_url,
            },
        }

        if paddle_customer_id:
            payload["customer_id"] = paddle_customer_id
        elif customer_email:
            payload["customer"] = {"email": customer_email}

        result = await self._request("POST", "/transactions", payload)
        data = result["data"]

        logger.info(
            "Created Paddle transaction %s for user %s (plan: %s)",
            data["id"],
            user_id,
            plan.value,
        )

        return {
            "transaction_id": data["id"],
            "checkout_url": data.get("checkout", {}).get("url"),
        }

    # -----------------------------------------------------------------------
    # Portal - Customer portal URL
    # -----------------------------------------------------------------------

    async def create_portal_session(
        self,
        paddle_customer_id: str,
    ) -> str:
        """Create a customer portal session URL.

        Users can manage subscriptions, update payment methods, view invoices.
        """
        result = await self._request(
            "POST",
            f"/customers/{paddle_customer_id}/portal-sessions",
            {},
        )
        return result["data"]["urls"]["general"]["overview"]

    # -----------------------------------------------------------------------
    # Subscription management
    # -----------------------------------------------------------------------

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Get subscription details from Paddle."""
        result = await self._request("GET", f"/subscriptions/{subscription_id}")
        data = result["data"]
        return {
            "id": data["id"],
            "status": data["status"],
            "plan": data.get("custom_data", {}).get("plan", "unknown"),
            "current_billing_period": data.get("current_billing_period"),
            "scheduled_change": data.get("scheduled_change"),
        }

    async def cancel_subscription(
        self,
        subscription_id: str,
        effective_from: str = "next_billing_period",
    ) -> None:
        """Cancel a subscription.

        Args:
            subscription_id: The Paddle subscription ID.
            effective_from: "immediately" or "next_billing_period"
        """
        await self._request(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            {"effective_from": effective_from},
        )
        logger.info(
            "Cancelled subscription %s (effective: %s)",
            subscription_id,
            effective_from,
        )

    async def update_subscription_plan(
        self,
        subscription_id: str,
        new_plan: PlanTier,
        proration: str = "prorated_immediately",
    ) -> None:
        """Change subscription to a different plan.

        Args:
            subscription_id: The Paddle subscription ID.
            new_plan: Target plan tier.
            proration: "prorated_immediately", "prorated_next_billing_period",
                      "full_immediately", "full_next_billing_period",
                      "do_not_bill"
        """
        plan_limits = get_plan(new_plan)
        if not plan_limits.paddle_price_id:
            raise ValueError(f"No Paddle price for {new_plan.value}")

        await self._request(
            "PATCH",
            f"/subscriptions/{subscription_id}",
            {
                "items": [
                    {
                        "price_id": plan_limits.paddle_price_id,
                        "quantity": 1,
                    }
                ],
                "proration_billing_mode": proration,
                "custom_data": {
                    "plan": new_plan.value,
                },
            },
        )
        logger.info(
            "Updated subscription %s to plan %s",
            subscription_id,
            new_plan.value,
        )

    # -----------------------------------------------------------------------
    # Webhooks
    # -----------------------------------------------------------------------

    def verify_webhook(
        self,
        payload: bytes,
        signature: str,
    ) -> dict[str, Any]:
        """Verify and parse a Paddle webhook event.

        Paddle uses HMAC-SHA256 for webhook signatures.

        Args:
            payload: Raw request body bytes.
            signature: Paddle-Signature header value.

        Returns:
            Parsed event dict.

        Raises:
            ValueError: If signature verification fails.
        """
        # Parse signature header: ts=xxx;h1=xxx
        parts = dict(p.split("=", 1) for p in signature.split(";"))
        ts = parts.get("ts", "")
        h1 = parts.get("h1", "")

        # Compute expected signature
        signed_payload = f"{ts}:{payload.decode()}"
        expected = hmac.new(
            self._webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, h1):
            raise ValueError("Invalid webhook signature")

        return json.loads(payload)

    async def handle_webhook_event(
        self,
        event: dict[str, Any],
    ) -> WebhookResult:
        """Process a verified Paddle webhook event.

        Handles:
        - transaction.completed → activate subscription (for one-time or first payment)
        - subscription.activated → subscription active
        - subscription.updated → plan change
        - subscription.canceled → cancel subscription
        - subscription.past_due → payment issue

        Returns:
            WebhookResult with action taken and user_id affected.
        """
        event_type = event.get("event_type", "")
        data = event.get("data", {})

        if event_type == "transaction.completed":
            custom_data = data.get("custom_data", {})
            user_id = custom_data.get("remembra_user_id")
            plan = custom_data.get("plan", "pro")
            subscription_id = data.get("subscription_id")
            customer_id = data.get("customer_id")

            # Get customer email from details
            customer = data.get("customer", {})
            customer_email = customer.get("email")
            customer_name = customer.get("name")

            logger.info(
                "Transaction completed: user=%s email=%s plan=%s sub=%s",
                user_id,
                customer_email,
                plan,
                subscription_id,
            )
            return WebhookResult(
                action="activate_subscription",
                user_id=user_id,
                plan=PlanTier(plan) if plan in [p.value for p in PlanTier] else PlanTier.PRO,
                paddle_customer_id=customer_id,
                paddle_subscription_id=subscription_id,
                customer_email=customer_email,
                customer_name=customer_name,
            )

        if event_type == "subscription.activated":
            custom_data = data.get("custom_data", {})
            user_id = custom_data.get("remembra_user_id")
            plan = custom_data.get("plan", "pro")
            subscription_id = data.get("id")

            return WebhookResult(
                action="activate_subscription",
                user_id=user_id,
                plan=PlanTier(plan) if plan in [p.value for p in PlanTier] else PlanTier.PRO,
                paddle_subscription_id=subscription_id,
            )

        if event_type == "subscription.updated":
            custom_data = data.get("custom_data", {})
            user_id = custom_data.get("remembra_user_id")
            status = data.get("status")
            plan = custom_data.get("plan", "pro")

            if status == "active":
                return WebhookResult(
                    action="update_subscription",
                    user_id=user_id,
                    plan=PlanTier(plan) if plan in [p.value for p in PlanTier] else PlanTier.PRO,
                    paddle_subscription_id=data.get("id"),
                )
            elif status == "past_due":
                return WebhookResult(
                    action="payment_issue",
                    user_id=user_id,
                    plan=PlanTier(plan) if plan in [p.value for p in PlanTier] else PlanTier.PRO,
                )

        if event_type == "subscription.canceled":
            custom_data = data.get("custom_data", {})
            user_id = custom_data.get("remembra_user_id")

            logger.info("Subscription cancelled for user %s", user_id)
            return WebhookResult(
                action="cancel_subscription",
                user_id=user_id,
                plan=PlanTier.FREE,
            )

        if event_type == "subscription.past_due":
            custom_data = data.get("custom_data", {})
            user_id = custom_data.get("remembra_user_id")

            logger.warning("Subscription past due for user %s", user_id)
            return WebhookResult(
                action="payment_failed",
                user_id=user_id,
            )

        # Unhandled event type
        return WebhookResult(action="ignored", event_type=event_type)


class WebhookResult:
    """Result of processing a Paddle webhook event."""

    def __init__(
        self,
        action: str,
        user_id: str | None = None,
        plan: PlanTier | None = None,
        paddle_customer_id: str | None = None,
        paddle_subscription_id: str | None = None,
        event_type: str | None = None,
        customer_email: str | None = None,
        customer_name: str | None = None,
    ) -> None:
        self.action = action
        self.user_id = user_id
        self.plan = plan
        self.paddle_customer_id = paddle_customer_id
        self.paddle_subscription_id = paddle_subscription_id
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
