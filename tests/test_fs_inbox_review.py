"""F46 review follow-up: a brief whose only inbox news is an aged-out untagged message says so in one line
(no "0 unread here" above it); a brief with no resolved project has no "here" and keeps listing every
unread message (the documented limit of the ageing-out).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from remembra.relay.handoff import render_brief
from remembra.services.agent_session import INBOX_UNTAGGED_FRESH_DAYS
from tests.agent_api_harness import build_api

STALE = "Built-in fix check FIX-BE7F45"


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _brief(api: dict[str, Any], agent: str, project: str | None = "widget") -> dict[str, Any]:
    params = {"agent_id": agent}
    if project:
        params["project_id"] = project
    res = api["http"].get("/api/v1/session/brief", params=params)
    assert res.status_code == 200, res.text
    return dict(res.json())


def _send(api: dict[str, Any], subject: str, *, age_days: float = 0.0, **kwargs: Any) -> str:
    async def _go() -> str:
        row = await api["app"].state.inbox_manager.send(
            owner_user_id="default_user",
            from_agent=kwargs.pop("from_agent", "claude-code"),
            to_agent="codex",
            subject=subject,
            body="Reply with the code, then mark this message done.",
            **kwargs,
        )
        if age_days:
            at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
            await api["app"].state.db.conn.execute(
                "UPDATE agent_inbox SET created_at = ? WHERE inbox_id = ?", (at, row["inbox_id"])
            )
            await api["app"].state.db.conn.commit()
        return str(row["inbox_id"])

    return str(api["http"].portal.call(_go))


# ---------------------------------------------------------------------------
# F46: one line for an aged-out message, with or without a project
# ---------------------------------------------------------------------------


def test_a_brief_with_only_an_aged_out_message_says_it_in_one_line(api):
    _send(api, STALE, age_days=10)
    text = _brief(api, "codex", "application-source")["rendered"]
    assert "unread here" not in text
    lines = [line for line in text.splitlines() if "older unread message" in line]
    assert len(lines) == 1
    assert (
        lines[0]
        .lstrip()
        .startswith(
            f"Inbox: 1 older unread message with no project (sent more than {INBOX_UNTAGGED_FRESH_DAYS} days ago) is not listed"
        )
    )
    assert "get_inbox shows it" in lines[0] and "result=rejected" in lines[0]


def test_an_aged_out_message_beside_a_live_one_keeps_both_lines(api):
    _send(api, STALE, age_days=10)
    _send(api, "please review", project_id="application-source")
    text = _brief(api, "codex", "application-source")["rendered"]
    assert "Inbox: 1 unread here (get_inbox for bodies;" in text
    stale = [line for line in text.splitlines() if "older unread message" in line]
    assert len(stale) == 1 and not stale[0].lstrip().startswith("Inbox:")


def test_render_brief_stale_only_from_stored_json():
    inbox = {"available": True, "agent_id": "codex", "unread_count": 0, "other_projects_unread": 0, "items": []}
    brief = {"project_id": "alpha", "agent_id": "codex", "handoff": None, "inbox": {**inbox, "stale_untagged_unread": 3}}
    text = render_brief(brief)
    assert "unread here" not in text
    assert "Inbox: 3 older unread messages with no project are not listed: get_inbox shows them" in text


def test_a_brief_with_no_project_lists_every_unread_message_and_ages_none_out(api):
    """No project resolved: there is no "here", so the brief lists all of the agent's unread messages (as it
    always did, and as tests/test_inbox_scope.py pins) and reports none as aged out."""
    _send(api, STALE, age_days=10)
    _send(api, "untagged and new")
    _send(api, "old but tagged", age_days=30, project_id="oorvel")
    brief = _brief(api, "codex", project=None)
    assert brief["project_id"] is None
    inbox = brief["inbox"]
    assert sorted(i["subject"] for i in inbox["items"]) == sorted([STALE, "old but tagged", "untagged and new"])
    assert inbox["unread_count"] == 3 and inbox["stale_untagged_unread"] == 0 and inbox["other_projects_unread"] == 0
    assert "older unread message" not in brief["rendered"]
