"""
Paddle configuration for sandbox vs production environments.

Price IDs for the 2026-09 catalog (Solo, Pro, Team per seat, Founding 100)
come from settings / environment — ``REMEMBRA_PADDLE_PRICE_SOLO_MONTHLY``,
``..._SOLO_ANNUAL``, ``..._PRO_MONTHLY``, ``..._PRO_ANNUAL``,
``..._TEAM_SEAT_MONTHLY``, ``..._TEAM_SEAT_ANNUAL`` and
``..._FOUNDING_ANNUAL``. Nothing is invented: when an ID is missing, checkout
for that plan/interval is unavailable and callers get a clear error.

The grandfathered $49 Pro and $199 Team price IDs are real, existing Paddle
prices and stay fixed here so renewals keep mapping to the legacy tiers.

Usage:
    from remembra.cloud.paddle_config import get_paddle_config

    config = get_paddle_config()            # environment from settings
    price = config.price_for(PlanTier.SOLO, BillingInterval.YEAR)
    mapping = config.resolve_price("pri_...")  # webhook: price -> tier
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum

from remembra.cloud.plans import BillingInterval, PlanTier

logger = logging.getLogger(__name__)


class PaddleEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class CheckoutUnavailableError(LookupError):
    """No Paddle price is configured for the requested plan / interval."""


@dataclass(frozen=True)
class PaddlePriceConfig:
    """Price IDs for one plan: monthly and annual (either may be unconfigured)."""

    monthly: str | None = None
    annual: str | None = None

    def get(self, interval: BillingInterval) -> str | None:
        return self.annual if interval == BillingInterval.YEAR else self.monthly


@dataclass(frozen=True)
class PriceMapping:
    """What a Paddle price ID buys."""

    tier: PlanTier
    interval: BillingInterval
    founding: bool = False


@dataclass(frozen=True)
class PaddleConfig:
    """Complete Paddle configuration for an environment."""

    environment: PaddleEnvironment
    api_base: str
    # 2026-09 catalog (from settings; None until the owner creates the prices)
    solo_prices: PaddlePriceConfig = PaddlePriceConfig()
    pro_prices: PaddlePriceConfig = PaddlePriceConfig()
    team_seat_prices: PaddlePriceConfig = PaddlePriceConfig()
    founding_price_id: str | None = None
    # Grandfathered catalog (fixed, existing Paddle prices)
    legacy_pro_product_id: str | None = None
    legacy_pro_prices: PaddlePriceConfig = PaddlePriceConfig()
    legacy_team_product_id: str | None = None
    legacy_team_prices: PaddlePriceConfig = PaddlePriceConfig()

    @property
    def is_sandbox(self) -> bool:
        return self.environment == PaddleEnvironment.SANDBOX

    def _prices(self, tier: PlanTier) -> PaddlePriceConfig | None:
        return {
            PlanTier.SOLO: self.solo_prices,
            PlanTier.PRO: self.pro_prices,
            PlanTier.TEAM: self.team_seat_prices,
            PlanTier.LEGACY_PRO: self.legacy_pro_prices,
            PlanTier.LEGACY_TEAM: self.legacy_team_prices,
        }.get(tier)

    def price_for(self, tier: PlanTier, interval: BillingInterval, *, founding: bool = False) -> str | None:
        """Configured price ID for a purchasable plan, or None."""
        if founding:
            if tier != PlanTier.SOLO or interval != BillingInterval.YEAR:
                return None
            return self.founding_price_id
        prices = self._prices(tier)
        return prices.get(interval) if prices else None

    def require_price(self, tier: PlanTier, interval: BillingInterval, *, founding: bool = False) -> str:
        """Configured price ID, or :class:`CheckoutUnavailableError` with a clear message."""
        price = self.price_for(tier, interval, founding=founding)
        if not price:
            label = "Founding 100 (Solo annual)" if founding else f"{tier.value} ({interval.value}ly)"
            raise CheckoutUnavailableError(f"Checkout for {label} is not available yet: no Paddle price is configured for it.")
        return price

    def resolve_price(self, price_id: str | None) -> PriceMapping | None:
        """Map a Paddle price ID (from a webhook) back to the plan it buys."""
        if not price_id:
            return None
        if self.founding_price_id and price_id == self.founding_price_id:
            return PriceMapping(PlanTier.SOLO, BillingInterval.YEAR, founding=True)
        for tier in (PlanTier.SOLO, PlanTier.PRO, PlanTier.TEAM, PlanTier.LEGACY_PRO, PlanTier.LEGACY_TEAM):
            prices = self._prices(tier)
            if prices is None:
                continue
            if prices.monthly and price_id == prices.monthly:
                return PriceMapping(tier, BillingInterval.MONTH)
            if prices.annual and price_id == prices.annual:
                return PriceMapping(tier, BillingInterval.YEAR)
        return None


# =============================================================================
# Base configurations: the fixed legacy prices per environment
# =============================================================================
SANDBOX_CONFIG = PaddleConfig(
    environment=PaddleEnvironment.SANDBOX,
    api_base="https://sandbox-api.paddle.com",
    legacy_pro_product_id="pro_01kmepzj0fnha19eznjanme5v4",
    legacy_pro_prices=PaddlePriceConfig(monthly="pri_01kmeq0ss2j2b74w9f1xwmvbc0"),  # $49/mo
    legacy_team_product_id="pro_01kmeq4v9ww2znyhg8ypnnm6gd",
    legacy_team_prices=PaddlePriceConfig(monthly="pri_01kmeq5y8ch8zfy2kw9qnrz6s1"),  # $199/mo
)

PRODUCTION_CONFIG = PaddleConfig(
    environment=PaddleEnvironment.PRODUCTION,
    api_base="https://api.paddle.com",
    legacy_pro_product_id="pro_01kmepaakyc11xgj8j2j863y3z",
    legacy_pro_prices=PaddlePriceConfig(monthly="pri_01kmepby4nfy150jbfjkpkev5h"),  # $49/mo
    legacy_team_product_id="pro_01kmepdm9jg61b75z4w3p355dy",
    legacy_team_prices=PaddlePriceConfig(monthly="pri_01kmepewmfpqdz413hc4f4fr3r"),  # $199/mo
)


def _clean_price_id(name: str, value: str | None) -> str | None:
    """Accept only real Paddle price IDs; anything else is treated as unset."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith("pri_"):
        logger.error("paddle_price_id_invalid setting=%s (must start with 'pri_'); treating as unset", name)
        return None
    return value


