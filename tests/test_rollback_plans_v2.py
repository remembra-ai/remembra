"""scripts/maintenance/rollback_plans_v2.sql leaves only plans the pre-launch image (b034314) can read.

The database is built by the current code (schema + the legacy-tier migration +
real subscriptions and keys), then the script runs through the sqlite3 CLI-
equivalent executescript, and every plan must parse under the old enum.
"""

from __future__ import annotations

from pathlib import Path

from remembra.auth.keys import APIKeyManager
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier
from remembra.storage.database import Database

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "maintenance" / "rollback_plans_v2.sql"
B034314_PLANS = {"free", "pro", "team", "enterprise"}  # PlanTier at b034314


async def test_rollback_script_restores_plans_the_old_image_can_parse(tmp_path) -> None:
    db = Database(str(tmp_path / "rollback.db"))
    await db.connect()
    await db.init_schema()
    try:
        now = "2026-09-20T00:00:00+00:00"
        # Rows as production has them before the relay-launch boot.
        await db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cloud_tenants (
                user_id TEXT PRIMARY KEY, email TEXT, name TEXT, plan TEXT NOT NULL DEFAULT 'free',
                stripe_customer_id TEXT, stripe_subscription_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        await db.conn.executemany(
            "INSERT INTO cloud_tenants (user_id, plan, stripe_subscription_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("u_pro", "pro", "sub_49", now, now),
                ("u_team", "team", "sub_199", now, now),
                ("u_free", "free", None, now, now),
                ("u_ent", "enterprise", None, now, now),
            ],
        )
        await db.conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES ('u_team', 'team@example.com', 'x', ?)", (now,)
        )
        await db.conn.execute(
            "INSERT INTO teams (id, name, slug, owner_id, plan, created_at, updated_at)"
            " VALUES ('t1', 'T', 't', 'u_team', 'team', ?, ?)",
            (now, now),
        )
        await db.conn.commit()

        meter = UsageMeter(db)
        await meter.init_schema()  # the relay-launch boot: legacy-tier migration
        await meter.apply_subscription("u_solo", PlanTier.SOLO, subscription_id="sub_solo")  # deploy-window buyer
        assert (await meter.get_tenant("u_pro"))["plan"] == "legacy_pro_49"
        cursor = await db.conn.execute("SELECT plan FROM teams WHERE id = 't1'")
        assert (await cursor.fetchone())[0] == "legacy_team_199"
        keys = APIKeyManager(db)
        agent_key = await keys.create_key(user_id="u_pro", name="codex", agent_id="codex")
        plain_key = await keys.create_key(user_id="u_pro", name="laptop")

        await db.conn.executescript(SCRIPT.read_text())

        cursor = await db.conn.execute("SELECT user_id, plan FROM cloud_tenants ORDER BY user_id")
        plans = dict(await cursor.fetchall())
        assert plans == {"u_ent": "enterprise", "u_free": "free", "u_pro": "pro", "u_solo": "pro", "u_team": "team"}
        assert set(plans.values()) <= B034314_PLANS
        cursor = await db.conn.execute("SELECT plan FROM teams")
        assert {row[0] for row in await cursor.fetchall()} <= B034314_PLANS
        cursor = await db.conn.execute("SELECT COUNT(*) FROM cloud_migrations WHERE name = '2026_09_plans_v2_legacy_tiers'")
        assert (await cursor.fetchone())[0] == 0
        cursor = await db.conn.execute("SELECT id, active FROM api_keys")
        active = {row[0]: bool(row[1]) for row in await cursor.fetchall()}
        assert active == {agent_key.id: False, plain_key.id: True}
        # Subscriptions are untouched, so renewals still find their accounts.
        assert (await meter.get_tenant("u_pro"))["stripe_subscription_id"] == "sub_49"

        # Running it twice is harmless.
        await db.conn.executescript(SCRIPT.read_text())
        cursor = await db.conn.execute("SELECT COUNT(*) FROM cloud_tenants WHERE plan NOT IN ('free','pro','team','enterprise')")
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()
