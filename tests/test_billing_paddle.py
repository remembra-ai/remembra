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

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set sandbox environment before imports
os.environ["PADDLE_ENVIRONMENT"] = "sandbox"

from remembra.cloud.billing_paddle import PaddleBillingManager, WebhookResult
from remembra.cloud.paddle_config import (
    SANDBOX_CONFIG,
    PRODUCTION_CONFIG,
    PaddleEnvironment,
    get_paddle_config,
    get_price_id_for_plan,
)
from remembra.cloud.plans_paddle import (
    PLANS,
    PlanTier,
    UsageSnapshot,
    get_plan,
)


# =============================================================================
# Fixtures
# =============================================================================


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


class TestPaddleConfig:
    """Test Paddle configuration management."""

    def test_sandbox_config_has_correct_api_base(self):
        """Sandbox config should point to sandbox API."""
        assert SANDBOX_CONFIG.api_base == "https://sandbox-api.paddle.com"
        assert SANDBOX_CONFIG.is_sandbox is True

    def test_production_config_has_correct_api_base(self):
        """Production config should point to production API."""
        assert PRODUCTION_CONFIG.api_base == "https://api.paddle.com"
        assert PRODUCTION_CONFIG.is_sandbox is False

    def test_get_config_by_string(self):
        """Should get config using string environment name."""
        config = get_paddle_config("sandbox")
        assert config == SANDBOX_CONFIG

        config = get_paddle_config("production")
        assert config == PRODUCTION_CONFIG

    def test_sandbox_has_pro_price_ids(self):
        """Sandbox should have Pro price IDs configured."""
        assert SANDBOX_CONFIG.pro_product_id == "pro_01kmepzj0fnha19eznjanme5v4"
        assert SANDBOX_CONFIG.pro_prices.monthly == "pri_01kmeq0ss2j2b74w9f1xwmvbc0"

    def test_sandbox_has_team_price_ids(self):
        """Sandbox should have Team price IDs configured."""
        assert SANDBOX_CONFIG.team_product_id == "pro_01kmeq4v9ww2znyhg8ypnnm6gd"
        assert SANDBOX_CONFIG.team_prices.monthly == "pri_01kmeq5y8ch8zfy2kw9qnrz6s1"

    def test_production_has_pro_price_ids(self):
        """Production should have Pro price IDs configured."""
        assert PRODUCTION_CONFIG.pro_product_id == "pro_01kmepaakyc11xgj8j2j863y3z"
        assert PRODUCTION_CONFIG.pro_prices.monthly == "pri_01kmepby4nfy150jbfjkpkev5h"

    def test_production_has_team_price_ids(self):
        """Production should have Team price IDs configured."""
        assert PRODUCTION_CONFIG.team_product_id == "pro_01kmepdm9jg61b75z4w3p355dy"
        assert PRODUCTION_CONFIG.team_prices.monthly == "pri_01kmepewmfpqdz413hc4f4fr3r"

    def test_get_price_id_for_plan(self):
        """Should get correct price ID for plan and environment."""
        pro_sandbox = get_price_id_for_plan("pro", "monthly", PaddleEnvironment.SANDBOX)
        assert pro_sandbox == "pri_01kmeq0ss2j2b74w9f1xwmvbc0"

        team_prod = get_price_id_for_plan("team", "monthly", PaddleEnvironment.PRODUCTION)
        assert team_prod == "pri_01kmepewmfpqdz413hc4f4fr3r"

    def test_invalid_plan_returns_none(self):
        """Should return None for invalid plan names."""
        price = get_price_id_for_plan("invalid", "monthly", PaddleEnvironment.SANDBOX)
        assert price is None


# =============================================================================
# Plan Tests
# =============================================================================


