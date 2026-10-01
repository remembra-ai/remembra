"""The desk's board (``GET /api/v1/marshal/board``): rules only, no model, no budget, nothing written.

The agents it diagnoses, the order and cap of its calls, every status-line
fragment (each code's short form, "n more to check", the inbox, the
fair-use threshold at 49% and 50%), the suggested questions, the model's
state in every offline reason and when limited, and the exact footer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from remembra.marshal.desk import board as board_module
from remembra.marshal.desk.board import FOOTER, agents_seen, rank, short_form, status_line, suggestions
from remembra.marshal.desk.budget import Snapshot
from remembra.marshal.desk.llm import desk_breaker
from tests.marshal_desk_harness import DeskHarness, desk_app


async def _get(h: DeskHarness, headers: dict[str, str]) -> dict[str, Any]:
    res = await h.http.get("/api/v1/marshal/board", headers=headers)
    assert res.status_code == 200, res.text
    return dict(res.json())


async def _user(h: DeskHarness, email: str = "board@example.com") -> tuple[str, dict[str, str]]:
    uid = await h.create_user(email)
    return uid, h.jwt(uid, email)


async def test_a_busy_account_gets_three_ranked_calls_and_writes_nothing(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid, jwt = await _user(h)
        claude = await h.api_key(uid, agent_id="claude-code")
        await h.seed_handoff(claude, agent="claude-code")
        for reader in ("codex", "cursor", "kimi"):
            await h.seed_pickup(await h.api_key(uid, agent_id=reader), reader=reader)
        stale = datetime.now(UTC) - timedelta(hours=3)
        await h.seed_checkpoint(uid, agent="gemini", created_at=stale)
        await h.http.post("/api/v1/inbox/send", json={"to_agent": "codex", "subject": "s", "body": "b"}, headers=claude)
        await h.http.get("/api/v1/cloud/usage/summary", headers=jwt)  # Home's first load
        script = h.openai_script()
        before, pickups = await h.total_changes(), await h.count("relay_pickups")
        body = await _get(h, jwt)
        assert await h.total_changes() == before and await h.count("relay_pickups") == pickups
        assert script.requests == [] and await h.count("marshal_reservations") == 0

        assert [(c["agent_id"], c["code"], c["proven"]) for c in body["calls"]] == [
            ("codex", "PICKS_UP_NEVER_CLOSES", True),
            ("cursor", "PICKS_UP_NEVER_CLOSES", True),
            ("kimi", "PICKS_UP_NEVER_CLOSES", True),
        ]
        assert body["calls"][0] == {
            "agent_id": "codex",
            "code": "PICKS_UP_NEVER_CLOSES",
            "proven": True,
            "text": "Codex read 1 brief but never handed off: its close hasn't reached Remembra.",
            "ask": "why does codex never close",
        }
        # Four calls in all (gemini's stale checkpoint is the fourth): three shown, three more to check.
        assert body["status_line"] == "codex never closes · 3 more to check · 1 unread in inbox"
        assert not set(body["suggestions"]) & {c["ask"] for c in body["calls"]}
        assert "how many unread inbox notes are waiting" in body["suggestions"]
        assert body["model"] == {"state": "ready", "reason": None, "name": "gpt-4o-mini", "retry_after_seconds": None}
        assert body["asks"]["used"] == 0 and body["asks"]["limit"] == 40 and body["asks"]["resets_at"].endswith("T00:00:00Z")
        assert body["footer"] == "Built by rules from your keys, trail and inbox. No model wrote this." == FOOTER
        assert body["generated_at"].endswith("Z")


async def test_the_stale_checkpoint_and_the_last_handoff_suggestion(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid, jwt = await _user(h)
        claude = await h.api_key(uid, agent_id="claude-code")
        await h.seed_checkpoint(uid, agent="gemini", created_at=datetime.now(UTC) - timedelta(hours=3))
        await h.seed_handoff(claude, agent="claude-code")
        body = await _get(h, jwt)
        assert [(c["agent_id"], c["code"]) for c in body["calls"]] == [("gemini", "STALE_CHECKPOINT")]
        assert body["status_line"] == "gemini stopped without a handoff"
        assert body["suggestions"] == ["what did claude-code hand off last"]


async def test_account_level_calls_and_empty_accounts(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid, jwt = await _user(h, "nokey@example.com")
        body = await _get(h, jwt)
        assert body["calls"] == [
            {
                "agent_id": None,
                "code": "KEY_MISSING",
                "proven": True,
                "text": "No API key active on your account, so the hooks can't load or save handoffs.",
                "ask": "why can't my agents reach Remembra",
            }
        ]
        assert body["status_line"] == "no active key" and body["suggestions"] == []

        await h.api_key(uid, name="never used")
        body = await _get(h, jwt)
        assert [(c["agent_id"], c["code"]) for c in body["calls"]] == [(None, "KEY_NEVER_USED")]
        assert body["status_line"] == "keys never used"

        uid2, jwt2 = await _user(h, "handed@example.com")
        key = await h.api_key(uid2, agent_id="claude-code")
        await h.seed_handoff(key, agent="claude-code")
        body = await _get(h, jwt2)
        assert body["calls"] == [] and body["status_line"] == "every agent handing off"
        assert body["suggestions"] == ["what did claude-code hand off last"]

        uid3, jwt3 = await _user(h, "quiet@example.com")
        quiet = await h.api_key(uid3)
        await h.http.get("/api/v1/trail/summary", headers=quiet)  # the key works, nothing was handed off
        body = await _get(h, jwt3)
        assert body["calls"] == [] and body["status_line"] == "no handoffs yet" and body["suggestions"] == []


async def test_key_problems_are_one_account_call_whatever_the_agents(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid, jwt = await _user(h)
        claude = await h.api_key(uid, agent_id="claude-code")
        await h.seed_handoff(claude, agent="claude-code")
        await h.seed_pickup(await h.api_key(uid, agent_id="codex"), reader="codex")
        await h.db.conn.execute("UPDATE api_keys SET active = 0 WHERE user_id = ?", (uid,))
        await h.db.conn.commit()
        body = await _get(h, jwt)
        assert [(c["agent_id"], c["code"]) for c in body["calls"]] == [(None, "KEY_MISSING")]


# ---------------------------------------------------------------------------
# The rules, one by one
# ---------------------------------------------------------------------------


def test_agents_seen_are_relay_agents_from_the_summary_then_readers_at_most_six() -> None:
    summary = {"agents": [{"agent_id": "Claude"}, {"agent_id": "dashboard"}, {"agent_id": "claude-app"}, {"agent_id": "gemini"}]}
    trail = [
        {"picked_up_by": [{"agent_id": "codex-cli"}, {"agent_id": "kimi"}]},
        {"picked_up_by": [{"agent_id": "qwen"}, {"agent_id": "cursor"}]},
    ]
    assert agents_seen(summary, trail) == ["claude-code", "gemini", "codex", "kimi", "qwen", "cursor"]
    assert agents_seen(
        {"agents": [{"agent_id": a} for a in ("claude-code", "codex", "cursor", "gemini", "qwen", "kimi")]}, []
    ) == [
        "claude-code",
        "codex",
        "cursor",
        "gemini",
        "qwen",
        "kimi",
    ]


def _c(code: str, agent: str | None, proven: bool) -> dict[str, Any]:
    return {"agent_id": agent, "code": code, "proven": proven, "text": "", "ask": board_module.ask_for(code, agent)}


def test_ranking_puts_proven_first_then_the_code_order_then_the_agent() -> None:
    calls = [
        _c("HOOKS_NOT_FIRING", "qwen", False),
        _c("NOTHING_WAITING", "kimi", True),
        _c("CODEX_TRUST_MISSING", "codex", False),
        _c("STALE_CHECKPOINT", "gemini", True),
        _c("PICKS_UP_NEVER_CLOSES", "cursor", True),
        _c("KEY_NEVER_USED", None, True),
        _c("HOOKS_NOT_FIRING", "claude-code", False),
    ]
    assert [(c["code"], c["agent_id"]) for c in rank(calls)] == [
        ("KEY_NEVER_USED", None),
        ("PICKS_UP_NEVER_CLOSES", "cursor"),
        ("STALE_CHECKPOINT", "gemini"),
        ("NOTHING_WAITING", "kimi"),
        ("CODEX_TRUST_MISSING", "codex"),
        ("HOOKS_NOT_FIRING", "claude-code"),
        ("HOOKS_NOT_FIRING", "qwen"),
    ]


@pytest.mark.parametrize(
    ("code", "agent", "fragment"),
    [
        ("KEY_MISSING", None, "no active key"),
        ("KEY_NEVER_USED", None, "keys never used"),
        ("PICKS_UP_NEVER_CLOSES", "codex", "codex never closes"),
        ("CODEX_TRUST_MISSING", "codex", "codex trust likely missing"),
        ("HOOKS_NOT_FIRING", "gemini", "gemini hooks likely not firing"),
        ("STALE_CHECKPOINT", "claude-code", "claude-code stopped without a handoff"),
        ("NOTHING_WAITING", "cursor", "cursor waiting"),
    ],
)
def test_every_short_form(code: str, agent: str | None, fragment: str) -> None:
    assert short_form(code, agent) == fragment
    assert status_line([_c(code, agent, True)], 0, None, True) == fragment


def test_status_line_fragments_and_the_fair_use_threshold() -> None:
    calls = [_c("PICKS_UP_NEVER_CLOSES", "codex", True), _c("STALE_CHECKPOINT", "gemini", True)]
    assert status_line(calls, 2, 64, True) == "codex never closes · 1 more to check · 2 unread in inbox"  # at most 3
    assert status_line(calls[:1], 2, 64, True) == "codex never closes · 2 unread in inbox · relay 64% of fair-use"
    assert status_line([], 0, 49, True) == "every agent handing off"
    assert status_line([], 0, 50, True) == "relay 50% of fair-use"
    assert status_line([], 3, None, False) == "3 unread in inbox"
    assert status_line([], 0, None, False) == "no handoffs yet"


def test_suggestions_are_deduplicated_and_capped() -> None:
    calls = [_c("KEY_MISSING", None, True), _c("KEY_NEVER_USED", None, True), _c("HOOKS_NOT_FIRING", "codex", False)]
    trail = [{"memory_type": "handoff", "agent_id": "Claude"}]
    assert suggestions(calls, trail, unread=2, fair_use=80) == [
        "what did claude-code hand off last",
        "how many unread inbox notes are waiting",
        "how close is relay activity to fair-use",
    ]
    assert suggestions([], [{"memory_type": "checkpoint", "agent_id": "codex"}]) == []


async def test_fair_use_is_the_floor_of_relay_events_over_the_soft_cap() -> None:
    class Meter:
        def __init__(self, events: int) -> None:
            self.events = events

        async def get_account(self, user_id: str) -> Any:
            return SimpleNamespace(pool="p", limits=SimpleNamespace(max_relay_events_per_month=5000))

        async def get_period_counters(self, pool: str, start: Any) -> dict[str, int]:
            return {"relay_events": self.events}

    now = datetime(2026, 10, 12, tzinfo=UTC)
    assert await board_module._fair_use(SimpleNamespace(usage_meter=Meter(2499)), "u", now) == 49
    assert await board_module._fair_use(SimpleNamespace(usage_meter=Meter(2500)), "u", now) == 50
    assert await board_module._fair_use(SimpleNamespace(usage_meter=None), "u", now) is None


# ---------------------------------------------------------------------------
# The model's state
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "marshal_model": "gpt-4o-mini",
        "marshal_openai_api_key": None,
        "openai_api_key": "sk-test",
        "marshal_daily_usd": 5.0,
        "marshal_monthly_usd": 100.0,
        "marshal_user_daily_asks": 40,
        "provider_quota_reset_seconds": 900.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_model_state_in_every_case() -> None:
    from tests.marshal_desk_harness import reset_breakers

    reset_breakers()
    ready = Snapshot(day_used_micro=0, month_used_micro=0, user_asks=3)
    state = board_module.model_state
    assert state(_settings(), ready) == {"state": "ready", "reason": None, "name": "gpt-4o-mini", "retry_after_seconds": None}
    assert state(_settings(openai_api_key=None), ready)["reason"] == "no_key"
    assert state(_settings(), Snapshot(4_990_000, 4_990_000, 0))["reason"] == "daily_budget"
    assert state(_settings(), Snapshot(0, 99_990_000, 0))["reason"] == "monthly_budget"
    assert state(_settings(), None)["reason"] == "daily_budget"  # an unreadable ledger refuses asks
    assert state(_settings(), Snapshot(0, 0, 40)) == {
        "state": "limited",
        "reason": "daily_asks",
        "name": "gpt-4o-mini",
        "retry_after_seconds": None,
    }
    breaker = desk_breaker(_settings())
    try:
        for _ in range(5):
            breaker.record_failure(TimeoutError("upstream"))
        opened = state(_settings(), ready)
        assert opened["state"] == "offline" and opened["reason"] == "breaker_open" and opened["retry_after_seconds"] >= 1
    finally:
        reset_breakers()
