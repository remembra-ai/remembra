"""F46: an abandoned untagged message stops being "unread here" in every project, forever.

A message sent with no project (a raw API send) was listed and counted as
"here" in the brief of every project, at any age. Now:

* the brief lists an untagged message from an agent as "here" only while it
  is fresh (:data:`INBOX_UNTAGGED_FRESH_DAYS`); older ones are counted in one
  line that says how to see and dismiss them, and a human's or the system's
  untagged message is never aged out;
* the brief says a message can be dismissed without acting on it;
* MCP ``get_inbox`` returns each row's project and can be narrowed to one;
* MCP ``send_to_inbox`` tags a message with the project this session's brief
  resolved, not only the static ``REMEMBRA_PROJECT``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.relay.handoff import render_brief
from remembra.security.untrusted import unwrap_untrusted
from remembra.services.agent_session import INBOX_UNTAGGED_FRESH_DAYS
from tests.agent_api_harness import build_api

STALE = "Built-in fix check FIX-BE7F45"


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _send(api: dict[str, Any], subject: str, *, age_days: float = 0.0, to: str = "codex", **kwargs: Any) -> str:
    """One message through the real InboxManager, optionally backdated."""

    async def _go() -> str:
        inbox = api["app"].state.inbox_manager
        row = await inbox.send(
            owner_user_id="default_user",
            from_agent=kwargs.pop("from_agent", "claude-code"),
            to_agent=to,
            subject=subject,
            body=kwargs.pop("body", "Reply with the code, then mark this message done."),
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


def _brief(api: dict[str, Any], project: str = "application-source", agent: str = "codex") -> dict[str, Any]:
    res = api["http"].get("/api/v1/session/brief", params={"project_id": project, "agent_id": agent})
    assert res.status_code == 200, res.text
    return dict(res.json())


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


def test_an_old_untagged_message_is_not_unread_here_in_every_project(api):
    stale = _send(api, STALE, age_days=11)
    for project in ("application-source", "yaadbooks", "anything-else"):
        brief = _brief(api, project)
        inbox = brief["inbox"]
        assert inbox["items"] == [] and inbox["unread_count"] == 0, project
        assert inbox["stale_untagged_unread"] == 1 and inbox["untagged_fresh_days"] == INBOX_UNTAGGED_FRESH_DAYS
        text = brief["rendered"]
        assert STALE not in text and stale not in text and "Reply with the code" not in text
        assert "unread here" not in text  # one line, not "0 unread here" and then the count
        assert (
            f"Inbox: 1 older unread message with no project (sent more than {INBOX_UNTAGGED_FRESH_DAYS} days ago) is not listed"
            in text
        )
        assert "get_inbox shows it" in text and "result=rejected" in text


def test_a_fresh_untagged_message_is_still_here(api):
    _send(api, "untagged and new")
    _send(api, "untagged, two days old", age_days=INBOX_UNTAGGED_FRESH_DAYS - 1)
    inbox = _brief(api)["inbox"]
    assert sorted(i["subject"] for i in inbox["items"]) == ["untagged and new", "untagged, two days old"]
    assert inbox["unread_count"] == 2 and inbox["stale_untagged_unread"] == 0
    assert "older unread message" not in _brief(api)["rendered"]


def test_an_old_message_tagged_with_this_project_stays_listed(api):
    _send(api, "old but ours", age_days=40, project_id="application-source")
    _send(api, "old and theirs", age_days=40, project_id="oorvel")
    inbox = _brief(api)["inbox"]
    assert [i["subject"] for i in inbox["items"]] == ["old but ours"]
    assert inbox["unread_count"] == 1 and inbox["other_projects_unread"] == 1 and inbox["stale_untagged_unread"] == 0


def test_an_old_untagged_message_from_the_owner_or_the_system_is_never_aged_out(api):
    _send(api, "owner decision", age_days=30, from_agent="mani", sender_kind="human", sender_verified=True)
    _send(api, "system notice", age_days=30, from_agent="remembra", sender_kind="system", sender_verified=True)
    _send(api, STALE, age_days=30)
    inbox = _brief(api)["inbox"]
    assert sorted(i["subject"] for i in inbox["items"]) == ["owner decision", "system notice"]
    assert inbox["unread_count"] == 2 and inbox["stale_untagged_unread"] == 1


def test_an_old_row_tagged_only_in_its_metadata_is_not_treated_as_untagged(api):
    """A row from before the project column names its project in metadata: it is listed as it was."""

    async def _legacy() -> None:
        db = api["app"].state.db
        await db.conn.execute(
            """
            INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata, status, created_at)
            VALUES ('inbox_legacy', 'default_user', 'planner', 'codex', 'legacy tagged', 'b',
                    '{"project_id": "application-source"}', 'unread', '2026-01-20T10:00:00+00:00')
            """
        )
        await db.conn.execute(
            """
            INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata, status, created_at)
            VALUES ('inbox_nometa', 'default_user', 'planner', 'codex', 'legacy untagged', 'b', 'not json', 'unread',
                    '2026-01-20T10:00:00+00:00')
            """
        )
        await db.conn.commit()

    api["http"].portal.call(_legacy)
    inbox = _brief(api)["inbox"]
    assert [i["subject"] for i in inbox["items"]] == ["legacy tagged"]
    assert inbox["unread_count"] == 1 and inbox["stale_untagged_unread"] == 1


def test_a_dismissed_stale_message_is_gone_from_the_count(api):
    stale = _send(api, STALE, age_days=11)
    res = api["http"].post(f"/api/v1/inbox/{stale}/ack", json={"result": "rejected", "note": "abandoned test message"})
    assert res.status_code == 200, res.text
    brief = _brief(api)
    assert brief["inbox"]["stale_untagged_unread"] == 0 and brief["inbox"]["unread_count"] == 0
    assert "Inbox:" not in brief["rendered"]


def test_the_brief_says_a_message_can_be_dismissed_without_acting_on_it(api):
    _send(api, "please review", project_id="application-source")
    text = _brief(api)["rendered"]
    assert "Inbox: 1 unread here (get_inbox for bodies; ack_inbox when done, or result=rejected to dismiss" in text


def test_a_brief_from_an_older_server_renders_as_before():
    """``render_brief`` also runs on briefs without the new field (an older server's JSON, a stored fixture)."""
    brief = {
        "project_id": "alpha",
        "agent_id": "codex",
        "handoff": None,
        "inbox": {"available": True, "agent_id": "codex", "unread_count": 2, "other_projects_unread": 1, "items": []},
    }
    text = render_brief(brief)
    assert "Inbox: 2 unread here; 1 more in other projects" in text and "older unread message" not in text
    brief["inbox"]["stale_untagged_unread"] = "three"  # a field this build cannot read is ignored, not rendered
    assert "older unread message" not in render_brief(brief)
    brief["inbox"].update(stale_untagged_unread=3, unread_count=0, other_projects_unread=0)
    text = render_brief(brief)
    assert "unread here" not in text
    assert "Inbox: 3 older unread messages with no project" in text and "are not listed" in text


# ---------------------------------------------------------------------------
# MCP get_inbox / send_to_inbox and the SDK
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_env(api, monkeypatch):
    http = api["http"]

    class ASGIMemory(Memory):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)
            self._client.close()
            self._client = http

    monkeypatch.setattr(server, "Memory", ASGIMemory)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "clawdbot")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "sess-mcp")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    return dict(json.loads(unwrap_untrusted(raw)))


