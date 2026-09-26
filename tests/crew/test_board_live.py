"""WP-13c live check: the Task Board and receipts against a real Remembra server.

Starts the real FastAPI app (crew routers and startup hooks, crew.db, JWT auth)
under uvicorn on a free local port, seeds one crew with three agent sessions
(two hooked Claude Code sessions and one MCP-only Codex session, each with a
session token) for a real dashboard user, and runs
``dashboard/src/components/crew/board/__tests__/live.test.ts`` under vitest.

That test creates tasks from the board, lets the agents start, checkpoint,
report and stall over plain HTTP, and drives the dashboard's own board code:
loading and grouping, the per-item receipt seal (checked against the server's
seal), "no report means no Done" (409), review approve, pick up a stalled
baton, per-criterion and whole-report waivers (with a stale login refused for
step-up), criteria edits after the lock with If-Match, reopen, and receipts
resolved with and without the task id. This file then checks the server's own
records agree: task states, the report invariant and the audit rows.
Skipped only when the dashboard toolchain is not installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi import FastAPI

import remembra.config as config_module
from remembra.api.v1 import websocket
from remembra.auth.users import JWT_ALGORITHM, UserManager
from remembra.crew import startup
from remembra.crew.reports import check_report_invariant
from tests.crew import wp8_seed as seed
from tests.crew.test_dashboard_live import LiveServer
from tests.security_harness import make_settings

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
VITEST = DASHBOARD / "node_modules" / ".bin" / "vitest"
LIVE_TEST = "src/components/crew/board/__tests__/live.test.ts"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not VITEST.exists(),
    reason="dashboard toolchain (node + dashboard/node_modules) not installed",
)

TOKENS = {"cs_a": "tok-board-cc1", "cs_b": "tok-board-codex1", "cs_c": "tok-board-cc2"}


@pytest.fixture
def board_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
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


def _stale_jwt(user_id: str, email: str, secret: str) -> str:
    """A valid access token whose login is 20 minutes old (step-up must refuse it)."""
    now = int(time.time())
    issued = now - 20 * 60
    payload = {"sub": user_id, "email": email, "iat": issued, "iat_ms": issued * 1000, "exp": now + 3600, "type": "access"}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


async def _seed(app: FastAPI) -> dict[str, Any]:
    users: UserManager = app.state.users
    user, error = await users.create_user(email="mani@example.com", password="Str0ng!Passw0rd")
    assert user is not None, error
    token = users.create_jwt_token(user.id, "mani@example.com")
    db = app.state.crew_db
    crew_id = await seed.crew(db, user.id, "yaadbooks")
    await seed.zone(db, crew_id, "zn_pos", "pos", globs=["src/app/pos/**"], title="POS section")
    await seed.zone(db, crew_id, "zn_reports", "reports", globs=["src/app/reports/**"], title="Reports")
    await seed.zone(db, crew_id, "zn_invoices", "invoices", globs=["src/app/invoices/**"], title="Invoices")

    def th(sid: str) -> str:
        return hashlib.sha256(TOKENS[sid].encode()).hexdigest()

    await seed.session(db, crew_id, "cs_a", user_id=user.id, callsign="cc-1", token_hash=th("cs_a"))
    await seed.session(
        db,
        crew_id,
        "cs_b",
        user_id=user.id,
        callsign="codex-1",
        agent_id="codex",
        client_kind="mcp",
        adapter="codex",
        adapter_enforcement="advisory",
        token_hash=th("cs_b"),
    )
    await seed.session(db, crew_id, "cs_c", user_id=user.id, callsign="cc-2", token_hash=th("cs_c"))
    return {
        "token": token,
        "stale": _stale_jwt(user.id, "mani@example.com", config_module.get_settings().jwt_secret),
        "crew_id": crew_id,
        "user_id": user.id,
    }


def test_task_board_against_a_live_server(board_server: LiveServer, tmp_path: Path) -> None:
    seeded = board_server.call(_seed(board_server.app))
    out = tmp_path / "board-report.json"
    env = {
        **os.environ,
        "CI": "1",
        "CREW_BOARD_URL": board_server.url,
        "CREW_BOARD_JWT": seeded["token"],
        "CREW_BOARD_STALE_JWT": seeded["stale"],
        "CREW_BOARD_CREW": seeded["crew_id"],
        "CREW_BOARD_TOKENS": json.dumps(TOKENS),
        "CREW_BOARD_OUT": str(out),
    }
    proc = subprocess.run(
        [str(VITEST), "run", LIVE_TEST],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-8000:] + proc.stderr[-3000:]
    assert "1 passed" in proc.stdout, proc.stdout[-3000:]  # it ran (skipped without CREW_BOARD_URL)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["seal_t1"] == "tests ✓ (observed) · pushed ✓ (observed)"

    crew_db = board_server.app.state.crew_db
    main_db = board_server.app.state.db

    async def facts() -> dict[str, Any]:
        tasks = await crew_db.fetchall(
            "SELECT number, status, owner_session_id FROM crew_tasks WHERE crew_id = ?", (seeded["crew_id"],)
        )
        waived = await crew_db.fetchone(
            "SELECT kind, review_state, is_current FROM crew_reports WHERE id = ?", (report["t4_report"],)
        )
        violations = await check_report_invariant(crew_db.conn, seeded["crew_id"])
        cursor = await main_db.conn.execute("SELECT action FROM audit_log WHERE user_id = ?", (seeded["user_id"],))
        audit = await cursor.fetchall()
        return {
            "tasks": {int(t["number"]): (t["status"], t["owner_session_id"]) for t in tasks},
            "waived": dict(waived) if waived else None,
            "violations": violations,
            "audit": [a["action"] for a in audit],
        }

    got = board_server.call(facts())
    assert got["tasks"][2][0] == "done"  # approved from review
    assert got["tasks"][3] == (report["t3_status"], "cs_b")  # the stalled baton went to codex-1
    assert got["tasks"][4][0] == "done"  # waived report
    assert got["tasks"][1][0] != "done"  # reopened
    assert got["waived"] == {"kind": "waived", "review_state": "waived", "is_current": 1}
    assert got["violations"] == []
    for action in ("crew_report_review", "crew_task_assign", "crew_report_waive", "crew_task_acceptance_changed"):
        assert action in got["audit"], got["audit"]
