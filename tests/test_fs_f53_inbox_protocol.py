"""F53: the agent inbox has an ``accepted`` state and a checked, first-class reply link.

* A recipient could only mark a message ``read`` (which removed it from its brief and its default
  ``get_inbox``) or finished; now ``accepted`` means "taken on, not finished", it stays visible to
  the recipient (``status=open`` / ``accepted``, the brief), and a plain ack can no longer turn a
  ``done`` row back into ``read``.
* ``in_reply_to`` was a key inside free-form metadata that nothing checked or showed. Sent as a
  field it must name a message the sender can see; it is returned top-level on every list, in the
  MCP ``get_inbox`` (summary too) and in the brief. The metadata key older clients send is stored
  as before.

Real routes, real keys and a real SQLite inbox (``security_harness``); the MCP tools run against the
production routes through ``agent_api_harness``.
"""

from __future__ import annotations

from typing import Any

import pytest

import remembra.mcp.server as server
import tests.test_mcp_agent_tools as mcp_tools
from remembra.api.v1 import inbox as inbox_api
from remembra.auth.rbac import Role
from remembra.client.memory import MemoryError as ClientError
from remembra.inbox import manager as inbox_manager
from remembra.inbox.manager import TERMINAL_STATUSES, VALID_INBOX_STATUSES, InboxManager
from remembra.relay.handoff import render_brief
from tests.security_harness import Harness, secure_app
from tests.test_mcp_agent_tools import _j

api = mcp_tools.api  # the production routes behind the MCP tools (agent_api_harness)
mcp_env = mcp_tools.mcp_env


@pytest.fixture()
async def h(tmp_path):
    async with secure_app(tmp_path, [inbox_api.router]) as harness:
        manager = InboxManager(harness.db)
        await manager.init_schema()
        harness.app.state.inbox_manager = manager
        harness.created["inbox"] = manager
        yield harness


async def _key(h: Harness, user_id: str = "u1", agent: str | None = "codex") -> dict[str, str]:
    created = await h.keys.create_key(user_id=user_id, name="k", agent_id=agent)
    await h.roles.assign_role(created.id, Role("editor"))
    return {"X-API-Key": created.key}


MSG = {"to_agent": "claude-code", "subject": "Deploy check", "body": "Is v0.17.6 live?", "project_id": "remembra"}


async def _send(h: Harness, key: dict[str, str], **change: Any):
    return await h.client.post("/api/v1/inbox/send", headers=key, json={**MSG, **change})


