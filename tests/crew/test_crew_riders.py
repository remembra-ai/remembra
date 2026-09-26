"""Crew L0 riders (continuity gap analysis §7) and the owner's sub-agent decision (open question 1).

crew.db's v1 DDL carries the nullable rider columns before its first deploy; the
writers pass the optional fields through. A sub-agent is its own session row,
linked by ``parent_session_id`` to the live session of the same account in the
same crew that started it (its own callsign, claims and checkpoints).
Over HTTP with the production router, real auth and a real crew.db.
"""

from __future__ import annotations

import json

from remembra.crew import schemas as S
from remembra.crew.store import crew_id_for
from tests.crew.test_crew_sessions_api import _join_body, _owner, crew_http

RIDER_COLUMNS = {
    "crew_sessions": {"provider", "parent_session_id", "sub_agent_id", "run_id", "capabilities", "context_window", "env_fp_id"},
    "crew_checkpoints": {"run_id", "state_before_ref", "decision_ids", "quality", "confidence", "continuity_seq"},
    "crew_decisions": {"evidence", "proposed_by_verified", "decided_by_verified", "intent_version"},
    "crew_footprints": {"content_hash", "artifact_id"},
}


async def test_rider_columns_exist_and_are_nullable(tmp_path) -> None:
    async with crew_http(tmp_path) as (_h, db):
        for table, columns in RIDER_COLUMNS.items():
            cursor = await db.conn.execute(f'PRAGMA table_info("{table}")')
            info = {r[1]: r[3] for r in await cursor.fetchall()}
            assert columns <= set(info), (table, columns - set(info))
            assert all(info[c] == 0 for c in columns), table  # NULL changes no behaviour


