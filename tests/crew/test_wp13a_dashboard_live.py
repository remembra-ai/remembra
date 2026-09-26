"""WP-13a live check: Mission Control, pickup slots, baton pass and the Site Board tree
against a real Remembra server.

Starts the real FastAPI app (crew routers, crew.db, event bus, ``/ws``) with JWT
auth enabled, plays the WP-13a scenario over real HTTP (``wp13a_scenario``: an
agent key registers a host, three sessions join, a dashboard login creates the
zones, sessions create and start tasks, heartbeat, checkpoint and hit the guard,
then cc-1 stalls out of credits with a baton ref), and runs
``dashboard/src/components/crew/lane/__tests__/live.test.tsx`` under vitest
against it. That test reads the crew exactly as the dashboard does, checks the
lane, slot and tree view models and rendered markup, and performs the human
actions (hand the baton, pause and resume; refused for an API key).
Afterwards the server's own rows confirm the pass. Skipped only when the
dashboard toolchain is not installed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from remembra.auth.rbac import Role
from tests.crew.test_dashboard_live import DASHBOARD, VITEST, LiveServer, live_server  # noqa: F401  (fixture)
from tests.crew.wp13a_scenario import Scenario

pytestmark = pytest.mark.skipif(not VITEST.exists(), reason="dashboard toolchain (dashboard/node_modules) not installed")


async def _seed(app) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    users = app.state.users
    user, error = await users.create_user(email="mani@example.com", password="Str0ng!Passw0rd")
    assert user is not None, error
    token = users.create_jwt_token(user.id, "mani@example.com")
    created = await app.state.api_key_manager.create_key(user_id=user.id, name="agents")
    await app.state.role_manager.assign_role(created.id, Role("admin"))
    return token, created.key


def test_wp13a_views_and_actions_against_a_live_server(live_server: LiveServer, tmp_path: Path) -> None:  # noqa: F811
    jwt, key = live_server.call(_seed(live_server.app))
    scenario = Scenario(live_server.url, jwt, key)
    try:
        out = scenario.run()
    finally:
        scenario.close()
    assert out["guard"] == "deny"
    assert out["stall_state"] == "quota_blocked"

    env = {
        **os.environ,
        "CI": "1",
        "WP13A_LIVE_URL": live_server.url,
        "WP13A_LIVE_JWT": jwt,
        "WP13A_LIVE_KEY": key,
        "WP13A_LIVE_CREW": out["crew_id"],
    }
    proc = subprocess.run(
        [str(VITEST), "run", "src/components/crew/lane/__tests__/live.test.tsx"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-3000:]
    assert "1 passed" in proc.stdout, proc.stdout[-3000:]  # it ran (skipped without WP13A_LIVE_URL)

    # the server agrees: one human_assign baton from cc-1 to cc-2 for T-1, POS no longer reserved
    async def rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        db = live_server.app.state.crew_db
        batons = await db.fetchall(
            "SELECT kind, from_session, to_session, task_id FROM crew_batons WHERE crew_id = ?", (out["crew_id"],)
        )
        reserved = await db.fetchall("SELECT id FROM crew_claims WHERE crew_id = ? AND state = 'reserved'", (out["crew_id"],))
        return batons, reserved

    batons, reserved = live_server.call(rows())
    sessions = out["sessions"]
    assert [(b["kind"], b["from_session"], b["to_session"], b["task_id"]) for b in batons] == [
        ("human_assign", sessions["a"]["id"], sessions["c"]["id"], out["tasks"]["t1"])
    ]
    assert reserved == []
