"""A real Remembra API process for the WP-9 live tests: the crew, relay and WebSocket routers over a
real crew.db and main database, auth on, one admin API key, served by uvicorn on 127.0.0.1.

    python -m tests.crew.wp9_live_server <workdir> <port>

Prints one JSON line ``{"key": …, "port": …}`` once it is listening. Test-only; nothing here is
reachable from outside the machine.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import uvicorn

from remembra.api.v1 import websocket
from tests.crew.wp9_support import crew_server


async def main(workdir: Path, port: int) -> None:
    async with crew_server(workdir) as srv:
        srv.h.app.include_router(websocket.router)
        detach = websocket.connection_manager.attach_crew_bus(srv.h.app.state.crew_bus)
        config = uvicorn.Config(srv.h.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        print(json.dumps({"key": srv.key, "port": port}), flush=True)
        try:
            await task
        finally:
            detach()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), int(sys.argv[2])))
