"""The desk over a real network path to a local OpenAI-compatible stub (never the real API).

``tests.marshal_stub_openai`` runs under uvicorn in its own process on a free
port; the desk is pointed at it with ``marshal_openai_base_url`` and no test
transport, so each model call goes through the real OpenAI SDK, the desk's
breaker transport and a real HTTP connection. One "why?" ask streams Example
A's grammar: the pre-read, one read the model asked for, a validated answer,
usage and done. A model slower than the client's timeout still received the
request, so the ask is charged at its bound and counted; a server that refuses
the connection received nothing, so the ask is given back.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from remembra.core import ai_spend
from remembra.marshal.desk.budget import to_micro
from remembra.marshal.desk.llm import upper_bound_usd
from tests.marshal_desk_harness import desk_app, event_names, sse_events

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_up(url: str, proc: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the stub exited with {proc.returncode}")
        try:
            httpx.get(url, timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError("the stub did not start")


def _start_stub(port: int, *args: str) -> subprocess.Popen[bytes]:
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
    env.pop("OPENAI_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.marshal_stub_openai", "--port", str(port), *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_until_up(f"http://127.0.0.1:{port}/_stub/requests", proc)
    return proc


async def test_one_ask_through_a_local_stub_server(tmp_path) -> None:
    port = _free_port()
    proc = _start_stub(port)
    try:
        base = f"http://127.0.0.1:{port}"
        async with desk_app(tmp_path, marshal_openai_base_url=f"{base}/v1") as h:
            assert h.app.state.marshal_llm_transport is None  # the network, not a test transport
            uid = await h.create_user("stub@example.com")
            claude = await h.api_key(uid, agent_id="claude-code")
            await h.seed_handoff(claude, agent="claude-code")
            res = await h.ask(
                h.jwt(uid, "stub@example.com"), "why is codex waiting", context={"agent_id": "codex"}, source="why_slip"
            )
            assert res.status_code == 200, res.text
            events = sse_events(res.text)
            assert event_names(events) == ["read", "read", "answer", "usage", "done"]
            answer = events[2][1]
            assert answer["fallback"] is False and [e["ref"] for e in answer["evidence"]] == ["r1", "r2"]
            assert [c["text"] for c in answer["commands"]] == ["/hooks", "remembra-relay doctor --agent codex"]
            usage = events[3][1]
            assert usage["model_calls"] == 2 and usage["input_tokens"] == 5000 and usage["cached_tokens"] == 1792
            assert usage["usd"] == 0.000682 and events[4][1] == {"ok": True}
        received = httpx.get(f"{base}/_stub/requests", timeout=5).json()
        assert len(received) == 2 and received[0]["model"] == "gpt-4o-mini"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def _hold(h: Any) -> dict[str, Any]:
    await ai_spend.drain_settles()
    [hold] = await h.rows("SELECT state, spent_micro FROM marshal_reservations")
    [day] = await h.rows("SELECT spent_micro, asks FROM marshal_budget WHERE period LIKE 'day:%'")
    return {**hold, "day_spent": day["spent_micro"], "day_asks": day["asks"]}


async def test_a_model_slower_than_the_timeout_is_charged_its_bound(tmp_path) -> None:
    port = _free_port()
    proc = _start_stub(port, "--delay", "3")
    try:
        base = f"http://127.0.0.1:{port}"
        async with desk_app(tmp_path, marshal_openai_base_url=f"{base}/v1", llm_timeout_seconds=0.5) as h:
            uid = await h.create_user("slow@example.com")
            res = await h.ask(h.jwt(uid, "slow@example.com"), "why is codex waiting")
            events = sse_events(res.text)
            assert event_names(events) == ["error", "usage", "done"] and events[0][1]["error"] == "model_unavailable"
            received = httpx.get(f"{base}/_stub/requests", timeout=5).json()
            assert len(received) == 1  # the request reached the provider, which goes on to answer it
            bound = upper_bound_usd("gpt-4o-mini", received[0]["messages"], received[0]["tools"])
            assert events[1][1]["usd"] == round(bound, 6) and events[1][1]["asks_today"] == 1
            spent = to_micro(bound)
            assert await _hold(h) == {"state": "settled", "spent_micro": spent, "day_spent": spent, "day_asks": 1}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_a_refused_connection_gives_the_ask_back(tmp_path) -> None:
    base = f"http://127.0.0.1:{_free_port()}"  # nothing listens there
    async with desk_app(tmp_path, marshal_openai_base_url=f"{base}/v1") as h:
        uid = await h.create_user("refused@example.com")
        res = await h.ask(h.jwt(uid, "refused@example.com"), "why is codex waiting")
        events = sse_events(res.text)
        assert event_names(events) == ["error", "usage", "done"] and events[0][1]["error"] == "model_unavailable"
        assert events[1][1]["usd"] == 0 and events[1][1]["asks_today"] == 0
        assert await _hold(h) == {"state": "released", "spent_micro": 0, "day_spent": 0, "day_asks": 0}
