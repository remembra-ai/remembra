"""WP-14: crew rate limits (per session / per host / reserved buckets), plan limits and soft caps."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, HTTPException, Request

import remembra.config as config_module
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PLANS, PlanTier, get_plan
from remembra.crew import limits as crew_limits_mod
from remembra.crew.limits import (
    CREW_RATE_BUCKETS,
    RESERVED_BUCKETS,
    SELF_HOSTED_CREW_LIMITS,
    CrewRateLimiter,
    budget_signals,
    burst_window_s,
    crew_limits,
    crew_limits_for_owner,
    crew_limits_for_tier,
    enforce_rate_limit,
    enforce_zone_capacity,
    promotion_decision,
    seat_for_join,
    set_crew_rate_limiter,
    should_coalesce,
    token_key,
)
from remembra.crew.schemas import CHECKPOINT_TRIGGERS, ROUTES, validate_event_payload
from tests.security_harness import make_settings, secure_app

SESSION_A = "cst_token_a_" + "a" * 40
SESSION_B = "cst_token_b_" + "b" * 40


@pytest.fixture()
def limiter() -> CrewRateLimiter:
    return CrewRateLimiter("memory://")


def exhaust(lim: CrewRateLimiter, bucket: str, n: int, **keys: str) -> None:
    for i in range(n):
        d = lim.check(bucket, **keys)
        assert d.allowed, (bucket, i, d)


# ---------------------------------------------------------------------------
# Bucket table (§11.2)
# ---------------------------------------------------------------------------


def test_bucket_table_matches_the_spec():
    expected = {
        "heartbeat": (None, None, "2/minute"),
        "events": ("30/minute", "120/minute", None),
        "guard": ("300/minute", "1200/minute", None),
        "claims": ("60/minute", "240/minute", None),
        "adopt": ("6/hour", "30/hour", None),
        "messages": ("20/minute", "60/minute", None),
        "needs_you": ("6/hour", "30/hour", None),
        "tasks": ("60/minute", "120/minute", None),
        "snapshot": ("1 per 10 seconds", None, None),
        "events_poll": ("20/minute", None, None),
        "join": (None, "30/minute", None),
        "ws_replay": (None, "30/minute", None),
        "default": (None, None, None),
    }
    assert {k: (v.per_session, v.per_user, v.per_host) for k, v in CREW_RATE_BUCKETS.items()} == expected
    assert set(RESERVED_BUCKETS) == {"guard", "claims"}
    # Every bucket the REST contract names exists.
    assert {r.bucket for r in ROUTES} <= set(CREW_RATE_BUCKETS)


def test_per_session_limit_isolates_sessions_of_one_user(limiter):
    exhaust(limiter, "events", 30, user_id="u1", session_token=SESSION_A)
    d = limiter.check("events", user_id="u1", session_token=SESSION_A)
    assert not d.allowed and d.scope == "session" and d.limit == "30/minute"
    assert 1 <= d.retry_after_s <= 60
    # Another session of the same user is unaffected.
    assert limiter.check("events", user_id="u1", session_token=SESSION_B).allowed


def test_per_user_limit_caps_the_sum_of_sessions(limiter):
    for s in range(4):
        exhaust(limiter, "events", 30, user_id="u1", session_token=f"session-{s}")
    d = limiter.check("events", user_id="u1", session_token="session-5")
    assert not d.allowed and d.scope == "user" and d.limit == "120/minute"
    # Another user is unaffected.
    assert limiter.check("events", user_id="u2", session_token="session-5").allowed


def test_a_refused_request_consumes_nothing(limiter):
    # session-x is refused by the user scope; its own session budget must stay untouched.
    for s in range(5):
        exhaust(limiter, "adopt", 6, user_id="u1", session_token=f"s{s}")
    assert not limiter.check("adopt", user_id="u1", session_token="sx").allowed
    # The same session under another user id (fresh user bucket) still has all 6.
    exhaust(limiter, "adopt", 6, user_id="u9", session_token="sx")
    assert not limiter.check("adopt", user_id="u9", session_token="sx").allowed


def test_one_session_flooding_guard_and_claims_does_not_starve_others(limiter):
    # §13.6: one session floods guard and claims; other sessions' auto-claims are unaffected.
    exhaust(limiter, "guard", 300, user_id="u1", session_token=SESSION_A)
    exhaust(limiter, "claims", 60, user_id="u1", session_token=SESSION_A)
    assert not limiter.check("guard", user_id="u1", session_token=SESSION_A).allowed
    assert not limiter.check("claims", user_id="u1", session_token=SESSION_A).allowed
    assert limiter.check("guard", user_id="u1", session_token=SESSION_B).allowed
    assert limiter.check("claims", user_id="u1", session_token=SESSION_B).allowed


def test_chatty_buckets_cannot_drain_the_reserved_ones(limiter):
    for s in range(3):
        exhaust(limiter, "messages", 20, user_id="u1", session_token=f"m{s}")
    assert not limiter.check("messages", user_id="u1", session_token="m9").allowed
    for s in range(4):
        exhaust(limiter, "events", 30, user_id="u1", session_token=f"e{s}")
    assert not limiter.check("events", user_id="u1", session_token="e9").allowed
    assert limiter.check("guard", user_id="u1", session_token="m0").allowed
    assert limiter.check("claims", user_id="u1", session_token="e0").allowed


def test_heartbeat_is_keyed_on_the_host_token(limiter):
    exhaust(limiter, "heartbeat", 2, host_token="host-token-1", user_id="u1")
    d = limiter.check("heartbeat", host_token="host-token-1", user_id="u1")
    assert not d.allowed and d.scope == "host"
    assert limiter.check("heartbeat", host_token="host-token-2", user_id="u1").allowed
    with pytest.raises(ValueError):
        limiter.check("heartbeat", user_id="u1")
    with pytest.raises(ValueError):
        limiter.check("no-such-bucket", user_id="u1")


def test_snapshot_one_per_ten_seconds_and_retry_after(limiter):
    assert limiter.check("snapshot", session_token=SESSION_A).allowed
    d = limiter.check("snapshot", session_token=SESSION_A)
    assert not d.allowed and 1 <= d.retry_after_s <= 10
    # A dashboard caller (no session) on a session-only bucket is not limited by it.
    assert limiter.check("snapshot", user_id="u1").allowed


def test_raw_tokens_never_reach_the_storage(limiter):
    limiter.check("events", user_id="u1", session_token=SESSION_A)
    limiter.check("heartbeat", host_token="host-secret-token")
    storage = limiter._storage
    keys = " ".join(str(k) for k in (*storage.events.keys(), *storage.storage.keys()))
    assert keys, "the limiter stored nothing"
    assert SESSION_A not in keys and "host-secret-token" not in keys
    assert token_key(SESSION_A) in keys


def test_enforce_rate_limit_raises_crew_429(limiter):
    exhaust(limiter, "tasks", 60, user_id="u1", session_token=SESSION_A)
    with pytest.raises(HTTPException) as e:
        enforce_rate_limit("tasks", user_id="u1", session_token=SESSION_A, limiter=limiter)
    assert e.value.status_code == 429
    body = e.value.detail
    assert isinstance(body, dict) and body["error"] == "rate_limited" and body["retry_after_s"] >= 1
    assert e.value.headers and int(e.value.headers["Retry-After"]) == body["retry_after_s"]


def test_enforce_rate_limit_respects_the_global_setting():
    previous = config_module._settings
    try:
        config_module._settings = make_settings(rate_limit_enabled=False)
        for _ in range(10):
            enforce_rate_limit("snapshot", session_token=SESSION_A)  # disabled: never raises
        config_module._settings = make_settings(rate_limit_enabled=True)
        set_crew_rate_limiter(CrewRateLimiter("memory://"))
        enforce_rate_limit("snapshot", session_token=SESSION_A)
        with pytest.raises(HTTPException):
            enforce_rate_limit("snapshot", session_token=SESSION_A)
    finally:
        set_crew_rate_limiter(None)
        config_module._settings = previous


async def test_http_route_returns_429_with_retry_after(tmp_path):
    router = APIRouter()
    lim = CrewRateLimiter("memory://")

    @router.post("/crews/{crew_id}/guard")
    async def guard(crew_id: str, request: Request) -> dict[str, str]:
        enforce_rate_limit("guard", user_id="u1", session_token=request.headers["X-Test-Session"], limiter=lim)
        return {"decision": "allow"}

    async with secure_app(tmp_path, [router]) as h:
        uid = await h.create_user("rl@example.com")
        key, _ = await h.api_key(uid, "editor")
        headers = {"X-API-Key": key, "X-Test-Session": SESSION_A}
        for _ in range(300):
            assert (await h.client.post("/api/v1/crews/crw_x/guard", headers=headers)).status_code == 200
        res = await h.client.post("/api/v1/crews/crw_x/guard", headers=headers)
        assert res.status_code == 429
        assert res.json()["detail"]["error"] == "rate_limited"
        assert int(res.headers["retry-after"]) >= 1
        other = {"X-API-Key": key, "X-Test-Session": SESSION_B}
        assert (await h.client.post("/api/v1/crews/crw_x/guard", headers=other)).status_code == 200


# ---------------------------------------------------------------------------
# PlanLimits crew fields (§12) — prices untouched
# ---------------------------------------------------------------------------


def test_approved_prices_are_unchanged():
    prices = {t: (p.price_monthly_cents, p.price_annual_cents) for t, p in PLANS.items()}
    assert prices == {
        PlanTier.FREE: (0, 0),
        PlanTier.SOLO: (1_200, 12_000),
        PlanTier.PRO: (2_900, 29_000),
        PlanTier.TEAM: (1_500, 15_000),
        PlanTier.ENTERPRISE: (None, None),
        PlanTier.LEGACY_PRO: (4_900, None),
        PlanTier.LEGACY_TEAM: (19_900, None),
    }
    assert [PLANS[t].max_smart_credits_per_month for t in (PlanTier.FREE, PlanTier.SOLO, PlanTier.PRO)] == [500, 2_200, 5_000]


def _crew_fields(tier: PlanTier) -> tuple:
    p = PLANS[tier]
    return (
        p.max_crew_sessions_live,
        p.max_zones,
        p.crew_events_per_day_soft,
        p.crew_memory_promotions_per_day,
        p.crew_event_retention_days,
        p.crew_teammates,
    )


def test_crew_plan_fields_match_spec_table():
    assert _crew_fields(PlanTier.FREE) == (3, 10, 5_000, 50, 14, False)
    assert _crew_fields(PlanTier.PRO) == (8, 50, 50_000, 200, 180, False)
    assert _crew_fields(PlanTier.TEAM) == (20, 200, 250_000, 1_000, 365, True)
    assert _crew_fields(PlanTier.LEGACY_PRO) == _crew_fields(PlanTier.PRO)
    assert _crew_fields(PlanTier.LEGACY_TEAM) == _crew_fields(PlanTier.TEAM)
    # Solo sits between Free and Pro; Enterprise at or above Team on every numeric field.
    for i in range(5):
        assert _crew_fields(PlanTier.FREE)[i] < _crew_fields(PlanTier.SOLO)[i] < _crew_fields(PlanTier.PRO)[i]
        assert _crew_fields(PlanTier.ENTERPRISE)[i] >= _crew_fields(PlanTier.TEAM)[i]
    assert PLANS[PlanTier.ENTERPRISE].crew_teammates


def test_crew_fields_are_never_seat_scaled():
    team = get_plan(PlanTier.TEAM)
    scaled = team.scaled(12)
    assert scaled.max_memories == team.max_memories * 12  # pooled fields do scale
    assert _crew_fields(PlanTier.TEAM) == (
        scaled.max_crew_sessions_live,
        scaled.max_zones,
        scaled.crew_events_per_day_soft,
        scaled.crew_memory_promotions_per_day,
        scaled.crew_event_retention_days,
        scaled.crew_teammates,
    )


def test_retention_table_matches_spec():
    def r(tier: PlanTier) -> tuple:
        x = crew_limits_for_tier(tier).retention
        return (x.events_days, x.activity_burst_days, x.checkpoint_facts_days, x.ended_sessions_days, x.brief_text_days)

    assert r(PlanTier.FREE) == (14, 7, 14, 90, 30)
    assert r(PlanTier.PRO) == (180, 30, 90, 365, 180)
    assert r(PlanTier.TEAM) == (365, 60, 180, 365, 365)
    for tier in PLANS:
        ret = crew_limits_for_tier(tier).retention
        assert ret.idempotency_hours == 72 and ret.footprints_after_end_days == 7 and ret.baton_ref_days_after_close == 7
        assert ret.events_days == PLANS[tier].crew_event_retention_days


async def test_crew_limits_come_from_the_owner_plan_via_the_real_meter(tmp_path):
    async with secure_app(tmp_path, []) as h:
        meter = UsageMeter(h.db)
        free_user = await h.create_user("free@example.com")
        team_owner = await h.create_user("team@example.com")
        await meter.register_tenant(team_owner, PlanTier.TEAM)
        await meter.apply_subscription(team_owner, PlanTier.TEAM, seats=12)
        free = await crew_limits_for_owner(meter, free_user)
        team = await crew_limits_for_owner(meter, team_owner)
        assert (free.tier, free.max_sessions_live, free.max_zones) == ("free", 3, 10)
        assert (team.tier, team.max_sessions_live, team.max_zones, team.teammates) == ("team", 20, 200, True)
    assert await crew_limits_for_owner(None, "anyone") == SELF_HOSTED_CREW_LIMITS
    assert SELF_HOSTED_CREW_LIMITS.tier == "self_hosted"


# ---------------------------------------------------------------------------
# Soft caps (§12)
# ---------------------------------------------------------------------------


def test_live_session_cap_degrades_to_observe_only():
    free = crew_limits_for_tier(PlanTier.FREE)
    ok = seat_for_join(2, free)
    assert not ok.observe_only and ok.can_claim and ok.upgrade_hint is None
    over = seat_for_join(3, free)
    assert over.observe_only and not over.can_claim and over.limit == 3
    assert over.upgrade_hint == "Solo allows 5 live sessions per crew."
    assert seat_for_join(8, crew_limits_for_tier(PlanTier.PRO)).upgrade_hint == "Team allows 20 live sessions per crew."
    assert seat_for_join(20, crew_limits_for_tier(PlanTier.TEAM)).upgrade_hint == "Enterprise allows 50 live sessions per crew."
    top = seat_for_join(500, crew_limits_for_tier(PlanTier.ENTERPRISE))
    assert top.observe_only and top.upgrade_hint is None
    assert seat_for_join(8, crew_limits_for_tier(PlanTier.LEGACY_PRO)).upgrade_hint == "Team allows 20 live sessions per crew."


def test_zone_cap_refuses_only_additions():
    free = crew_limits_for_tier(PlanTier.FREE)
    enforce_zone_capacity(9, 1, free)
    enforce_zone_capacity(12, 0, free)  # over the cap after a downgrade: existing zones stay
    with pytest.raises(HTTPException) as e:
        enforce_zone_capacity(10, 1, free)
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "zone_cap"
    assert "Solo allows 25 zones per crew." in e.value.detail["message"]


def test_only_activity_bursts_coalesce_and_only_over_the_cap_window():
    free = crew_limits_for_tier(PlanTier.FREE)
    under, over = 100, 5_000
    assert burst_window_s(under, free) == 60 and burst_window_s(over, free) == 300
    assert should_coalesce("activity.burst", 30, under, free)
    assert not should_coalesce("activity.burst", 90, under, free)
    assert should_coalesce("activity.burst", 90, over, free)
    assert not should_coalesce("activity.burst", 301, over, free)
    assert not should_coalesce("activity.burst", None, over, free)
    never_dropped = [
        "claim.granted",
        "guard.blocked",
        "collision.detected",
        "task.status_changed",
        "report.submitted",
        "checkpoint.created",
        "handoff.created",
        "baton.passed",
        "decision.proposed",
        "message.posted",
        "session.lost",
        "activity.commit",
    ]
    for event_type in never_dropped:
        assert not should_coalesce(event_type, 0, 10**9, free), event_type


def test_budget_signals_fire_once_at_80_and_100_percent():
    free = crew_limits_for_tier(PlanTier.FREE)
    assert budget_signals(3_998, 3_999, free) == []
    warn = budget_signals(3_999, 4_000, free)
    assert warn == [("budget.warning", {"metric": "crew_events_per_day", "used": 4_000, "limit": 5_000})]
    assert budget_signals(4_000, 4_001, free) == []
    cap = budget_signals(4_999, 5_000, free)
    assert [t for t, _ in cap] == ["budget.cap_reached"]
    assert budget_signals(5_000, 9_000, free) == []
    both = budget_signals(3_990, 5_010, free)
    assert [t for t, _ in both] == ["budget.warning", "budget.cap_reached"]
    for event_type, payload in both:
        assert validate_event_payload(event_type, payload) == []
    # Counting events one by one over a whole day yields each signal exactly once.
    fired = [t for n in range(6_000) for t, _ in budget_signals(n, n + 1, free)]
    assert fired == ["budget.warning", "budget.cap_reached"]


def test_promotions_defer_over_cap_except_quota_and_lost():
    free = crew_limits_for_tier(PlanTier.FREE)
    assert set(crew_limits_mod.ALWAYS_PROMOTE_TRIGGERS) <= set(CHECKPOINT_TRIGGERS)
    assert promotion_decision(49, "commit", free).promote
    at_cap = promotion_decision(50, "commit", free)
    assert not at_cap.promote and not at_cap.counts and at_cap.reason == "cap_reached_deferred"
    for trigger in ("quota", "lost"):
        d = promotion_decision(10_000, trigger, free)
        assert d.promote and d.counts
    assert promotion_decision(50, "close", free).promote is False


def test_crew_limits_from_plan_object():
    lim = crew_limits(get_plan("pro"))
    assert (lim.tier, lim.max_sessions_live, lim.events_per_day_soft, lim.memory_promotions_per_day) == ("pro", 8, 50_000, 200)


def test_process_limiter_uses_the_configured_backend():
    previous = config_module._settings
    try:
        set_crew_rate_limiter(None)
        config_module._settings = make_settings(rate_limit_enabled=True, rate_limit_storage="memory")
        lim = crew_limits_mod.get_crew_rate_limiter()
        assert lim.storage_uri == "memory://"
        assert crew_limits_mod.get_crew_rate_limiter() is lim
        lim.check("join", user_id="u1")
        lim.reset()
        exhaust(lim, "join", 30, user_id="u1")
    finally:
        set_crew_rate_limiter(None)
        config_module._settings = previous
