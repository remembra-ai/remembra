"""Read endpoints the dashboard needs: trail detail + agent filter, activity
summary, inbox list-all and per-agent counts.

Production routes over a real SQLite ``Database`` and a real ``MemoryService``
(only the vector store / embedder are fakes), via ``agent_api_harness``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.services.relay import RelayService
from tests.agent_api_harness import build_api, seed

FACTS = {
    "branch": "feat/rounding",
    "head_commit": "b" * 40,
    "upstream": "origin/feat/rounding",
    "unpushed_commits": 1,
    "commits": [{"sha": "c" * 40, "subject": "fix: invoice rounding"}],
    "files_changed": ["src/invoice.py"],
    "tests": [{"cmd": "pytest -q tests/test_rounding.py", "passed": False, "summary": "1 failed"}],
    "todos_open": ["handle negative totals"],
    "next_step": "fix the rounding test",
}


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _as(api, **kwargs):
    user = AuthenticatedUser(user_id=kwargs.pop("user_id", "default_user"), api_key_id="k1", rate_limit_tier="standard", **kwargs)
    api["app"].dependency_overrides[get_current_user] = lambda: user
    return user


def _get(api, path, params=None, status=200):
    res = api["http"].get(f"/api/v1{path}", params=params or {})
    assert res.status_code == status, res.text
    return res.json()


def _post(api, path, body, status=200):
    res = api["http"].post(f"/api/v1{path}", json=body)
    assert res.status_code == status, res.text
    return res.json()


def _close(api, agent="claude-code", session="s1", project="widget", facts=None):
    body = {"agent_id": agent, "session_id": session, "project_id": project, "facts": facts or FACTS}
    return _post(api, "/session/close", body)


def _seed_handoff(api, memory_id, agent, created_at, project="widget", memory_type="handoff", user_id="default_user"):
    seed(
        api,
        memory_id,
        f"[HANDOFF] {agent}",
        created_at.replace(tzinfo=None),
        project_id=project,
        memory_type=memory_type,
        metadata={"agent_id": agent, "session_id": memory_id},
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Trail: expandable detail and agent filter
# ---------------------------------------------------------------------------


def test_trail_items_carry_the_handoff_sections(api):
    _close(api)
    item = _get(api, "/trail", {"project_id": "widget"})["items"][0]
    detail = item["detail"]
    assert detail["structured"] is True
    assert any("fix: invoice rounding" in d for d in detail["done"])
    assert any("handle negative totals" in n for n in detail["not_done"])
    assert detail["failing"][0].startswith("FAILING: pytest -q tests/test_rounding.py")
    assert detail["next"] == "fix the rounding test"
    assert detail["commits"][0]["subject"] == "fix: invoice rounding"
    assert detail["unpushed_commits"] == 1 and detail["upstream"] == "origin/feat/rounding"
    assert detail["grounding_status"] == "none"
    # The compact fields are unchanged.
    assert item["failing"] == len(detail["failing"]) and item["open"] == len(detail["not_done"])
    assert item["branch"] == "feat/rounding" and item["head_commit"] == "b" * 40


def test_trail_checkpoint_detail_is_its_content(api):
    api["http"].post(
        "/api/v1/memories",
        json={"content": "checkpoint: migrations half done", "project_id": "widget", "memory_type": "checkpoint"},
    )
    item = _get(api, "/trail", {"project_id": "widget"})["items"][0]
    assert item["memory_type"] == "checkpoint"
    assert item["detail"] == {"structured": False, "content": "checkpoint: migrations half done"}


def test_trail_filters_by_agent(api):
    _close(api, agent="claude-code", session="s1")
    _close(api, agent="codex", session="s2")
    _close(api, agent="codex", session="s3", project="other")
    codex = _get(api, "/trail", {"agent_id": "codex"})
    assert codex["total"] == 2 and {i["agent_id"] for i in codex["items"]} == {"codex"}
    assert codex["agent_id"] == "codex"
    scoped = _get(api, "/trail", {"agent_id": "codex", "project_id": "widget"})
    assert scoped["total"] == 1 and scoped["items"][0]["project_id"] == "widget"
    assert _get(api, "/trail", {"agent_id": "gemini"})["total"] == 0
    assert _get(api, "/trail")["total"] == 3  # no filter: every agent, every project
    _get(api, "/trail", {"agent_id": "bad agent!"}, status=400)


# ---------------------------------------------------------------------------
# Activity summary
# ---------------------------------------------------------------------------


def test_summary_counts_agents_projects_and_daily_series(api):
    now = datetime.now(UTC)
    _seed_handoff(api, "h1", "claude-code", now - timedelta(minutes=5))
    _seed_handoff(api, "h2", "claude-code", now - timedelta(days=2))
    _seed_handoff(api, "h3", "codex", now - timedelta(days=3), project="api")
    _seed_handoff(api, "h4", "codex", now - timedelta(days=20), project="api")  # outside the week and the series
    _seed_handoff(api, "c1", "codex", now - timedelta(days=1), memory_type="checkpoint")
    _seed_handoff(api, "x1", "cursor", now - timedelta(hours=1), user_id="someone_else")  # other tenant

    out = _get(api, "/trail/summary", {"days": 14})
    assert out["days"] == 14
    assert out["total_handoffs"] == 4 and out["total_checkpoints"] == 1
    assert out["week"] == {"handoffs": 3, "checkpoints": 1, "agents": ["claude-code", "codex"], "projects": ["api", "widget"]}

    agents = {a["agent_id"]: a for a in out["agents"]}
    assert set(agents) == {"claude-code", "codex"}  # never another tenant's agent
    assert out["agents"][0]["agent_id"] == "claude-code"  # most recently active first
    claude = agents["claude-code"]
    assert claude["handoffs"] == 2 and claude["sessions_7d"] == 2 and claude["projects"] == ["widget"]
    assert len(claude["daily"]) == 14 and claude["daily"][13] == 1 and claude["daily"][11] == 1 and sum(claude["daily"]) == 2
    last = datetime.fromisoformat(claude["last_active"])
    assert abs((last - (now - timedelta(minutes=5))).total_seconds()) < 2
    codex = agents["codex"]
    assert codex["handoffs"] == 2 and codex["checkpoints"] == 1
    assert codex["sessions_7d"] == 1  # the checkpoint is activity, not a session
    assert codex["daily"][12] == 1 and codex["daily"][10] == 1 and sum(codex["daily"]) == 2
    assert codex["projects"] == ["api", "widget"]

    projects = {p["project_id"]: p for p in out["projects"]}
    assert projects["api"]["handoffs"] == 2 and projects["api"]["agents"] == ["codex"]
    assert projects["widget"]["agents"] == ["claude-code", "codex"]


def test_summary_excludes_superseded_handoffs(api):
    _close(api, session="s1")
    _close(api, session="s1", facts={**FACTS, "next_step": "ship it"})  # re-close: supersedes
    out = _get(api, "/trail/summary")
    assert out["total_handoffs"] == 1 and out["agents"][0]["sessions_7d"] == 1


def test_summary_respects_project_restricted_keys(api):
    now = datetime.now(UTC)
    _seed_handoff(api, "h1", "claude-code", now - timedelta(hours=1), project="alpha")
    _seed_handoff(api, "h2", "codex", now - timedelta(hours=2), project="beta")
    _as(api, project_ids=["alpha"])
    out = _get(api, "/trail/summary")
    assert [p["project_id"] for p in out["projects"]] == ["alpha"]
    assert [a["agent_id"] for a in out["agents"]] == ["claude-code"]


def test_summary_is_empty_for_a_new_user(api):
    out = _get(api, "/trail/summary", {"days": 7, "tz_offset_minutes": -240})
    assert out["agents"] == [] and out["projects"] == [] and out["total_handoffs"] == 0
    assert out["week"] == {"handoffs": 0, "checkpoints": 0, "agents": [], "projects": []}
    _get(api, "/trail/summary", {"days": 0}, status=422)
    _get(api, "/trail/summary", {"tz_offset_minutes": 2000}, status=422)


def test_summary_day_boundaries_follow_the_timezone_offset(api):
    """23:30 UTC is already tomorrow at UTC+1 and still today at UTC-5."""
    service = RelayService(db=api["app"].state.db)
    now = datetime(2026, 9, 25, 23, 45, tzinfo=UTC)
    _seed_handoff(api, "late", "codex", datetime(2026, 9, 25, 23, 30, tzinfo=UTC))
    _seed_handoff(api, "early", "codex", datetime(2026, 9, 25, 0, 30, tzinfo=UTC))

    def _daily(offset):
        return api["http"].portal.call(
            lambda: service.activity_summary("default_user", days=3, tz_offset_minutes=offset, now=now)
        )["agents"][0]["daily"]

    assert _daily(0) == [0, 0, 2]  # both on Sep 25 UTC
    assert _daily(60) == [0, 1, 1]  # UTC+1: 01:30 Sep 25 (yesterday) and 00:30 Sep 26 (today)
    assert _daily(-300) == [0, 1, 1]  # UTC-5: 19:30 Sep 24 and 18:30 Sep 25 (today)


# ---------------------------------------------------------------------------
# Inbox: list across agents, per-agent counts
# ---------------------------------------------------------------------------


def _send(api, to, subject, sender="dashboard", **extra):
    return _post(
        api, "/inbox/send", {"to_agent": to, "from_agent": sender, "subject": subject, "body": subject, **extra}, status=201
    )


def test_inbox_messages_lists_every_agent_newest_first(api):
    a = _send(api, "claude-code", "fix the rounding test")
    b = _send(api, "codex", "review the PR")
    c = _send(api, "dashboard", "deploy is green", sender="codex")
    _post(api, f"/inbox/{a['inbox_id']}/ack", {"result": "done"})
    _post(api, f"/inbox/{b['inbox_id']}/ack", {})  # read, still open

    open_items = _get(api, "/inbox/messages")
    assert open_items["status"] == "open" and open_items["total"] == 2
    assert [i["inbox_id"] for i in open_items["items"]] == [c["inbox_id"], b["inbox_id"]]
    assert _get(api, "/inbox/messages", {"status": "unread"})["total"] == 1
    everything = _get(api, "/inbox/messages", {"status": "all"})
    assert everything["total"] == 3 and everything["items"][-1]["status"] == "done"
    # agent filter matches either direction
    codex = _get(api, "/inbox/messages", {"status": "all", "agent_id": "codex"})
    assert {i["inbox_id"] for i in codex["items"]} == {b["inbox_id"], c["inbox_id"]}
    page = _get(api, "/inbox/messages", {"status": "all", "limit": 1, "offset": 1})
    assert page["total"] == 3 and [i["inbox_id"] for i in page["items"]] == [b["inbox_id"]]
    _get(api, "/inbox/messages", {"status": "bogus"}, status=422)


def test_inbox_messages_skip_expired_and_other_tenants(api):
    _send(api, "codex", "expired", expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    _as(api, user_id="someone_else")
    _send(api, "codex", "not yours")
    _as(api)
    assert _get(api, "/inbox/messages", {"status": "all"})["total"] == 0
    assert _get(api, "/inbox/summary") == {"unread_total": 0, "open_total": 0, "agents": []}


def test_inbox_summary_counts_per_agent(api):
    first = _send(api, "claude-code", "one")
    _send(api, "claude-code", "two")
    third = _send(api, "codex", "three", sender="claude-code")
    _post(api, f"/inbox/{first['inbox_id']}/ack", {})
    _post(api, f"/inbox/{third['inbox_id']}/ack", {"result": "blocked", "note": "needs creds"})
    out = _get(api, "/inbox/summary")
    assert out["unread_total"] == 1 and out["open_total"] == 2
    agents = {a["agent_id"]: a for a in out["agents"]}
    assert agents["claude-code"] == {**agents["claude-code"], "unread": 1, "open": 2, "received": 2, "sent": 1}
    assert agents["codex"]["received"] == 1 and agents["codex"]["open"] == 0
    assert agents["dashboard"]["sent"] == 2 and agents["dashboard"]["received"] == 0
    assert out["agents"][0]["agent_id"] == "claude-code"  # most unread first


def test_message_sent_from_the_dashboard_appears_in_the_agents_next_brief(api):
    _close(api, agent="codex", session="s1")
    _send(api, "claude-code", "when you're back, fix the rounding test")
    brief = _get(api, "/session/brief", {"project_id": "widget", "agent_id": "claude-code"})
    assert brief["inbox"]["unread_count"] == 1
    assert brief["inbox"]["items"][0]["subject"] == "when you're back, fix the rounding test"
    assert brief["inbox"]["items"][0]["from_agent"] == "dashboard"
    assert "fix the rounding test" in brief["rendered"]
