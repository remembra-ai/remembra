-- Roll the database back to what b034314 (the pre-relay-launch image) can read.
--
-- The relay-launch boot rewrites cloud_tenants.plan 'pro' -> 'legacy_pro_49'
-- and 'team' -> 'legacy_team_199' (and teams.plan to match), and new checkouts
-- can write 'solo'. b034314 only knows free / pro / team / enterprise: its
-- PlanTier(tenant["plan"]) raises, so every store and usage call of those
-- accounts returns 500 until this runs.
--
-- Run it BEFORE redeploying b034314, against a backup-taken database:
--   sqlite3 /data/remembra.db ".backup /data/remembra-pre-rollback.db"
--   sqlite3 /data/remembra.db < scripts/maintenance/rollback_plans_v2.sql
--
-- Afterwards, by hand: review accounts that bought a new-catalog plan during
-- the deploy window (a $12 Solo is shown as the old $49 Pro under b034314).
-- Redeploying relay-launch later re-runs the legacy-tier migration, which turns
-- every paid 'pro' / 'team' row (including those buyers) back into a legacy tier.

BEGIN;

UPDATE cloud_tenants SET plan = 'pro' WHERE plan IN ('legacy_pro_49', 'solo');
UPDATE cloud_tenants SET plan = 'team' WHERE plan = 'legacy_team_199';
UPDATE teams SET plan = 'pro' WHERE plan IN ('legacy_pro_49', 'solo');
UPDATE teams SET plan = 'team' WHERE plan = 'legacy_team_199';
DELETE FROM cloud_migrations WHERE name = '2026_09_plans_v2_legacy_tiers';

-- b034314 ignores api_keys.agent_id, so an agent-bound key would act for the
-- whole account. Deactivate them; owners issue new keys after the rollback.
UPDATE api_keys SET active = 0 WHERE agent_id IS NOT NULL AND agent_id != '';

COMMIT;
