"""Marshal's MCP tools through the real server: the stdio JSON-RPC process, and FastMCP's call_tool.

The stdio process runs with a fake HOME (a saved key, Codex hooks without trust) and no REMEMBRA_*
environment, so nothing can reach a real server; ``check_server`` is off where a key exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.marshal_fixtures import KEY, FakeHome

pytest.importorskip("mcp")

SRC = Path(__file__).resolve().parents[1] / "src"


def _rpc(msg_id: int | None, method: str, params: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return json.dumps(body) + "\n"


def _stdio(home: FakeHome, calls: list[tuple[str, dict[str, Any]]], extra_env: dict[str, str] | None = None) -> dict[int, Any]:
    """Send each request and wait for its answer before the next (the server stops reading at EOF)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_")}
    env.update({"PYTHONPATH": str(SRC), "HOME": str(home.home), "REMEMBRA_MCP_TRANSPORT": "stdio", **(extra_env or {})})
    proc = subprocess.Popen(
        [sys.executable, "-m", "remembra.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=home.root,
    )
    assert proc.stdin is not None and proc.stdout is not None
    out: dict[int, Any] = {}
    raw: list[str] = []

    def send(line: str) -> None:
        assert proc.stdin is not None
        proc.stdin.write(line)
        proc.stdin.flush()

    def wait_for(msg_id: int) -> None:
        assert proc.stdout is not None
        while msg_id not in out:
            line = proc.stdout.readline()
            assert line, f"the server closed its output before answering {msg_id}"
            raw.append(line)
            message = json.loads(line)  # every stdout line is JSON-RPC
            if "id" in message:
                out[message["id"]] = message

    try:
        init = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        send(_rpc(1, "initialize", init))
        wait_for(1)
        send(_rpc(None, "notifications/initialized"))
        for n, (method, params) in enumerate(calls, start=2):
            send(_rpc(n, method, params))
            wait_for(n)
    finally:
        proc.stdin.close()
        proc.wait(30)
    assert KEY not in "".join(raw)
    return out


def _text(message: dict[str, Any]) -> dict[str, Any]:
    return json.loads(message["result"]["content"][0]["text"])


def test_three_tools_over_stdio(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.credentials()
    fh.codex_mcp()
    fh.hooks("codex")
    before = sorted(p.name for p in (fh.home / ".codex").iterdir())
    out = _stdio(
        fh,
        [
            ("tools/list", {}),
            ("tools/call", {"name": "remembra_doctor", "arguments": {"check_server": False}}),
            ("tools/call", {"name": "remembra_doctor", "arguments": {"agent": "codex", "check_server": False}}),
            ("tools/call", {"name": "remembra_setup", "arguments": {"agents": ["codex"]}}),
            ("tools/call", {"name": "remembra_help", "arguments": {"question": "How do I uninstall?"}}),
            ("tools/call", {"name": "remembra_help", "arguments": {"question": "Do you offer refunds?"}}),
            ("tools/call", {"name": "remembra_doctor", "arguments": {"agent": "notepad", "check_server": False}}),
            ("prompts/list", {}),
            ("prompts/get", {"name": "store-summary", "arguments": {}}),
            ("prompts/get", {"name": "setup-check", "arguments": {}}),
            ("prompts/get", {"name": "doctor", "arguments": {"agent": "codex"}}),
        ],
    )
    instructions = out[1]["result"]["instructions"]
    assert "call remembra_doctor and show its rendered slip; never run a fix without the user's yes" in instructions

    listed = {t["name"]: t for t in out[2]["result"]["tools"]}
    for name in ("remembra_doctor", "remembra_setup", "remembra_help"):
        hints = listed[name]["annotations"]
        assert hints["readOnlyHint"] is True and hints["destructiveHint"] is False, name
    assert "Show `rendered` verbatim in a code block" in listed["remembra_doctor"]["description"]
    assert "runs_where: user_terminal" in listed["remembra_doctor"]["description"]

    doctor = _text(out[3])
    assert doctor["status"] == "ok" and doctor["changed_nothing"] is True
    assert doctor["rendered"].rstrip().endswith("Nothing was changed.")
    ids = {f["id"] for f in doctor["findings"]}
    assert "CODEX_TRUST_MISSING" in ids
    trust = next(f for f in doctor["findings"] if f["id"] == "CODEX_TRUST_MISSING")
    assert trust["fix"]["runs_where"] == "codex_ui" and trust["proven"] is True and trust["marker"] == "[!!]"
    assert "\033[" not in doctor["rendered"]

    scoped = _text(out[4])
    assert [a["name"] for a in scoped["agents"]] == ["codex"]

    setup = _text(out[5])
    assert setup["agents"] == ["codex"] and setup["changed_nothing"] is True
    titles = [s["title"] for s in setup["steps"]]
    assert "Trust the hooks in Codex" in titles and titles[-1] == "Check"
    key_step = next(s for s in setup["steps"] if "key" in s["title"].lower())
    assert key_step["done"] is True  # the fake HOME has a saved key

    help_answer = _text(out[6])
    assert help_answer["answer_status"] == "answered"
    assert help_answer["sections"][0]["url"] == "https://docs.remembra.dev/guides/relay/#uninstall"
    assert "remembra-relay disconnect --apply" in help_answer["rendered"]
    refunds = _text(out[7])
    assert refunds["answer_status"] == "read_the_page" and "https://remembra.dev/refunds" in refunds["pages"]
    assert refunds["sections"] == []

    unknown = _text(out[8])
    assert unknown["status"] == "error" and "unknown agent" in unknown["error"]

    prompts = {p["name"] for p in out[9]["result"]["prompts"]}
    assert {"doctor", "store-summary", "setup-check", "recall-context"} <= prompts
    summary = out[10]["result"]["messages"][0]["content"]["text"]
    assert "close_session" in summary and "store_memory" not in summary
    check = out[11]["result"]["messages"][0]["content"]["text"]
    assert "health_check" in check and "remembra_doctor" in check
    doctor_prompt = out[12]["result"]["messages"][0]["content"]["text"]
    assert "remembra_doctor tool for codex" in doctor_prompt and "Never ask me for my key" in doctor_prompt

    assert sorted(p.name for p in (fh.home / ".codex").iterdir()) == before


def test_remote_transport_answers_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import remembra.mcp.server as server

    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = asyncio.run(server.mcp.call_tool(name, args))
        return json.loads(result[0][0].text)  # type: ignore[index]

    for name, args in (("remembra_doctor", {}), ("remembra_setup", {"agents": ["codex"]})):
        payload = call(name, args)
        assert payload == {
            "status": "local_only",
            "message": "Marshal reads this machine's files; it runs in the local MCP server only. "
            "Run remembra-relay doctor in a terminal.",
        }
    help_answer = call("remembra_help", {"question": "what is free on every plan"})
    assert help_answer["status"] == "ok" and help_answer["answer_status"] == "answered"
    assert help_answer["plan_facts"] and help_answer["pricing_page"] == "https://remembra.dev/pricing"


def test_the_doctor_tool_checks_the_key_the_hooks_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP server's own REMEMBRA_API_KEY comes from its MCP entry, not from the hooks' environment."""
    from remembra.marshal import tools

    fh = FakeHome(tmp_path)
    fh.hooks("claude-code")
    env = fh.environ(REMEMBRA_API_KEY=KEY, REMEMBRA_URL="https://mcp-only.example")
    payload = tools.doctor_payload(None, False, environ=env, home=fh.home, which=fh.which)
    assert any("the key checked is the one this MCP server uses" in u for u in payload["unchecked"])
    fh.credentials()
    payload = tools.doctor_payload(None, False, environ=env, home=fh.home, which=fh.which)
    assert not any("MCP server uses" in u for u in payload["unchecked"])
    assert "from ~/.remembra/credentials" in payload["rendered"]
