"""Exploit regressions: privilege escalation, tenant isolation of admin, session revocation.

SEC-2 (self-service admin / key escalation / global admin), SEC-9 (logout,
deactivation, password change must cut access), SEC-12 (superadmin by
unverified email), SEC-18 (spoofed X-Forwarded-For), SEC-6 (rate-limit keying).
All run against the real routers + SQLite with auth enabled.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from remembra.api.v1 import admin, auth, keys, memories
from remembra.auth.middleware import get_client_ip
from remembra.core.limiter import limiter
from remembra.security.audit import AuditAction
from tests.security_harness import MASTER_KEY, make_settings, secure_app

ROUTERS = [keys.router, admin.router, auth.router, memories.router]


# ---------------------------------------------------------------------------
# SEC-2: key minting / role escalation
# ---------------------------------------------------------------------------


async def test_jwt_user_cannot_mint_admin_key(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("a@example.com")
        r = await h.client.post("/api/v1/keys", json={"role": "admin"}, headers=h.jwt(uid))
        assert r.status_code == 403, r.text
        r = await h.client.post("/api/v1/keys", json={"permission": "admin"}, headers=h.jwt(uid))
        assert r.status_code == 403, r.text
        # Legit flow still works.
        r = await h.client.post("/api/v1/keys", json={"role": "editor", "name": "ok"}, headers=h.jwt(uid))
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "editor"


async def test_api_key_cannot_mint_above_itself_or_outside_projects(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("b@example.com")
        viewer, _ = await h.api_key(uid, "viewer", project_ids=["alpha"])
        hdr = {"X-API-Key": viewer}
        assert (await h.client.post("/api/v1/keys", json={"role": "editor"}, headers=hdr)).status_code == 403
        assert (
            await h.client.post("/api/v1/keys", json={"role": "viewer", "project_ids": ["beta"]}, headers=hdr)
        ).status_code == 403
        # Omitting project_ids must not produce an unrestricted key.
        r = await h.client.post("/api/v1/keys", json={"role": "viewer"}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.json()["project_ids"] == ["alpha"]


async def test_scoped_key_mints_only_scoped_keys(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("c@example.com")
        scoped, _ = await h.api_key(uid, "editor", scopes=["memory:recall"])
        r = await h.client.post("/api/v1/keys", json={"role": "editor"}, headers={"X-API-Key": scoped})
        assert r.status_code == 201, r.text
        role = await h.roles.get_role(r.json()["id"])
        assert role.scopes == ["memory:recall"]


async def test_master_key_can_still_provision_admin(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        r = await h.client.post(
            "/api/v1/keys",
            json={"user_id": "tenant-x", "role": "admin"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "admin"


async def test_api_key_cannot_patch_own_role_or_projects(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("d@example.com")
        viewer, viewer_id = await h.api_key(uid, "viewer", project_ids=["alpha"])
        hdr = {"X-API-Key": viewer}
        r = await h.client.patch(f"/api/v1/keys/{viewer_id}", json={"role": "admin"}, headers=hdr)
        assert r.status_code == 403, r.text
        r = await h.client.patch(f"/api/v1/keys/{viewer_id}", json={"project_ids": []}, headers=hdr)
        assert r.status_code == 403, r.text
        role = await h.roles.get_role(viewer_id)
        assert role.role.value == "viewer" and role.project_ids == ["alpha"]
        # Renaming is still allowed.
        r = await h.client.patch(f"/api/v1/keys/{viewer_id}", json={"name": "renamed"}, headers=hdr)
        assert r.status_code == 200, r.text


async def test_dashboard_session_cannot_escalate_key_to_admin(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("e@example.com")
        _, key_id = await h.api_key(uid, "viewer")
        r = await h.client.patch(f"/api/v1/keys/{key_id}", json={"role": "admin"}, headers=h.jwt(uid))
        assert r.status_code == 403
        r = await h.client.patch(f"/api/v1/keys/{key_id}", json={"role": "editor"}, headers=h.jwt(uid))
        assert r.status_code == 200
        assert (await h.roles.get_role(key_id)).role.value == "editor"


async def test_low_privilege_key_cannot_revoke_broader_key(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("f@example.com")
        viewer, _ = await h.api_key(uid, "viewer")
        _, editor_id = await h.api_key(uid, "editor")
        r = await h.client.delete(f"/api/v1/keys/{editor_id}", headers={"X-API-Key": viewer})
        assert r.status_code == 403
        assert (await h.keys.get_key_info(editor_id)).active


# ---------------------------------------------------------------------------
# SEC-2: tenant admin is scoped to its own tenant
# ---------------------------------------------------------------------------


async def test_admin_audit_is_scoped_to_own_tenant(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        audit = h.app.state.audit_logger
        await audit.log_event(user_id="tenant-a", action=AuditAction.MEMORY_STORE, resource_id="mem-a")
        await audit.log_event(user_id="tenant-b", action=AuditAction.MEMORY_STORE, resource_id="SECRET-B")
        admin_a, _ = await h.api_key("tenant-a", "admin")
        hdr = {"X-API-Key": admin_a}
        for path in ("/api/v1/admin/audit", "/api/v1/admin/audit/export/json", "/api/v1/admin/audit/export/csv"):
            r = await h.client.get(path, headers=hdr)
            assert r.status_code == 200, r.text
            assert "SECRET-B" not in r.text and "tenant-b" not in r.text
            assert "mem-a" in r.text
        r = await h.client.get("/api/v1/admin/audit", params={"user_id": "tenant-b"}, headers=hdr)
        assert r.status_code == 403


async def test_admin_cannot_assign_roles_on_foreign_or_synthetic_keys(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        admin_a, _ = await h.api_key("tenant-a", "admin")
        _, victim_key = await h.api_key("tenant-b", "viewer")
        hdr = {"X-API-Key": admin_a}
        r = await h.client.post("/api/v1/admin/roles", json={"api_key_id": victim_key, "role": "admin"}, headers=hdr)
        assert r.status_code == 404
        assert (await h.roles.get_role(victim_key)).role.value == "viewer"
        # The shared JWT pseudo-key must never carry a role (would apply to every dashboard user).
        r = await h.client.post("/api/v1/admin/roles", json={"api_key_id": "jwt_auth", "role": "admin"}, headers=hdr)
        assert r.status_code == 400
        r = await h.client.delete(f"/api/v1/admin/roles/{victim_key}", headers=hdr)
        assert r.status_code == 404
        with pytest.raises(ValueError):
            await h.roles.assign_role("jwt_auth", admin.Role.ADMIN)


async def test_project_restricted_admin_cannot_widen_projects(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        admin_a, _ = await h.api_key("tenant-a", "admin", project_ids=["alpha"])
        _, other = await h.api_key("tenant-a", "viewer", project_ids=["alpha"])
        hdr = {"X-API-Key": admin_a}
        r = await h.client.post(
            "/api/v1/admin/roles", json={"api_key_id": other, "role": "editor", "project_ids": []}, headers=hdr
        )
        assert r.status_code == 403
        r = await h.client.post(
            "/api/v1/admin/roles", json={"api_key_id": other, "role": "editor", "project_ids": ["alpha"]}, headers=hdr
        )
        assert r.status_code == 200, r.text


async def test_sleep_time_run_forced_to_own_tenant(tmp_path):
    calls: list = []

    class Worker:
        running = False
        last_run = None

        async def run_consolidation(self, user_id=None):
            calls.append(user_id)
            from datetime import datetime

            return SimpleNamespace(
                started_at=datetime.now(),
                completed_at=None,
                memories_scanned=0,
                duplicates_merged=0,
                entities_resolved=0,
                relationships_discovered=0,
                importance_rescored=0,
                memories_decayed=0,
                errors=[],
            )

    async with secure_app(tmp_path, ROUTERS, state={"sleep_worker": Worker()}) as h:
        admin_a, _ = await h.api_key("tenant-a", "admin")
        hdr = {"X-API-Key": admin_a}
        assert (
            await h.client.post("/api/v1/admin/sleep-time/run", params={"user_id": "tenant-b"}, headers=hdr)
        ).status_code == 403
        r = await h.client.post("/api/v1/admin/sleep-time/run", headers=hdr)
        assert r.status_code == 200, r.text
        assert calls == ["tenant-a"]  # never None (= every tenant)


# ---------------------------------------------------------------------------
# SEC-12: superadmin requires a verified owner email (or explicit id allow-list)
# ---------------------------------------------------------------------------


async def test_unverified_owner_email_is_not_superadmin(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        squatter = await h.create_user("owner@example.com", verified=False)
        r = await h.client.get("/api/v1/admin/users", headers=h.jwt(squatter, "owner@example.com"))
        assert r.status_code == 403
        r = await h.client.get("/api/v1/auth/me", headers=h.jwt(squatter, "owner@example.com"))
        assert r.status_code == 200 and r.json()["is_admin"] is False


async def test_verified_owner_is_superadmin_but_editor_key_is_not(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        owner = await h.create_user("owner@example.com", verified=True)
        assert (await h.client.get("/api/v1/admin/users", headers=h.jwt(owner, "owner@example.com"))).status_code == 200
        editor_key, _ = await h.api_key(owner, "editor")
        assert (await h.client.get("/api/v1/admin/users", headers={"X-API-Key": editor_key})).status_code == 403
        admin_key, _ = await h.api_key(owner, "admin")
        assert (await h.client.get("/api/v1/admin/users", headers={"X-API-Key": admin_key})).status_code == 200


async def test_superadmin_user_id_allowlist(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("ops@example.com")
        h.settings.superadmin_user_ids = [uid]
        assert (await h.client.get("/api/v1/admin/stats", headers=h.jwt(uid, "ops@example.com"))).status_code == 200


# ---------------------------------------------------------------------------
# SEC-9: logout / deactivation / password change cut API access
# ---------------------------------------------------------------------------


async def test_logged_out_jwt_rejected_by_main_api(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("g@example.com")
        hdr = h.jwt(uid, "g@example.com")
        assert (await h.client.get("/api/v1/keys", headers=hdr)).status_code == 200
        assert (await h.client.post("/api/v1/auth/logout", headers=hdr)).status_code == 200
        r = await h.client.get("/api/v1/memories", headers=hdr)
        assert r.status_code == 401
        r = await h.client.get("/api/v1/keys", headers=hdr)
        assert r.status_code == 401


async def test_deactivated_account_loses_jwt_and_api_keys(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("h@example.com", password="Str0ng!Passw0rd")
        key, key_id = await h.api_key(uid, "editor")
        hdr = h.jwt(uid, "h@example.com")
        r = await h.client.request("DELETE", "/api/v1/auth/me", json={"password": "Str0ng!Passw0rd"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert (await h.client.get("/api/v1/memories", headers={"X-API-Key": key})).status_code == 401
        assert (await h.client.get("/api/v1/memories", headers=hdr)).status_code == 401
        info = await h.keys.get_key_info(key_id)
        assert info is not None and not info.active  # soft-revoked, not deleted


async def test_api_key_rejected_when_account_deactivated_directly(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("i@example.com")
        key, _ = await h.api_key(uid, "editor")
        hdr = {"X-API-Key": key}
        assert (await h.client.get("/api/v1/keys", headers=hdr)).status_code == 200
        await h.db.deactivate_user(uid)
        assert (await h.client.get("/api/v1/memories", headers=hdr)).status_code == 401


async def test_superadmin_deactivation_revokes_keys(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        owner = await h.create_user("owner@example.com", verified=True)
        victim = await h.create_user("j@example.com")
        key, _ = await h.api_key(victim, "editor")
        r = await h.client.post(
            f"/api/v1/admin/users/{victim}/activate", params={"active": "false"}, headers=h.jwt(owner, "owner@example.com")
        )
        assert r.status_code == 200, r.text
        assert (await h.client.get("/api/v1/memories", headers={"X-API-Key": key})).status_code == 401


async def test_password_change_invalidates_existing_sessions(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("k@example.com", password="Str0ng!Passw0rd")
        stolen = h.jwt(uid, "k@example.com")
        r = await h.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "Str0ng!Passw0rd", "new_password": "N3w!Passw0rdX"},
            headers=stolen,
        )
        assert r.status_code == 200, r.text
        assert (await h.client.get("/api/v1/memories", headers=stolen)).status_code == 401
        assert (await h.client.get("/api/v1/auth/me", headers=stolen)).status_code == 401
        # A fresh login works.
        r = await h.client.post("/api/v1/auth/login", json={"email": "k@example.com", "password": "N3w!Passw0rdX"})
        assert r.status_code == 200, r.text
        fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert (await h.client.get("/api/v1/keys", headers=fresh)).status_code == 200


# ---------------------------------------------------------------------------
# SEC-18: X-Forwarded-For only from trusted proxies
# ---------------------------------------------------------------------------


def _req(peer: str, headers: dict[str, str]):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_forwarded_for_ignored_from_untrusted_peer(monkeypatch):
    import remembra.config as cfg

    monkeypatch.setattr(cfg, "_settings", make_settings())
    assert get_client_ip(_req("203.0.113.5", {"X-Forwarded-For": "1.2.3.4"})) == "203.0.113.5"
    assert get_client_ip(_req("203.0.113.5", {"X-Real-IP": "1.2.3.4"})) == "203.0.113.5"


def test_forwarded_for_from_trusted_proxy_uses_rightmost_untrusted_hop(monkeypatch):
    import remembra.config as cfg

    monkeypatch.setattr(cfg, "_settings", make_settings())
    # Client spoofs a leading hop; the proxy appends the real address.
    req = _req("10.0.0.2", {"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.0.0.9"})
    assert get_client_ip(req) == "198.51.100.7"
    monkeypatch.setattr(cfg, "_settings", make_settings(trusted_proxies=[]))
    assert get_client_ip(req) == "10.0.0.2"


# ---------------------------------------------------------------------------
# SEC-6: login brute force can't be spread across fake API-key buckets
# ---------------------------------------------------------------------------


async def test_login_rate_limit_ignores_rotating_api_key_header(tmp_path):
    limiter.enabled = True
    limiter.reset()
    try:
        async with secure_app(tmp_path, ROUTERS) as h:
            await h.create_user("l@example.com")
            codes = []
            for i in range(12):
                r = await h.client.post(
                    "/api/v1/auth/login",
                    json={"email": f"nobody{i}@example.com", "password": "wrong-password"},
                    headers={"X-API-Key": f"rem_rotating_{i:04d}"},
                )
                codes.append(r.status_code)
            assert 429 in codes, codes
            assert codes.index(429) == 10  # 10/minute per client IP, header ignored
    finally:
        limiter.reset()
        limiter.enabled = False


async def test_per_account_lockout_after_repeated_failures(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        await h.create_user("m@example.com", password="Str0ng!Passw0rd")
        for _ in range(5):
            r = await h.client.post("/api/v1/auth/login", json={"email": "m@example.com", "password": "bad-guess"})
            assert r.status_code == 401
        # Even the right password is refused while locked (distributed guessing defence).
        r = await h.client.post("/api/v1/auth/login", json={"email": "m@example.com", "password": "Str0ng!Passw0rd"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ---------------------------------------------------------------------------
# SEC-20: TOTP secret encrypted at rest + codes are single-use
# ---------------------------------------------------------------------------


async def test_totp_secret_encrypted_and_codes_not_replayable(tmp_path):
    import pyotp

    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("totp@example.com", password="Str0ng!Passw0rd")
        hdr = h.jwt(uid, "totp@example.com")
        r = await h.client.post("/api/v1/auth/2fa/setup", headers=hdr)
        assert r.status_code == 200, r.text
        secret = r.json()["secret"]
        stored = (await h.db.get_user_by_id(uid))["totp_secret"]
        assert stored.startswith("enc:v1:") and secret not in stored

        totp = pyotp.TOTP(secret)
        assert (await h.client.post("/api/v1/auth/2fa/enable", json={"code": totp.now()}, headers=hdr)).status_code == 200

        # The enabling code is consumed; log in with the next step's code (inside the ±1 window).
        next_code = totp.at(int(time.time()) + 30)
        creds = {"email": "totp@example.com", "password": "Str0ng!Passw0rd", "totp_code": next_code}
        first = await h.client.post("/api/v1/auth/login", json=creds)
        assert first.status_code == 200, first.text
        replay = await h.client.post("/api/v1/auth/login", json=creds)
        assert replay.status_code == 401


async def test_signup_does_not_reveal_existing_accounts(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        body = {"email": "exists@example.com", "password": "Str0ng!Passw0rd"}
        first = await h.client.post("/api/v1/auth/signup", json=body)
        second = await h.client.post("/api/v1/auth/signup", json=body)
        assert first.status_code == second.status_code == 201
        assert set(first.json()) == set(second.json())
        assert first.json()["id"] != second.json()["id"]
        # Only one account exists.
        cur = await h.db.conn.execute("SELECT COUNT(*) FROM users WHERE email = ?", ("exists@example.com",))
        assert (await cur.fetchone())[0] == 1


async def test_login_errors_are_uniform(tmp_path):
    async with secure_app(tmp_path, ROUTERS) as h:
        await h.create_user("u1@example.com", password="Str0ng!Passw0rd")
        await h.create_user("u2@example.com", password="Str0ng!Passw0rd", active=False)
        details = set()
        for email, pw in [("u1@example.com", "bad"), ("ghost@example.com", "bad"), ("u2@example.com", "Str0ng!Passw0rd")]:
            r = await h.client.post("/api/v1/auth/login", json={"email": email, "password": pw})
            assert r.status_code == 401
            details.add(r.json()["detail"])
        assert details == {"Invalid email or password"}


async def test_email_verification_flow_enables_owner(tmp_path, monkeypatch):
    sent: dict = {}

    async def fake_send(self, to, verify_url):
        from remembra.cloud.email import EmailResult

        sent["token"] = verify_url.split("token=")[1]
        return EmailResult(success=True)

    from remembra.api.v1 import auth as auth_api
    from remembra.cloud import email as email_mod

    monkeypatch.setattr(email_mod.EmailService, "send_email_verification_email", fake_send)
    monkeypatch.setattr(email_mod.ResendBackend, "__init__", lambda self, api_key=None: None)
    monkeypatch.setattr(auth_api, "EMAIL_AVAILABLE", True)
    async with secure_app(tmp_path, ROUTERS) as h:
        owner = await h.create_user("owner@example.com")
        hdr = h.jwt(owner, "owner@example.com")
        assert (await h.client.get("/api/v1/admin/stats", headers=hdr)).status_code == 403
        assert (await h.client.post("/api/v1/auth/verify-email/request", headers=hdr)).status_code == 200
        bad = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": "x" * 32}, headers=hdr)
        assert bad.status_code == 400
        ok = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": sent["token"]}, headers=hdr)
        assert ok.status_code == 200, ok.text
        assert (await h.client.get("/api/v1/admin/stats", headers=hdr)).status_code == 200
        # Single use.
        again = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": sent["token"]}, headers=hdr)
        assert again.status_code == 400


async def test_auth_logs_contain_no_emails_or_key_material(tmp_path, capsys, caplog):
    async with secure_app(tmp_path, ROUTERS) as h:
        email = "private.person@example.com"
        await h.client.post("/api/v1/auth/signup", json={"email": email, "password": "Str0ng!Passw0rd"})
        await h.client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
        await h.client.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong"})
        forged = "rem_ForgedKeyMaterial0123456789abcdef"
        await h.client.get("/api/v1/memories", headers={"X-API-Key": forged})
        await h.client.post("/api/v1/auth/forgot-password", json={"email": email})
    captured = capsys.readouterr()
    logs = captured.out + captured.err + caplog.text
    assert email not in logs and "ghost@example.com" not in logs
    assert "rem_Forged" not in logs
