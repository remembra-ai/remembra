"""WP-13b live check: the Zone Map and Policy data paths against a real Remembra server.

Starts the real FastAPI app (crew routers, startup hooks, crew.db, event bus, ``/ws``) under uvicorn
with JWT auth on, builds the scene through the real WP-5 services (``wp13b_scene``), and runs
``dashboard/src/components/crew/zones/__tests__/zones.live.test.ts`` under vitest against it: the
code the screens run (listing + overlay tree + zone states over the live store, drawer actions,
step-up with a stale login, pending-change approval, enforcement, bypass codes, the event tail,
setup mode and Undo). Afterwards the server state and the setup-mode zones.yml are checked here,
the file with the server's own parser. Skipped only when the dashboard toolchain is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from remembra.crew.policy import parse_zones_yaml
from tests.crew import wp13b_scene as scene
from tests.crew import test_dashboard_live as live

# Same harness as the WP-12 live check: real app under uvicorn, skipped without the dashboard toolchain.
pytestmark = live.pytestmark
live_server = live.live_server
DASHBOARD, VITEST, LiveServer = live.DASHBOARD, live.VITEST, live.LiveServer


def test_zone_map_and_policy_against_a_live_server(live_server: LiveServer, tmp_path: Path) -> None:
    seeded = live_server.call(scene.build(live_server.app))
    out = tmp_path / "wp13b-live.json"
    env = {
        **os.environ,
        "CI": "1",
        "CREW_LIVE_URL": live_server.url,
        "CREW_LIVE_JWT": seeded["token"],
        "CREW_LIVE_STALE_JWT": seeded["stale"],
        "CREW_LIVE_CREW": seeded["crew_id"],
        "CREW_LIVE_CREW2": seeded["crew2"],
        "CREW_LIVE_EMAIL": scene.EMAIL,
        "CREW_LIVE_PASSWORD": scene.PASSWORD,
        "CREW_LIVE_OUT": str(out),
    }
    proc = subprocess.run(
        [str(VITEST), "run", "src/components/crew/zones/__tests__/zones.live.test.ts"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-8000:] + proc.stderr[-3000:]
    assert "1 passed" in proc.stdout, proc.stdout[-3000:]  # it ran (skipped without CREW_LIVE_URL)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["reauths"] >= 2  # unfreeze and approve each asked for a fresh login

    # Setup mode's zones.yml parses with the server's parser to exactly the plan on screen.
    policy = parse_zones_yaml(report["setup_yaml"])
    assert [(z.slug, z.title, list(z.include)) for z in policy.zones] == [
        ("catalog", "catalog", ["src/catalog/**"]),
        ("till", "Checkout till", ["src/checkout/**", "src/cart/**"]),
    ]
    suggested = parse_zones_yaml(report["suggest_yaml"])
    assert sorted(z.slug for z in suggested.zones) == ["cart", "catalog", "checkout"]

    # The repo-zone edit came back as a patch against the file, not an applied change.
    assert "--- a/.remembra/zones.yml" in report["export_patch"]

    async def server_state() -> dict[str, object]:
        db = live_server.app.state.crew_db
        crew = await db.fetchone("SELECT last_seq, settings FROM crews WHERE id = ?", (seeded["crew_id"],))
        change = await db.fetchone("SELECT state, decided_by FROM crew_zone_changes WHERE id = ?", (seeded["change_id"],))
        payroll = await db.fetchone(
            "SELECT archived_at FROM crew_zones WHERE crew_id = ? AND slug = 'payroll'", (seeded["crew_id"],)
        )
        code = await db.fetchone(
            "SELECT used_at, session_id, scope FROM crew_bypass_codes WHERE id = ?", (report["bypass_code_id"],)
        )
        temp = await db.fetchall(
            "SELECT slug FROM crew_zones WHERE crew_id = ? AND source = 'suggested' AND archived_at IS NULL", (seeded["crew2"],)
        )
        return {"crew": crew, "change": change, "payroll": payroll, "code": code, "temp": temp}

    got = live_server.call(server_state())
    assert got["crew"]["last_seq"] == report["last_seq"]  # type: ignore[index]
    assert json.loads(got["crew"]["settings"])["enforcement"] == "enforce"  # type: ignore[index]
    assert got["change"] == {"state": "applied", "decided_by": seeded["user_id"]}
    assert got["payroll"]["archived_at"] is not None  # type: ignore[index]
    assert got["code"] == {"used_at": None, "session_id": "cs_b", "scope": "push"}
    assert got["temp"] == []
