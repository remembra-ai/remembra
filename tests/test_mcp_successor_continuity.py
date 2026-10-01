"""Separate MCP processes must not bury unresolved work with a newer handoff.

Real stdio, HTTP authentication and SQLite; synthetic users/content and fake
vectors. This does not establish automatic hooks or actual model adoption.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.crew.mcp_support import live_server
from tests.crew.test_mcp_crew import as_json


def test_separate_mcp_successors_keep_failures_until_real_owner_confirmation(tmp_path):
    with live_server(tmp_path, crew=False) as live:
        email = "successor-owner@example.com"
        user = live.user(email)
        keys = {agent: live.key(user, "editor", agent_id=agent) for agent in ("codex", "claude-code", "qwen-code")}
        login = live.api("POST", "/auth/login", headers={}, json={"email": email, "password": "Str0ng!Passw0rd"})
        human = {"Authorization": "Bearer " + login["access_token"]}
        source = str(Path(__file__).resolve().parents[1] / "src")
        base_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("REMEMBRA_", "OPENAI_", "ANTHROPIC_", "CLAUDE"))
        }

        async def process(agent, session, operation):
            env = {
                **base_env,
                "PYTHONPATH": source,
                "REMEMBRA_URL": live.url,
                "REMEMBRA_API_KEY": keys[agent],
                "REMEMBRA_PROJECT": "unrelated-default",
                "REMEMBRA_AGENT_ID": agent,
                "REMEMBRA_SESSION_ID": session,
                "REMEMBRA_MCP_TRANSPORT": "stdio",
            }
            params = StdioServerParameters(command=sys.executable, args=["-m", "remembra.mcp.server"], env=env, cwd=tmp_path)
            with open(os.devnull, "w") as errors:
                async with stdio_client(params, errlog=errors) as (reader, writer), ClientSession(reader, writer) as client:
                    await client.initialize()

                    async def call(tool, arguments=None):
                        result = await client.call_tool(tool, arguments or {})
                        assert not result.isError, (tool, result)
                        if tool in {"session_brief", "session_open_work"} and '"ok": false' not in result.content[0].text:
                            # The full wire response is framed as untrusted;
                            # inner markers are escaped to prevent breakout.
                            assert '<remembra-data untrusted="true">' in result.content[0].text
                            assert "</remembra-data>" in result.content[0].text
                        return as_json(result.content[0].text)

                    return await operation(client, call)

        async def first(_client, call):
            await call("session_brief", {"project_id": "widget", "recent_n": 0})
            result = await call(
                "close_session",
                {
                    "facts": {
                        "branch": "main",
                        "unpushed_commits": 0,
                        "errors": ["Synthetic provider failure"],
                        "todos_open": ["Validate the complete release"],
                        "tests": [{"cmd": "synthetic acceptance", "passed": False, "summary": "Customer journey failed"}],
                        "notes": "Synthetic local evidence. Not deployed.",
                    },
                    "end_reason": "done",
                },
            )
            assert result["project_id"] == "widget"
            return result["handoff_id"]

        original = asyncio.run(process("codex", "original", first))

        async def unrelated(_client, call):
            brief = await call("session_brief", {"project_id": "widget", "recent_n": 0})
            assert brief["handoff_id"] == original
            assert "Synthetic provider failure" in brief["brief"]
            assert "Not deployed" in brief["brief"]
            assert '[remembra-data untrusted="true">' in brief["brief"]
            page = await call("session_open_work")
            assert page["project_id"] == "widget" and page["total"] == 3
            # This newer successful report cannot resolve someone else's work.
            closed = await call(
                "close_session", {"facts": {"notes": "Unrelated synthetic documentation check completed."}, "end_reason": "done"}
            )
            return closed["handoff_id"]

        newer = asyncio.run(process("claude-code", "unrelated", unrelated))

        async def successor(client, call):
            advertised = {tool.name: tool for tool in (await client.list_tools()).tools}
            actions = advertised["session_open_work"].inputSchema["properties"]["action"]["enum"]
            assert actions == ["list", "propose_resolution", "reopen"]
            brief = await call("session_brief", {"project_id": "widget", "recent_n": 0})
            assert brief["handoff_id"] == newer
            assert "Synthetic provider failure" in brief["brief"]
            first_page = await call("session_open_work", {"limit": 1})
            rest = await call("session_open_work", {"after": first_page["next_after"]})
            items = first_page["items"] + rest["items"]
            assert first_page["total"] == 3 and len(items) == 3
            assert len({item["id"] for item in items}) == 3
            item = next(item for item in items if item["kind"] == "todo")
            assert (await call("session_open_work", {"project_id": "foreign"}))["total"] == 0
            proposed = await call(
                "session_open_work",
                {
                    "action": "propose_resolution",
                    "item_id": item["id"],
                    "version": item["version"],
                    "evidence_memory_id": newer,
                },
            )
            assert proposed["state"] == "resolution_proposed"
            assert (await call("session_open_work"))["total"] == 3
            stale = await call("session_open_work", {"action": "reopen", "item_id": item["id"], "version": item["version"]})
            assert stale["ok"] is False and stale["code"] == 409
            denied = await client.call_tool(
                "session_open_work", {"action": "confirm_resolution", "item_id": item["id"], "version": proposed["version"]}
            )
            assert denied.isError
            body = {"action": "confirm_resolution", "project_id": "widget", "version": proposed["version"]}
            live.api("POST", f"/session/open-work/{item['id']}", headers={"X-API-Key": keys["qwen-code"]}, status=403, json=body)
            confirmed = live.api("POST", f"/session/open-work/{item['id']}", headers=human, json=body)
            assert confirmed["state"] == "resolved"
            assert (await call("session_open_work"))["total"] == 2
            reopened = await call(
                "session_open_work", {"action": "reopen", "item_id": item["id"], "version": confirmed["version"]}
            )
            assert reopened["state"] == "open" and reopened["version"] == confirmed["version"] + 1
            assert (await call("session_open_work"))["total"] == 3
            await call(
                "close_session",
                {
                    "facts": {"notes": "Synthetic MCP successor check completed; original work remains open."},
                    "end_reason": "done",
                },
            )

        asyncio.run(process("qwen-code", "successor", successor))
