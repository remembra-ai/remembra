"""Crew text never stores or fans out credentials, and a human redaction removes the body from the event log.

* Channel messages (post and edit), task title and body (create and patch) and
  decision title and text go through the same redaction as the agent inbox
  (``remembra.security.secrets.scrub``) before they are stored or emitted.
* ``CrewChannel.redact`` tombstones the ``message.posted`` / ``message.edited``
  events that carried the body, so ``fetch_events`` (the REST feed and the
  WebSocket ``since_seq`` replay) no longer returns it; the chain still
  verifies; mention deliveries still queued lose the body.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from remembra.crew.events import fetch_events, verify_crew_chain
from remembra.crew.inbox import Author
from remembra.crew.tasks import Caller, TaskService
from tests.crew.wp7_support import CREW_A, OWNER, add_session, make_env

TOKEN = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
TOKEN2 = "ghp_" + "zY9xW7vU5tS3rQ1pN9mL7kJ5hG3fE1dC9bA7"


async def _all_event_text(db) -> str:  # noqa: ANN001
    rows = await db.fetchall("SELECT payload, summary, refs FROM crew_events WHERE crew_id = ?", (CREW_A,))
    return json.dumps(rows)


async def test_message_bodies_are_redacted_before_storage_and_events(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    try:
        a = await add_session(env.db, CREW_A, "cs_a", callsign="cc-1", agent_id="claude-code")
        res = await env.channel.post(env.crew, a, kind="chat", body=f"deploy token is {TOKEN} use it", client_msg_id="m1")
        assert TOKEN not in res.message["body"] and "[REDACTED:github_token]" in res.message["body"]
        row = await env.db.fetchone("SELECT body FROM crew_messages WHERE id = ?", (res.message["id"],))
        assert TOKEN not in row["body"]
        assert TOKEN not in await _all_event_text(env.db)
        assert all(TOKEN not in json.dumps(e) for e in env.events)

        edited = await env.channel.edit(res.message["id"], a, body=f"new token {TOKEN2}", now=datetime.now(UTC))
        assert TOKEN2 not in edited["message"]["body"]
        assert TOKEN2 not in await _all_event_text(env.db)
        history = await env.channel.history(res.message["id"])
        assert all(TOKEN not in h["prev_body"] for h in history)

        # a kind=decision message: the decision row and its events are redacted too
        human = Author.human(OWNER, privileged=True)
        await env.channel.post(env.crew, human, kind="decision", body=f"Use key {TOKEN}\nfor staging", client_msg_id="d1")
        rows = await env.db.fetchall("SELECT title, decision FROM crew_decisions WHERE crew_id = ?", (CREW_A,))
        assert rows and all(TOKEN not in (r["title"] + r["decision"]) for r in rows)
        assert TOKEN not in await _all_event_text(env.db)
    finally:
        await env.db.close()


async def test_decisions_are_redacted(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    try:
        a = await add_session(env.db, CREW_A, "cs_a", callsign="cc-1", agent_id="claude-code")
        out = await env.decisions.create(
            env.crew,
            a,
            title=f"rotate {TOKEN}",
            decision=f"the key {TOKEN} is retired",
            rationale=f"it leaked: {TOKEN}",
            alternatives=[f"keep {TOKEN}"],
        )
        blob = json.dumps(out) + await _all_event_text(env.db)
        row = await env.db.fetchone("SELECT * FROM crew_decisions WHERE id = ?", (out["id"],))
        assert TOKEN not in blob and TOKEN not in json.dumps(dict(row))
    finally:
        await env.db.close()


async def test_task_title_and_body_are_redacted(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    try:
        svc = TaskService(env.log)
        human = Caller.for_human(OWNER, privileged=True)
        res = await svc.create(
            CREW_A,
            human,
            {"title": f"use {TOKEN}", "body": f"export GH={TOKEN}", "zone_ids": [], "acceptance": [], "depends_on": []},
        )
        task = res.task
        row = await env.db.fetchone("SELECT title, body FROM crew_tasks WHERE id = ?", (task["id"],))
        assert TOKEN not in row["title"] and TOKEN not in row["body"]
        patched = await svc.patch(CREW_A, task["id"], human, {"body": f"now {TOKEN2}"}, if_match=int(task["version"]))
        assert TOKEN2 not in json.dumps(patched.task)
        row = await env.db.fetchone("SELECT body FROM crew_tasks WHERE id = ?", (task["id"],))
        assert TOKEN2 not in row["body"]
        assert TOKEN not in await _all_event_text(env.db) and TOKEN2 not in await _all_event_text(env.db)
    finally:
        await env.db.close()


async def test_a_redacted_message_leaves_the_event_feed_and_the_chain_verifies(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    try:
        a = await add_session(env.db, CREW_A, "cs_a", callsign="cc-1", agent_id="claude-code")
        await add_session(env.db, CREW_A, "cs_old", callsign="codex-1", agent_id="codex", state="ended")
        other = await env.channel.post(env.crew, a, kind="note", body="keep this note", client_msg_id="k1")
        secret = "customer Jane Roe lives at 12 Hope Road"  # not a credential: only a human redaction removes it
        res = await env.channel.post(env.crew, a, kind="chat", body=f"{secret} (@codex fyi)", client_msg_id="m1")
        await env.channel.edit(res.message["id"], a, body=f"{secret}, apt 4 (@codex fyi)", now=datetime.now(UTC))
        # a mention to an agent with no live session waits in the outbox with the body
        queued = await env.db.fetchall("SELECT payload FROM crew_outbox WHERE crew_id = ?", (CREW_A,))
        assert any(secret in r["payload"] for r in queued)
        before = await fetch_events(env.db.conn, CREW_A, after_seq=0)
        assert any(secret in json.dumps(e) for e in before)

        out = await env.channel.redact(res.message["id"], Author.human(OWNER, privileged=True))

        assert out["message"]["body"] == "" and out["message"]["redacted"]
        after = await fetch_events(env.db.conn, CREW_A, after_seq=0)
        assert [e["seq"] for e in after] == [e["seq"] for e in before] + [out["seq"]]
        assert not any(secret in json.dumps(e) for e in after)
        removed = [e for e in after if e["summary"] == "removed (message redacted by a human)"]
        assert sorted(e["type"] for e in removed) == ["message.edited", "message.posted"]
        assert any("keep this note" in json.dumps(e) for e in after)  # other messages untouched
        assert other.message["id"]
        queued = await env.db.fetchall("SELECT payload FROM crew_outbox WHERE crew_id = ?", (CREW_A,))
        assert queued and not any(secret in r["payload"] for r in queued)
        report = await verify_crew_chain(env.db.conn, CREW_A)
        assert report.ok, report.errors
        assert report.tombstoned == 2
        # redacting again is a no-op
        again = await env.channel.redact(res.message["id"], Author.human(OWNER, privileged=True))
        assert again["seq"] is None
    finally:
        await env.db.close()
