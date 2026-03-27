"""
Paddle configuration for sandbox vs production environments.

Usage:
    from remembra.cloud.paddle_config import get_paddle_config, PaddleEnvironment

    config = get_paddle_config(PaddleEnvironment.SANDBOX)
    # or
    config = get_paddle_config(PaddleEnvironment.PRODUCTION)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class PaddleEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


@dataclass(frozen=True)
class PaddlePriceConfig:
    """Price IDs for a plan tier."""

    monthly: str
    annual: str | None = None


@dataclass(frozen=True)
class PaddleConfig:
    """Complete Paddle configuration for an environment."""

    environment: PaddleEnvironment
    api_base: str
    pro_product_id: str
    pro_prices: PaddlePriceConfig
    team_product_id: str
    team_prices: PaddlePriceConfig

    @property
    def is_sandbox(self) -> bool:
        return self.environment == PaddleEnvironment.SANDBOX


# =============================================================================
# Sandbox Configuration (for testing)
# =============================================================================
SANDBOX_CONFIG = PaddleConfig(
    environment=PaddleEnvironment.SANDBOX,
    api_base="https://sandbox-api.paddle.com",
    # Pro Plan
    pro_product_id="pro_01kmepzj0fnha19eznjanme5v4",
    pro_prices=PaddlePriceConfig(
        monthly="pri_01kmeq0ss2j2b74w9f1xwmvbc0",  # $49/mo
        annual=None,  # Not created yet
    ),
    # Team Plan
    team_product_id="pro_01kmeq4v9ww2znyhg8ypnnm6gd",
    team_prices=PaddlePriceConfig(
        monthly="pri_01kmeq5y8ch8zfy2kw9qnrz6s1",  # $199/mo
        annual=None,  # Not created yet
    ),
)


# =============================================================================
# Production Configuration
# =============================================================================
PRODUCTION_CONFIG = PaddleConfig(
    environment=PaddleEnvironment.PRODUCTION,
    api_base="https://api.paddle.com",
    # Pro Plan
    pro_product_id="pro_01kmepaakyc11xgj8j2j863y3z",
    pro_prices=PaddlePriceConfig(
        monthly="pri_01kmepby4nfy150jbfjkpkev5h",  # $49/mo
        annual=None,  # Not created yet
    ),
    # Team Plan
    team_product_id="pro_01kmepdm9jg61b75z4w3p355dy",
    team_prices=PaddlePriceConfig(
        monthly="pri_01kmepewmfpqdz413hc4f4fr3r",  # $199/mo
        annual=None,  # Not created yet
    ),
)


@dataclass
class PaddleSettings:
    """Runtime Paddle settings from environment."""

    api_key: str
    client_token: str | None
    webhook_secret: str | None
    sandbox: bool
    config: PaddleConfig


def get_paddle_settings() -> PaddleSettings:
    """Get Paddle settings from environment/config.

    Reads PADDLE_API_KEY, PADDLE_CLIENT_TOKEN, PADDLE_SANDBOX, etc.
    """
    from remembra.config import get_settings

    settings = get_settings()

    if not settings.paddle_api_key:
        raise ValueError("PADDLE_API_KEY not configured")

    sandbox = settings.paddle_sandbox
    config = SANDBOX_CONFIG if sandbox else PRODUCTION_CONFIG

    return PaddleSettings(
        api_key=settings.paddle_api_key,
        client_token=settings.paddle_client_token,
        webhook_secret=settings.paddle_webhook_secret,
        sandbox=sandbox,
        config=config,
    )


# Alias for backward compatibility
def get_paddle_config(env: PaddleEnvironment | str | None = None) -> PaddleConfig:
    """Get Paddle configuration for an environment.

    Args:
        env: "sandbox" or "production", or None to auto-detect from settings

    Returns:
        PaddleConfig with all price IDs and settings.
    """
    if env is None:
        # Auto-detect from settings
        try:
            from remembra.config import get_settings

            settings = get_settings()
            if settings.paddle_sandbox:
                return SANDBOX_CONFIG
            return PRODUCTION_CONFIG
        except Exception:
            return PRODUCTION_CONFIG

    if isinstance(env, str):
        env = PaddleEnvironment(env.lower())

    if env == PaddleEnvironment.SANDBOX:
        return SANDBOX_CONFIG
    return PRODUCTION_CONFIG


def get_price_id_for_plan(
    plan: str,
    billing_interval: str = "monthly",
    env: PaddleEnvironment = PaddleEnvironment.PRODUCTION,
) -> str | None:
    """Get the Paddle price ID for a plan tier.

    Args:
        plan: "pro" or "team"
        billing_interval: "monthly" or "annual"
        env: Paddle environment

    Returns:
        Price ID string or None if not configured.
    """
    config = get_paddle_config(env)

    if plan.lower() == "pro":
        prices = config.pro_prices
    elif plan.lower() == "team":
        prices = config.team_prices
    else:
        return None

    if billing_interval == "annual":
        return prices.annual
    return prices.monthly