class TestPlanLimits:
    """Test plan limit definitions and enforcement."""

    def test_free_plan_limits(self):
        """Free plan should have restricted limits."""
        plan = get_plan(PlanTier.FREE)
        assert plan.max_memories == 25_000
        assert plan.max_users == 1
        assert plan.max_projects == 1
        assert plan.has_webhooks is False
        assert plan.has_sso is False

    def test_pro_plan_limits(self):
        """Pro plan should have higher limits."""
        plan = get_plan(PlanTier.PRO)
        assert plan.max_memories == 500_000
        assert plan.max_users == 5
        assert plan.max_projects == 5
        assert plan.has_webhooks is True
        assert plan.has_observability is True

    def test_team_plan_limits(self):
        """Team plan should have team-level limits."""
        plan = get_plan(PlanTier.TEAM)
        assert plan.max_memories == 2_000_000
        assert plan.max_users == 25
        assert plan.has_priority_support is True

    def test_enterprise_plan_limits(self):
        """Enterprise plan should have highest limits."""
        plan = get_plan(PlanTier.ENTERPRISE)
        assert plan.max_memories == 10_000_000
        assert plan.has_sso is True

    def test_plan_has_paddle_price_id(self):
        """Paid plans should have Paddle price IDs."""
        pro = get_plan(PlanTier.PRO)
        team = get_plan(PlanTier.TEAM)

        # In sandbox env, these should return sandbox price IDs
        assert pro.paddle_price_id is not None
        assert team.paddle_price_id is not None
        assert pro.paddle_price_id.startswith("pri_")
        assert team.paddle_price_id.startswith("pri_")

    def test_free_plan_has_no_price_id(self):
        """Free plan should not have a Paddle price ID."""
        free = get_plan(PlanTier.FREE)
        assert free.paddle_price_id is None

    def test_get_plan_by_string(self):
        """Should get plan using string tier name."""
        plan = get_plan("pro")
        assert plan.tier == PlanTier.PRO


class TestUsageLimits:
    """Test usage limit checking."""

    def test_store_within_limit(self):
        """Should allow store when within limit."""
        usage = UsageSnapshot(
            user_id="test",
            plan=PlanTier.FREE,
            memories_stored=1000,
            stores_this_month=1000,
        )
        result = usage.check_limit("store")
        assert result.allowed is True

    def test_store_at_memory_limit(self):
        """Should deny store when at memory limit."""
        usage = UsageSnapshot(
            user_id="test",
            plan=PlanTier.FREE,
            memories_stored=25_000,  # At free limit
            stores_this_month=1000,
        )
        result = usage.check_limit("store")
        assert result.allowed is False
        assert "Memory limit reached" in result.reason
        assert result.upgrade_hint is not None

    def test_store_at_monthly_limit(self):
        """Should deny store when at monthly limit."""
        usage = UsageSnapshot(
            user_id="test",
            plan=PlanTier.FREE,
            memories_stored=1000,
            stores_this_month=25_000,  # At monthly limit
        )
        result = usage.check_limit("store")
        assert result.allowed is False
        assert "Monthly store limit" in result.reason

    def test_recall_within_limit(self):
        """Should allow recall when within limit."""
        usage = UsageSnapshot(
            user_id="test",
            plan=PlanTier.FREE,
            recalls_this_month=10_000,
        )
        result = usage.check_limit("recall")
        assert result.allowed is True

    def test_recall_at_limit(self):
        """Should deny recall when at limit."""
        usage = UsageSnapshot(
            user_id="test",
            plan=PlanTier.FREE,
            recalls_this_month=50_000,  # At free limit
        )
        result = usage.check_limit("recall")
        assert result.allowed is False
        assert "Monthly recall limit" in result.reason

    def test_pro_plan_has_higher_limits(self):
        """Pro plan should allow more operations."""
        usage = UsageSnapshot(
            user_id="test",
            plan=PlanTier.PRO,
            memories_stored=100_000,  # Over free limit
            stores_this_month=100_000,  # Over free limit
        )
        result = usage.check_limit("store")
        assert result.allowed is True

    def test_limit_check_result_to_dict(self):
        """Should convert limit check result to dict."""
        result = UsageSnapshot(
            user_id="test",
            plan=PlanTier.FREE,
            memories_stored=25_000,
        ).check_limit("store")

        d = result.to_dict()
        assert "allowed" in d
        assert "reason" in d
        assert "limit" in d


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
                "custom_data": {
                    "remembra_user_id": "user_123",
                    "plan": "pro",
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
        assert result.plan == PlanTier.PRO
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
                "custom_data": {
                    "remembra_user_id": "user_456",
                    "plan": "team",
                },
            },
        }

        result = await billing_manager.handle_webhook_event(event)

        assert result.action == "activate_subscription"
        assert result.user_id == "user_456"
        assert result.plan == PlanTier.TEAM
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
