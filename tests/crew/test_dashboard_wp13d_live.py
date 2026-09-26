"""WP-13d live check: the dashboard Channel, Inbox tabs, notifications and notify targets.

Starts the real FastAPI app (crew routers and startup hooks, crew.db, event bus,
``/ws``) under uvicorn with JWT auth, seeds a crew with real agent traffic
posted through the real channel service (a key-verified question to ``@mani``,
a self-declared agent's proposed decision, an ``@crew`` note), and runs
``dashboard/src/components/crew/__tests__/wp13d.live.test.ts`` under vitest
against it. The TypeScript side drives exactly what the screens run (the crew
API client, the channel/inbox/notify models and the live store over the
socket); this side then checks the server's own rows agree. The webhook target
answers its signed challenge through an in-process receiver (no network).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import remembra.config as config_module
from remembra.api.v1 import websocket
from remembra.auth.rbac import Role
from remembra.auth.users import UserManager
from remembra.crew import startup
from remembra.crew.channel import CrewChannel
from remembra.crew.decisions import CrewDecisions, CrewRef
from remembra.crew.inbox import Author, CrewInbox
from remembra.crew.notify import WebhookSender, verify_signature
from tests.crew import wp8_seed as seed
from tests.crew.test_dashboard_live import DASHBOARD, VITEST, LiveServer
from tests.crew.wp7_support import Receiver, public_resolver
from tests.security_harness import make_settings

pytestmark = pytest.mark.skipif(not VITEST.exists(), reason="dashboard toolchain (dashboard/node_modules) not installed")


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
    monkeypatch.setattr(websocket, "connection_manager", websocket.ConnectionManager())
    monkeypatch.setattr(startup, "_HOOKS", dict(startup._HOOKS))
    monkeypatch.setattr(config_module, "_settings", make_settings(auth_enabled=True))
    monkeypatch.setenv("REMEMBRA_CREW_DB_PATH", str(tmp_path / "crew" / "crew.db"))
    monkeypatch.delenv(startup.TAILER_ENV, raising=False)
    server = LiveServer(tmp_path)
    server.start()
    try:
        yield server
    finally:
        server.stop()


async def _seed(app: FastAPI, receiver: Receiver) -> dict[str, str]:
    users: UserManager = app.state.users
    user, error = await users.create_user(email="mani@example.com", password="Str0ng!Passw0rd")
    assert user is not None, error
    # Mani's own login email is verified, so it is an alert target without a mailed confirmation code
    await app.state.db.update_user_email_verified(user.id, True)
    token = users.create_jwt_token(user.id, "mani@example.com")
    key = await app.state.api_key_manager.create_key(user_id=user.id, name="agent-admin")
    await app.state.role_manager.assign_role(key.id, Role.ADMIN)
    app.state.crew_webhook_sender = WebhookSender(resolver=public_resolver, transport=receiver.transport())

    db = app.state.crew_db
    crew_id = await seed.crew(db, user.id, "yaadbooks")
    await seed.zone(db, crew_id, "zn_pos", "pos", globs=["src/app/pos/**"], title="POS section")
    await seed.zone(db, crew_id, "zn_reports", "reports", globs=["src/app/reports/**"], title="Reports")
    await seed.task(
        db, crew_id, "tsk_14", 14, "Split tender payments", status="in_progress", zone_ids=["zn_pos"], owner_session_id="cs_a"
    )
    await seed.session(db, crew_id, "cs_a", user_id=user.id, callsign="cc-1", current_task_id="tsk_14")
    await seed.session(
        db,
        crew_id,
        "cs_b",
        user_id=user.id,
        callsign="codex-1",
        agent_id="codex",
        adapter_enforcement="advisory",
        client_kind="mcp",
    )
    await seed.session(db, crew_id, "cs_c", user_id=user.id, callsign="cc-9", verified=False)
    await seed.claim(db, crew_id, "clm_pos", holder="cs_a", zone_id="zn_pos", task_id="tsk_14")

    # Agent traffic through the real services (events on the bus, inbox items, decisions).
    log = app.state.crew_events
    inbox = CrewInbox(log)
    decisions = CrewDecisions(log, inbox)
    channel = CrewChannel(log, inbox=inbox, decisions=decisions, bus=app.state.crew_bus)
    crew = CrewRef(crew_id, user.id, "yaadbooks")

    async def author(sid: str) -> Author:
        row = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (sid,))
        assert row is not None
        return Author.session(row)

    await channel.post(
        crew, await author("cs_b"), kind="question", body="@mani should GCT round per line or per invoice?", client_msg_id="q-1"
    )
    await channel.post(crew, await author("cs_c"), kind="decision", body="Receipts print the GCT line last", client_msg_id="d-1")
    await channel.post(
        crew, await author("cs_a"), kind="note", body="@crew standup: POS split tender tests are green", client_msg_id="n-1"
    )
    # A second piece of crew work, already taken by codex-1 (first taker wins).
    codex = await author("cs_b")
    await channel.post(
        crew, await author("cs_a"), kind="request_release", body="@crew who can take the receipts export?", client_msg_id="n-2"
    )
    taken = await db.fetchone(
        "SELECT id FROM crew_inbox_items WHERE crew_id = ? AND audience = 'crew' ORDER BY created_seq DESC LIMIT 1", (crew_id,)
    )
    assert taken is not None
    await inbox.claim(str(taken["id"]), claimer=codex.principal, actor=codex.actor())
    return {"token": token, "key": key.key, "crew_id": crew_id, "user_id": user.id}


def test_wp13d_screens_against_a_live_server(live_server: LiveServer, tmp_path: Path) -> None:
    receiver = Receiver()
    seeded = live_server.call(_seed(live_server.app, receiver))
    out = tmp_path / "wp13d-report.json"
    env = {
        **os.environ,
        "CI": "1",
        "CREW_LIVE_URL": live_server.url,
        "CREW_LIVE_JWT": seeded["token"],
        "CREW_LIVE_KEY": seeded["key"],
        "CREW_LIVE_CREW": seeded["crew_id"],
        "CREW_LIVE_OUT": str(out),
    }
    proc = subprocess.run(
        [str(VITEST), "run", "src/components/crew/__tests__/wp13d.live.test.ts"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-8000:] + proc.stderr[-3000:]
    assert "1 passed" in proc.stdout, proc.stdout[-3000:]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "dashboard login" in report["key_confirm_error"]
    assert report["queue_items"]["cs_a"] >= 1

    db = live_server.app.state.crew_db
    crew_id = seeded["crew_id"]

    async def rows(sql: str, *params: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in await db.fetchall(sql, params)]

    # Decisions: the human /decide is in force, the agent's proposal was confirmed by the human.
    decided = live_server.call(
        rows("SELECT title, state, decided_by_kind, confirmed_by FROM crew_decisions WHERE crew_id = ? ORDER BY number", crew_id)
    )
    assert decided == [
        {
            "title": "Receipts print the GCT line last",
            "state": "in_force",
            "decided_by_kind": "agent",
            "confirmed_by": seeded["user_id"],
        },
        {
            "title": "GCT rounds half-up per line",
            "state": "in_force",
            "decided_by_kind": "human",
            "confirmed_by": seeded["user_id"],
        },
    ]
    # The human's answer replied to the agent's question, and that Needs-you item is resolved.
    answers = live_server.call(
        rows(
            "SELECT body, reply_to_id, thread_root_id, author_kind FROM crew_messages"
            " WHERE crew_id = ? AND kind = 'answer' ORDER BY seq",
            crew_id,
        )
    )
    assert [a["author_kind"] for a in answers] == ["human", "human"]
    question = live_server.call(rows("SELECT id FROM crew_messages WHERE crew_id = ? AND kind = 'question'", crew_id))[0]["id"]
    assert all(a["thread_root_id"] == question for a in answers)
    items = live_server.call(rows("SELECT kind, state FROM crew_inbox_items WHERE crew_id = ? AND audience = 'project'", crew_id))
    assert {"kind": "human_question", "state": "resolved"} in items
    assert {"kind": "decision_to_confirm", "state": "resolved"} in items
    crew_items = live_server.call(
        rows("SELECT state, claimed_by FROM crew_inbox_items WHERE crew_id = ? AND audience = 'crew'", crew_id)
    )
    assert {"state": "claimed", "claimed_by": seeded["user_id"]} in crew_items
    assert {"state": "claimed", "claimed_by": "cs_b"} in crew_items
    # /freeze from the composer froze POS as a human hold.
    zone = live_server.call(rows("SELECT frozen_by FROM crew_zones WHERE id = 'zn_pos'"))[0]
    assert zone["frozen_by"]
    # The notification cursor moved; both targets are stored; the webhook got a correctly signed challenge.
    cursor = live_server.call(
        rows("SELECT last_seq FROM crew_read_cursors WHERE crew_id = ? AND stream = 'notifications'", crew_id)
    )
    assert cursor and cursor[0]["last_seq"] > 0
    targets = live_server.call(
        rows("SELECT kind, target, verified_at FROM crew_notify_targets WHERE user_id = ? ORDER BY kind", seeded["user_id"])
    )
    assert [(t["kind"], t["target"]) for t in targets] == [
        ("email", "mani@example.com"),
        ("webhook", "https://bridge.example.com/remembra"),
    ]
    assert all(t["verified_at"] for t in targets)
    challenges = [r for r in receiver.requests if json.loads(r.content).get("type") == "crew.notification.challenge"]
    assert len(challenges) == 1  # one signed challenge, answered before the target was stored
    assert verify_signature(report["signing_secret"], challenges[0].content, challenges[0].headers["X-Remembra-Signature"])
    settings = live_server.call(rows("SELECT settings FROM crews WHERE id = ?", crew_id))[0]["settings"]
    assert json.loads(settings)["notify"]["realtime"] == ["email", "webhook"]
