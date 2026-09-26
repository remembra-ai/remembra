"""
Usage metering for Remembra Cloud.

Tracks per-tenant usage (memories, recalls, relay events, smart credits) and
holds the smart-credit ledger:

* ``cloud_credit_periods`` — per account and billing period (a calendar month,
  or a subscription year for annual plans — the yearly credit bank):
  credits used, credits currently reserved, and actual AI dollars.
* ``cloud_credit_reservations`` — one row per enrichment reservation. A
  reservation is taken BEFORE any LLM call (atomic ``UPDATE ... WHERE used +
  reserved + n <= limit``) and settled from the real OpenAI usage when the
  background work finishes; the unused part is refunded.
* ``cloud_ai_spend_monthly`` — actual AI dollars per month for the free group
  vs paid accounts (feeds the global free-tier circuit breaker).
* ``cloud_revenue_events`` — net paid revenue per Paddle transaction (the
  breaker budget is a share of last month's revenue).

Data lives in the main SQLite database.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from remembra.cloud.plans import (
    CREDIT_USD,
    FOUNDING_MAX_REDEMPTIONS,
    BillingInterval,
    PlanLimits,
    PlanTier,
    UsageSnapshot,
    charge_for,
    credits_for_usd,
    get_plan,
)
from remembra.config import get_settings
from remembra.storage.database import FOUNDING_HOLDS_DDL, RELAY_RECORD_SQL, RELAY_WRITTEN_SQL, RELAY_WRITTEN_TYPES

logger = logging.getLogger(__name__)

# Embedding list prices, USD per 1M input tokens (unknown models: the priciest).
EMBEDDING_PRICES_PER_M: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}
UNKNOWN_EMBEDDING_PRICE_PER_M = 0.13
EMBEDDING_CHARS_PER_TOKEN = 4

# Tiers whose limits are shared by the members of the owner's teams.
_POOLED_MIN_USERS = 2

_LEGACY_MIGRATION = "2026_09_plans_v2_legacy_tiers"
_FOUNDING_LAPSE_MIGRATION = "2026_09_founding_lapse_holds"
# A Founding 100 checkout keeps its seat this long while the buyer pays.
FOUNDING_CHECKOUT_HOLD = timedelta(hours=2)
# A founder whose subscription ends keeps the seat (and the price) this long.
FOUNDING_LAPSE_GRACE = timedelta(days=14)


# A pending Founding checkout hold is never extended, and an account gets one per this window.
FOUNDING_HOLD_WINDOW = timedelta(hours=24)


class FoundingHoldLimitError(Exception):
    """This account's Founding checkout hold expired unpaid less than a day ago (``retry_at``: when a new one is allowed)."""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__(f"founding hold limit until {retry_at.isoformat()}")
        self.retry_at = retry_at


@dataclass(frozen=True)
class FoundingSeatHold:
    """A Founding seat held for one checkout, and the ``founding_holds`` row it replaced (kind, until, txn, created)."""

    user_id: str
    until: str
    prior: tuple[str, str, str | None, str] | None


# cloud_tenants.signup_source for tenants created by POST /api/v1/cloud/signup.
TENANT_SIGNUP_SOURCE = "cloud_signup"


def now_utc() -> datetime:
    """Current time (UTC, aware). Module-level so tests can move the clock."""
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _next_month(start: datetime) -> datetime:
    return datetime(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1, tzinfo=UTC)


def _add_years(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return value.replace(year=value.year + years, day=28)


def _months_elapsed(start: datetime, now: datetime) -> int:
    """Whole months from ``start`` to ``now`` (0 during the first month)."""
    months = (now.year - start.year) * 12 + (now.month - start.month)
    if (now.day, now.time()) < (start.day, start.time()):
        months -= 1
    return max(0, months)


@dataclass(frozen=True)
class CreditPeriod:
    """A billing period for smart credits."""

    key: str
    start: datetime
    end: datetime
    interval: BillingInterval

    @classmethod
    def monthly(cls, now: datetime) -> CreditPeriod:
        start = _month_start(now)
        return cls(key=f"M:{month_key(now)}", start=start, end=_next_month(start), interval=BillingInterval.MONTH)

    @classmethod
    def yearly(cls, now: datetime, anchor: datetime) -> CreditPeriod:
        """The subscription year containing ``now`` (anchor = subscription start)."""
        anchor = anchor.astimezone(UTC)
        years = max(0, now.year - anchor.year)
        start = _add_years(anchor, years)
        if start > now:
            start = _add_years(anchor, years - 1) if years > 0 else anchor
        end = _add_years(start, 1)
        return cls(key=f"Y:{start.date().isoformat()}", start=start, end=end, interval=BillingInterval.YEAR)


@dataclass(frozen=True)
class AccountState:
    """Everything metering needs to know about one account right now."""

    user_id: str
    tier: PlanTier
    limits: PlanLimits  # seat-scaled
    interval: BillingInterval
    seats: int
    founding: bool
    email_verified: bool
    free_group: bool  # Free tier or an unpaid promo trial (counts against the free breaker)
    period: CreditPeriod
    credit_limit: int
    memory_cap: int
    # Users whose memories count against this account's cap (team members of a
    # pooled plan share the owner's ledger, limits and memory cap).
    pool_user_ids: tuple[str, ...] = field(default=())
    # The requesting user when it is a team member billed to ``user_id`` (the owner).
    member_user_id: str | None = None
    created_at: datetime | None = None
    # Yearly bank held back (R-27): the whole year's credits and when they unlock.
    full_credit_limit: int | None = None
    bank_unlock_at: datetime | None = None

    @property
    def pool(self) -> tuple[str, ...]:
        return self.pool_user_ids or (self.user_id,)


@dataclass(frozen=True)
class CreditBalance:
    used: int
    reserved: int
    limit: int
    llm_usd: float

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)


