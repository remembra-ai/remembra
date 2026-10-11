"""The Marshal desk end to end: the dashboard's own client against a live server and a local model stub.

The desk's app (``create_app()`` with auth on, over real SQLite; see
``marshal_desk_harness``) is served by uvicorn on a free local port, and the
desk's model is ``tests.marshal_stub_openai`` in its own process, reached
through ``marshal_openai_base_url`` (never the real API). One account is on
``marshal_allow_users``, a second is not. The relay has real handoffs and a
codex that picked one up and never closed.

``dashboard/src/lib/__tests__/marshalDesk.live.test.tsx`` then runs under
vitest against it, one step at a time, driving exactly what the browser runs
(loadDeskSettings, getBoard, the why? slip, the reducer, runAsk over the real
SSE stream, the rendered desk). Between steps this test changes the ledger the
way a busy day would, and checks what the dashboard can't see: no pickup
recorded, no credits spent, no model call made for a refused ask. Skipped
only when the dashboard toolchain is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

import remembra.config as config_module
from remembra.marshal.desk import budget
from tests.marshal_desk_harness import DeskHarness, desk_app

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
VITEST = DASHBOARD / "node_modules" / ".bin" / "vitest"
LIVE_TEST = "src/lib/__tests__/marshalDesk.live.test.tsx"
MODEL_DELAY_S = 0.4  # each model call, so the reads visibly stream before the answer

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not VITEST.exists(),
    reason="dashboard toolchain (node + dashboard/node_modules) not installed",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_stub(port: int) -> subprocess.Popen[bytes]:
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
    env.pop("OPENAI_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.marshal_stub_openai", "--port", str(port), "--delay", str(MODEL_DELAY_S)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the stub exited with {proc.returncode}")
        try:
            httpx.get(f"http://127.0.0.1:{port}/_stub/requests", timeout=0.5)
            return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("the stub did not start")


async def _stub_calls(stub: str) -> int:
    async with httpx.AsyncClient(timeout=5) as http:
        return len((await http.get(f"{stub}/_stub/requests")).json())


async def _step(name: str, url: str, env: dict[str, str], out: Path) -> dict[str, Any]:
    """Run one step of the live vitest file against ``url``; its report is what the dashboard saw."""
    run_env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "TYPESAFE_API_KEY")}
    run_env.update(env)
    run_env.update(
        {
            "CI": "1",
            "VITE_API_URL": url,  # the client's API_V1
            "MARSHAL_LIVE_URL": url,
            "MARSHAL_LIVE_STEP": name,
            "MARSHAL_LIVE_OUT": str(out),
            "MARSHAL_LIVE_MODEL_DELAY_MS": str(int(MODEL_DELAY_S * 1000)),
        }
    )
    proc = await asyncio.create_subprocess_exec(
        str(VITEST),
        "run",
        LIVE_TEST,
        cwd=DASHBOARD,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
    text = stdout.decode(errors="replace")
    assert proc.returncode == 0, text[-6000:] + stderr.decode(errors="replace")[-3000:]
    assert "1 passed" in text, text[-3000:]  # the step ran (the file is skipped without MARSHAL_LIVE_URL)
    return dict(json.loads(out.read_text(encoding="utf-8")))


async def _credits(h: DeskHarness, jwt: dict[str, str]) -> dict[str, Any]:
    res = await h.http.get("/api/v1/cloud/usage/summary", headers=jwt)
    assert res.status_code == 200, res.text
    credits = res.json()["credits"]
    return {k: credits[k] for k in ("used", "reserved", "llm_usd_used")}


async def test_the_dashboard_desk_against_a_live_server(tmp_path: Path) -> None:
    stub_port = _free_port()
    stub = f"http://127.0.0.1:{stub_port}"
    proc = _start_stub(stub_port)
    try:
        async with desk_app(tmp_path, marshal_openai_base_url=f"{stub}/v1") as h:
            assert h.app.state.marshal_llm_transport is None  # the model is the stub, over the network
            owner = await h.create_user("owner@example.com")
            other = await h.create_user("other@example.com")
            config_module._settings = h.settings.model_copy(update={"marshal_allow_users": [owner]})
            jwt = h.jwt(owner, "owner@example.com")
            claude = await h.api_key(owner, agent_id="claude-code")
            codex = await h.api_key(owner, agent_id="codex")
            await h.seed_handoff(claude, agent="claude-code", next_step="wire the parser tests")
            await h.seed_pickup(codex, reader="codex")
            await h.seed_handoff(claude, agent="claude-code", next_step="update the README")

            port = _free_port()
            server = uvicorn.Server(uvicorn.Config(h.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
            serving = asyncio.create_task(server.serve())
            deadline = time.monotonic() + 20
            while not server.started:
                assert time.monotonic() < deadline and not serving.done(), "the live server did not start"
                await asyncio.sleep(0.05)
            url = f"http://127.0.0.1:{port}"
            accounts = {
                "MARSHAL_LIVE_JWT": jwt["Authorization"].removeprefix("Bearer "),
                "MARSHAL_LIVE_JWT_OTHER": h.jwt(other, "other@example.com")["Authorization"].removeprefix("Bearer "),
                "MARSHAL_LIVE_API_KEY": claude["X-API-Key"],
            }
            try:
                # -- ask: a streamed why? answer and a follow-up; the other account and a key refused --------
                pickups = await h.count("relay_pickups")
                memories = await h.count("memories")
                credits = await _credits(h, jwt)
                seen = await _step("ask", url, accounts, tmp_path / "ask.json")
                assert seen["board"]["calls"][0]["code"] == "PICKS_UP_NEVER_CLOSES"
                assert [r["tool"] for r in seen["first"]["reads"]] == ["diagnose_agent", "trail_summary"]
                assert seen["first"]["read_to_answer_ms"] >= 2 * MODEL_DELAY_S * 1000 * 0.8
                assert [e["ref"] for e in seen["first"]["answer"]["evidence"]] == ["r1", "r2"]
                assert seen["rendered_footer"].endswith("· not billed to your credits")
                assert seen["opted_out"] == {"board": 403, "ask": 403}
                assert await h.count("marshal_prefs", "user_id = ? AND desk = 1", (owner,)) == 1  # turned back on
                assert seen["other_account"] == {"settings": "off", "board": 404, "ask": 404}
                assert seen["api_key"] == {"ask": 403, "ask_error": "marshal_login_required", "board": 403}
                # Nothing the desk read was written back: no pickup, no memory, no smart credits.
                assert await h.count("relay_pickups") == pickups
                assert await h.count("memories") == memories
                assert await _credits(h, jwt) == credits
                assert await _stub_calls(stub) == 4  # two asks, two model calls each

                # -- offline: the platform's $5 for the day is spent ------------------------------------------
                day = f"day:{budget.day_key(datetime.now(UTC))}"
                async with h.db.transaction():
                    cursor = await h.db.conn.execute("SELECT spent_micro FROM marshal_budget WHERE period = ?", (day,))
                    spent = int((await cursor.fetchone())[0])
                    await h.db.conn.execute("UPDATE marshal_budget SET spent_micro = ? WHERE period = ?", (5_000_000, day))
                seen = await _step("offline", url, accounts, tmp_path / "offline.json")
                assert seen["refused"]["data"]["reason"] == "daily_budget"
                assert await _stub_calls(stub) == 4  # refused before any model call
                assert await h.count("marshal_reservations") == 2  # and before a hold was written

                # -- limit: the account's 40th ask answers, the 41st is refused -------------------------------
                async with h.db.transaction():
                    await h.db.conn.execute("UPDATE marshal_budget SET spent_micro = ? WHERE period = ?", (spent, day))
                    await h.db.conn.execute(
                        "UPDATE marshal_user_day SET asks = 39 WHERE user_id = ? AND day = ?", (owner, day.removeprefix("day:"))
                    )
                seen = await _step("limit", url, accounts, tmp_path / "limit.json")
                assert (seen["answered"], seen["asks_today"]) == (1, 40)
                assert seen["refused"]["status"] == 429 and seen["refused"]["data"]["used"] == 40
                assert await _stub_calls(stub) == 6
                assert await h.count("marshal_reservations", "state = 'settled'") == 3
                assert await h.count("relay_pickups") == pickups
                assert await _credits(h, jwt) == credits
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, timeout=20)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
