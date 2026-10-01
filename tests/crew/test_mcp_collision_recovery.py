"""MCP agents can reconcile their own notices without a human override."""

import asyncio
import os
from pathlib import Path
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from remembra.crew import schemas as S
from remembra.mcp import server
from mcp.server.fastmcp.exceptions import ToolError
import pytest
from tests.crew.test_mcp_crew import (
    _two_agents,
    agent,
    call,
    crew_id_of,
    use,
)
from tests.crew.test_mcp_crew import clock as clock
from tests.crew.test_mcp_crew import live as live


def collision_for(live, project):
    rows = live.crew_rows("SELECT * FROM crew_collisions WHERE crew_id = ? AND state = 'open'", (crew_id_of(live, project),))
    assert len(rows) == 1
    return rows[0]


def overlap(live):
    project, a, _, b, _ = _two_agents(live, "collision-recovery")
    path = "src/app/reports/export.ts"
    use(a)
    call("crew_checkpoint", files_changed=[path])
    use(b)
    call("crew_checkpoint", files_changed=[path])
    return project, a, b, collision_for(live, project)


def test_party_can_acknowledge_then_resolve_through_registered_mcp(live, clock):
    project, _, b, row = overlap(live)
    use(b)
    listed = call("crew_collision")
    assert row["id"] in listed and S.DATA_OPEN in listed and row["subject"] in listed
    acked = call("crew_collision", action="ack", collision=row["id"])
    assert "acknowledged" in acked and "remains unresolved" in acked
    assert row["id"] in call("crew_collision")  # acknowledgement cannot silently bury the notice
    result = call("crew_collision", action="resolve", collision=row["id"], resolution="Compared both commits; changes reconciled")
    assert f"COLLISION {row['id']}: resolved" in result
    stored = live.crew_rows("SELECT state, resolved_by FROM crew_collisions WHERE id = ?", (row["id"],))[0]
    seat = server._crew.seat_for_scope(server._crew_caller())
    assert stored == {"state": "resolved", "resolved_by": seat.session_id}
    assert row["id"] not in call("crew_collision")


def test_non_party_cannot_resolve_even_in_same_account_and_crew(live, clock):
    project, _, _, row = overlap(live)
    use(agent(live, "cursor", project + "-outsider", project))
    call("crew_status", project_id=project)
    denied = call("crew_collision", action="resolve", collision=row["id"], resolution="No authority")
    assert "not a party" in denied
    seat = server._crew.seat_for_scope(server._crew_caller())
    result = live.api(
        "POST",
        f"/collisions/{row['id']}/resolve",
        headers={"X-API-Key": live.state["key"], "X-Remembra-Crew-Session": seat.token},
        json={"resolution": "No authority"},
        status=403,
    )
    assert result["detail"]["error"] == "not_a_party"
    assert live.crew_rows("SELECT state FROM crew_collisions WHERE id = ?", (row["id"],)) == [{"state": "open"}]


def test_resolution_does_not_bypass_the_other_partys_held_claim(live, clock):
    project, a, _, b, _ = _two_agents(live, "collision-claim")
    use(a)
    assert call("crew_claim", zone="pos").startswith("GRANTED")
    use(b)
    call("crew_checkpoint", files_changed=["src/app/pos/cart.ts"])
    row = collision_for(live, project)
    assert row["kind"] == "exclusive_breach"
    resolved = call("crew_collision", action="resolve", collision=row["id"], resolution="Discarded conflicting edit")
    assert ": resolved" in resolved
    assert call("crew_guard", paths=["src/app/pos/cart.ts"]).startswith("DENY")


def test_invalid_collision_path_and_empty_resolution_are_refused(live, clock):
    project, _, b, row = overlap(live)
    use(b)
    assert "collision id" in call("crew_collision", action="resolve", collision="../sessions/other/leave", resolution="x")
    assert "explanation" in call("crew_collision", action="resolve", collision=row["id"], resolution=" ")
    with pytest.raises(ToolError, match="Input should be 'list', 'ack' or 'resolve'"):
        call("crew_collision", action="dismiss", collision=row["id"])
    assert live.crew_rows("SELECT state FROM crew_collisions WHERE id = ?", (row["id"],)) == [{"state": "open"}]


def test_stdio_client_discovers_and_resolves_its_own_collision(live, clock):
    """Exercise JSON-RPC validation, subprocess identity and real REST authorization."""
    project = "collision-wire"
    path = "src/app/reports/wire.ts"
    use(agent(live, "claude-code", "wire-other", project))
    call("crew_status", project_id=project)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMEMBRA_", "CLAUDE", "OPENAI_", "ANTHROPIC_"))}
    env.update(
        REMEMBRA_URL=live.url,
        REMEMBRA_API_KEY=live.state["key"],
        REMEMBRA_PROJECT=project,
        REMEMBRA_AGENT_ID="codex",
        REMEMBRA_SESSION_ID="wire-child",
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
    )

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "remembra.mcp.server"], env=env)
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    tool = next(t for t in tools.tools if t.name == "crew_collision")
                    assert tool.inputSchema["properties"]["action"]["enum"] == ["list", "ack", "resolve"]
                    await client.call_tool("crew_status", {"project_id": project})
                    await client.call_tool("crew_checkpoint", {"files_changed": [path]})
                    await server.mcp.call_tool("crew_checkpoint", {"files_changed": [path]})
                    listed = await client.call_tool("crew_collision", {})
                    text = "\n".join(b.text for b in listed.content if hasattr(b, "text"))
                    collision = re.search(r"col_[A-Za-z0-9_-]+", text)
                    assert not listed.isError and collision is not None
                    denied = await client.call_tool("crew_collision", {"action": "dismiss", "collision": collision[0]})
                    assert denied.isError
                    result = await client.call_tool(
                        "crew_collision",
                        {"action": "resolve", "collision": collision[0], "resolution": "Compared both synthetic checkpoints"},
                    )
                    assert not result.isError
                    assert any(f"COLLISION {collision[0]}: resolved" in b.text for b in result.content if hasattr(b, "text"))
                    stored = live.crew_rows("SELECT state, resolved_by FROM crew_collisions WHERE id = ?", (collision[0],))[0]
                    child = live.crew_rows("SELECT id FROM crew_sessions WHERE session_id = ?", ("wire-child",))[0]
                    assert stored == {"state": "resolved", "resolved_by": child["id"]}
                    await client.call_tool(
                        "close_session",
                        {
                            "project_id": project,
                            "facts": {"notes": "Synthetic MCP wire collision test completed"},
                            "end_reason": "done",
                        },
                    )

    asyncio.run(run())
