"""WP-13e live check: the dashboard Event Feed's data path against a real Remembra server.

Starts the real FastAPI app (crew routers, crew.db, event bus, ``/ws``) with JWT
auth on a free local port, seeds a crew and then a few hundred events through
the real event log (``CrewEventLog.emit``: seq, hash chain, moment rules and the
closed-contract validation), one of every L0 type from the shared contract
samples, and runs ``dashboard/src/components/crew/feed/__tests__/live.test.ts``
under vitest against it. That test drives the code the browser runs: the crew
runtime and its shared socket, ``FeedLog`` tapping that socket, the events
endpoint for the first window, gap filling for a tap that drops frames, older
pages down to seq 1, the polling follow, and the feed model over the result.
Skipped only when the dashboard toolchain is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import remembra.config as config_module
from remembra.api.v1 import websocket
from remembra.crew import startup
from remembra.crew.events import Actor
from tests.crew.test_dashboard_live import LiveServer, _seed
from tests.security_harness import make_settings

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
VITEST = DASHBOARD / "node_modules" / ".bin" / "vitest"
SAMPLES = REPO / "tests" / "crew" / "vectors" / "events" / "samples.json"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not VITEST.exists(),
    reason="dashboard toolchain (node + dashboard/node_modules) not installed",
)

SEEDED_EVENTS = 330


@pytest.fixture
def feed_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
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


async def _seed_events(app: FastAPI, crew_id: str, count: int) -> int:
    """Emit ``count`` events through the real event log, cycling through every L0 contract sample."""
    samples: list[dict[str, Any]] = json.loads(SAMPLES.read_text(encoding="utf-8"))["valid"]
    log = app.state.crew_events
    emitted = 0
    while emitted < count:
        for sample in samples:
            if emitted >= count:
                break
            actor = Actor(**sample["actor"])
            await log.emit(
                crew_id=crew_id,
                type=sample["type"],
                actor=actor,
                payload=sample["payload"],
                summary=f"{sample['type']} (seeded {emitted + 1})",
                severity=sample["severity"],
                refs=sample["refs"],
                origin=sample["origin"],
            )
            emitted += 1
    row = await app.state.crew_db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (crew_id,))
    return int(row["last_seq"])


def test_event_feed_against_a_live_server(feed_server: LiveServer, tmp_path: Path) -> None:
    seeded = feed_server.call(_seed(feed_server.app))
    head = feed_server.call(_seed_events(feed_server.app, seeded["crew_id"], SEEDED_EVENTS))
    assert head >= SEEDED_EVENTS
    out = tmp_path / "feed-live-report.json"
    env = {
        **os.environ,
        "CI": "1",
        "FEED_LIVE_URL": feed_server.url,
        "FEED_LIVE_JWT": seeded["token"],
        "FEED_LIVE_CREW": seeded["crew_id"],
        "FEED_LIVE_OUT": str(out),
    }
    proc = subprocess.run(
        [str(VITEST), "run", "src/components/crew/feed/__tests__/live.test.ts"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-3000:]
    assert "1 passed" in proc.stdout, proc.stdout[-3000:]  # the live test ran (skipped without FEED_LIVE_URL)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["window"] == 120
    assert report["live_rest_calls"] == 0  # live events arrived over the WebSocket, not by polling
    assert report["lossy_contiguous"] is True
    assert report["live_arrivals"] >= 5

    async def last_seq() -> int:
        row = await feed_server.app.state.crew_db.fetchone("SELECT last_seq FROM crews WHERE id = ?", (seeded["crew_id"],))
        return int(row["last_seq"])

    server_head = feed_server.call(last_seq())
    assert report["head"] == server_head
    assert report["total_loaded"] == server_head - 1  # everything up to the head before the last note
