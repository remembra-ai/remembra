"""A successor keeps the quota reason regardless of hook delivery order."""

import pytest

from remembra.crew.core import CrewCore
from remembra.relay.handoff import render_crew_block
from tests.crew.sessions_support import make_env
from tests.crew.test_crew_wave2_sessions_fixes import CREW, _holder


@pytest.mark.parametrize("stall_first", [True, False])
async def test_session_end_does_not_replace_original_quota_reason(tmp_path, stall_first):
    env = await make_env(tmp_path)
    try:
        joined, zone, task, claim = await _holder(env)

        async def stall():
            await env.svc.stall(joined.session, error="billing_error", facts={}, baton_ref=None)

        async def leave():
            await env.svc.leave(joined.session, reason="other", facts={}, summary=None, baton=False, baton_ref=None)

        if stall_first:
            await stall()
            await leave()
        else:
            await leave()
            await stall()
        holder = await env.one("SELECT * FROM crew_sessions WHERE id=?", (joined.session["id"],))
        assert holder["state"] == "ended"
        baton = await CrewCore(env.db)._baton_entry(
            CREW,
            await env.one("SELECT * FROM crew_claims WHERE id=?", (claim,)),
            holder,
            await env.one("SELECT * FROM crew_tasks WHERE id=?", (task,)),
            await env.one("SELECT * FROM crew_zones WHERE id=?", (zone,)),
        )
        assert (baton["error"], baton["source"]) == ("billing_error", "reported")
        assert "billing_error, reported" in render_crew_block({"batons": [baton]})
        await env.chain_ok(CREW)
    finally:
        await env.db.close()
