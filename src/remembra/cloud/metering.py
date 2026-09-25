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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from remembra.cloud.plans import (
    CREDIT_USD,
    BillingInterval,
    PlanLimits,
    PlanTier,
    UsageSnapshot,
    charge_for,
    credits_for_usd,
    get_plan,
)
from remembra.config import get_settings

logger = logging.getLogger(__name__)

_LEGACY_MIGRATION = "2026_09_plans_v2_legacy_tiers"


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
        for column in ("relay_events", "credits_used", "degraded_stores"):
            await self._add_column("cloud_usage_daily", f"{column} INTEGER DEFAULT 0")
        await self._add_column("cloud_usage_daily", "llm_usd REAL DEFAULT 0")

        await self._migrate_legacy_tiers()

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
    ) -> None:
        """Apply a verified billing event: plan, interval, seats, yearly-bank anchor.

        ``founding`` is sticky (a redemption record for the Founding 100 cap).
        Falling back to Free clears the interval and seat count.
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
            await self._db.conn.execute(
                "UPDATE cloud_tenants SET billing_interval = NULL, seats = NULL, period_anchor = NULL,"
                " updated_at = ? WHERE user_id = ?",
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
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    interval.value if interval else None,
                    seats,
                    anchor.isoformat() if anchor else tenant.get("period_anchor"),
                    1 if founding else 0,
                    now.isoformat(),
                    user_id,
                ),
            )
        await self._db.conn.commit()

    async def founding_redemptions(self) -> int:
        """Accounts that ever bought the Founding 100 price."""
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

    async def get_account(self, user_id: str) -> AccountState:
        """Plan, billing period, seat-scaled limits and the credit limit for ``user_id``."""
        settings = get_settings()
        now = now_utc()
        tier = await self.get_tenant_plan(user_id)
        tenant = await self.get_tenant(user_id)
        user_row = await self._user_row(user_id)
        verified = bool(user_row and user_row.get("email_verified"))

        paid = tier not in (PlanTier.FREE,)
        seats = int((tenant or {}).get("seats") or 1)
        limits = get_plan(tier).scaled(seats)
        interval = BillingInterval.MONTH
        if paid and tenant and tenant.get("billing_interval") == BillingInterval.YEAR.value:
            interval = BillingInterval.YEAR
        if interval == BillingInterval.YEAR:
            anchor = _parse_dt((tenant or {}).get("period_anchor")) or _parse_dt((tenant or {}).get("created_at")) or now
            period = CreditPeriod.yearly(now, anchor)
        else:
            period = CreditPeriod.monthly(now)

        credit_limit = limits.credit_allowance(interval)
        if tier == PlanTier.FREE and not verified and limits.unverified_credit_cap is not None:
            credit_limit = min(credit_limit, limits.unverified_credit_cap)

        trial = tier != PlanTier.FREE and self._is_trial(tenant)
        return AccountState(
            user_id=user_id,
            tier=tier,
            limits=limits,
            interval=interval,
            seats=limits.max_users if get_plan(tier).per_seat else seats,
            founding=bool((tenant or {}).get("founding")),
            email_verified=verified,
            free_group=tier == PlanTier.FREE or trial,
            period=period,
            credit_limit=credit_limit,
            memory_cap=limits.memory_cap(now, settings.memory_cap_notice_effective_at),
        )

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
            charged = charge_for(min_credits, actual_usd) if enriched else credits_for_usd(actual_usd)
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
        """Charge AI spend that ran without a reservation (e.g. sleep-time work). Returns credits."""
        if actual_usd <= 0:
            return 0
        charged = credits_for_usd(actual_usd)
        now = now_utc()
        async with self._tx():
            await self._ensure_period(account.user_id, account.period.key)
            await self._db.conn.execute(
                "UPDATE cloud_credit_periods SET credits_used = credits_used + ?, llm_usd = llm_usd + ?"
                " WHERE user_id = ? AND period_key = ?",
                (charged, actual_usd, account.user_id, account.period.key),
            )
            await self._increment_daily(account.user_id, now, credits_used=charged, llm_usd=actual_usd)
            await self._add_ai_spend(now, "free" if account.free_group else "paid", actual_usd)
        return charged

    async def expire_stale_reservations(self, *, user_id: str | None = None, older_than: timedelta | None = None) -> int:
        """Release reservations whose work never settled (lost task / restart).

        The hold is released and the chunk minimum is charged; if the work does
        finish later, :meth:`settle_reservation` charges the difference.
        """
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
        rows = await cursor.fetchall()
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
        """Free-group AI spend this month, including open (not yet settled) free reservations."""
        now = now or now_utc()
        spent = await self.ai_spend_month("free", now)
        cursor = await self._db.conn.execute(
            "SELECT COALESCE(SUM(credits), 0) FROM cloud_credit_reservations"
            " WHERE status = 'open' AND free_group = 1 AND created_at >= ?",
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

    async def project_exists(self, user_id: str, project_id: str) -> bool:
        cursor = await self._db.conn.execute(
            "SELECT 1 FROM memories WHERE user_id = ? AND project_id = ? LIMIT 1", (user_id, project_id)
        )
        return await cursor.fetchone() is not None

    async def count_projects(self, user_id: str) -> int:
        cursor = await self._db.conn.execute("SELECT COUNT(DISTINCT project_id) FROM memories WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_period_counters(self, user_id: str, start: datetime, end: datetime | None = None) -> dict[str, int]:
        """Summed daily counters (stores, recalls, relay events, degraded stores) in [start, end)."""
        query = """
            SELECT
                COALESCE(SUM(stores), 0), COALESCE(SUM(recalls), 0),
                COALESCE(SUM(relay_events), 0), COALESCE(SUM(degraded_stores), 0)
            FROM cloud_usage_daily WHERE user_id = ? AND date >= ?
        """
        params: list[Any] = [user_id, start.strftime("%Y-%m-%d")]
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
            memories_stored=await self.count_memories(user_id),
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