def test_mcp_get_inbox_says_which_project_each_message_belongs_to(mcp_env):
    _send(mcp_env, "oorvel storefront", project_id="oorvel")
    _send(mcp_env, "ours", project_id="application-source")
    _send(mcp_env, STALE, age_days=11)
    for summary in (False, True):
        items = {i["subject"]: i for i in _j(server.get_inbox(summary=summary))["items"]}
        assert {s: i["project_id"] for s, i in items.items()} == {
            "oorvel storefront": "oorvel",
            "ours": "application-source",
            STALE: None,
        }


def test_mcp_get_inbox_can_be_narrowed_to_one_project(mcp_env):
    _send(mcp_env, "oorvel storefront", project_id="oorvel")
    _send(mcp_env, "ours", project_id="application-source")
    _send(mcp_env, STALE, age_days=11)
    out = _j(server.get_inbox(project_id="application-source"))
    assert [i["subject"] for i in out["items"]] == ["ours"] and out["project_id"] == "application-source"
    assert _j(server.get_inbox(project_id="  "))["count"] == 3  # blank: no filter
    everything = _j(server.get_inbox())
    assert everything["count"] == 3 and everything["project_id"] is None


def test_sdk_get_inbox_passes_the_project_and_scope_only_when_given(api):
    client: Memory = api["make_client"](project="clawdbot", agent_id="codex")
    _send(api, "oorvel storefront", project_id="oorvel")
    _send(api, STALE, age_days=11)
    seen: list[dict[str, Any]] = []
    real = client._request

    def spy(method: str, path: str, **kwargs: Any) -> Any:
        seen.append(dict(kwargs.get("params") or {}))
        return real(method, path, **kwargs)

    client._request = spy  # type: ignore[method-assign]
    assert len(client.get_inbox("codex")) == 2
    assert set(seen[-1]) == {"agent_id", "status", "limit"}  # as before: nothing new on the wire by default
    assert [r["subject"] for r in client.get_inbox("codex", project_id="oorvel")] == ["oorvel storefront"]
    assert seen[-1]["project_id"] == "oorvel"
    assert [r["subject"] for r in client.get_inbox("codex", scope="unscoped")] == [STALE]
    assert seen[-1]["scope"] == "unscoped"


def test_mcp_send_tags_the_project_this_sessions_brief_resolved(mcp_env):
    """Both agents on one machine share a static REMEMBRA_PROJECT; the session's own project is the one that counts."""
    assert _j(server.send_to_inbox(to_agent="claude-code", subject="before any brief", body="b"))["ok"] is True
    server.session_brief(project_id="oorvel", observe=True)  # a look elsewhere changes nothing
    assert _j(server.send_to_inbox(to_agent="claude-code", subject="after a look", body="b"))["ok"] is True
    server.session_brief(project_id="application-source")
    assert _j(server.send_to_inbox(to_agent="claude-code", subject="after the brief", body="b"))["ok"] is True
    explicit = server.send_to_inbox(to_agent="claude-code", subject="explicit", body="b", metadata={"project_id": "oorvel"})
    assert _j(explicit)["ok"] is True

    rows = mcp_env["make_client"](project="clawdbot", agent_id="claude-code").get_inbox("claude-code", status="all")
    assert {r["subject"]: r["project_id"] for r in rows} == {
        "before any brief": "clawdbot",
        "after a look": "clawdbot",
        "after the brief": "application-source",
        "explicit": "oorvel",
    }
