"""F47: a tool result goes out once, as text.

Every Remembra MCP tool returns ``str``. The MCP SDK treats a ``str`` return as
structured output unless told otherwise, so each result went out twice (once in
``content[0].text`` and again in ``structuredContent.result``) and every tool
advertised an ``outputSchema`` for that wrapper. These tests go through the
SDK's own request handlers, which is what a client talks to.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp import types

import remembra.mcp.server as server


def _list_tools() -> list[types.Tool]:
    return asyncio.run(server.mcp.list_tools())


def _call(name: str, args: dict[str, Any]) -> types.CallToolResult:
    handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args))
    result = asyncio.run(handler(request))
    assert isinstance(result.root, types.CallToolResult)
    return result.root


def test_no_tool_advertises_an_output_schema() -> None:
    tools = _list_tools()
    assert len(tools) >= 31  # the 28 memory/crew tools plus doctor, setup and help
    with_schema = sorted(t.name for t in tools if t.outputSchema is not None)
    assert with_schema == []


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("remembra_help", {"question": "how do I connect codex?"}),  # registered by marshal/mcp_tools.py
        ("timeline", {"order": "sideways"}),  # registered in mcp/server.py; answers before any client call
    ],
)
def test_a_result_is_sent_once_as_text(monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any]) -> None:
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    result = _call(name, args)
    assert result.isError is False
    assert result.structuredContent is None
    assert [block.type for block in result.content] == ["text"]
    text = result.content[0].text  # type: ignore[union-attr]
    assert json.loads(text)  # the tool's own JSON, not a {"result": "..."} wrapper around it
    wire = result.model_dump_json(exclude_none=True)
    assert wire.count(json.dumps(text)) == 1  # the text is on the wire exactly once


def test_a_tool_can_still_opt_in_to_structured_output() -> None:
    """The default is text only; an explicit ``structured_output=True`` is still honoured."""
    mcp = server._TextFastMCP(name="probe")

    @mcp.tool()
    def plain() -> str:
        return "x"

    @mcp.tool(structured_output=True)
    def shaped() -> dict[str, int]:
        return {"n": 1}

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert tools["plain"].outputSchema is None
    assert tools["shaped"].outputSchema is not None
