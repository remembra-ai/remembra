"""
Comprehensive tests for Paddle billing integration.

Tests cover:
- Customer creation and management
- Checkout transaction creation
- Subscription management (upgrade/downgrade/cancel)
- Webhook signature verification
- Webhook event processing
- Plan limits and usage checks

Run with:
    PADDLE_ENVIRONMENT=sandbox pytest tests/test_billing_paddle.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid

import pytest

# Set sandbox environment before imports
os.environ["PADDLE_ENVIRONMENT"] = "sandbox"

import remembra.config as config_module
from remembra.cloud.billing_paddle import PaddleBillingManager, WebhookResult
from remembra.cloud.paddle_config import (
    PRODUCTION_CONFIG,
    SANDBOX_CONFIG,
    CheckoutUnavailableError,
    PaddleEnvironment,
    get_paddle_config,
    get_price_id_for_plan,
)
from remembra.cloud.plans import (
    BillingInterval,
    PlanTier,
    UsageSnapshot,
    get_plan,
)
from remembra.config import Settings


# =============================================================================
# Fixtures
# =============================================================================

PADDLE_INTEGRATION_SKIP_REASON = (
    "Paddle integration tests require REMEMBRA_RUN_PADDLE_TESTS=1, PADDLE_SANDBOX_API_KEY, and PADDLE_SANDBOX_CLIENT_TOKEN."
)

RUN_PADDLE_INTEGRATION = (
    os.environ.get("REMEMBRA_RUN_PADDLE_TESTS") == "1"
    and bool(os.environ.get("PADDLE_SANDBOX_API_KEY"))
    and bool(os.environ.get("PADDLE_SANDBOX_CLIENT_TOKEN"))
)


@pytest.fixture
def sandbox_api_key():
    """Sandbox API key for testing - requires PADDLE_SANDBOX_API_KEY env var."""
    return os.environ.get("PADDLE_SANDBOX_API_KEY", "test_placeholder_key")


@pytest.fixture
def sandbox_client_token():
    """Sandbox client token for testing - requires PADDLE_SANDBOX_CLIENT_TOKEN env var."""
    return os.environ.get("PADDLE_SANDBOX_CLIENT_TOKEN", "test_placeholder_token")


@pytest.fixture
def webhook_secret():
    """Test webhook secret."""
    return "whsec_test_secret_1234567890"


@pytest.fixture
def billing_manager(sandbox_api_key, webhook_secret):
    """Create a PaddleBillingManager for sandbox testing."""
    return PaddleBillingManager(
        api_key=sandbox_api_key,
        webhook_secret=webhook_secret,
        sandbox=True,
    )


@pytest.fixture
def test_user_id():
    """Generate a unique test user ID."""
    return f"test_user_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def test_email():
    """Generate a unique test email."""
    return f"test_{uuid.uuid4().hex[:8]}@remembra.dev"


# =============================================================================
# Configuration Tests
# =============================================================================


NEW_PRICES = {
    "paddle_price_solo_monthly": "pri_solo_m",
    "paddle_price_solo_annual": "pri_solo_y",
    "paddle_price_pro_monthly": "pri_pro_m",
    "paddle_price_pro_annual": "pri_pro_y",
    "paddle_price_team_seat_monthly": "pri_team_m",
    "paddle_price_team_seat_annual": "pri_team_y",
    "paddle_price_founding_annual": "pri_founding",
}


@pytest.fixture
def catalog_prices():
    """Settings carrying the 2026-09 price IDs (as the owner will set them via env)."""
    previous = config_module._settings
    config_module._settings = Settings(**NEW_PRICES)
    yield
    config_module._settings = previous


@pytest.fixture
def no_catalog_prices():
    previous = config_module._settings
    config_module._settings = Settings()
    yield
    config_module._settings = previous


class TestPaddleConfig:
    """Paddle configuration: legacy prices fixed, new catalog from settings."""

    def test_sandbox_config_has_correct_api_base(self):
        assert SANDBOX_CONFIG.api_base == "https://sandbox-api.paddle.com"
        assert SANDBOX_CONFIG.is_sandbox is True

    def test_production_config_has_correct_api_base(self):
        assert PRODUCTION_CONFIG.api_base == "https://api.paddle.com"
        assert PRODUCTION_CONFIG.is_sandbox is False

    def test_legacy_price_ids_stay_mapped_to_grandfathered_tiers(self):
        assert SANDBOX_CONFIG.legacy_pro_prices.monthly == "pri_01kmeq0ss2j2b74w9f1xwmvbc0"
        assert SANDBOX_CONFIG.legacy_team_prices.monthly == "pri_01kmeq5y8ch8zfy2kw9qnrz6s1"
        assert PRODUCTION_CONFIG.legacy_pro_prices.monthly == "pri_01kmepby4nfy150jbfjkpkev5h"
        assert PRODUCTION_CONFIG.legacy_team_prices.monthly == "pri_01kmepewmfpqdz413hc4f4fr3r"
        prod = get_paddle_config("production")
        assert prod.resolve_price("pri_01kmepby4nfy150jbfjkpkev5h").tier == PlanTier.LEGACY_PRO
        assert prod.resolve_price("pri_01kmepewmfpqdz413hc4f4fr3r").tier == PlanTier.LEGACY_TEAM

    def test_catalog_prices_come_from_settings(self, catalog_prices):
        config = get_paddle_config("production")
        assert config.price_for(PlanTier.SOLO, BillingInterval.MONTH) == "pri_solo_m"
        assert config.price_for(PlanTier.SOLO, BillingInterval.YEAR) == "pri_solo_y"
        assert config.price_for(PlanTier.TEAM, BillingInterval.YEAR) == "pri_team_y"
        assert config.price_for(PlanTier.SOLO, BillingInterval.YEAR, founding=True) == "pri_founding"
        assert config.price_for(PlanTier.SOLO, BillingInterval.MONTH, founding=True) is None  # annual only
        mapping = config.resolve_price("pri_founding")
        assert (mapping.tier, mapping.interval, mapping.founding) == (PlanTier.SOLO, BillingInterval.YEAR, True)
        assert config.resolve_price("pri_pro_y").interval == BillingInterval.YEAR
        assert get_price_id_for_plan("pro", "annual", PaddleEnvironment.SANDBOX) == "pri_pro_y"
        assert get_price_id_for_plan("founding", "yearly") == "pri_founding"

    def test_missing_price_means_checkout_unavailable(self, no_catalog_prices):
        config = get_paddle_config("production")
        assert config.price_for(PlanTier.SOLO, BillingInterval.MONTH) is None
        with pytest.raises(CheckoutUnavailableError, match="not available yet"):
            config.require_price(PlanTier.PRO, BillingInterval.YEAR)

    def test_invalid_price_id_is_treated_as_unset(self):
        previous = config_module._settings
        config_module._settings = Settings(paddle_price_solo_monthly="price_1Stripe")
        try:
            assert get_paddle_config("production").price_for(PlanTier.SOLO, BillingInterval.MONTH) is None
        finally:
            config_module._settings = previous

    def test_invalid_plan_returns_none(self):
        assert get_price_id_for_plan("invalid", "monthly", PaddleEnvironment.SANDBOX) is None


# =============================================================================
# Plan Tests
# =============================================================================


class TestPlanLimits:
    """The owner-approved 2026-09 catalog."""

    def test_free_plan_limits(self):
        plan = get_plan(PlanTier.FREE)
        assert plan.max_memories == 10_000
        assert plan.max_smart_credits_per_month == 500
        assert plan.unverified_credit_cap == 25
        assert plan.llm_ceiling_usd_month == 1.25
        assert (plan.max_content_chars, plan.max_batch_items, plan.max_projects, plan.max_api_keys) == (8_000, 10, 3, 3)
        assert (plan.recall_burst_per_min, plan.relay_burst_per_min, plan.enrichment_concurrency) == (20, 30, 2)
        assert plan.has_webhooks is False

    def test_solo_plan_limits(self):
        plan = get_plan(PlanTier.SOLO)
        assert (plan.price_monthly_cents, plan.price_annual_cents) == (1_200, 12_000)
        assert plan.max_smart_credits_per_month == 2_200 and plan.llm_ceiling_usd_month == 5.5
        assert plan.max_memories == 50_000 and plan.max_recalls_per_month == 50_000
        assert plan.max_relay_events_per_month == 25_000
        assert (plan.max_content_chars, plan.max_batch_items, plan.max_api_keys) == (50_000, 100, 10)
        assert plan.enrichment_concurrency == 4

    def test_pro_plan_limits(self):
        plan = get_plan(PlanTier.PRO)
        assert (plan.price_monthly_cents, plan.price_annual_cents) == (2_900, 29_000)
        assert plan.max_smart_credits_per_month == 5_000 and plan.llm_ceiling_usd_month == 12.5
        assert plan.max_memories == 125_000 and plan.max_recalls_per_month == 250_000
        assert plan.max_api_keys == 25 and plan.enrichment_concurrency == 8
        assert plan.has_observability is True

    def test_team_plan_is_per_seat_with_three_seat_minimum(self):
        plan = get_plan(PlanTier.TEAM)
        assert plan.per_seat and plan.min_seats == 3
        assert (plan.price_monthly_cents, plan.price_annual_cents) == (1_500, 15_000)
        unknown = plan.scaled(None)  # seat count unknown (e.g. a promo trial): the minimum
        assert unknown.max_users == 3 and unknown.max_smart_credits_per_month == 6_600
        paid_one = plan.scaled(1)  # exactly the seats paid for, never rounded up
        assert paid_one.max_users == 1 and paid_one.max_smart_credits_per_month == 2_200
        five = plan.scaled(5)
        assert five.max_memories == 250_000 and five.max_api_keys == 50 and five.max_users == 5

    def test_enterprise_has_explicit_ceiling(self):
        plan = get_plan(PlanTier.ENTERPRISE)
        assert plan.has_sso is True
        assert plan.max_smart_credits_per_month > 0  # no uncapped plans

    def test_legacy_tiers_have_grandfathered_ceilings(self):
        pro49 = get_plan(PlanTier.LEGACY_PRO)
        team199 = get_plan(PlanTier.LEGACY_TEAM)
        assert pro49.max_smart_credits_per_month == 12_000 and pro49.llm_ceiling_usd_month == 30.0
        assert team199.max_smart_credits_per_month == 60_000 and team199.llm_ceiling_usd_month == 150.0
        assert pro49.max_memories == 250_000 and team199.max_memories == 600_000
        assert PlanTier("legacy_pro_49") is PlanTier.LEGACY_PRO

    def test_annual_plans_bank_twelve_months_of_credits(self):
        assert get_plan(PlanTier.SOLO).credit_allowance(BillingInterval.YEAR) == 26_400
        assert get_plan(PlanTier.SOLO).credit_allowance(BillingInterval.MONTH) == 2_200

    def test_get_plan_by_string(self):
        assert get_plan("pro").tier == PlanTier.PRO
        assert get_plan("legacy_team_199").tier == PlanTier.LEGACY_TEAM


class TestUsageLimits:
    """Stores are only rejected at the memory cap; recalls at the monthly cap."""

    def test_store_within_limit(self):
        usage = UsageSnapshot(user_id="test", plan=PlanTier.FREE, memories_stored=1000, stores_this_month=99_999)
        assert usage.check_limit("store").allowed is True  # no monthly store cap any more

    def test_store_at_memory_limit(self):
        usage = UsageSnapshot(user_id="test", plan=PlanTier.FREE, memories_stored=10_000)
        result = usage.check_limit("store")
        assert result.allowed is False
        assert "Memory limit reached" in result.reason
        assert result.upgrade_hint is not None

    def test_effective_memory_cap_overrides_catalog(self):
        usage = UsageSnapshot(user_id="test", plan=PlanTier.FREE, memories_stored=10_000, max_memories=25_000)
        assert usage.check_limit("store").allowed is True

    def test_recall_within_limit(self):
        usage = UsageSnapshot(user_id="test", plan=PlanTier.FREE, recalls_this_month=9_999)
        assert usage.check_limit("recall").allowed is True

    def test_recall_at_limit(self):
        usage = UsageSnapshot(user_id="test", plan=PlanTier.FREE, recalls_this_month=10_000)
        result = usage.check_limit("recall")
        assert result.allowed is False
        assert "Monthly recall limit" in result.reason

    def test_pro_plan_has_higher_limits(self):
        usage = UsageSnapshot(user_id="test", plan=PlanTier.PRO, memories_stored=100_000)
        assert usage.check_limit("store").allowed is True

    def test_limit_check_result_to_dict(self):
        d = UsageSnapshot(user_id="test", plan=PlanTier.FREE, memories_stored=10_000).check_limit("store").to_dict()
        assert "allowed" in d and "reason" in d and "limit" in d


# =============================================================================
# Billing Manager Tests
# =============================================================================


class TestPaddleBillingManager:
    """Test PaddleBillingManager methods."""

    def test_manager_uses_sandbox_api(self, billing_manager):
        """Manager in sandbox mode should use sandbox API."""
        assert billing_manager._sandbox is True
        assert billing_manager._api_base == "https://sandbox-api.paddle.com"

    def test_manager_uses_production_api(self, sandbox_api_key, webhook_secret):
        """Manager in production mode should use production API."""
        manager = PaddleBillingManager(
            api_key=sandbox_api_key,
            webhook_secret=webhook_secret,
            sandbox=False,
        )
        assert manager._sandbox is False
        assert manager._api_base == "https://api.paddle.com"

    def test_headers_include_auth(self, billing_manager):
        """Headers should include Bearer token."""
        headers = billing_manager._headers()
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["Content-Type"] == "application/json"


class TestWebhookVerification:
    """Test webhook signature verification."""

    def test_valid_signature_passes(self, billing_manager, webhook_secret):
        """Valid webhook signature should verify."""
        payload = b'{"event_type":"test","data":{}}'
        ts = str(int(time.time()))

        # Create valid signature
        signed_payload = f"{ts}:{payload.decode()}"
        h1 = hmac.new(
            webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        signature = f"ts={ts};h1={h1}"

        result = billing_manager.verify_webhook(payload, signature)
        assert result["event_type"] == "test"

    def test_invalid_signature_fails(self, billing_manager):
        """Invalid webhook signature should raise ValueError."""
        payload = b'{"event_type":"test","data":{}}'
        ts = str(int(time.time()))
        signature = f"ts={ts};h1=invalid_signature"

        with pytest.raises(ValueError, match="Invalid webhook signature"):
            billing_manager.verify_webhook(payload, signature)

    def test_tampered_payload_fails(self, billing_manager, webhook_secret):
        """Tampered payload should fail verification."""
        original_payload = b'{"event_type":"test","data":{}}'
        tampered_payload = b'{"event_type":"hacked","data":{}}'
        ts = str(int(time.time()))

        # Sign original
        signed_payload = f"{ts}:{original_payload.decode()}"
        h1 = hmac.new(
            webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        signature = f"ts={ts};h1={h1}"

        # Verify tampered
        with pytest.raises(ValueError, match="Invalid webhook signature"):
            billing_manager.verify_webhook(tampered_payload, signature)


class TestWebhookEventProcessing:
    """Test webhook event handler."""

    @pytest.mark.asyncio
    async def test_transaction_completed_event(self, billing_manager):
        """Transaction completed should return activate_subscription."""
        event = {
            "event_type": "transaction.completed",
            "data": {
                # The price ID decides the plan (the sandbox $49 legacy Pro price);
                # custom_data "plan" is browser-controlled and ignored.
                "items": [{"price": {"id": "pri_01kmeq0ss2j2b74w9f1xwmvbc0"}, "quantity": 1}],
                "custom_data": {
                    "remembra_user_id": "user_123",
                    "plan": "enterprise",
                },
                "subscription_id": "sub_123",
                "customer_id": "ctm_123",
                "customer": {
                    "email": "test@example.com",
                    "name": "Test User",
                },
            },
        }

        result = await billing_manager.handle_webhook_event(event)

        assert result.action == "activate_subscription"
        assert result.user_id == "user_123"
        assert result.plan == PlanTier.LEGACY_PRO
        assert result.paddle_customer_id == "ctm_123"
        assert result.paddle_subscription_id == "sub_123"
        assert result.customer_email == "test@example.com"

    @pytest.mark.asyncio
    async def test_subscription_activated_event(self, billing_manager):
        """Subscription activated should return activate_subscription."""
        event = {
            "event_type": "subscription.activated",
            "data": {
                "id": "sub_456",
                "items": [{"price": {"id": "pri_01kmeq5y8ch8zfy2kw9qnrz6s1"}, "quantity": 1}],  # sandbox $199 Team
                "custom_data": {
                    "remembra_user_id": "user_456",
                    "plan": "team",
                },
            },
        }

        result = await billing_manager.handle_webhook_event(event)

        assert result.action == "activate_subscription"
        assert result.user_id == "user_456"
        assert result.plan == PlanTier.LEGACY_TEAM
        assert result.paddle_subscription_id == "sub_456"

    @pytest.mark.asyncio
    async def test_subscription_cancelled_event(self, billing_manager):
        """Subscription cancelled should return cancel_subscription."""
        event = {
            "event_type": "subscription.canceled",
            "data": {
                "custom_data": {
                    "remembra_user_id": "user_789",
                },
            },
        }

        result = await billing_manager.handle_webhook_event(event)

        assert result.action == "cancel_subscription"
        assert result.user_id == "user_789"
        assert result.plan == PlanTier.FREE

    @pytest.mark.asyncio
    async def test_subscription_past_due_event(self, billing_manager):
        """Subscription past due should return payment_failed."""
        event = {
            "event_type": "subscription.past_due",
            "data": {
                "custom_data": {
                    "remembra_user_id": "user_late",
                },
            },
        }

        result = await billing_manager.handle_webhook_event(event)

        assert result.action == "payment_failed"
        assert result.user_id == "user_late"

    @pytest.mark.asyncio
    async def test_unknown_event_type_ignored(self, billing_manager):
        """Unknown event types should be ignored."""
        event = {
            "event_type": "unknown.event",
            "data": {},
        }

        result = await billing_manager.handle_webhook_event(event)

        assert result.action == "ignored"
        assert result.event_type == "unknown.event"

    def test_webhook_result_to_dict(self):
        """WebhookResult should convert to dict properly."""
        result = WebhookResult(
            action="activate_subscription",
            user_id="user_123",
            plan=PlanTier.PRO,
            customer_email="test@example.com",
        )

        d = result.to_dict()
        assert d["action"] == "activate_subscription"
        assert d["user_id"] == "user_123"
        assert d["plan"] == "pro"
        assert d["customer_email"] == "test@example.com"


# =============================================================================
# Integration Tests (require actual API calls)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not RUN_PADDLE_INTEGRATION,
    reason=PADDLE_INTEGRATION_SKIP_REASON,
)
class TestPaddleAPIIntegration:
    """
    Integration tests that make real API calls to Paddle sandbox.

    Run with: pytest tests/test_billing_paddle.py -v -m integration
    """

    async def test_create_customer(self, billing_manager, test_user_id, test_email):
        """Should create a customer in Paddle sandbox."""
        customer_id = await billing_manager.create_customer(
            user_id=test_user_id,
            email=test_email,
            name="Test User",
        )

        assert customer_id is not None
        assert customer_id.startswith("ctm_")

    async def test_get_customer(self, billing_manager, test_user_id, test_email):
        """Should retrieve customer details."""
        # First create a customer
        customer_id = await billing_manager.create_customer(
            user_id=test_user_id,
            email=test_email,
        )

        # Then retrieve
        customer = await billing_manager.get_customer(customer_id)

        assert customer["id"] == customer_id
        assert customer["email"] == test_email
        assert customer["custom_data"]["remembra_user_id"] == test_user_id

    async def test_create_checkout_transaction_pro(self, billing_manager, test_user_id, test_email):
        """Should create a checkout transaction for Pro plan."""
        transaction = await billing_manager.create_checkout_transaction(
            paddle_customer_id=None,
            plan=PlanTier.PRO,
            user_id=test_user_id,
            customer_email=test_email,
        )

        assert "transaction_id" in transaction
        assert transaction["transaction_id"].startswith("txn_")

    async def test_create_checkout_transaction_team(self, billing_manager, test_user_id, test_email):
        """Should create a checkout transaction for Team plan."""
        transaction = await billing_manager.create_checkout_transaction(
            paddle_customer_id=None,
            plan=PlanTier.TEAM,
            user_id=test_user_id,
            customer_email=test_email,
        )

        assert "transaction_id" in transaction
        assert transaction["transaction_id"].startswith("txn_")

    async def test_create_portal_session(self, billing_manager, test_user_id, test_email):
        """Should create a customer portal session."""
        # First create a customer
        customer_id = await billing_manager.create_customer(
            user_id=test_user_id,
            email=test_email,
        )

        # Create portal session
        portal_url = await billing_manager.create_portal_session(customer_id)

        assert portal_url is not None
        assert "paddle.com" in portal_url


# =============================================================================
# Test Card Numbers (Paddle Sandbox)
# =============================================================================

# For reference when doing manual checkout testing:
TEST_CARDS = {
    "success": "4242424242424242",  # Always succeeds
    "3ds_required": "4000002500003155",  # Requires 3D Secure
    "declined": "4000000000000002",  # Always declined
}


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
