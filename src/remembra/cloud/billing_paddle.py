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
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from remembra.cloud.paddle_config import CheckoutUnavailableError, PriceMapping, get_paddle_config
from remembra.cloud.plans import BillingInterval, PlanTier, get_plan

# Paddle recommends rejecting webhooks whose signed timestamp is >5 min off.
WEBHOOK_TOLERANCE_SECONDS = 300

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
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Paddle API."""
        url = f"{self._api_base}{endpoint}"
        async with httpx.AsyncClient() as client:
            if method == "GET":
                response = await client.get(url, headers=self._headers())
            elif method == "POST":
                response = await client.post(url, headers=self._headers(), json=data)
            elif method == "PATCH":
                response = await client.patch(url, headers=self._headers(), json=data)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result

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
        customer_id: str = result["data"]["id"]
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

    def _price_for(
        self,
        plan: PlanTier,
        interval: BillingInterval,
        founding: bool,
    ) -> str:
        """Configured Paddle price for this environment (CheckoutUnavailableError if missing)."""
        config = get_paddle_config("sandbox" if self._sandbox else "production")
        return config.require_price(plan, interval, founding=founding)

    async def create_checkout_transaction(
        self,
        paddle_customer_id: str | None,
        plan: PlanTier,
        user_id: str,
        customer_email: str | None = None,
        *,
        interval: BillingInterval = BillingInterval.MONTH,
        quantity: int = 1,
        founding: bool = False,
    ) -> dict[str, Any]:
        """Create a Paddle transaction for checkout.

        Returns transaction_id and checkout URL (for hosted checkout).
        For overlay checkout, use transaction_id with Paddle.js.

        Raises:
            CheckoutUnavailableError: no Paddle price is configured for the plan/interval.
        """
        if plan.value not in ("solo", "pro", "team"):
            raise CheckoutUnavailableError(f"The {plan.value} plan is not sold through self-serve checkout.")
        price_id = self._price_for(plan, interval, founding)
        limits = get_plan(plan)
        quantity = max(limits.min_seats, quantity) if limits.per_seat else 1

        payload: dict[str, Any] = {
            "items": [{"price_id": price_id, "quantity": quantity}],
            "custom_data": {
                "remembra_user_id": user_id,
                "plan": plan.value,
                "interval": interval.value,
                "founding": founding,
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
            "Created Paddle transaction %s for user %s (plan: %s, interval: %s, qty: %s, founding: %s)",
            data["id"],
            user_id,
            plan.value,
            interval.value,
            quantity,
            founding,
        )

        return {
            "transaction_id": data["id"],
            "checkout_url": data.get("checkout", {}).get("url"),
        }

    async def create_checkout_session(
        self,
        customer_id: str | None,
        plan: PlanTier,
        user_id: str,
        email: str | None = None,
        *,
        interval: BillingInterval = BillingInterval.MONTH,
        quantity: int = 1,
        founding: bool = False,
    ) -> dict[str, Any]:
        """Create a checkout session (alias for create_checkout_transaction).

        Returns dict with:
          - transaction_id: For Paddle.js overlay checkout
          - checkout_url: For redirect-based checkout (fallback)
        """
        return await self.create_checkout_transaction(
            paddle_customer_id=customer_id,
            plan=plan,
            user_id=user_id,
            customer_email=email,
            interval=interval,
            quantity=quantity,
            founding=founding,
        )

    # -----------------------------------------------------------------------
    # Customer lookup
    # -----------------------------------------------------------------------

    async def get_customer_by_email(self, email: str) -> str | None:
        """Look up a Paddle customer ID by email address.

        Returns the customer ID if found, None otherwise.
        """
        try:
            result = await self._request(
                "GET",
                f"/customers?email={email}",
            )
            customers = result.get("data", [])
            if customers and len(customers) > 0:
                customer_id: str = customers[0]["id"]
                return customer_id
            return None
        except Exception as e:
            logger.warning("Failed to look up customer by email %s: %s", email, e)
            return None

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
        overview_url: str = result["data"]["urls"]["general"]["overview"]
        return overview_url

    async def create_portal_session_by_email(self, email: str) -> str | None:
        """Create a customer portal session URL by looking up email.

        Returns portal URL if customer found, None otherwise.
        """
        customer_id = await self.get_customer_by_email(email)
        if not customer_id:
            return None
        return await self.create_portal_session(customer_id)

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
        *,
        interval: BillingInterval = BillingInterval.MONTH,
        quantity: int = 1,
    ) -> None:
        """Change subscription to a different plan.

        Args:
            subscription_id: The Paddle subscription ID.
            new_plan: Target plan tier.
            proration: "prorated_immediately", "prorated_next_billing_period",
                      "full_immediately", "full_next_billing_period",
                      "do_not_bill"
            interval: Billing interval of the target price.
            quantity: Seats (Team; at least the plan minimum).
        """
        price_id = self._price_for(new_plan, interval, founding=False)
        limits = get_plan(new_plan)
        quantity = max(limits.min_seats, quantity) if limits.per_seat else 1

        await self._request(
            "PATCH",
            f"/subscriptions/{subscription_id}",
            {
                "items": [{"price_id": price_id, "quantity": quantity}],
                "proration_billing_mode": proration,
                "custom_data": {"plan": new_plan.value, "interval": interval.value},
            },
        )
        logger.info(
            "Updated subscription %s to plan %s (%s, qty %s)",
            subscription_id,
            new_plan.value,
            interval.value,
            quantity,
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
        # Fail closed: with no secret every signature would be forgeable.
        if not self._webhook_secret:
            raise ValueError("Webhook secret is not configured")

        # Parse signature header: ts=xxx;h1=xxx (Paddle may send several h1 during rotation)
        ts = ""
        signatures: list[str] = []
        for part in (signature or "").split(";"):
            key, sep, value = part.strip().partition("=")
            if not sep:
                continue
            if key == "ts":
                ts = value
            elif key == "h1":
                signatures.append(value)
        if not ts or not signatures:
            raise ValueError("Invalid webhook signature")
        try:
            timestamp = int(ts)
        except ValueError:
            raise ValueError("Invalid webhook signature") from None
        # Replay protection: reject events signed outside the tolerance window.
        if abs(time.time() - timestamp) > WEBHOOK_TOLERANCE_SECONDS:
            raise ValueError("Webhook timestamp outside tolerance")

        signed_payload = f"{ts}:{payload.decode()}"
        expected = hmac.new(
            self._webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise ValueError("Invalid webhook signature")

        event: dict[str, Any] = json.loads(payload)
        return event

    def _resolve_purchase(self, data: dict[str, Any]) -> tuple[PriceMapping | None, int, bool]:
        """(what the items buy, quantity, whether it came from a known price ID).

        The price ID is authoritative: renewals of the grandfathered $49/$199
        prices keep mapping to the legacy tiers whatever custom_data says. Falls
        back to our own checkout custom_data (plan / interval / founding).
        """
        config = get_paddle_config("sandbox" if self._sandbox else "production")
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            price_id = (price.get("id") if isinstance(price, dict) else None) or item.get("price_id")
            mapping = config.resolve_price(price_id)
            if mapping is not None:
                quantity = item.get("quantity")
                return mapping, quantity if isinstance(quantity, int) and quantity > 0 else 1, True

        custom = data.get("custom_data") or {}
        plan_name = str(custom.get("plan") or "").strip().lower()
        try:
            interval = BillingInterval.parse(custom.get("interval"))
        except ValueError:
            interval = BillingInterval.MONTH
        founding = bool(custom.get("founding")) or plan_name == "founding"
        if founding:
            return PriceMapping(PlanTier.SOLO, BillingInterval.YEAR, founding=True), 1, False
        try:
            tier = PlanTier(plan_name)
        except ValueError:
            return None, 1, False
        return PriceMapping(tier, interval), 1, False

    @staticmethod
    def _period_anchor(data: dict[str, Any]) -> datetime | None:
        period = data.get("current_billing_period") or data.get("billing_period") or {}
        starts = period.get("starts_at") if isinstance(period, dict) else None
        if not starts:
            return None
        try:
            parsed = datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _net_revenue_usd(data: dict[str, Any]) -> float | None:
        """Net earnings (after Paddle fees and tax) of a completed USD transaction, in dollars."""
        if str(data.get("currency_code") or "").upper() != "USD":
            return None
        totals = (data.get("details") or {}).get("totals") or {}
        earnings = totals.get("earnings")
        try:
            return int(str(earnings)) / 100 if earnings is not None else None
        except ValueError:
            return None

    def _subscription_result(
        self, action: str, data: dict[str, Any], user_id: str | None, subscription_id: str | None, **extra: Any
    ) -> WebhookResult:
        mapping, quantity, from_price = self._resolve_purchase(data)
        if mapping is None:
            logger.warning("paddle_event_without_known_plan action=%s", action)
            return WebhookResult(action="ignored", event_type=action, user_id=user_id)
        limits = get_plan(mapping.tier)
        return WebhookResult(
            action=action,
            user_id=user_id,
            plan=mapping.tier,
            paddle_subscription_id=subscription_id,
            interval=mapping.interval,
            seats=max(limits.min_seats, quantity) if limits.per_seat else None,
            founding=mapping.founding,
            period_anchor=self._period_anchor(data),
            plan_from_price=from_price,
            **extra,
        )

    async def handle_webhook_event(
        self,
        event: dict[str, Any],
    ) -> WebhookResult:
        """Process a verified Paddle webhook event.

        Handles:
        - transaction.completed → activate subscription (+ record net revenue)
        - subscription.activated → subscription active
        - subscription.updated → plan / seat / interval change
        - subscription.canceled → back to Free
        - subscription.past_due → payment issue

        Returns:
            WebhookResult with action taken and user_id affected.
        """
        event_type = event.get("event_type", "")
        data = event.get("data", {}) or {}
        custom_data = data.get("custom_data") or {}
        user_id = custom_data.get("remembra_user_id")

        if event_type == "transaction.completed":
            customer = data.get("customer") or {}
            subscription_id = data.get("subscription_id")
            logger.info(
                "Transaction completed: user=%s plan=%s sub=%s",
                user_id,
                custom_data.get("plan"),
                subscription_id,
            )
            result = self._subscription_result(
                "activate_subscription",
                data,
                user_id,
                subscription_id,
                paddle_customer_id=data.get("customer_id"),
                customer_email=customer.get("email"),
                customer_name=customer.get("name"),
            )
            result.transaction_id = data.get("id")
            result.revenue_usd = self._net_revenue_usd(data)
            return result

        if event_type == "subscription.activated":
            return self._subscription_result(
                "activate_subscription", data, user_id, data.get("id"), paddle_customer_id=data.get("customer_id")
            )

        if event_type == "subscription.updated":
            status = data.get("status")
            if status == "active":
                return self._subscription_result("update_subscription", data, user_id, data.get("id"))
            if status == "past_due":
                return WebhookResult(action="payment_issue", user_id=user_id)

        if event_type == "subscription.canceled":
            logger.info("Subscription cancelled for user %s", user_id)
            return WebhookResult(
                action="cancel_subscription",
                user_id=user_id,
                plan=PlanTier.FREE,
            )

        if event_type == "subscription.past_due":
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
        interval: BillingInterval | None = None,
        seats: int | None = None,
        founding: bool = False,
        period_anchor: datetime | None = None,
        plan_from_price: bool = False,
        transaction_id: str | None = None,
        revenue_usd: float | None = None,
    ) -> None:
        self.action = action
        self.user_id = user_id
        self.plan = plan
        self.paddle_customer_id = paddle_customer_id
        self.paddle_subscription_id = paddle_subscription_id
        self.event_type = event_type
        self.customer_email = customer_email
        self.customer_name = customer_name
        self.interval = interval
        self.seats = seats
        self.founding = founding
        self.period_anchor = period_anchor
        self.plan_from_price = plan_from_price
        self.transaction_id = transaction_id
        self.revenue_usd = revenue_usd

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action}
        if self.user_id:
            d["user_id"] = self.user_id
        if self.plan:
            d["plan"] = self.plan.value
        if self.interval:
            d["interval"] = self.interval.value
        if self.seats:
            d["seats"] = self.seats
        if self.founding:
            d["founding"] = True
        if self.event_type:
            d["event_type"] = self.event_type
        if self.customer_email:
            d["customer_email"] = self.customer_email
        return d
