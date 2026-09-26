"""Local preview of the Zone Map and Policy screens: the real API (crew routers, crew.db, /ws, JWT
auth) on 127.0.0.1 with the WP-13b scene, plus a slow "crew at work" loop so the live band and the
tree have something to follow. Test-only; binds to loopback; every credential is a throwaway value.

    PYTHONPATH=src python -m tests.crew.wp13b_preview_server <workdir> <port>

Prints one JSON line ``{"port", "token", "user"}`` once listening; point the dashboard dev server
at it with ``REMEMBRA_API_PROXY=http://127.0.0.1:<port> npm run dev`` and store the token as
``remembra_jwt_token`` (and the user JSON as ``remembra_user``) in the page's localStorage.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import remembra.config as config_module
from remembra.crew import claims as C
from remembra.crew import startup
from remembra.crew import zones as Z
from remembra.crew.limits import crew_limits_for_tier
from tests.crew import wp13b_scene as scene
from tests.crew.test_dashboard_live import LiveServer
from tests.security_harness import make_settings


async def crew_at_work(app, crew_id: str) -> None:  # type: ignore[no-untyped-def]
    """cc-2 takes and lets go of billing every few seconds: claim events for the live band and tree."""
    ops = Z.CrewOps(app.state.crew_events, None, crew_limits_for_tier("pro"))
    db = app.state.crew_db
    row = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_c'")
    zone = await db.fetchone("SELECT id FROM crew_zones WHERE crew_id = ? AND slug = 'billing'", (crew_id,))
    if row is None or zone is None:
        return
    who = Z.Principal.for_session(row)
    while True:
        await asyncio.sleep(6)
        try:
            got = await C.request_claim(ops, crew_id, who, zone_id=str(zone["id"]), mode="exclusive", source="mcp")
            await asyncio.sleep(6)
            if got.claim:
                live = await C._reload(db.conn, str(got.claim["id"]))  # noqa: SLF001 - preview helper
                if live["state"] == "active":
                    await C.release_claim(ops, live, who, baton=False, note="preview loop")
        except Exception as e:  # keep the preview running whatever a human does meanwhile
            print(f"preview loop: {e}", file=sys.stderr, flush=True)


def main(workdir: Path, port: int) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["REMEMBRA_CREW_DB_PATH"] = str(workdir / "crew" / "crew.db")
    os.environ.pop(startup.TAILER_ENV, None)
    config_module._settings = make_settings(auth_enabled=True)  # noqa: SLF001 - test settings
    server = LiveServer(workdir)
    server.port = port
    server.server.config.port = port
    server.start()
    seeded = server.call(scene.build(server.app))
    assert server.loop is not None
    asyncio.run_coroutine_threadsafe(crew_at_work(server.app, seeded["crew_id"]), server.loop)
    user = {"id": seeded["user_id"], "email": scene.EMAIL, "name": "Mani"}
    print(json.dumps({"port": port, "token": seeded["token"], "user": user, "crew": seeded["crew_id"]}), flush=True)
    try:
        while server.thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))