def with_catalog_prices(base: PaddleConfig) -> PaddleConfig:
    """``base`` plus the 2026-09 catalog price IDs from settings/environment."""
    try:
        from remembra.config import get_settings

        s = get_settings()
    except Exception:  # settings unavailable (e.g. bare SDK install)
        return base

    def price(name: str) -> str | None:
        return _clean_price_id(name, getattr(s, name, None))

    return replace(
        base,
        solo_prices=PaddlePriceConfig(
            monthly=price("paddle_price_solo_monthly"),
            annual=price("paddle_price_solo_annual"),
        ),
        pro_prices=PaddlePriceConfig(
            monthly=price("paddle_price_pro_monthly"),
            annual=price("paddle_price_pro_annual"),
        ),
        team_seat_prices=PaddlePriceConfig(
            monthly=price("paddle_price_team_seat_monthly"),
            annual=price("paddle_price_team_seat_annual"),
        ),
        founding_price_id=price("paddle_price_founding_annual"),
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

    Reads PADDLE_API_KEY, PADDLE_CLIENT_TOKEN, PADDLE_SANDBOX and the
    REMEMBRA_PADDLE_PRICE_* price IDs.
    """
    from remembra.config import get_settings

    settings = get_settings()

    if not settings.paddle_api_key:
        raise ValueError("PADDLE_API_KEY not configured")

    sandbox = settings.paddle_sandbox
    return PaddleSettings(
        api_key=settings.paddle_api_key,
        client_token=settings.paddle_client_token,
        webhook_secret=settings.paddle_webhook_secret,
        sandbox=sandbox,
        config=get_paddle_config(PaddleEnvironment.SANDBOX if sandbox else PaddleEnvironment.PRODUCTION),
    )


def get_paddle_config(env: PaddleEnvironment | str | None = None) -> PaddleConfig:
    """Paddle configuration for an environment, with catalog prices from settings.

    Args:
        env: "sandbox" or "production", or None to auto-detect from settings.
    """
    if env is None:
        try:
            from remembra.config import get_settings

            env = PaddleEnvironment.SANDBOX if get_settings().paddle_sandbox else PaddleEnvironment.PRODUCTION
        except Exception:
            env = PaddleEnvironment.PRODUCTION

    if isinstance(env, str) and not isinstance(env, PaddleEnvironment):
        env = PaddleEnvironment(env.lower())

    base = SANDBOX_CONFIG if env == PaddleEnvironment.SANDBOX else PRODUCTION_CONFIG
    return with_catalog_prices(base)


def get_price_id_for_plan(
    plan: str,
    billing_interval: str = "monthly",
    env: PaddleEnvironment | str | None = None,
) -> str | None:
    """Paddle price ID for a plan name ("solo", "pro", "team", "founding", legacy tiers), or None."""
    try:
        interval = BillingInterval.parse(billing_interval)
    except ValueError:
        return None
    name = plan.strip().lower()
    config = get_paddle_config(env)
    if name == "founding":
        return config.price_for(PlanTier.SOLO, interval, founding=True)
    try:
        tier = PlanTier(name)
    except ValueError:
        return None
    return config.price_for(tier, interval)