async def _inbox(h: Harness, key: dict[str, str], agent: str, status: str | None = None) -> list[dict[str, Any]]:
    params = {"agent_id": agent, **({"status": status} if status else {})}
    resp = await h.client.get("/api/v1/inbox", headers=key, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _ack(h: Harness, key: dict[str, str], inbox_id: str, **body: Any):
    return await h.client.post(f"/api/v1/inbox/{inbox_id}/ack", headers=key, json=body)


# ---------------------------------------------------------------------------
# accepted
# ---------------------------------------------------------------------------


def test_the_status_model_names_accepted_as_open():
    assert set(VALID_INBOX_STATUSES) == {"unread", "read", "accepted", "done", "blocked", "rejected"}
    open_statuses = set(inbox_manager.OPEN_STATUSES)
    assert open_statuses == {"unread", "read", "accepted"} and not open_statuses & TERMINAL_STATUSES


async def test_a_message_can_be_accepted_and_stays_visible_to_its_recipient(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    inbox_id = (await _send(h, codex)).json()["inbox_id"]
    acked = await _ack(h, claude, inbox_id, result="accepted", note="on it after the deploy")
    assert acked.status_code == 200, acked.text
    assert acked.json()["status"] == "accepted" and acked.json()["ack_result"] == "accepted"
    assert acked.json()["ack_note"] == "on it after the deploy"
    assert await _inbox(h, claude, "claude-code") == []  # not unread any more
    for status in ("open", "accepted", "all"):
        rows = await _inbox(h, claude, "claude-code", status)
        assert [(r["inbox_id"], r["status"]) for r in rows] == [(inbox_id, "accepted")], status
    listed = await h.client.get("/api/v1/inbox/messages", headers=claude, params={"status": "open"})
    assert [r["status"] for r in listed.json()["items"]] == ["accepted"], listed.text
    only = await h.client.get("/api/v1/inbox/messages", headers=claude, params={"status": "accepted"})
    assert only.status_code == 200 and only.json()["total"] == 1, only.text
    counts = (await h.client.get("/api/v1/inbox/summary", headers=claude)).json()
    assert counts["open_total"] == 1 and counts["unread_total"] == 0, counts
    # and it is finished the usual way
    done = await _ack(h, claude, inbox_id, result="done", note="deployed")
    assert done.json()["status"] == "done" and await _inbox(h, claude, "claude-code", "open") == []


async def test_a_plain_ack_never_reopens_or_downgrades_a_row(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    done_id = (await _send(h, codex)).json()["inbox_id"]
    taken_id = (await _send(h, codex, subject="Second")).json()["inbox_id"]
    await _ack(h, claude, done_id, result="done", note="shipped")
    await _ack(h, claude, taken_id, result="accepted")
    for inbox_id, status in ((done_id, "done"), (taken_id, "accepted")):
        again = await _ack(h, claude, inbox_id)  # "mark read" on a row that is past read
        assert again.status_code == 200, again.text
        assert again.json()["status"] == status and again.json()["ack_result"] == status, again.text
    assert (await _ack(h, claude, done_id)).json()["ack_note"] == "shipped"  # a plain ack without a note keeps the note
    noted = await _ack(h, claude, taken_id, note="waiting on the migration")
    assert noted.json()["status"] == "accepted" and noted.json()["ack_note"] == "waiting on the migration"
    # the unread -> read step older clients use is unchanged
    plain_id = (await _send(h, codex, subject="Third")).json()["inbox_id"]
    assert (await _ack(h, claude, plain_id)).json()["status"] == "read"
    assert {r["inbox_id"] for r in await _inbox(h, claude, "claude-code", "open")} == {plain_id, taken_id}


async def test_bad_ack_results_and_filters_are_refused(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    inbox_id = (await _send(h, codex)).json()["inbox_id"]
    assert (await _ack(h, claude, inbox_id, result="maybe")).status_code == 422
    bad = await h.client.get("/api/v1/inbox", headers=claude, params={"agent_id": "claude-code", "status": "closed"})
    assert bad.status_code == 422
    manager: InboxManager = h.created["inbox"]
    with pytest.raises(ValueError, match="result must be one of"):
        await manager.ack("u1", inbox_id, result="maybe")
    with pytest.raises(ValueError, match="status must be"):
        await manager.get_for_agent("u1", "claude-code", status="closed")
    with pytest.raises(ValueError, match="not found"):
        await manager.ack("u1", "inbox_0000000000000000", result="accepted")


# ---------------------------------------------------------------------------
# in_reply_to
# ---------------------------------------------------------------------------


async def test_in_reply_to_must_name_a_message_the_sender_can_see(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    original = (await _send(h, codex)).json()["inbox_id"]
    missing = await _send(h, claude, to_agent="codex", subject="Re", body="yes", in_reply_to="inbox_0123456789abcdef")
    assert missing.status_code == 422 and missing.json()["detail"]["error"] == "in_reply_to_not_found", missing.text
    for bad in ("msg_01ABC", "inbox_", "inbox_ZZZZ", "not an id"):
        resp = await _send(h, claude, to_agent="codex", subject="Re", body="yes", in_reply_to=bad)
        assert resp.status_code == 422 and resp.json()["detail"]["error"] == "in_reply_to_invalid", (bad, resp.text)
    # another account's message is "no message you can see", the same answer as a missing one
    other = await _key(h, user_id="u2", agent="claude-code")
    foreign = await _send(h, other, to_agent="codex", subject="Re", body="yes", in_reply_to=original)
    assert foreign.status_code == 422 and foreign.json()["detail"]["error"] == "in_reply_to_not_found"
    # a key bound to an agent that is no party to the original cannot link to it either
    gemini = await _key(h, agent="gemini")
    third = await _send(h, gemini, to_agent="codex", subject="Re", body="yes", in_reply_to=original)
    assert third.status_code == 422 and third.json()["detail"]["error"] == "in_reply_to_not_found"
    rows = await _inbox(h, codex, "codex", "all")
    assert rows == []  # nothing was stored by the refused sends


async def test_a_reply_is_linked_first_class_and_filed_with_its_original(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    original = (await _send(h, codex, project_id="beta")).json()["inbox_id"]
    reply = await h.client.post(
        "/api/v1/inbox/send",
        headers=claude,
        json={"to_agent": "codex", "subject": "Re: Deploy check", "body": "yes, live", "in_reply_to": original},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["project_id"] == "beta"  # no project named: the original's
    (row,) = await _inbox(h, codex, "codex")
    assert row["in_reply_to"] == original and row["metadata"]["in_reply_to"] == original and row["project_id"] == "beta"
    owner = await _key(h, agent=None)  # a key bound to no agent lists every agent's messages
    listed = (await h.client.get("/api/v1/inbox/messages", headers=owner, params={"status": "all"})).json()["items"]
    assert {r["inbox_id"]: r["in_reply_to"] for r in listed} == {row["inbox_id"]: original, original: None}
    # an explicit project in the body is kept; the sender of the original can reply into its own thread too
    explicit = await _send(h, codex, subject="PS", body="and the worker?", in_reply_to=original, project_id="remembra")
    assert explicit.status_code == 201 and explicit.json()["project_id"] == "remembra", explicit.text
    # sending the same reply again is the same message, not a second one
    again = await h.client.post(
        "/api/v1/inbox/send",
        headers=claude,
        json={"to_agent": "codex", "subject": "Re: Deploy check", "body": "yes, live", "in_reply_to": original},
    )
    assert again.status_code == 200 and again.json()["inbox_id"] == reply.json()["inbox_id"], again.text


async def test_the_metadata_key_older_clients_send_is_stored_as_before_and_shown(h):
    codex, claude = await _key(h), await _key(h, agent="claude-code")
    legacy = await _send(h, codex, metadata={"in_reply_to": "inbox_0123456789abcdef"})  # names nothing: still delivered
    assert legacy.status_code == 201, legacy.text
    odd = await _send(h, codex, subject="Odd", metadata={"in_reply_to": {"not": "an id"}})
    assert odd.status_code == 201, odd.text
    rows = {r["inbox_id"]: r for r in await _inbox(h, claude, "claude-code")}
    assert rows[legacy.json()["inbox_id"]]["in_reply_to"] == "inbox_0123456789abcdef"
    assert rows[odd.json()["inbox_id"]]["in_reply_to"] is None
    assert rows[odd.json()["inbox_id"]]["metadata"]["in_reply_to"] == {"not": "an id"}
    plain = await _send(h, codex, subject="Plain")
    assert plain.status_code == 201
    rows = {r["inbox_id"]: r for r in await _inbox(h, claude, "claude-code")}
    assert rows[plain.json()["inbox_id"]]["in_reply_to"] is None


# ---------------------------------------------------------------------------
# MCP tools and the brief
# ---------------------------------------------------------------------------


def test_mcp_accept_shows_in_get_inbox_and_in_the_next_brief(mcp_env):
    codex = mcp_env["make_client"](project="alpha", agent_id="codex")
    sent = codex.send_to_inbox(to_agent="claude-code", subject="review the POS diff", body="details " * 20)
    accepted = _j(server.ack_inbox(sent["inbox_id"], result="accepted", note="after lunch"))
    assert accepted["ok"] is True and accepted["row_status"] == "accepted", accepted
    assert _j(server.get_inbox())["count"] == 0
    for status in ("open", "accepted"):
        items = _j(server.get_inbox(status=status))["items"]
        assert [(i["inbox_id"], i["status"]) for i in items] == [(sent["inbox_id"], "accepted")], status
    brief = _j(server.session_brief())
    assert brief["inbox"]["unread_count"] == 0 and brief["inbox"]["accepted_count"] == 1, brief["inbox"]
    assert brief["inbox"]["accepted_items"][0]["inbox_id"] == sent["inbox_id"]
    assert "1 accepted (taken on, not finished" in brief["brief"], brief["brief"]
    assert f"[{sent['inbox_id']}]" in brief["brief"] and "review the POS diff" in brief["brief"]
    assert _j(server.ack_inbox(sent["inbox_id"], result="done"))["row_status"] == "done"
    assert "accepted (taken on" not in _j(server.session_brief())["brief"]


def test_mcp_reply_link_is_an_argument_checked_and_shown_everywhere(mcp_env):
    codex = mcp_env["make_client"](project="beta", agent_id="codex")
    original = codex.send_to_inbox(
        to_agent="claude-code", subject="check CONT-1", body="reply please", metadata={"project_id": "beta"}
    )
    reply = _j(server.send_to_inbox(to_agent="codex", subject="Re: check CONT-1", body="ok", in_reply_to=original["inbox_id"]))
    assert reply["ok"] is True and reply["in_reply_to"] == original["inbox_id"], reply
    rows = {r["inbox_id"]: r for r in codex.get_inbox(agent_id="codex", status="all")}
    got = rows[reply["inbox_id"]]
    assert got["project_id"] == "beta" and got["in_reply_to"] == original["inbox_id"]
    assert got["metadata"]["in_reply_to"] == original["inbox_id"]  # what a 0.17.5 reader looks at
    # shown top-level by get_inbox, in summary mode too
    monkey_agent = _j(server.get_inbox(agent_id="codex", summary=True))["items"]
    assert monkey_agent[0]["in_reply_to"] == original["inbox_id"] and "metadata" not in monkey_agent[0]
    assert _j(server.get_inbox(agent_id="codex"))["items"][0]["in_reply_to"] == original["inbox_id"]
    # the argument is checked: an id that names nothing is refused, and nothing is sent
    unknown = _j(server.send_to_inbox(to_agent="codex", subject="x", body="x", in_reply_to="inbox_0123456789ab"))
    assert unknown["ok"] is False and "no message you can see" in unknown["error"], unknown
    malformed = _j(server.send_to_inbox(to_agent="codex", subject="x", body="x", in_reply_to="msg_01ABC"))
    assert malformed["ok"] is False and "inbox_" in malformed["error"], malformed
    assert len(codex.get_inbox(agent_id="codex", status="all")) == 1
    # the brief of the agent that got the reply names what it answers
    codex_brief = codex.session_brief()
    item = codex_brief["inbox"]["items"][0]
    assert item["in_reply_to"] == original["inbox_id"]
    text = render_brief(codex_brief)
    assert f"(reply to {original['inbox_id']})" in text, text


def test_a_fixed_client_still_works_against_a_0_17_5_server(mcp_env, monkeypatch):
    """The deployed server knows neither ``accepted`` nor ``status=open``: it answers 422. The tools
    say what to do instead of failing blind, and ``open`` is answered from the full list."""
    codex = mcp_env["make_client"](project="alpha", agent_id="codex")
    first = codex.send_to_inbox(to_agent="claude-code", subject="one", body="b")
    second = codex.send_to_inbox(to_agent="claude-code", subject="two", body="b")
    server.ack_inbox(first["inbox_id"])  # read
    server.ack_inbox(second["inbox_id"], result="done")
    client = server._get_client()
    real_get, real_ack = client.get_inbox, client.ack_inbox

    def old_get(agent_id: str, status: str = "unread", limit: int = 20) -> Any:
        if status not in ("unread", "all"):
            raise ClientError("Input should be 'unread' or 'all'", status_code=422)
        rows = real_get(agent_id=agent_id, status=status, limit=limit)
        return [{k: v for k, v in r.items() if k != "in_reply_to"} for r in rows]

    def old_ack(inbox_id: str, result: str | None = None, note: str | None = None) -> Any:
        if result == "accepted":
            raise ClientError("Input should be 'done', 'blocked' or 'rejected'", status_code=422)
        return real_ack(inbox_id=inbox_id, result=result, note=note)

    monkeypatch.setattr(client, "get_inbox", old_get)
    monkeypatch.setattr(client, "ack_inbox", old_ack)
    opened = _j(server.get_inbox(status="open"))
    assert opened["ok"] is True and [i["subject"] for i in opened["items"]] == ["one"], opened
    assert _j(server.get_inbox(status="accepted"))["items"] == []
    refused = _j(server.ack_inbox(first["inbox_id"], result="accepted"))
    assert refused["ok"] is False and "does not support" in refused["error"] and "accepted" in refused["error"], refused
    assert _j(server.ack_inbox(first["inbox_id"], result="done"))["row_status"] == "done"


def test_an_old_brief_without_the_new_fields_still_renders():
    brief = {
        "agent_id": "claude-code",
        "project_id": "alpha",
        "inbox": {
            "available": True,
            "unread_count": 1,
            "items": [{"inbox_id": "inbox_aaaaaaaaaaaaaaaa", "from_agent": "codex", "subject": "s", "body_preview": "b"}],
        },
    }
    text = render_brief(brief)
    assert "Inbox: 1 unread here" in text and "accepted" not in text and "reply to" not in text
