"""The stdio MCP server keeps stdout for JSON-RPC only (logs go to stderr).

structlog's default logger prints to stdout; before the fix a failed tool call
wrote the error sanitizer's debug line ahead of the JSON-RPC response, and
strict MCP hosts dropped the message.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")

SRC = Path(__file__).resolve().parents[1] / "src"


def _rpc(msg_id: int | None, method: str, params: dict | None = None) -> str:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return json.dumps(body) + "\n"


def test_failing_tool_call_leaves_only_json_rpc_on_stdout(tmp_path: Path) -> None:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_")},
        "PYTHONPATH": str(SRC),
        "HOME": str(tmp_path),
        # Nothing listens here: every tool call fails and goes through the error sanitizer.
        "REMEMBRA_URL": "http://127.0.0.1:9",
        "REMEMBRA_API_KEY": "rem_test_not_a_real_key",
        "REMEMBRA_AGENT_ID": "stdio-test",
        "REMEMBRA_MCP_TRANSPORT": "stdio",
    }
    stdin = "".join(
        [
            _rpc(
                1,
                "initialize",
                {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            ),
            _rpc(None, "notifications/initialized"),
            _rpc(2, "tools/call", {"name": "update_memory", "arguments": {"memory_id": "nope", "content": "x"}}),
        ]
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "remembra.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    out_lines: list[str] = []
    err_text: list[str] = []
    answered = threading.Event()

    def read_stdout() -> None:
        for line in proc.stdout:
            out_lines.append(line)
            try:
                if json.loads(line).get("id") == 2:
                    answered.set()
            except ValueError:
                pass  # not JSON: the assertions below fail on it
        answered.set()  # stdout ended: nothing more to wait for

    readers = [threading.Thread(target=read_stdout), threading.Thread(target=lambda: err_text.append(proc.stderr.read()))]
    for reader in readers:
        reader.start()
    try:
        proc.stdin.write(stdin)
        proc.stdin.flush()
        # stdin stays open until the answer to the tools/call has been read, as an MCP host keeps it. When
        # stdin ends, the MCP SDK cancels a call that is still running and its answer is never written, so
        # closing stdin straight after the request made this test depend on which of the two came first.
        assert answered.wait(60), "no answer to the tools/call within 60 s"
        proc.stdin.close()
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
        for reader in readers:
            reader.join(timeout=10)
    stdout, stderr = "".join(out_lines), "".join(err_text)
    lines = [line for line in stdout.splitlines() if line.strip()]
    ids = []
    for line in lines:
        message = json.loads(line)  # every stdout line is a JSON-RPC message
        assert message.get("jsonrpc") == "2.0", line
        ids.append(message.get("id"))
    assert {1, 2} <= set(ids), (stdout, stderr)
    responses = {json.loads(line)["id"]: json.loads(line) for line in lines if "id" in json.loads(line)}
    text = responses[2]["result"]["content"][0]["text"]
    assert json.loads(text)["status"] == "error"
    # The log line still exists, on stderr.
    assert "sanitizing_error_message" in stderr or "error" in stderr.lower()
