"""Where the pickup/inbox fixes (F48, F46) meet the fixes integrated before them.

* F46 ages an abandoned untagged *unread* message out of the brief; the accepted-messages list (F53)
  is what an agent took on, and is never aged out.
* F46 gives ``get_inbox`` a ``project_id`` filter; ``project`` on that read is still answered, not
  dropped (F22), and the answer names the filter.
* F48's ``observe`` / ``pickup_recorded`` flags stay in a full brief that is cut to its budget (F32).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.mcp import arguments
from remembra.relay.handoff import render_brief
from remembra.security.untrusted import unwrap_untrusted
from remembra.services.agent_session import INBOX_UNTAGGED_FRESH_DAYS
from tests.agent_api_harness import build_api


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _send(api: dict[str, Any], subject: str, *, age_days: float, status: str | None = None) -> str:
    """One untagged agent message through the real InboxManager, backdated, optionally accepted."""

    async def _go() -> str:
        row = await api["app"].state.inbox_manager.send(
            owner_user_id="default_user", from_agent="claude-code", to_agent="codex", subject=subject, body="do it"
        )
        conn = api["app"].state.db.conn
        at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
        await conn.execute("UPDATE agent_inbox SET created_at = ? WHERE inbox_id = ?", (at, row["inbox_id"]))
        if status:
            await conn.execute("UPDATE agent_inbox SET status = ? WHERE inbox_id = ?", (status, row["inbox_id"]))
        await conn.commit()
        return str(row["inbox_id"])

    return str(api["http"].portal.call(_go))


def test_an_accepted_untagged_message_is_never_aged_out_of_the_brief(api):
    owed = _send(api, "taken on long ago", age_days=INBOX_UNTAGGED_FRESH_DAYS + 20, status="accepted")
    _send(api, "abandoned", age_days=INBOX_UNTAGGED_FRESH_DAYS + 20)
    res = api["http"].get("/api/v1/session/brief", params={"project_id": "application-source", "agent_id": "codex"})
    assert res.status_code == 200, res.text
    brief = res.json()
    inbox = brief["inbox"]
    # the unread one is counted, not listed (F46) ...
    assert inbox["items"] == [] and inbox["unread_count"] == 0 and inbox["stale_untagged_unread"] == 1
    # ... and the one the agent took on is still owed (F53)
    assert inbox["accepted_count"] == 1 and [i["inbox_id"] for i in inbox["accepted_items"]] == [owed]
    text = render_brief(brief)
    assert "Inbox: 1 older unread message with no project" in text and "abandoned" not in text
    assert "Inbox: 1 accepted" in text and "taken on long ago" in text


def test_get_inbox_answers_project_with_the_filter_it_takes():
    declared = ("agent_id", "status", "limit", "summary", "project_id")
    refused = arguments.prepare("get_inbox", declared, {"project": "widget"}).refusal
    assert refused and refused.startswith("INVALID ARGUMENTS: get_inbox does not take project"), refused
    assert "per agent" in refused and "project_id" in refused, refused
    narrowed = arguments.prepare("get_inbox", declared, {"project_id": "widget"})
    assert narrowed.refusal is None and narrowed.arguments == {"project_id": "widget"}
    # every other tool that declares project_id still reads "project" as it
    assert arguments.aliases_for("store_memory", ("content", "project_id")) == {"project": "project_id"}
    assert "project" not in arguments.aliases_for("get_inbox", declared)


def test_the_pickup_flags_survive_a_full_brief_cut_to_its_budget():
    big = "x" * (server.BRIEF_DETAIL_BUDGET_CHARS // 2)
    brief: dict[str, Any] = {
        "project_id": "widget",
        "agent_id": "codex",
        "rendered": "short brief",
        "warnings": [],
        "extra_one": big,
        "extra_two": big,
        "extra_three": big,
        "observe": True,
        "pickup_recorded": False,
    }
    summary = {"brief": "short brief", "handoff_id": None, "inbox_unread": 0}
    out = json.loads(unwrap_untrusted(server._dump_data(server._brief_detail(brief, summary))))
    assert "extra_one" in out["truncated"] or "extra_two" in out["truncated"], out["truncated"]
    assert out["observe"] is True and out["pickup_recorded"] is False
