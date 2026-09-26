#!/usr/bin/env python3
"""Start an MCP server command over stdio and check that it answers.

    python scripts/mcp_stdio_check.py -- uvx remembra-mcp==0.16.0
    python scripts/mcp_stdio_check.py --expect-tool session_brief -- remembra-mcp

Sends ``initialize``, then ``notifications/initialized`` and ``tools/list``
the way an MCP client does, and fails unless the server replies to both with
a result (and lists every ``--expect-tool``). The server's stderr is passed
through so an install or import error is visible. Nothing is sent to the
Remembra API: ``initialize`` and ``tools/list`` are answered locally.

Used by CI and the release workflow on the exact command an MCP Registry
client derives from server.json. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class CheckError(RuntimeError):
    pass


def _reader(stream: Any, sink: list[dict[str, Any]], raw: list[str]) -> None:
    for line in iter(stream.readline, ""):
        raw.append(line)
        try:
            message = json.loads(line)
        except ValueError:
            continue  # stdio servers must not print anything else, but keep going
        if isinstance(message, dict):
            sink.append(message)


def _wait_for(messages: list[dict[str, Any]], request_id: int, deadline: float, proc: subprocess.Popen[str]) -> dict[str, Any]:
    while time.monotonic() < deadline:
        for message in messages:
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
        if proc.poll() is not None:
            raise CheckError(f"server exited with code {proc.returncode} before answering request {request_id}")
        time.sleep(0.05)
    raise CheckError(f"no answer to request {request_id} in time")


def check(
    command: list[str], timeout: float = 120.0, expect_tools: list[str] | None = None, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Run ``command``, do the MCP handshake, return {"server": ..., "tools": [...]}. Raises CheckError."""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # inherit: install and startup errors show up in the log
        text=True,
        env=env if env is not None else dict(os.environ),
    )
    messages: list[dict[str, Any]] = []
    raw: list[str] = []
    thread = threading.Thread(target=_reader, args=(proc.stdout, messages, raw), daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    assert proc.stdin is not None

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "remembra-mcp-stdio-check", "version": "1"},
                },
            }
        )
        init = _wait_for(messages, 1, deadline, proc)
        if "error" in init:
            raise CheckError(f"initialize failed: {init['error']}")
        server = (init.get("result") or {}).get("serverInfo") or {}
        if not server.get("name"):
            raise CheckError(f"initialize result has no serverInfo.name: {init.get('result')}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _wait_for(messages, 2, deadline, proc)
        if "error" in listed:
            raise CheckError(f"tools/list failed: {listed['error']}")
        tools = [t.get("name") for t in (listed.get("result") or {}).get("tools") or [] if isinstance(t, dict)]
        missing = [name for name in expect_tools or [] if name not in tools]
        if missing:
            raise CheckError(f"tools/list is missing {missing}; got {tools}")
        return {"server": server, "tools": tools}
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=float, default=120.0, help="Seconds for install + handshake (default 120)")
    parser.add_argument("--expect-tool", action="append", default=[], help="A tool the server must list (repeatable)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the server command")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the server command after --")
    try:
        result = check(command, timeout=args.timeout, expect_tools=args.expect_tool)
    except (CheckError, OSError) as e:
        print(f"mcp stdio check FAILED for {' '.join(command)}: {e}", file=sys.stderr)
        return 1
    print(
        f"mcp stdio check ok: {' '.join(command)} -> {result['server'].get('name')} "
        f"{result['server'].get('version', '')}, {len(result['tools'])} tools"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
