"""
Usage metering for Remembra Cloud.

Tracks per-tenant usage of memories stored, recalls, API keys,
and storage. Used for plan limit enforcement and billing.

Data is stored in SQLite alongside the main database.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from remembra.cloud.plans import PlanTier, UsageSnapshot

logger = logging.getLogger(__name__)


class UsageMeter:
    """Tracks and queries usage metrics per tenant.

    Usage data is stored in the main SQLite database in a
    `cloud_usage` table that records daily aggregates.

    Args:
        db: The application's Database instance.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def init_schema(self) -> None:
        """Create usage tracking tables if they don't exist."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS cloud_tenants (
                user_id TEXT PRIMARY KEY,
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
        """)
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Tenant management
    # -----------------------------------------------------------------------

    async def register_tenant(
        self,
        user_id: str,
        plan: PlanTier = PlanTier.FREE,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
    ) -> None:
        """Register a new tenant or update existing."""
        now = datetime.now(UTC).isoformat()
        await self._db.conn.execute(
            """
            INSERT INTO cloud_tenants (
                user_id, plan, stripe_customer_id, stripe_subscription_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                plan = excluded.plan,
                stripe_customer_id = COALESCE(excluded.stripe_customer_id, cloud_tenants.stripe_customer_id),
                stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, cloud_tenants.stripe_subscription_id),
                updated_at = excluded.updated_at
            """,
            (user_id, plan.value, stripe_customer_id, stripe_subscription_id, now, now),
        )
        await self._db.conn.commit()

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

    async def get_tenant_plan(self, user_id: str) -> PlanTier:
        """Get the plan tier for a user. Returns FREE if not registered."""
        tenant = await self.get_tenant(user_id)
        if tenant is None:
            return PlanTier.FREE
        return PlanTier(tenant["plan"])

    async def update_plan(
        self,
        user_id: str,
        plan: PlanTier,
        stripe_subscription_id: str | None = None,
    ) -> None:
        """Update a tenant's plan (e.g., after Stripe webhook)."""
        now = datetime.now(UTC).isoformat()
        await self._db.conn.execute(
            """
            UPDATE cloud_tenants
            SET plan = ?, stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                updated_at = ?
            WHERE user_id = ?
            """,
            (plan.value, stripe_subscription_id, now, user_id),
        )
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Usage tracking
    # -----------------------------------------------------------------------

    async def record_store(self, user_id: str) -> None:
        """Record a memory store event."""
        await self._increment(user_id, "stores")

    async def record_recall(self, user_id: str) -> None:
        """Record a memory recall event."""
        await self._increment(user_id, "recalls")

    async def record_delete(self, user_id: str) -> None:
        """Record a memory delete event."""
        await self._increment(user_id, "deletes")

    async def _increment(self, user_id: str, column: str) -> None:
        """Increment a daily usage counter."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await self._db.conn.execute(
            f"""
            INSERT INTO cloud_usage_daily (user_id, date, {column})
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET
                {column} = {column} + 1
            """,
            (user_id, today),
        )
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Usage queries
    # -----------------------------------------------------------------------

    async def get_usage_snapshot(self, user_id: str) -> UsageSnapshot:
        """Get a complete usage snapshot for plan enforcement.

        Queries:
        - Total memories stored (from memories table)
        - This month's stores and recalls (from usage table)
        - Active API key count (from api_keys table)
        - Current plan tier (from tenants table)
        """
        plan = await self.get_tenant_plan(user_id)

        # Total memories
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        memories_stored = row[0] if row else 0

        # This month's usage
        month_start = datetime.now(UTC).strftime("%Y-%m-01")
        cursor = await self._db.conn.execute(
            """
            SELECT
                COALESCE(SUM(stores), 0) as total_stores,
                COALESCE(SUM(recalls), 0) as total_recalls
            FROM cloud_usage_daily
            WHERE user_id = ? AND date >= ?
            """,
            (user_id, month_start),
        )
        row = await cursor.fetchone()
        stores_this_month = row[0] if row else 0
        recalls_this_month = row[1] if row else 0

        # Active API keys
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND active = TRUE",
            (user_id,),
        )
        row = await cursor.fetchone()
        api_keys_active = row[0] if row else 0

        return UsageSnapshot(
            user_id=user_id,
            plan=plan,
            memories_stored=memories_stored,
            recalls_this_month=recalls_this_month,
            stores_this_month=stores_this_month,
            api_keys_active=api_keys_active,
        )

    async def get_monthly_usage(
        self,
        user_id: str,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, Any]:
        """Get aggregate usage for a specific month.

        Defaults to current month if year/month not provided.
        """
        now = datetime.now(UTC)
        year = year or now.year
        month = month or now.month
        month_start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            month_end = f"{year + 1:04d}-01-01"
        else:
            month_end = f"{year:04d}-{month + 1:02d}-01"

        cursor = await self._db.conn.execute(
            """
            SELECT
                COALESCE(SUM(stores), 0) as total_stores,
                COALESCE(SUM(recalls), 0) as total_recalls,
                COALESCE(SUM(deletes), 0) as total_deletes,
                COUNT(DISTINCT date) as active_days
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
        }

    async def get_daily_usage(
        self,
        user_id: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Get daily usage breakdown for the last N days."""
        cursor = await self._db.conn.execute(
            """
            SELECT date, stores, recalls, deletes
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
                "stores": row["stores"],
                "recalls": row["recalls"],
                "deletes": row["deletes"],
            }
            for row in rows
        ]
