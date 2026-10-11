"""M2 gate: ``GET /session/brief?preview=1`` reads a brief without recording anything.

The pickup record (R-18) is the one write a brief makes. With ``preview=1`` it
is skipped whatever the caller's agent, an agent-scoped key included, so the
Marshal desk can show what the next agent would see without ticking a pickup.
A ``marshal:`` principal is refused (403 ``preview_required``) without it.
Production routes over real SQLite (``tests/marshal_desk_harness.py``).
"""

from __future__ import annotations

from typing import Any

from remembra.auth.middleware import AuthenticatedUser, connector_principal
from tests.marshal_desk_harness import DeskHarness, desk_app


def _marshal(user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        api_key_id="marshal:0123456789ab",
        rate_limit_tier="standard",
        name="marshal",
        role="viewer",
        scopes=["memory:recall"],
    )


async def _world(h: DeskHarness) -> dict[str, Any]:
    uid = await h.create_user("preview@example.com")
    claude = await h.api_key(uid, name="claude", agent_id="claude-code")
    codex = await h.api_key(uid, name="codex", agent_id="codex")
    handoff = await h.seed_handoff(claude, agent="claude-code", project="widget")
    return {"uid": uid, "claude": claude, "codex": codex, "handoff": handoff}


async def test_preview_with_an_agent_scoped_key_records_no_pickup_and_writes_nothing(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        w = await _world(h)
        # The key's own last-used stamp is the only write any authenticated read makes: measure it first.
        before = await h.total_changes()
        res = await h.http.get("/api/v1/trail/summary", headers=w["codex"])
        assert res.status_code == 200
        key_stamp = await h.total_changes() - before
        assert key_stamp == 1

        pickups = await h.count("relay_pickups")
        before = await h.total_changes()
        res = await h.http.get("/api/v1/session/brief", params={"project_id": "widget", "preview": "1"}, headers=w["codex"])
        assert res.status_code == 200, res.text
        brief = res.json()
        assert brief["handoff"]["id"] == w["handoff"]  # the handoff was served to codex, another agent
        assert await h.count("relay_pickups") == pickups == 0
        assert await h.total_changes() - before == key_stamp  # nothing but the key's stamp

        # The same brief without preview records exactly one pickup (the existing behaviour).
        res = await h.http.get("/api/v1/session/brief", params={"project_id": "widget"}, headers=w["codex"])
        assert res.status_code == 200
        rows = await h.rows("SELECT handoff_id, reader_agent, reader_verified FROM relay_pickups")
        assert rows == [{"handoff_id": w["handoff"], "reader_agent": "codex", "reader_verified": 1}]


async def test_preview_false_explicitly_is_the_old_brief(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        w = await _world(h)
        res = await h.http.get("/api/v1/session/brief", params={"project_id": "widget", "preview": "false"}, headers=w["codex"])
        assert res.status_code == 200
        assert await h.count("relay_pickups") == 1


async def test_a_marshal_principal_needs_preview(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        w = await _world(h)
        with connector_principal(_marshal(w["uid"])):
            refused = await h.http.get("/api/v1/session/brief", params={"project_id": "widget", "agent_id": "codex"})
            before = await h.total_changes()
            allowed = await h.http.get("/api/v1/session/brief", params={"project_id": "widget", "preview": "1"})
        assert refused.status_code == 403
        assert refused.json() == {"detail": {"error": "preview_required", "message": "Marshal reads briefs with preview=1 only."}}
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["handoff"]["id"] == w["handoff"]
        assert await h.count("relay_pickups") == 0
        assert await h.total_changes() == before  # an in-process principal stamps no key either


async def test_the_openapi_schema_documents_preview(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        schema = (await h.http.get("/openapi.json")).json()
        params = {p["name"]: p for p in schema["paths"]["/api/v1/session/brief"]["get"]["parameters"]}
        preview = params["preview"]
        assert preview["in"] == "query" and preview["schema"]["type"] == "boolean"
        assert preview["schema"]["default"] is False
        assert "without recording a pickup" in preview["description"]
