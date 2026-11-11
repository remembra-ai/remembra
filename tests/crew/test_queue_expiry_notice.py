"""Queue expiry reaches an existing MCP client through the actual HTTP/session-inbox path."""

from datetime import timedelta

import tests.crew.test_mcp_crew as base
from remembra.crew import claims as C
from remembra.crew.store import now_iso
from tests.crew.test_mcp_crew import _two_agents, as_json, call, crew_id_of, use

live = base.live
clock = base.clock


def test_expiry_reaches_waiting_agent_once_without_a_new_claim_request(live, clock):
    project, holder, _, waiter, _ = _two_agents(live, "queueexpiry")
    use(holder)
    assert call("crew_claim", zone="billing").startswith("GRANTED")
    use(waiter)
    assert call("crew_claim", zone="billing", wait_s=1).startswith("QUEUED")
    crew = crew_id_of(live, project)

    async def expire():
        db = live.app.state.crew_db
        async with db.transaction():
            await db.conn.execute(
                "UPDATE crew_claims SET updated_at = ? WHERE crew_id = ? AND state = 'queued'",
                (now_iso(C.utcnow() - timedelta(seconds=C.QUEUE_TTL_S + 1)), crew),
            )
        await C.sweep(C.CrewOps(live.app.state.crew_events))

    live.run(expire())
    clock.advance(61)
    notice = as_json(call("list_status", project_id=project))["crew_notice"]
    assert "Queued wait expired" in notice and "claim again to rejoin" in notice, notice
    clock.advance(6)
    assert "crew_notice" not in as_json(call("list_status", project_id=project))
    row = live.crew_rows("SELECT state FROM crew_claims WHERE crew_id = ? AND state = 'expired'", (crew,))
    assert len(row) == 1