class UsageMeter:
    """Tracks and queries usage metrics per tenant.

    Args:
        db: The application's Database instance.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[None]:
        transaction = getattr(self._db, "transaction", None)
        if transaction is None:
            yield
            await self._db.conn.commit()
            return
        async with transaction():
            yield

    async def _add_column(self, table: str, ddl: str) -> None:
        try:
            await self._db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            await self._db.conn.commit()
        except Exception:
            pass  # column already exists

    async def init_schema(self) -> None:
        """Create usage tracking tables if they don't exist and migrate old ones."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS cloud_tenants (
                user_id TEXT PRIMARY KEY,
                email TEXT,
                name TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cloud_usage_daily (
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                stores INTEGER DEFAULT 0,
                recalls INTEGER DEFAULT 0,
                deletes INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );

            CREATE INDEX IF NOT EXISTS idx_usage_user_date
                ON cloud_usage_daily(user_id, date);

            CREATE TABLE IF NOT EXISTS cloud_credit_periods (
                user_id TEXT NOT NULL,
                period_key TEXT NOT NULL,
                credits_used INTEGER NOT NULL DEFAULT 0,
                credits_reserved INTEGER NOT NULL DEFAULT 0,
                llm_usd REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, period_key)
            );

            CREATE TABLE IF NOT EXISTS cloud_credit_reservations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                period_key TEXT NOT NULL,
                credits INTEGER NOT NULL,
                min_credits INTEGER NOT NULL,
                free_group INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                charged INTEGER NOT NULL DEFAULT 0,
                actual_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                settled_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_credit_res_open
                ON cloud_credit_reservations(status, created_at);

            CREATE TABLE IF NOT EXISTS cloud_ai_spend_monthly (
                month TEXT NOT NULL,
                grp TEXT NOT NULL,
                usd REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (month, grp)
            );

            CREATE TABLE IF NOT EXISTS cloud_revenue_events (
                transaction_id TEXT PRIMARY KEY,
                month TEXT NOT NULL,
                net_usd REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cloud_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)
        await self._db.conn.commit()

        # Columns added after the tables first shipped.
        await self._add_column("cloud_tenants", "email TEXT")
        await self._add_column("cloud_tenants", "name TEXT")
        # Promo trials store an expiry; update_plan() has always written it.
        await self._add_column("cloud_tenants", "promo_expires_at TEXT")
        await self._add_column("cloud_tenants", "billing_interval TEXT")
        await self._add_column("cloud_tenants", "period_anchor TEXT")
        await self._add_column("cloud_tenants", "seats INTEGER")
        await self._add_column("cloud_tenants", "founding INTEGER DEFAULT 0")
        # Something about the account needs the owner's attention (e.g. a
        # Founding 100 charge past the cap that must be refunded).
        await self._add_column("cloud_tenants", "billing_flag TEXT")
        # Tenants from the master-key /cloud/signup backend have no users row:
        # their email verification lives here (see verify_tenant_email).
        await self._add_column("cloud_tenants", "email_verified INTEGER DEFAULT 0")
        await self._add_column("cloud_tenants", "signup_source TEXT")
        for column in ("relay_events", "credits_used", "degraded_stores", "unenriched_writes"):
            await self._add_column("cloud_usage_daily", f"{column} INTEGER DEFAULT 0")
        await self._add_column("cloud_usage_daily", "llm_usd REAL DEFAULT 0")
        # R-27: when a new yearly bank unlocks in full (NULL = no hold).
        await self._add_column("cloud_tenants", "bank_unlock_at TEXT")
        # Also created by the versioned migrations; kept here for databases that
        # only run the metering schema.
        await self._db.conn.executescript(FOUNDING_HOLDS_DDL)
        await self._db.conn.commit()

        await self._migrate_legacy_tiers()
        await self._migrate_founding_lapses()

    async def _migrate_legacy_tiers(self) -> None:
        """One time: existing $49 Pro / $199 Team subscribers become the grandfathered tiers.

        Runs exactly once per database (recorded in ``cloud_migrations``), so a
        customer who buys the NEW Pro/Team plan afterwards is never touched.
        Unpaid promo trials (an expiry and no subscription) stay on the new Pro.
        """
        cursor = await self._db.conn.execute("SELECT 1 FROM cloud_migrations WHERE name = ?", (_LEGACY_MIGRATION,))
        if await cursor.fetchone():
            return
        now = now_utc().isoformat()
        trial = "(promo_expires_at IS NOT NULL AND stripe_subscription_id IS NULL)"
        async with self._tx():
            await self._db.conn.execute(
                f"UPDATE cloud_tenants SET plan = ?, updated_at = ? WHERE plan = 'pro' AND NOT {trial}",
                (PlanTier.LEGACY_PRO.value, now),
            )
            await self._db.conn.execute(
                f"UPDATE cloud_tenants SET plan = ?, updated_at = ? WHERE plan = 'team' AND NOT {trial}",
                (PlanTier.LEGACY_TEAM.value, now),
            )
            await self._db.conn.execute("INSERT INTO cloud_migrations (name, applied_at) VALUES (?, ?)", (_LEGACY_MIGRATION, now))
        # Teams carry their owner's billing plan; keep them in step (teams are optional).
        try:
            await self._db.conn.execute(
                """
                UPDATE teams SET plan = (SELECT t.plan FROM cloud_tenants t WHERE t.user_id = teams.owner_id)
                WHERE owner_id IN (SELECT user_id FROM cloud_tenants WHERE plan IN (?, ?))
                """,
                (PlanTier.LEGACY_PRO.value, PlanTier.LEGACY_TEAM.value),
            )
            await self._db.conn.commit()
        except Exception:
            logger.info("cloud_legacy_team_plan_sync_skipped (no teams table)")
        logger.info("cloud_legacy_tier_migration_applied")

    async def _migrate_founding_lapses(self) -> None:
        """One time: founders already back on Free stop holding the price and get the lapse grace.

        Before R-26 ``founding`` was sticky, so a founder who cancelled kept a
        seat forever. From now on ``founding = 1`` means "holds the price
        today"; a lapsed founder keeps the seat for 14 days from their last
        change and can resubscribe at $108 until then.
        """
        cursor = await self._db.conn.execute("SELECT 1 FROM cloud_migrations WHERE name = ?", (_FOUNDING_LAPSE_MIGRATION,))
        if await cursor.fetchone():
            return
        now = now_utc()
        async with self._tx():
            cursor = await self._db.conn.execute(
                "SELECT user_id, updated_at FROM cloud_tenants WHERE founding = 1 AND plan = ?", (PlanTier.FREE.value,)
            )
            for user_id, updated_at in await cursor.fetchall():
                since = _parse_dt(updated_at) or now
                await self._db.conn.execute(
                    "INSERT OR REPLACE INTO founding_holds (user_id, kind, until, created_at) VALUES (?, 'lapsed', ?, ?)",
                    (user_id, (since + FOUNDING_LAPSE_GRACE).isoformat(), now.isoformat()),
                )
            await self._db.conn.execute(
                "UPDATE cloud_tenants SET founding = 0 WHERE founding = 1 AND plan = ?", (PlanTier.FREE.value,)
            )
            await self._db.conn.execute(
                "INSERT INTO cloud_migrations (name, applied_at) VALUES (?, ?)", (_FOUNDING_LAPSE_MIGRATION, now.isoformat())
            )

    # -----------------------------------------------------------------------
    # Tenant management
    # -----------------------------------------------------------------------

    async def register_tenant(
        self,
        user_id: str,
        plan: PlanTier = PlanTier.FREE,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
        email: str | None = None,
        name: str | None = None,
    ) -> None:
        """Register a new tenant or update existing."""
        now = now_utc().isoformat()
        await self._db.conn.execute(
            """
            INSERT INTO cloud_tenants (
                user_id, email, name, plan, stripe_customer_id, stripe_subscription_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                plan = excluded.plan,
                email = COALESCE(excluded.email, cloud_tenants.email),
                name = COALESCE(excluded.name, cloud_tenants.name),
                stripe_customer_id = COALESCE(excluded.stripe_customer_id, cloud_tenants.stripe_customer_id),
                stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, cloud_tenants.stripe_subscription_id),
                updated_at = excluded.updated_at
            """,
            (user_id, email, name, plan.value, stripe_customer_id, stripe_subscription_id, now, now),
        )
        await self._db.conn.commit()

    async def apply_subscription(
        self,
        user_id: str,
        plan: PlanTier,
        *,
        interval: BillingInterval | None = None,
        seats: int | None = None,
        period_anchor: datetime | None = None,
        founding: bool = False,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        email: str | None = None,
        name: str | None = None,
        bank_unlock_at: datetime | None = None,
    ) -> None:
        """Apply a verified billing event: plan, interval, seats, yearly-bank anchor.

        ``founding`` marks the account as holding the Founding 100 price (use
        :meth:`claim_founding`, which enforces the cap). ``bank_unlock_at``
        holds a NEW yearly bank back until then (R-27); renewals leave it.
        Falling back to Free clears the interval, the seat count and the bank
        hold, and a founder's seat becomes a 14-day lapse hold.
        """
        await self.register_tenant(
            user_id,
            plan=plan,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            email=email,
            name=name,
        )
        now = now_utc()
        if plan == PlanTier.FREE:
            async with self._tx():
                await self.end_founding(user_id)
                await self._db.conn.execute(
                    "UPDATE cloud_tenants SET billing_interval = NULL, seats = NULL, period_anchor = NULL,"
                    " bank_unlock_at = NULL, updated_at = ? WHERE user_id = ?",
                    (now.isoformat(), user_id),
                )
        else:
            tenant = await self.get_tenant(user_id) or {}
            anchor = period_anchor
            if anchor is None and interval == BillingInterval.YEAR:
                # Keep the existing year anchor on renewals/updates without a period.
                anchor = _parse_dt(tenant.get("period_anchor")) or now
            await self._db.conn.execute(
                """
                UPDATE cloud_tenants SET
                    billing_interval = COALESCE(?, billing_interval),
                    seats = COALESCE(?, seats),
                    period_anchor = ?,
                    founding = CASE WHEN ? THEN 1 ELSE COALESCE(founding, 0) END,
                    bank_unlock_at = COALESCE(?, bank_unlock_at),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    interval.value if interval else None,
                    seats,
                    anchor.isoformat() if anchor else tenant.get("period_anchor"),
                    1 if founding else 0,
                    bank_unlock_at.isoformat() if bank_unlock_at else None,
                    now.isoformat(),
                    user_id,
                ),
            )
        await self._db.conn.commit()

    async def founding_redemptions(self) -> int:
        """Accounts holding the Founding 100 price right now."""
        cursor = await self._db.conn.execute("SELECT COUNT(*) FROM cloud_tenants WHERE founding = 1")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_tenant(self, user_id: str) -> dict[str, Any] | None:
        """Get tenant record by user_id."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM cloud_tenants WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def mark_tenant_signup(self, user_id: str, source: str = TENANT_SIGNUP_SOURCE) -> None:
        """Record where a tenant came from (the unverified-email hold keys off it)."""
        await self._db.conn.execute("UPDATE cloud_tenants SET signup_source = ? WHERE user_id = ?", (source, user_id))
        await self._db.conn.commit()

    async def email_has_verified_account(self, email: str, *, exclude_user_id: str) -> bool:
        """True when another account already holds ``email`` as a VERIFIED address.

        One free account per verified email: a second API-signup tenant (or a
        dashboard account) cannot claim an address someone already verified.
        Same check as every other verify path (:func:`remembra.auth.users.email_verified_on_another_account`).
        """
        from remembra.auth.users import email_verified_on_another_account

        return await email_verified_on_another_account(self._db, email, exclude_user_id=exclude_user_id)

    async def set_tenant_email_verified(self, user_id: str) -> None:
        await self._db.conn.execute(
            "UPDATE cloud_tenants SET email_verified = 1, updated_at = ? WHERE user_id = ?",
            (now_utc().isoformat(), user_id),
        )
        await self._db.conn.commit()

    async def get_user_email(self, user_id: str) -> str | None:
        """Get user's email from user_id.

        Checks both the users table (auth signup) and cloud_tenants table
        (API key signup) since users can come from either flow.
        """
        cursor = await self._db.conn.execute(
            "SELECT email FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            email: str = row[0]
            return email

        cursor = await self._db.conn.execute(
            "SELECT email FROM cloud_tenants WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def _user_row(self, user_id: str) -> dict[str, Any] | None:
        try:
            cursor = await self._db.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
        except Exception:  # no users table in minimal deployments
            return None
        return dict(row) if row is not None else None

    async def get_tenant_plan(self, user_id: str) -> PlanTier:
        """Get the plan tier for a user. Returns FREE if not registered.

        Owner emails configured via REMEMBRA_OWNER_EMAILS automatically
        get Enterprise access without requiring database entries.
        """
        # Owner bypass: only for a *verified* owner account (or an explicit
        # superadmin user id) — an unverified signup claiming an owner address
        # must not get Enterprise.
        settings = get_settings()
        if settings.owner_emails or settings.superadmin_user_ids:
            from remembra.auth.superadmin import account_is_owner

            user_row = await self._user_row(user_id)
            if user_row is not None and account_is_owner(user_row):
                logger.debug("owner_plan_bypass user=%s", user_id)
                return PlanTier.ENTERPRISE

        tenant = await self.get_tenant(user_id)
        if tenant is None:
            return PlanTier.FREE
        if self._trial_expired(tenant):
            return PlanTier.FREE
        try:
            return PlanTier(tenant["plan"])
        except ValueError:
            logger.error("unknown_plan_on_tenant user=%s plan=%s; treating as free", user_id, tenant.get("plan"))
            return PlanTier.FREE

    @staticmethod
    def _is_trial(tenant: dict[str, Any] | None) -> bool:
        return bool(tenant and tenant.get("promo_expires_at") and not tenant.get("stripe_subscription_id"))

    @classmethod
    def _trial_expired(cls, tenant: dict[str, Any]) -> bool:
        """A promo trial ends at promo_expires_at unless a paid subscription exists."""
        if not cls._is_trial(tenant):
            return False
        expires = _parse_dt(tenant.get("promo_expires_at"))
        return expires is not None and expires < now_utc()

    async def update_plan(
        self,
        user_id: str,
        plan: PlanTier,
        stripe_subscription_id: str | None = None,
        promo_expires_at: datetime | None = None,
    ) -> None:
        """Update a tenant's plan (e.g., after a promo code)."""
        now = now_utc().isoformat()
        promo_expires_str = promo_expires_at.isoformat() if promo_expires_at else None

        await self._db.conn.execute(
            """
            UPDATE cloud_tenants
            SET plan = ?,
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                promo_expires_at = COALESCE(?, promo_expires_at),
                updated_at = ?
            WHERE user_id = ?
            """,
            (plan.value, stripe_subscription_id, promo_expires_str, now, user_id),
        )
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Account state (plan + period + effective limits)
    # -----------------------------------------------------------------------

    async def get_account(self, user_id: str, *, include_team: bool = True) -> AccountState:
        """Plan, billing period, seat-scaled limits and the credit limit for ``user_id``.

        A user without a paid plan of their own who is a member of a team whose
        owner holds a pooled plan (Team, legacy Pro/Team, Enterprise) is billed
        to that owner: the returned account is the owner's (ledger, period,
        pooled limits, memory cap across the whole pool) with
        ``member_user_id`` set. ``include_team=False`` returns the user's own
        account only.
        """
        own = await self._own_account(user_id)
        if not include_team or own.tier != PlanTier.FREE:
            return own
        owner = await self._pooled_billing_owner(user_id)
        if owner is None:
            return own
        return replace(owner, member_user_id=user_id)

    async def _own_account(self, user_id: str) -> AccountState:
        settings = get_settings()
        now = now_utc()
        tier = await self.get_tenant_plan(user_id)
        tenant = await self.get_tenant(user_id)
        user_row = await self._user_row(user_id)
        if user_row is not None:
            verified = bool(user_row.get("email_verified"))
        else:
            verified = bool((tenant or {}).get("email_verified"))
        created_at = _parse_dt((user_row or {}).get("created_at")) or _parse_dt((tenant or {}).get("created_at"))

        paid = tier not in (PlanTier.FREE,)
        raw_seats = (tenant or {}).get("seats")
        limits = get_plan(tier).scaled(int(raw_seats) if raw_seats else None)
        interval = BillingInterval.MONTH
        if paid and tenant and tenant.get("billing_interval") == BillingInterval.YEAR.value:
            interval = BillingInterval.YEAR
        full_credit_limit: int | None = None
        bank_unlock_at: datetime | None = None
        if interval == BillingInterval.YEAR:
            anchor = _parse_dt((tenant or {}).get("period_anchor")) or _parse_dt((tenant or {}).get("created_at")) or now
            period = CreditPeriod.yearly(now, anchor)
            released = min(12, int(settings.annual_credit_upfront_months) + _months_elapsed(period.start, now))
            full_credit_limit = limits.credit_allowance(interval, released)
            unlock = _parse_dt((tenant or {}).get("bank_unlock_at"))
            if unlock is not None and now < unlock and period.start <= unlock <= period.end:
                # A new yearly bank: until the refund window closes only a
                # month's worth is spendable (R-27), then the rest unlocks.
                bank_unlock_at = unlock
                released = min(released, int(settings.annual_credit_initial_months))
            credit_limit = limits.credit_allowance(interval, released)
        else:
            period = CreditPeriod.monthly(now)
            credit_limit = limits.credit_allowance(interval)

        if tier == PlanTier.FREE and self._unverified_cap_applies(user_row, tenant, verified, created_at):
            if limits.unverified_credit_cap is not None:
                credit_limit = min(credit_limit, limits.unverified_credit_cap)

        trial = tier != PlanTier.FREE and self._is_trial(tenant)
        pool: tuple[str, ...] = (user_id,)
        if limits.max_users >= _POOLED_MIN_USERS:
            pool = await self._team_pool(user_id, limits.max_users)
        return AccountState(
            user_id=user_id,
            tier=tier,
            limits=limits,
            interval=interval,
            seats=limits.max_users if get_plan(tier).per_seat else int(raw_seats or 1),
            founding=bool((tenant or {}).get("founding")),
            email_verified=verified,
            free_group=tier == PlanTier.FREE or trial,
            period=period,
            credit_limit=credit_limit,
            memory_cap=limits.memory_cap(now, settings.memory_cap_notice_effective_at),
            pool_user_ids=pool,
            created_at=created_at,
            full_credit_limit=full_credit_limit,
            bank_unlock_at=bank_unlock_at,
        )

    @staticmethod
    def _unverified_cap_applies(
        user_row: dict[str, Any] | None,
        tenant: dict[str, Any] | None,
        verified: bool,
        created_at: datetime | None,
    ) -> bool:
        """The 25-credit hold for unverified Free accounts.

        Off until ``unverified_credit_cap_effective_at`` is set (the dashboard
        verify-email flow must be live first); then it applies only to accounts
        created at or after that time (existing users are grandfathered).

        Dashboard accounts verify through ``/auth/verify-email``. Tenants
        provisioned by the master-key ``/cloud/signup`` backend have no user
        record; they verify through ``/cloud/verify-email`` and are held too.
        Any other tenant-only record (admin-assigned plans, legacy rows, no
        email to verify) stays exempt.
        """
        if verified:
            return False
        if user_row is None and not (tenant and tenant.get("signup_source") == TENANT_SIGNUP_SOURCE and tenant.get("email")):
            return False
        effective = get_settings().unverified_credit_cap_effective_at
        if effective is None:
            return False
        effective = effective if effective.tzinfo else effective.replace(tzinfo=UTC)
        return created_at is None or created_at >= effective

    async def _team_rows(self, query: str, params: tuple[Any, ...]) -> list[Any]:
        try:
            cursor = await self._db.conn.execute(query, params)
            return list(await cursor.fetchall())
        except Exception:  # no teams tables in minimal deployments
            return []

    async def _team_pool(self, owner_id: str, seats: int) -> tuple[str, ...]:
        """The owner plus the members of the owner's teams, earliest joiners first, up to the paid seats."""
        rows = await self._team_rows(
            "SELECT m.user_id, MIN(m.joined_at) AS joined FROM team_members m JOIN teams t ON t.id = m.team_id"
            " WHERE t.owner_id = ? AND m.user_id != ? GROUP BY m.user_id ORDER BY joined, m.user_id",
            (owner_id, owner_id),
        )
        members = [str(r[0]) for r in rows if r[0]]
        return (owner_id, *members[: max(0, seats - 1)])

    async def _pooled_billing_owner(self, member_id: str) -> AccountState | None:
        """The account of a team owner with a pooled paid plan that ``member_id`` belongs to."""
        rows = await self._team_rows(
            "SELECT DISTINCT t.owner_id FROM team_members m JOIN teams t ON t.id = m.team_id"
            " WHERE m.user_id = ? AND t.owner_id != ?",
            (member_id, member_id),
        )
        best: AccountState | None = None
        for (owner_id,) in rows:
            if not owner_id:
                continue
            candidate = await self._own_account(str(owner_id))
            if candidate.tier == PlanTier.FREE or candidate.limits.max_users < _POOLED_MIN_USERS:
                continue
            if member_id not in candidate.pool:
                continue
            if best is None or candidate.credit_limit > best.credit_limit:
                best = candidate
        return best

    # -----------------------------------------------------------------------
    # Smart-credit ledger
    # -----------------------------------------------------------------------

    async def _ensure_period(self, user_id: str, period_key: str) -> None:
        await self._db.conn.execute(
            "INSERT OR IGNORE INTO cloud_credit_periods (user_id, period_key) VALUES (?, ?)",
            (user_id, period_key),
        )

    async def get_credit_balance(self, account: AccountState) -> CreditBalance:
        cursor = await self._db.conn.execute(
            "SELECT credits_used, credits_reserved, llm_usd FROM cloud_credit_periods WHERE user_id = ? AND period_key = ?",
            (account.user_id, account.period.key),
        )
        row = await cursor.fetchone()
        used, reserved, usd = (int(row[0]), int(row[1]), float(row[2])) if row else (0, 0, 0.0)
        return CreditBalance(used=used, reserved=reserved, limit=account.credit_limit, llm_usd=usd)

    async def reserve_credits(self, account: AccountState, credits: int, *, min_credits: int) -> str | None:
        """Atomically reserve ``credits`` against the account's period ceiling.

        Returns a reservation id, or None when the reservation does not fit
        (the caller then degrades the write to atomic).
        """
        if credits <= 0:
            raise ValueError("credits must be positive")
        for attempt in range(2):
            reservation_id = uuid.uuid4().hex
            async with self._tx():
                await self._ensure_period(account.user_id, account.period.key)
                cursor = await self._db.conn.execute(
                    """
                    UPDATE cloud_credit_periods SET credits_reserved = credits_reserved + ?
                    WHERE user_id = ? AND period_key = ? AND credits_used + credits_reserved + ? <= ?
                    """,
                    (credits, account.user_id, account.period.key, credits, account.credit_limit),
                )
                if cursor.rowcount:
                    await self._db.conn.execute(
                        """
                        INSERT INTO cloud_credit_reservations
                            (id, user_id, period_key, credits, min_credits, free_group, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            reservation_id,
                            account.user_id,
                            account.period.key,
                            credits,
                            min_credits,
                            1 if account.free_group else 0,
                            now_utc().isoformat(),
                        ),
                    )
                    return reservation_id
            if attempt == 0 and not await self.expire_stale_reservations(user_id=account.user_id):
                break
        return None

    async def settle_reservation(self, reservation_id: str, actual_usd: float, *, enriched: bool = True) -> int:
        """Settle a reservation from actual AI spend; refund the rest. Returns credits charged.

        ``enriched=False`` (the write failed before enrichment ran) charges only
        what was actually spent. Settling an already expired reservation charges
        the difference to its earlier minimum charge. Idempotent for settled ones.
        """
        actual_usd = max(0.0, float(actual_usd))
        now = now_utc()
        async with self._tx():
            cursor = await self._db.conn.execute(
                "SELECT user_id, period_key, credits, min_credits, free_group, status, charged"
                " FROM cloud_credit_reservations WHERE id = ?",
                (reservation_id,),
            )
            row = await cursor.fetchone()
            if row is None or row[5] == "settled":
                return 0
            user_id, period_key, reserved, min_credits, free_group, status, prior_charge = row
            uncapped = charge_for(min_credits, actual_usd) if enriched else credits_for_usd(actual_usd)
            # The reservation is the write's hard AI budget: never charge past it,
            # so credits_used cannot pass the plan ceiling. Spend above the hold
            # (the last call's estimate was short) is a platform loss, logged.
            charged = min(uncapped, int(reserved))
            if uncapped > charged:
                logger.warning(
                    "credit_settle_over_reservation user=%s reservation=%s reserved=%s actual_credits=%s platform_loss_usd=%.5f",
                    user_id,
                    reservation_id,
                    reserved,
                    uncapped,
                    max(0.0, actual_usd - int(reserved) * CREDIT_USD),
                )
            if status == "expired":
                # The hold was already released and the minimum charged.
                delta = max(0, charged - int(prior_charge))
                await self._db.conn.execute(
                    "UPDATE cloud_credit_periods SET credits_used = credits_used + ?, llm_usd = llm_usd + ?"
                    " WHERE user_id = ? AND period_key = ?",
                    (delta, actual_usd, user_id, period_key),
                )
                final_charge = int(prior_charge) + delta
                daily_credits = delta
            else:
                await self._db.conn.execute(
                    """
                    UPDATE cloud_credit_periods SET
                        credits_reserved = MAX(0, credits_reserved - ?),
                        credits_used = credits_used + ?,
                        llm_usd = llm_usd + ?
                    WHERE user_id = ? AND period_key = ?
                    """,
                    (reserved, charged, actual_usd, user_id, period_key),
                )
                final_charge = charged
                daily_credits = charged
            await self._db.conn.execute(
                "UPDATE cloud_credit_reservations SET status = 'settled', charged = ?, actual_usd = ?, settled_at = ?"
                " WHERE id = ?",
                (final_charge, actual_usd, now.isoformat(), reservation_id),
            )
            await self._increment_daily(user_id, now, credits_used=daily_credits, llm_usd=actual_usd)
            if actual_usd > 0:
                await self._add_ai_spend(now, "free" if free_group else "paid", actual_usd)
        logger.info(
            "credit_reservation_settled user=%s reserved=%s charged=%s usd=%.5f",
            user_id,
            reserved,
            final_charge,
            actual_usd,
        )
        return final_charge

    async def record_unreserved_spend(self, account: AccountState, actual_usd: float) -> int:
        """Charge AI spend that ran without a reservation (e.g. sleep-time work). Returns credits.

        The charge never takes the account past its credit limit (the work was
        budgeted by the credits left); any excess is logged as platform loss.
        The real dollars are always recorded.
        """
        if actual_usd <= 0:
            return 0
        wanted = credits_for_usd(actual_usd)
        now = now_utc()
        async with self._tx():
            await self._ensure_period(account.user_id, account.period.key)
            balance = await self.get_credit_balance(account)
            charged = min(wanted, balance.remaining)
            if wanted > charged:
                logger.warning("unreserved_spend_over_limit user=%s credits=%s charged=%s", account.user_id, wanted, charged)
            await self._db.conn.execute(
                "UPDATE cloud_credit_periods SET credits_used = credits_used + ?, llm_usd = llm_usd + ?"
                " WHERE user_id = ? AND period_key = ?",
                (charged, actual_usd, account.user_id, account.period.key),
            )
            await self._increment_daily(account.user_id, now, credits_used=charged, llm_usd=actual_usd)
            await self._add_ai_spend(now, "free" if account.free_group else "paid", actual_usd)
        return charged

    async def expire_stale_reservations(
        self,
        *,
        user_id: str | None = None,
        older_than: timedelta | None = None,
        created_before: datetime | None = None,
    ) -> int:
        """Release reservations whose work never settled (lost task / restart).

        The hold is released and the chunk minimum is charged; if the work does
        finish later, :meth:`settle_reservation` charges the difference (capped
        at the reservation). A reservation whose spend job is still alive in
        this process is never expired, however old: its work is queued or
        running and will settle. ``created_before`` (startup) expires the holds
        of a previous process: everything opened before this one started.
        """
        from remembra.core import ai_spend

        if created_before is not None:
            cutoff = created_before.isoformat()
        else:
            if older_than is None:
                older_than = timedelta(minutes=get_settings().credit_reservation_stale_minutes)
            cutoff = (now_utc() - older_than).isoformat()
        query = (
            "SELECT id, user_id, period_key, credits, min_credits FROM cloud_credit_reservations"
            " WHERE status = 'open' AND created_at < ?"
        )
        params: list[Any] = [cutoff]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        cursor = await self._db.conn.execute(query, params)
        rows = [row for row in await cursor.fetchall() if not ai_spend.is_live_reservation(str(row[0]))]
        if not rows:
            return 0
        now = now_utc()
        async with self._tx():
            for rid, uid, period_key, reserved, min_credits in rows:
                cursor = await self._db.conn.execute(
                    "UPDATE cloud_credit_reservations SET status = 'expired', charged = ?, settled_at = ?"
                    " WHERE id = ? AND status = 'open'",
                    (min_credits, now.isoformat(), rid),
                )
                if not cursor.rowcount:
                    continue
                await self._db.conn.execute(
                    "UPDATE cloud_credit_periods SET credits_reserved = MAX(0, credits_reserved - ?),"
                    " credits_used = credits_used + ? WHERE user_id = ? AND period_key = ?",
                    (reserved, min_credits, uid, period_key),
                )
                await self._increment_daily(uid, now, credits_used=min_credits)
        logger.warning("credit_reservations_expired count=%s", len(rows))
        return len(rows)

    # -----------------------------------------------------------------------
    # Global free-tier circuit breaker
    # -----------------------------------------------------------------------

    async def _add_ai_spend(self, now: datetime, group: str, usd: float) -> None:
        await self._db.conn.execute(
            """
            INSERT INTO cloud_ai_spend_monthly (month, grp, usd) VALUES (?, ?, ?)
            ON CONFLICT(month, grp) DO UPDATE SET usd = usd + excluded.usd
            """,
            (month_key(now), group, usd),
        )

    async def ai_spend_month(self, group: str, now: datetime | None = None) -> float:
        now = now or now_utc()
        cursor = await self._db.conn.execute(
            "SELECT usd FROM cloud_ai_spend_monthly WHERE month = ? AND grp = ?", (month_key(now), group)
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def record_revenue(self, transaction_id: str, net_usd: float, at: datetime | None = None) -> bool:
        """Record net paid revenue for one Paddle transaction (idempotent). Returns True if new."""
        at = at or now_utc()
        cursor = await self._db.conn.execute(
            "INSERT OR IGNORE INTO cloud_revenue_events (transaction_id, month, net_usd, recorded_at) VALUES (?, ?, ?, ?)",
            (transaction_id, month_key(at), float(net_usd), now_utc().isoformat()),
        )
        await self._db.conn.commit()
        return bool(cursor.rowcount)

    async def revenue_for_month(self, month: str) -> float | None:
        """Net paid revenue recorded for ``month`` (YYYY-MM), or None when nothing is known."""
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_usd), 0) FROM cloud_revenue_events WHERE month = ?", (month,)
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        return float(row[1])

    async def free_breaker_budget(self, now: datetime | None = None) -> float:
        """max(floor, pct x last month's net paid revenue if known, else floor)."""
        settings = get_settings()
        now = now or now_utc()
        last_month = month_key(_month_start(now) - timedelta(days=1))
        revenue = await self.revenue_for_month(last_month)
        floor = float(settings.free_breaker_min_usd)
        if revenue is None:
            return floor
        return max(floor, float(settings.free_breaker_revenue_pct) * revenue)

    async def free_tier_spend(self, now: datetime | None = None) -> float:
        """Free-group AI spend this month, including free reservations not settled yet.

        Open holds count at their full size, and so do expired ones (released
        from the account but not yet settled: their work may have spent up to
        the hold and a late settle only then records the real dollars).
        """
        now = now or now_utc()
        spent = await self.ai_spend_month("free", now)
        cursor = await self._db.conn.execute(
            "SELECT COALESCE(SUM(credits), 0) FROM cloud_credit_reservations"
            " WHERE status IN ('open', 'expired') AND free_group = 1 AND created_at >= ?",
            (_month_start(now).isoformat(),),
        )
        row = await cursor.fetchone()
        held = float(row[0]) * CREDIT_USD if row else 0.0
        return spent + held

    async def free_breaker_open(self, now: datetime | None = None) -> bool:
        """True while this month's free-tier AI spend has reached the budget (resets next month)."""
        if not get_settings().free_breaker_enabled:
            return False
        now = now or now_utc()
        return await self.free_tier_spend(now) >= await self.free_breaker_budget(now)

    # -----------------------------------------------------------------------
    # AI spend outside a metered write (ai_spend.AttributionPolicy)
    # -----------------------------------------------------------------------

    async def allow_unattributed_ai(self, user_id: str) -> bool:
        """Optional paid AI outside a write (Jev on a recall): never for the free group.

        Recalls never use credits, so for Free accounts (unbounded in number)
        that spend would be invisible to every ceiling; they just skip it.
        """
        account = await self.get_account(user_id)
        return not account.free_group

    async def record_unattributed_ai(self, user_id: str, usd: float) -> None:
        """Count AI dollars spent outside a write in the monthly group totals (no credits)."""
        if usd <= 0:
            return
        account = await self.get_account(user_id)
        now = now_utc()
        async with self._tx():
            await self._add_ai_spend(now, "free" if account.free_group else "paid", usd)
            await self._increment_daily(account.user_id, now, llm_usd=usd)

    # -----------------------------------------------------------------------
    # Unenriched writes (atomic / degraded / relay): daily cap + embedding spend
    # -----------------------------------------------------------------------

    async def take_unenriched_writes(self, account: AccountState, count: int) -> bool:
        """Count ``count`` unenriched writes for today; False when the plan's daily cap would be passed.

        Atomic (check and increment in one UPDATE), so concurrent requests
        cannot overshoot the cap. Plans without a cap always succeed.
        """
        if count <= 0:
            return True
        cap = account.limits.max_unenriched_writes_per_day
        today = now_utc().strftime("%Y-%m-%d")
        async with self._tx():
            await self._db.conn.execute(
                "INSERT OR IGNORE INTO cloud_usage_daily (user_id, date) VALUES (?, ?)", (account.user_id, today)
            )
            if cap is None:
                await self._db.conn.execute(
                    "UPDATE cloud_usage_daily SET unenriched_writes = COALESCE(unenriched_writes, 0) + ?"
                    " WHERE user_id = ? AND date = ?",
                    (count, account.user_id, today),
                )
                return True
            cursor = await self._db.conn.execute(
                "UPDATE cloud_usage_daily SET unenriched_writes = COALESCE(unenriched_writes, 0) + ?"
                " WHERE user_id = ? AND date = ? AND COALESCE(unenriched_writes, 0) + ? <= ?",
                (count, account.user_id, today, count, cap),
            )
            return bool(cursor.rowcount)

    @staticmethod
    def embedding_usd(texts: list[str], model: str | None = None) -> float:
        """Estimated embedding cost of ``texts`` (characters / 4 tokens at the model's list price)."""
        name = (model or get_settings().embedding_model or "").strip().lower()
        price = EMBEDDING_PRICES_PER_M.get(name, UNKNOWN_EMBEDDING_PRICE_PER_M)
        tokens = sum(len(t or "") for t in texts) / EMBEDDING_CHARS_PER_TOKEN
        return tokens * price / 1_000_000

    async def record_embedding_spend(self, account: AccountState, texts: list[str]) -> float:
        """Add the embedding cost of a write to the group's monthly AI spend (free breaker). No credits."""
        usd = self.embedding_usd(texts)
        if usd <= 0:
            return 0.0
        now = now_utc()
        async with self._tx():
            await self._add_ai_spend(now, "free" if account.free_group else "paid", usd)
        return usd

    # -----------------------------------------------------------------------
    # Founding 100 and billing flags
    # -----------------------------------------------------------------------

    async def _founding_seats_taken(self, now: datetime, *, excluding: str | None = None) -> int:
        """Seats in use: founders holding the price plus live holds (checkouts in progress, recent lapses)."""
        cursor = await self._db.conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM cloud_tenants WHERE founding = 1)
                 + (SELECT COUNT(*) FROM founding_holds h
                    WHERE h.until > ? AND h.user_id != ?
                      AND NOT EXISTS (SELECT 1 FROM cloud_tenants t WHERE t.user_id = h.user_id AND t.founding = 1))
            """,
            (now.isoformat(), excluding or ""),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def founding_seats_taken(self, *, excluding: str | None = None) -> int:
        """Founding 100 seats in use now (holders, open checkouts and founders inside the 14-day lapse grace).

        ``excluding``: leave out that account's own hold (its seat is already its own).
        """
        return await self._founding_seats_taken(now_utc(), excluding=excluding)

    async def founding_hold_of(self, user_id: str) -> tuple[str, datetime] | None:
        """``(kind, until)`` of the live seat hold ``user_id`` has without holding the price, else None."""
        cursor = await self._db.conn.execute(
            """
            SELECT h.kind, h.until FROM founding_holds h
            WHERE h.user_id = ? AND NOT EXISTS (SELECT 1 FROM cloud_tenants t WHERE t.user_id = h.user_id AND t.founding = 1)
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        until = _parse_dt(row[1]) if row else None
        if row is None or until is None or until <= now_utc():
            return None
        return str(row[0]), until

    async def end_founding(self, user_id: str) -> None:
        """The account stops holding the Founding price; its seat stays held 14 days (the lapse grace)."""
        now = now_utc()
        async with self._tx():
            cursor = await self._db.conn.execute("SELECT COALESCE(founding, 0) FROM cloud_tenants WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if not (row and row[0]):
                return
            await self._db.conn.execute(
                "INSERT OR REPLACE INTO founding_holds (user_id, kind, until, created_at) VALUES (?, 'lapsed', ?, ?)",
                (user_id, (now + FOUNDING_LAPSE_GRACE).isoformat(), now.isoformat()),
            )
            await self._db.conn.execute(
                "UPDATE cloud_tenants SET founding = 0, updated_at = ? WHERE user_id = ?", (now.isoformat(), user_id)
            )

    async def hold_founding_seat(self, user_id: str) -> FoundingSeatHold | None:
        """Reserve a Founding 100 seat for a checkout (atomic). None when all 100 are taken.

        Checkouts in progress count against the cap, so the 101st buyer is
        refused before paying instead of after. A founder inside the 14-day
        lapse grace already holds a seat and may buy the price back. The
        returned hold remembers the row it replaced, so a checkout that cannot
        be created gives it back (:meth:`release_founding_hold`).
        """
        now = now_utc()
        async with self._tx():
            cursor = await self._db.conn.execute(
                "SELECT kind, until, transaction_id, created_at FROM founding_holds WHERE user_id = ?", (user_id,)
            )
            existing = await cursor.fetchone()
            until = now + FOUNDING_CHECKOUT_HOLD
            created = now
            live = existing is not None and (_parse_dt(existing[1]) or now) > now
            if existing is not None and str(existing[0]) == "pending":
                # One checkout hold per account per day, never extended: re-opening
                # checkout every two hours must not keep a seat forever.
                first = _parse_dt(existing[3]) or now
                if live:
                    until, created = _parse_dt(existing[1]) or until, first
                elif now - first < FOUNDING_HOLD_WINDOW:
                    raise FoundingHoldLimitError(first + FOUNDING_HOLD_WINDOW)
            elif live and existing is not None:
                until = max(until, _parse_dt(existing[1]) or until)  # a lapsed founder keeps the 14-day grace
            if not live and await self._founding_seats_taken(now, excluding=user_id) >= FOUNDING_MAX_REDEMPTIONS:
                return None
            await self._db.conn.execute(
                """
                INSERT INTO founding_holds (user_id, kind, until, created_at) VALUES (?, 'pending', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET kind = 'pending', until = excluded.until, created_at = excluded.created_at
                """,
                (user_id, until.isoformat(), created.isoformat()),
            )
        prior = (str(existing[0]), str(existing[1]), existing[2], str(existing[3])) if existing is not None else None
        return FoundingSeatHold(user_id=user_id, until=until.isoformat(), prior=prior)

    async def drop_founding_holds(self, user_id: str) -> None:
        """Give back any seat ``user_id`` holds without holding the price (an open checkout or a
        lapse grace): a deleted account will not buy, so its seat goes to the next buyer."""
        async with self._tx():
            await self._db.conn.execute("DELETE FROM founding_holds WHERE user_id = ?", (user_id,))

    async def set_founding_hold_transaction(self, user_id: str, transaction_id: str) -> None:
        await self._db.conn.execute(
            "UPDATE founding_holds SET transaction_id = ? WHERE user_id = ? AND kind = 'pending'", (transaction_id, user_id)
        )
        await self._db.conn.commit()

    async def release_founding_hold(self, hold: FoundingSeatHold) -> None:
        """Undo ``hold`` because its checkout will not be paid (the Paddle transaction could not be created).

        The row it replaced comes back: a lapsed founder keeps the 14-day
        grace and an earlier open checkout keeps its hold. A hold this request
        did not set (a later checkout replaced it, or a payment claimed the
        seat) is left alone.
        """
        async with self._tx():
            if hold.prior is None:
                await self._db.conn.execute(
                    "DELETE FROM founding_holds WHERE user_id = ? AND kind = 'pending' AND until = ?",
                    (hold.user_id, hold.until),
                )
            else:
                kind, until, transaction_id, created_at = hold.prior
                await self._db.conn.execute(
                    "UPDATE founding_holds SET kind = ?, until = ?, transaction_id = ?, created_at = ?"
                    " WHERE user_id = ? AND kind = 'pending' AND until = ?",
                    (kind, until, transaction_id, created_at, hold.user_id, hold.until),
                )

    async def claim_founding(self, user_id: str) -> bool:
        """Mark ``user_id`` as holding the Founding 100 price if it has a seat (atomic). True if it holds one.

        A holder keeps it (renewals). The account's own live hold (a checkout
        it started, or its 14-day lapse grace) is converted; otherwise a free
        seat is taken only while fewer than 100 are in use. The count and the
        flag change in one transaction, so concurrent webhooks cannot pass the cap.
        """
        now = now_utc()
        async with self._tx():
            tenant = await self.get_tenant(user_id)
            if tenant is None:
                return False
            if tenant.get("founding"):
                return True
            cursor = await self._db.conn.execute("SELECT until FROM founding_holds WHERE user_id = ?", (user_id,))
            hold = await cursor.fetchone()
            held = hold is not None and (_parse_dt(hold[0]) or now) > now
            if not held and await self._founding_seats_taken(now, excluding=user_id) >= FOUNDING_MAX_REDEMPTIONS:
                return False
            await self._db.conn.execute(
                "UPDATE cloud_tenants SET founding = 1, updated_at = ? WHERE user_id = ?", (now.isoformat(), user_id)
            )
            await self._db.conn.execute("DELETE FROM founding_holds WHERE user_id = ?", (user_id,))
        return True

    async def set_billing_flag(self, user_id: str, flag: str | None) -> None:
        await self._db.conn.execute(
            "UPDATE cloud_tenants SET billing_flag = ?, updated_at = ? WHERE user_id = ?",
            (flag, now_utc().isoformat(), user_id),
        )
        await self._db.conn.commit()

    async def list_billing_flags(self) -> list[dict[str, Any]]:
        """Every account with a billing flag the owner has not cleared, most recent first."""
        columns = (
            "t.user_id, {email} AS email, t.plan, t.billing_flag, t.billing_interval, t.seats,"
            " COALESCE(t.founding, 0) AS founding, t.stripe_customer_id, t.stripe_subscription_id, t.updated_at"
        )
        where = "WHERE t.billing_flag IS NOT NULL AND t.billing_flag != '' ORDER BY t.updated_at DESC"
        try:
            cursor = await self._db.conn.execute(
                f"SELECT {columns.format(email='COALESCE(u.email, t.email)')}"
                f" FROM cloud_tenants t LEFT JOIN users u ON u.id = t.user_id {where}"
            )
        except Exception:  # no users table in minimal deployments
            cursor = await self._db.conn.execute(f"SELECT {columns.format(email='t.email')} FROM cloud_tenants t {where}")
        return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def active_subscription_id(tenant: dict[str, Any] | None) -> str | None:
        """The paid subscription a tenant row holds right now, or None.

        A tenant on a paid plan with a recorded subscription id holds it until
        a cancel, refund or chargeback for that id drops it to Free (the id is
        kept after that, but no longer counts). Promo trials record no id.
        """
        if not tenant:
            return None
        held = tenant.get("stripe_subscription_id")
        if not held or str(tenant.get("plan") or PlanTier.FREE.value) == PlanTier.FREE.value:
            return None
        return str(held)

    async def tenants_for_billing(self, *, subscription_id: str | None = None, customer_id: str | None = None) -> list[str]:
        """Every account recording this Paddle subscription id, or this customer id (one payer can pay for several)."""
        column, value = ("stripe_subscription_id", subscription_id) if subscription_id else ("stripe_customer_id", customer_id)
        if not value:
            return []
        cursor = await self._db.conn.execute(f"SELECT user_id FROM cloud_tenants WHERE {column} = ?", (value,))
        return [str(row[0]) for row in await cursor.fetchall()]

    async def find_tenant_by_billing_ids(self, *, subscription_id: str | None, customer_id: str | None) -> str | None:
        """The user id holding a Paddle subscription (preferred) or customer id."""
        for column, value in (("stripe_subscription_id", subscription_id), ("stripe_customer_id", customer_id)):
            if not value:
                continue
            cursor = await self._db.conn.execute(
                f"SELECT user_id FROM cloud_tenants WHERE {column} = ? ORDER BY updated_at DESC LIMIT 1", (value,)
            )
            row = await cursor.fetchone()
            if row:
                return str(row[0])
        return None

    # -----------------------------------------------------------------------
    # Usage tracking
    # -----------------------------------------------------------------------

    async def record_store(self, user_id: str, count: int = 1) -> None:
        """Record ``count`` memory store events."""
        await self._increment(user_id, "stores", count)

    async def record_recall(self, user_id: str, count: int = 1) -> None:
        """Record ``count`` memory recall events."""
        await self._increment(user_id, "recalls", count)

    async def record_delete(self, user_id: str) -> None:
        """Record a memory delete event."""
        await self._increment(user_id, "deletes")

    async def record_relay_event(self, user_id: str, count: int = 1) -> None:
        """Record relay events (handoff / checkpoint / status / inbox). Never touches credits."""
        await self._increment(user_id, "relay_events", count)

    async def record_degraded_store(self, user_id: str, count: int = 1) -> None:
        """Record stores that were saved atomically because enrichment was unavailable."""
        await self._increment(user_id, "degraded_stores", count)

    async def _increment_daily(self, user_id: str, now: datetime, *, credits_used: int = 0, llm_usd: float = 0.0) -> None:
        if credits_used <= 0 and llm_usd <= 0:
            return
        await self._db.conn.execute(
            """
            INSERT INTO cloud_usage_daily (user_id, date, credits_used, llm_usd)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                credits_used = COALESCE(credits_used, 0) + excluded.credits_used,
                llm_usd = COALESCE(llm_usd, 0) + excluded.llm_usd
            """,
            (user_id, now.strftime("%Y-%m-%d"), max(0, credits_used), max(0.0, llm_usd)),
        )

    async def _increment(self, user_id: str, column: str, amount: int = 1) -> None:
        """Increment a daily usage counter by ``amount``."""
        if column not in ("stores", "recalls", "deletes", "relay_events", "degraded_stores"):
            raise ValueError(f"Unknown usage counter: {column}")
        if amount <= 0:
            return
        today = now_utc().strftime("%Y-%m-%d")
        await self._db.conn.execute(
            f"""
            INSERT INTO cloud_usage_daily (user_id, date, {column})
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                {column} = COALESCE({column}, 0) + excluded.{column}
            """,
            (user_id, today, amount),
        )
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Usage queries
    # -----------------------------------------------------------------------

    async def count_memories(self, user_id: str) -> int:
        cursor = await self._db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _count(self, sql: str, params: list[Any]) -> int:
        cursor = await self._db.conn.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_pool_memories(self, account: AccountState) -> int:
        """Memories counted toward the account's memory cap, over every user sharing it (a team pool).

        Everything a session close writes is left out, superseded versions
        included: relay handoffs and checkpoints, and the relay's own
        ``last_agent:`` / ``branch:`` status values (a new value per close).
        Handoffs are free on every plan, so closing sessions must never fill the
        cap and block a note. See :meth:`count_pool_relay_records`.

        Runs on every store: all rows are counted from ``idx_memories_user``
        and the relay-written ones, reached through ``idx_memories_user_type``,
        are subtracted, so only those rows are read.
        """
        pool = list(account.pool)
        marks = ",".join("?" for _ in pool)
        types = ",".join("?" for _ in RELAY_WRITTEN_TYPES)
        total = await self._count(f"SELECT COUNT(*) FROM memories WHERE user_id IN ({marks})", pool)  # noqa: S608
        relay = await self._count(
            f"SELECT COUNT(*) FROM memories WHERE user_id IN ({marks}) AND memory_type IN ({types})"  # noqa: S608
            f" AND {RELAY_WRITTEN_SQL}",
            [*pool, *RELAY_WRITTEN_TYPES],
        )
        return total - relay

    async def count_pool_relay_records(self, account: AccountState) -> int:
        """Relay handoffs and checkpoints stored by the account's pool (not counted toward the cap)."""
        pool = list(account.pool)
        marks = ",".join("?" for _ in pool)
        return await self._count(
            f"SELECT COUNT(*) FROM memories WHERE user_id IN ({marks}) AND memory_type IN ('handoff', 'checkpoint')"  # noqa: S608
            f" AND {RELAY_RECORD_SQL}",
            pool,
        )

    async def project_exists(self, user_id: str, project_id: str) -> bool:
        """Whether ``project_id`` already counts toward the project limit (see :meth:`count_projects`)."""
        cursor = await self._db.conn.execute(
            f"SELECT 1 FROM memories WHERE user_id = ? AND project_id = ? AND NOT {RELAY_WRITTEN_SQL} LIMIT 1",  # noqa: S608
            (user_id, project_id),
        )
        return await cursor.fetchone() is not None

    async def count_projects(self, user_id: str) -> int:
        """Projects counted toward the plan's project limit: those holding anything besides what
        session closes write (a project with only handoffs and relay status is free, like handoffs)."""
        return await self._count(
            f"SELECT COUNT(DISTINCT project_id) FROM memories WHERE user_id = ? AND NOT {RELAY_WRITTEN_SQL}",  # noqa: S608
            [user_id],
        )

    async def get_period_counters(
        self, user_id: str | tuple[str, ...], start: datetime, end: datetime | None = None
    ) -> dict[str, int]:
        """Summed daily counters (stores, recalls, relay events, degraded stores) in [start, end).

        ``user_id`` may be a tuple of users (a team pool): their counters are summed.
        """
        users = (user_id,) if isinstance(user_id, str) else tuple(user_id)
        marks = ",".join("?" for _ in users)
        query = f"""
            SELECT
                COALESCE(SUM(stores), 0), COALESCE(SUM(recalls), 0),
                COALESCE(SUM(relay_events), 0), COALESCE(SUM(degraded_stores), 0)
            FROM cloud_usage_daily WHERE user_id IN ({marks}) AND date >= ?
        """
        params: list[Any] = [*users, start.strftime("%Y-%m-%d")]
        if end is not None:
            query += " AND date < ?"
            params.append(end.strftime("%Y-%m-%d"))
        cursor = await self._db.conn.execute(query, params)
        row = await cursor.fetchone()
        stores, recalls, relay, degraded = (int(v) for v in row) if row else (0, 0, 0, 0)
        return {"stores": stores, "recalls": recalls, "relay_events": relay, "degraded_stores": degraded}

    async def get_usage_snapshot(self, user_id: str) -> UsageSnapshot:
        """Usage snapshot for plan enforcement (effective, seat-scaled limits)."""
        account = await self.get_account(user_id)
        month = await self.get_period_counters(user_id, _month_start(now_utc()))

        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND active = TRUE",
            (user_id,),
        )
        row = await cursor.fetchone()
        api_keys_active = row[0] if row else 0

        return UsageSnapshot(
            user_id=user_id,
            plan=account.tier,
            memories_stored=await self.count_pool_memories(account),
            recalls_this_month=month["recalls"],
            stores_this_month=month["stores"],
            api_keys_active=api_keys_active,
            limits=account.limits,
            max_memories=account.memory_cap,
        )

    async def get_monthly_usage(
        self,
        user_id: str,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, Any]:
        """Get aggregate usage for a specific month (defaults to the current month)."""
        now = now_utc()
        year = year or now.year
        month = month or now.month
        month_start = f"{year:04d}-{month:02d}-01"
        month_end = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"

        cursor = await self._db.conn.execute(
            """
            SELECT
                COALESCE(SUM(stores), 0) as total_stores,
                COALESCE(SUM(recalls), 0) as total_recalls,
                COALESCE(SUM(deletes), 0) as total_deletes,
                COUNT(DISTINCT date) as active_days,
                COALESCE(SUM(relay_events), 0),
                COALESCE(SUM(credits_used), 0),
                COALESCE(SUM(degraded_stores), 0)
            FROM cloud_usage_daily
            WHERE user_id = ? AND date >= ? AND date < ?
            """,
            (user_id, month_start, month_end),
        )
        row = await cursor.fetchone()

        return {
            "user_id": user_id,
            "period": f"{year:04d}-{month:02d}",
            "stores": row[0] if row else 0,
            "recalls": row[1] if row else 0,
            "deletes": row[2] if row else 0,
            "active_days": row[3] if row else 0,
            "relay_events": row[4] if row else 0,
            "credits_used": row[5] if row else 0,
            "degraded_stores": row[6] if row else 0,
        }

    async def get_daily_usage(
        self,
        user_id: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Get daily usage breakdown for the last N days."""
        cursor = await self._db.conn.execute(
            """
            SELECT date, stores, recalls, deletes, relay_events, credits_used, degraded_stores
            FROM cloud_usage_daily
            WHERE user_id = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (user_id, days),
        )
        rows = await cursor.fetchall()
        return [
            {
                "date": row["date"],
                "stores": row["stores"] or 0,
                "recalls": row["recalls"] or 0,
                "deletes": row["deletes"] or 0,
                "relay_events": row["relay_events"] or 0,
                "credits_used": row["credits_used"] or 0,
                "degraded_stores": row["degraded_stores"] or 0,
            }
            for row in rows
        ]
