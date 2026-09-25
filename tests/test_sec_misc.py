"""SEC-17 (plugins/calibrate superadmin), SEC-20 (team invites, link_space, error echoes), SEC-22 (debug plan gate)."""

from __future__ import annotations

from remembra.api.v1 import debug, plugins, teams
from remembra.cloud.metering import UsageMeter
from remembra.plugins.builtin.auto_tagger import AutoTaggerPlugin
from remembra.plugins.manager import PluginManager
from remembra.spaces.manager import SpaceManager
from remembra.teams.manager import TeamManager
from tests.security_harness import secure_app


async def test_plugin_activation_is_superadmin_only(tmp_path):
    async with secure_app(tmp_path, [plugins.router]) as h:
        manager = PluginManager()
        manager.register_class(AutoTaggerPlugin)
        h.app.state.plugin_manager = manager
        uid = await h.create_user("tenant@example.com", verified=True)
        hdr = h.jwt(uid, "tenant@example.com")
        r = await h.client.post("/api/v1/plugins/activate", json={"name": "auto-tagger"}, headers=hdr)
        assert r.status_code == 403
        assert manager.list_plugins() == []
        assert (await h.client.get("/api/v1/plugins/registry", headers=hdr)).status_code == 200

        owner = await h.create_user("owner@example.com", verified=True)
        ohdr = h.jwt(owner, "owner@example.com")
        r = await h.client.post("/api/v1/plugins/activate", json={"name": "auto-tagger"}, headers=ohdr)
        assert r.status_code == 201, r.text
        assert (await h.client.patch("/api/v1/plugins/auto-tagger", json={"enabled": False}, headers=hdr)).status_code == 403
        assert (await h.client.delete("/api/v1/plugins/auto-tagger", headers=hdr)).status_code == 403
        assert manager.get_plugin("auto-tagger").enabled


async def test_calibrate_is_superadmin_only_and_debug_recall_is_plan_gated(tmp_path):
    async with secure_app(tmp_path, [debug.router]) as h:
        uid = await h.create_user("tenant@example.com", verified=True)
        hdr = h.jwt(uid, "tenant@example.com")
        assert (await h.client.post("/api/v1/debug/calibrate", headers=hdr)).status_code == 403

        h.app.state.usage_meter = UsageMeter(h.db)  # cloud billing on; tenant is on the free plan
        r = await h.client.post("/api/v1/debug/recall", json={"query": "x"}, headers=hdr)
        assert r.status_code == 403 and "observability" in r.text

        write_only, _ = await h.api_key(uid, "editor", scopes=["memory:store"])
        r = await h.client.get("/api/v1/debug/analytics", headers={"X-API-Key": write_only})
        assert r.status_code == 403  # debug reads need memory:recall


async def test_team_invite_bound_to_email_and_link_space_requires_space_admin(tmp_path):
    async with secure_app(tmp_path, [teams.router]) as h:
        tm = TeamManager(h.db)
        await tm.init_schema()
        sm = SpaceManager(h.db)
        await sm.init_schema()
        h.app.state.team_manager = tm

        owner = await h.create_user("owner-team@example.com")
        invitee = await h.create_user("invitee@example.com")
        thief = await h.create_user("thief@example.com")
        team = await tm.create_team(name="Acme", owner_id=owner)
        invite = await tm.create_invite(team["id"], "invitee@example.com", "member", owner)

        r = await h.client.post(
            "/api/v1/teams/invites/accept", json={"token": invite["token"]}, headers=h.jwt(thief, "thief@example.com")
        )
        assert r.status_code == 400
        assert await tm.get_membership(team["id"], thief) is None
        r = await h.client.post(
            "/api/v1/teams/invites/accept", json={"token": invite["token"]}, headers=h.jwt(invitee, "invitee@example.com")
        )
        assert r.status_code == 200, r.text

        victim_space = await sm.create_space(name="private", owner_id=thief)
        r = await h.client.post(
            f"/api/v1/teams/{team['id']}/spaces", json={"space_id": victim_space["id"]}, headers=h.jwt(owner, "o@x.com")
        )
        assert r.status_code == 403
        own_space = await sm.create_space(name="shared", owner_id=owner)
        r = await h.client.post(
            f"/api/v1/teams/{team['id']}/spaces", json={"space_id": own_space["id"]}, headers=h.jwt(owner, "o@x.com")
        )
        assert r.status_code == 201, r.text


async def test_key_creation_failure_does_not_echo_internals(tmp_path, monkeypatch):
    from remembra.api.v1 import keys

    async with secure_app(tmp_path, [keys.router]) as h:
        uid = await h.create_user("err@example.com")

        async def boom(**_):
            raise RuntimeError("sqlite3 path=/var/lib/remembra/remembra.db secret=abc")

        monkeypatch.setattr(h.app.state.api_key_manager, "create_key", boom)
        r = await h.client.post("/api/v1/keys", json={"role": "viewer"}, headers=h.jwt(uid))
        assert r.status_code == 500
        assert "sqlite3" not in r.text and "/var/lib" not in r.text