async def test_a_sub_agent_joins_as_its_own_session_linked_to_its_parent(tmp_path) -> None:
    async with crew_http(tmp_path) as (h, db):
        uid, key = await _owner(h)
        parent = await h.client.post(
            "/api/v1/crews/join", headers=key, json=_join_body("parent-1", provider="anthropic", capabilities=["edit", "bash"])
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["session_id"]
        child = await h.client.post(
            "/api/v1/crews/join",
            headers=key,
            json=_join_body("parent-1:sub:1", parent_session_id=parent_id, sub_agent_id="general-purpose"),
        )
        assert child.status_code == 201, child.text
        child_id = child.json()["session_id"]
        assert child_id != parent_id
        cursor = await db.conn.execute(
            "SELECT id, callsign, parent_session_id, sub_agent_id, provider, capabilities FROM crew_sessions ORDER BY joined_at"
        )
        rows = {r[0]: r for r in await cursor.fetchall()}
        assert rows[parent_id][2] is None and rows[parent_id][4] == "anthropic"
        assert json.loads(rows[parent_id][5]) == ["edit", "bash"]
        assert rows[child_id][2] == parent_id and rows[child_id][3] == "general-purpose"
        assert rows[child_id][1] != rows[parent_id][1]  # its own callsign
        # the session view (events, snapshot) carries the link and stays inside the contract
        cursor = await db.conn.execute(
            "SELECT payload FROM crew_events WHERE type = 'session.joined' AND session_id = ?", (child_id,)
        )
        payload = json.loads((await cursor.fetchone())[0])
        assert payload["session"]["parent_session_id"] == parent_id
        assert payload["session"]["sub_agent_id"] == "general-purpose"
        assert S.validate(payload["session"], S.SESSION_VIEW) == []


async def test_parent_must_be_a_live_session_of_the_same_account_and_crew(tmp_path) -> None:
    async with crew_http(tmp_path) as (h, db):
        _uid, key = await _owner(h)
        other_uid, other_key = await _owner(h, "other@example.com")
        mine = (await h.client.post("/api/v1/crews/join", headers=key, json=_join_body("p-1"))).json()["session_id"]
        theirs = (await h.client.post("/api/v1/crews/join", headers=other_key, json=_join_body("q-1"))).json()["session_id"]
        other_crew = (
            await h.client.post("/api/v1/crews/join", headers=key, json=_join_body("r-1", project_id="another"))
        ).json()["session_id"]

        cases = [
            (theirs, 422, "cross_crew_reference"),  # the other account's session is in its own crew
            (other_crew, 422, "cross_crew_reference"),
            ("cs_doesnotexist", 422, "cross_crew_reference"),
        ]
        for parent, code, error in cases:
            res = await h.client.post(
                "/api/v1/crews/join", headers=key, json=_join_body(f"sub-{parent}", parent_session_id=parent)
            )
            assert res.status_code == code and error in res.text, (parent, res.text)

        # same crew, same project, another account: a teammate's session cannot be a parent
        crew_id = crew_id_for(_uid, "yaadbooks")
        # (moved into this crew as a team member's session would be; callsign changed to stay unique)
        await db.conn.execute("UPDATE crew_sessions SET crew_id = ?, callsign = 'cc-9' WHERE id = ?", (crew_id, theirs))
        await db.conn.commit()
        res = await h.client.post("/api/v1/crews/join", headers=key, json=_join_body("sub-mate", parent_session_id=theirs))
        assert res.status_code == 422 and "parent_session_mismatch" in res.text, res.text

        await db.conn.execute(
            "UPDATE crew_sessions SET state = 'ended', ended_at = '2026-09-26T00:00:00.000Z' WHERE id = ?", (mine,)
        )
        await db.conn.commit()
        res = await h.client.post("/api/v1/crews/join", headers=key, json=_join_body("sub-late", parent_session_id=mine))
        assert res.status_code == 409 and "parent_session_ended" in res.text, res.text
        cursor = await db.conn.execute("SELECT COUNT(*) FROM crew_sessions WHERE session_id LIKE 'sub-%'")
        assert (await cursor.fetchone())[0] == 0
        assert other_uid != _uid


def test_contract_carries_the_optional_rider_fields() -> None:
    join = {
        "agent_id": "claude-code",
        "session_id": "s",
        "adapter": "claude-code",
        "client_kind": "hook",
        "source": "startup",
    }
    assert S.validate(join, S.REQUEST_SHAPES["Join"]) == []
    assert (
        S.validate({**join, "parent_session_id": "cs_abc", "sub_agent_id": "x", "provider": "openai"}, S.REQUEST_SHAPES["Join"])
        == []
    )
    assert S.validate({**join, "parent_session_id": "nope"}, S.REQUEST_SHAPES["Join"]) != []
    ckp = {"session_id": "cs_abc", "trigger": "commit", "facts": {}}
    assert S.validate({**ckp, "decisions": ["dec_1"], "state_before": "ref"}, S.REQUEST_SHAPES["Checkpoint"]) == []
    assert S.validate({**ckp, "decisions": ["tsk_1"]}, S.REQUEST_SHAPES["Checkpoint"]) != []
    for name in ("failure.recorded", "failure.resolved", "artifact.recorded"):
        assert S.EVENT_SPECS[name].release == "L1" and name not in S.L0_EVENT_TYPES


async def test_checkpoint_passes_decisions_and_state_before_through(tmp_path) -> None:
    import pytest

    from remembra.crew.checkpoints import CheckpointService
    from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS
    from remembra.crew.tasks import Caller, CrewServiceError
    from tests.crew.wp6_support import CREW, event_log, open_db, seed_crew, seed_session

    db = await open_db(tmp_path)
    try:
        await seed_crew(db)

        async def resolver(owner: str):  # noqa: ANN202
            return SELF_HOSTED_CREW_LIMITS

        svc = CheckpointService(event_log(db), limits_resolver=resolver)
        s = await seed_session(db)
        await db.conn.execute(
            "INSERT INTO crew_decisions (id, crew_id, number, title, state, created_at)"
            " VALUES ('dec_one', ?, 1, 'x', 'in_force', 'now')",
            (CREW,),
        )
        await db.conn.commit()
        body = {"session_id": s["id"], "trigger": "turn", "facts": {}, "decisions": ["dec_one"], "state_before": "abc1234"}
        res = await svc.ingest(CREW, Caller.for_session(s), body)
        row = await db.fetchone(
            "SELECT decision_ids, state_before_ref FROM crew_checkpoints WHERE id = ?", (res.checkpoint["id"],)
        )
        assert json.loads(row["decision_ids"]) == ["dec_one"] and row["state_before_ref"] == "abc1234"
        with pytest.raises(CrewServiceError) as err:
            await svc.ingest(CREW, Caller.for_session(s), {**body, "facts": {"n": 2}, "decisions": ["dec_elsewhere"]})
        assert "cross_crew_reference" in str(err.value.__dict__) or "cross_crew_reference" in repr(err.value)
        plain = await svc.ingest(CREW, Caller.for_session(s), {"session_id": s["id"], "trigger": "turn", "facts": {"n": 3}})
        row = await db.fetchone(
            "SELECT decision_ids, state_before_ref FROM crew_checkpoints WHERE id = ?", (plain.checkpoint["id"],)
        )
        assert row["decision_ids"] is None and row["state_before_ref"] is None  # NULL: no behaviour change
    finally:
        await db.close()


async def test_decisions_record_who_was_verified(tmp_path) -> None:
    from remembra.crew.inbox import Author
    from tests.crew.wp7_support import CREW_A, OWNER, add_session, make_env

    env = await make_env(tmp_path)
    try:
        agent = await add_session(env.db, CREW_A, "cs_a", callsign="cc-1")
        proposed = await env.decisions.create(env.crew, agent, title="Use Decimal", decision="Money is Decimal")
        human = await env.decisions.create(env.crew, Author.human(OWNER), title="GCT", decision="Round half-up")
        rows = {
            r["id"]: r for r in await env.db.fetchall("SELECT id, proposed_by_verified, decided_by_verified FROM crew_decisions")
        }
        assert rows[human["id"]]["proposed_by_verified"] == 1 and rows[human["id"]]["decided_by_verified"] == 1
        assert rows[proposed["id"]]["proposed_by_verified"] == (1 if agent.verified else 0)
        assert rows[proposed["id"]]["decided_by_verified"] is None
        await env.decisions.confirm(proposed["id"], Author.human(OWNER), env.crew)
        row = await env.db.fetchone("SELECT decided_by_verified FROM crew_decisions WHERE id = ?", (proposed["id"],))
        assert row["decided_by_verified"] == 1
    finally:
        await env.db.close()
