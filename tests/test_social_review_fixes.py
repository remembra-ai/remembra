"""Regression tests for the Sign in with GitHub / Google security review.

1. Login CSRF / session swap: a login code only works in the browser the
   callback ran in (HttpOnly cookie set with the code, required and burned at
   /oauth/exchange).
2. Pre-registered accounts: the first proof of mailbox control (a password
   reset on an unverified account) verifies the email and opens a review of
   the credentials set up before it (nothing is revoked until the owner
   chooses; tests/test_account_review.py).
3. GitHub never links by email into an existing account; it is connected
   from a signed-in session (Settings) instead.
4. One free account per verified email, in both directions: a verified API
   signup tenant blocks a social account, dashboard verification and the
   reset-verifies step for the same address.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
import pytest

from remembra.api.v1 import auth, keys, social_auth
from remembra.auth import social
from remembra.cloud.metering import UsageMeter
from remembra.connector.store import SCHEMA_SQL as CONNECTOR_SCHEMA
from remembra.security import state as security_state
from remembra.webhooks.manager import WebhookManager
from tests.security_harness import JWT_SECRET, secure_app
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter
from tests.test_social_login import (
    FakeProviders,
    callback,
    count,
    exchange,
    oauth_settings,
    query_of,
    sign_in,
    start,
)

ROUTERS = [auth.router, social_auth.router, keys.router]


@pytest.fixture()
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProviders]:
    """Fake GitHub / Google endpoints (same as tests/test_social_login.py)."""
    fake = FakeProviders()
    monkeypatch.setattr(social, "http_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    social.google_jwks.reset()
    set_cloud_rate_limiter(CloudRateLimiter())
    yield fake
    social.google_jwks.reset()
    set_cloud_rate_limiter(None)


@pytest.fixture()
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    sent: list[tuple[str, str, str]] = []

    async def record(to: str, provider_name: str, provider_email: str) -> None:
        sent.append((to, provider_name, provider_email))

    monkeypatch.setattr(social, "link_notifier", record)
    return sent


def other_browser(h: Any) -> httpx.AsyncClient:
    """A second browser on the same server: its own (empty) cookie jar."""
    return httpx.AsyncClient(transport=h.client._transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 1. The login code is bound to the browser
# ---------------------------------------------------------------------------


async def test_login_code_minted_in_one_browser_cannot_sign_in_another(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        # Attacker: runs the flow in their own browser and stops at the 303.
        attacker_code = (await sign_in(h, providers, "github"))["code"]
        # Victim: opens https://app.../oauth/callback#code=<attacker_code>; the
        # dashboard exchanges it from the victim's browser (no binding cookie).
        async with other_browser(h) as victim:
            r = await victim.post("/api/v1/auth/oauth/exchange", json={"code": attacker_code})
            assert r.status_code == 400
            assert "did not finish in the browser that started it" in r.json()["detail"]
            assert "access_token" not in r.text
        # The code was burned, so it cannot be retried from anywhere.
        again = await exchange(h, attacker_code)
        assert again.status_code == 400 and "expired or was already used" in again.json()["detail"]


async def test_a_binding_cookie_from_another_flow_does_not_match(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        attacker_code = (await sign_in(h, providers, "github"))["code"]
        async with other_browser(h) as victim:
            # The victim has a binding cookie of their own (their own Google sign-in).
            r = await victim.get("/api/v1/auth/oauth/google/start")
            providers.authorize(r.headers["location"], "victim-code")
            cb = await victim.get(
                "/api/v1/auth/oauth/google/callback", params={"code": "victim-code", "state": query_of(r)["state"]}
            )
            victim_code = dict(parse_qsl(urlsplit(cb.headers["location"]).fragment))["code"]
            assert victim.cookies.get(social.login_cookie_name())
            refused = await victim.post("/api/v1/auth/oauth/exchange", json={"code": attacker_code})
            assert refused.status_code == 400
            # The victim's own code still works in the victim's browser.
            ok = await victim.post("/api/v1/auth/oauth/exchange", json={"code": victim_code})
            assert ok.status_code == 200 and ok.json()["user"]["email"] == "person@gmail.com"


async def test_binding_cookie_attributes_and_cleanup(tmp_path, providers, monkeypatch) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        providers.authorize(r.headers["location"])
        cb = await h.client.get(
            "/api/v1/auth/oauth/github/callback", params={"code": "auth-code-1", "state": query_of(r)["state"]}
        )
        cookies = cb.headers.get_list("set-cookie")
        binding = next(c for c in cookies if c.startswith("remembra_oauth_login="))
        assert "HttpOnly" in binding and "SameSite=lax" in binding and "Max-Age=300" in binding and "Path=/" in binding
        # The state cookie is still cleared.
        assert any(c.startswith("remembra_oauth_github=") and "Max-Age=0" in c for c in cookies)
        # Only a hash of the binding secret is stored.
        secret = h.client.cookies.get("remembra_oauth_login")
        stored = await h.db.conn.execute("SELECT browser_hash FROM oauth_login_codes")
        assert secret not in str(await stored.fetchall())

        code = dict(parse_qsl(urlsplit(cb.headers["location"]).fragment))["code"]
        ok = await exchange(h, code)
        assert ok.status_code == 200
        assert any(c.startswith("remembra_oauth_login=") and "Max-Age=0" in c for c in ok.headers.get_list("set-cookie"))

    # HTTPS: __Host- prefixed and Secure.
    import tests.test_social_login as fake_module

    monkeypatch.setattr(fake_module, "API", "https://api.example.test")  # the fake provider's expected redirect_uri
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings(public_url="https://api.example.test")) as h:
        r = await start(h, "github")
        providers.authorize(r.headers["location"], "auth-code-2")
        h.client.cookies.set("__Host-remembra_oauth_github", r.cookies["__Host-remembra_oauth_github"])
        cb = await h.client.get(
            "/api/v1/auth/oauth/github/callback", params={"code": "auth-code-2", "state": query_of(r)["state"]}
        )
        binding = next(c for c in cb.headers.get_list("set-cookie") if c.startswith("__Host-remembra_oauth_login="))
        assert "Secure" in binding and "Path=/" in binding and "Domain" not in binding


async def test_error_redirects_set_no_binding_cookie(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        providers.github_emails = []
        providers.authorize(r.headers["location"])
        cb = await h.client.get(
            "/api/v1/auth/oauth/github/callback", params={"code": "auth-code-1", "state": query_of(r)["state"]}
        )
        assert not any("remembra_oauth_login=" in c for c in cb.headers.get_list("set-cookie"))


async def test_two_factor_step_keeps_the_binding(tmp_path, providers) -> None:
    import pyotp

    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        secret, _, _ = await h.users.setup_totp(uid)
        assert (await h.users.enable_totp(uid, pyotp.TOTP(secret).now()))[0]
        code = (await sign_in(h, providers, "google"))["code"]
        first = await exchange(h, code)
        assert first.json()["requires_2fa"] is True
        async with other_browser(h) as elsewhere:  # the 2FA answer must come from the same browser too
            r = await elsewhere.post(
                "/api/v1/auth/oauth/exchange", json={"code": code, "totp_code": pyotp.TOTP(secret).at(time.time() + 30)}
            )
            assert r.status_code == 400


async def test_existing_login_codes_table_gets_the_binding_column(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        # A deployment that ran the first version of this branch.
        await h.db.conn.executescript(
            """
            CREATE TABLE oauth_login_codes (code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL,
                new_account INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL);
            INSERT INTO oauth_login_codes VALUES ('legacy', 'u1', 'github', 0, 9999999999);
            CREATE TABLE oauth_login_states (state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, browser_hash TEXT NOT NULL,
                code_verifier TEXT NOT NULL, nonce TEXT NOT NULL, from_page TEXT NOT NULL, expires_at REAL NOT NULL);
            """
        )
        social._initialized.discard(h.db.conn)
        frag = await sign_in(h, providers, "github")
        assert (await exchange(h, frag["code"])).status_code == 200
        # A code issued before the upgrade has no binding and is refused.
        assert social.login_code_bound_to_browser({"browser_hash": None}, "anything") is False


# ---------------------------------------------------------------------------
# 2. Pre-registered account: the mailbox owner's reset opens a review
# ---------------------------------------------------------------------------


async def _squatter_credentials(h: Any, uid: str) -> dict[str, Any]:
    import pyotp

    raw_key, _ = await h.api_key(uid)
    secret, _, _ = await h.users.setup_totp(uid)
    assert (await h.users.enable_totp(uid, pyotp.TOTP(secret).now()))[0]
    await WebhookManager(h.db).init_schema()
    now = time.time()
    await h.db.conn.execute(
        "INSERT INTO webhooks (id, user_id, url, events, secret, active, created_at, updated_at)"
        " VALUES ('wh1', ?, 'https://attacker.example/hook', '[\"memory.created\"]', 's', 1, 'x', 'x')",
        (uid,),
    )
    await h.db.conn.executescript(CONNECTOR_SCHEMA)
    await h.db.conn.execute(
        "INSERT INTO oauth_grants (grant_id, user_id, client_id, scope, resource, project_ids, agent_id, created_at)"
        " VALUES ('g1', ?, 'c1', 'memory', 'r', '[]', 'a', ?)",
        (uid, now),
    )
    await h.db.conn.execute(
        "INSERT INTO oauth_tokens (token_hash, grant_id, client_id, kind, created_at, expires_at)"
        " VALUES ('t1', 'g1', 'c1', 'access', ?, ?)",
        (now, now + 3600),
    )
    await h.db.conn.commit()
    return {"key": raw_key}


async def test_preregistered_account_reset_revokes_nothing_and_opens_a_review(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        # Attacker registers the victim's address, never verifies it, sets up access.
        uid = await h.create_user("victim@gmail.com", verified=False)
        squat = await _squatter_credentials(h, uid)
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": squat["key"]})).status_code == 200

        providers.google_claims = {"email": "victim@gmail.com"}
        # The mailbox owner resets the password: that proves the mailbox, revokes nothing.
        token, _ = await h.users.create_password_reset_token("victim@gmail.com")
        ok, err = await h.users.reset_password("victim@gmail.com", token, "N3w!Passw0rdX")
        assert ok, err

        user = await h.db.get_user_by_id(uid)
        assert user["email_verified"] and user["totp_enabled"]
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": squat["key"]})).status_code == 200
        assert await count(h, "SELECT COUNT(*) FROM webhooks WHERE user_id = ? AND active = 1", uid) == 1
        assert await count(h, "SELECT COUNT(*) FROM oauth_grants WHERE user_id = ? AND revoked_at IS NULL", uid) == 1
        assert await count(h, "SELECT COUNT(*) FROM account_reviews WHERE user_id = ? AND completed_at IS NULL", uid) == 1

        # The victim signs in with the new password without the squatter's 2FA, and
        # that session may act on the review (tests/test_account_review.py).
        r = await h.client.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": "N3w!Passw0rdX"})
        assert r.status_code == 200 and r.json().get("access_token")
        claims = jwt.decode(r.json()["access_token"], JWT_SECRET, algorithms=["HS256"])
        assert claims.get("rvw")

        # Google now links into the (verified) account. It proves the address again,
        # so that Google account may act on the review too (the squatter's 2FA does not apply).
        body = (await exchange(h, (await sign_in(h, providers, "google"))["code"])).json()
        assert body["user"]["id"] == uid and body["requires_2fa"] is False
        assert jwt.decode(body["access_token"], JWT_SECRET, algorithms=["HS256"]).get("rvw") == claims["rvw"]


async def test_reset_of_a_verified_account_keeps_its_keys(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("owner@gmail.com", verified=True)
        raw_key, _ = await h.api_key(uid)
        token, _ = await h.users.create_password_reset_token("owner@gmail.com")
        assert (await h.users.reset_password("owner@gmail.com", token, "N3w!Passw0rdX"))[0]
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": raw_key})).status_code == 200


# ---------------------------------------------------------------------------
# 3. GitHub: no email auto-link; connect from a signed-in session
# ---------------------------------------------------------------------------


def stale_jwt(uid: str, email: str, age_seconds: int) -> dict[str, str]:
    issued = time.time() - age_seconds
    payload = {
        "sub": uid,
        "email": email,
        "iat": int(issued),
        "iat_ms": int(issued * 1000),
        "exp": int(issued) + 86400,
        "type": "access",
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, JWT_SECRET, algorithm='HS256')}"}


async def connect(h: Any, fake: FakeProviders, provider: str, headers: dict[str, str], code: str) -> dict[str, str]:
    r = await h.client.post(f"/api/v1/auth/oauth/{provider}/link", headers=headers)
    assert r.status_code == 200, r.text
    start_path = r.json()["start_path"]
    s = await h.client.get(start_path)
    assert s.status_code == 302, s.text
    fake.authorize(s.headers["location"], code)
    return await callback(h, provider, {"code": code, "state": query_of(s)["state"]})


async def test_stale_github_email_does_not_link_into_a_verified_account(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await h.create_user("alice@company.example", verified=True)
        # A different person's GitHub account still lists alice's old work address as verified.
        providers.github_user = {"id": 999, "login": "not-alice"}
        providers.github_emails = [{"email": "alice@company.example", "primary": True, "verified": True}]
        frag = await sign_in(h, providers, "github")
        assert frag == {"error": "account_exists_link_required", "provider": "github", "from": "login"}
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_codes") == 0
        assert await count(h, "SELECT COUNT(*) FROM users") == 1
        assert notices == []


async def test_github_is_connected_from_a_signed_in_session(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("alice@company.example", verified=True)
        hdr = h.jwt(uid, "alice@company.example")
        listed = (await h.client.get("/api/v1/auth/identities", headers=hdr)).json()
        assert listed["identities"] == [] and [p["id"] for p in listed["available"]] == ["github", "google"]

        providers.github_emails = [{"email": "alice.personal@example.org", "primary": True, "verified": True}]
        frag = await connect(h, providers, "github", hdr, "link-code-1")
        assert frag == {"linked": "1", "provider": "github", "from": "settings"}
        assert not any("remembra_oauth_login" in k for k in h.client.cookies)  # nothing to exchange
        assert notices == [("alice@company.example", "GitHub", "alice.personal@example.org")]

        listed = (await h.client.get("/api/v1/auth/identities", headers=hdr)).json()
        assert [(i["provider"], i["email"]) for i in listed["identities"]] == [("github", "alice.personal@example.org")]

        # Sign in with GitHub now reaches the same account.
        body = (await exchange(h, (await sign_in(h, providers, "github", code="login-code-1"))["code"])).json()
        assert body["user"]["id"] == uid and body["new_account"] is False

        # Disconnect.
        assert (await h.client.delete("/api/v1/auth/identities/github", headers=hdr)).json() == {"removed": True}
        assert (await h.client.delete("/api/v1/auth/identities/github", headers=hdr)).status_code == 404
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0


async def test_connect_needs_a_recent_session_and_a_single_use_ticket(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("alice@company.example", verified=True)
        assert (await h.client.post("/api/v1/auth/oauth/github/link")).status_code == 401
        old = await h.client.post("/api/v1/auth/oauth/github/link", headers=stale_jwt(uid, "alice@company.example", 3600))
        assert old.status_code == 403 and "sign in again" in old.json()["detail"]
        recent = stale_jwt(uid, "alice@company.example", 60)
        assert (await h.client.post("/api/v1/auth/oauth/github/link", headers=recent)).status_code == 200

        start_path = (await h.client.post("/api/v1/auth/oauth/github/link", headers=h.jwt(uid))).json()["start_path"]
        assert (await h.client.get(start_path)).status_code == 302
        # Replayed (e.g. read back from browser history): refused without reaching GitHub.
        replay = await h.client.get(start_path)
        assert replay.status_code == 303
        assert dict(parse_qsl(urlsplit(replay.headers["location"]).fragment)) == {
            "error": "invalid_state",
            "provider": "github",
            "from": "settings",
        }
        # A ticket for Google cannot start a GitHub flow; a forged one is refused.
        google_path = (await h.client.post("/api/v1/auth/oauth/google/link", headers=h.jwt(uid))).json()["start_path"]
        ticket = dict(parse_qsl(urlsplit(google_path).query))["link"]
        assert (await h.client.get("/api/v1/auth/oauth/github/start", params={"link": ticket})).status_code == 303
        assert (await h.client.get("/api/v1/auth/oauth/github/start", params={"link": "forged"})).status_code == 303
        # Expired.
        path = (await h.client.post("/api/v1/auth/oauth/github/link", headers=h.jwt(uid))).json()["start_path"]
        await h.db.conn.execute("UPDATE oauth_link_tickets SET expires_at = ?", (time.time() - 1,))
        await h.db.conn.commit()
        assert (await h.client.get(path)).status_code == 303
        # ?from=settings without a ticket is just a sign-in.
        plain = await h.client.get("/api/v1/auth/oauth/github/start", params={"from": "settings"})
        assert plain.status_code == 302
        cursor = await h.db.conn.execute(
            "SELECT from_page, link_user_id FROM oauth_login_states ORDER BY expires_at DESC LIMIT 1"
        )
        assert tuple(await cursor.fetchone()) == ("login", None)
        assert notices == []


@pytest.mark.parametrize("provider", ["google", "github"])
async def test_link_ticket_minted_in_one_browser_never_links_in_another(tmp_path, providers, notices, provider) -> None:
    """Account-link CSRF: the attacker's ticket, opened by a victim, must not attach the victim's identity."""
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        attacker = await h.create_user("attacker@evil.example", verified=True)
        minted = await h.client.post(f"/api/v1/auth/oauth/{provider}/link", headers=h.jwt(attacker, "attacker@evil.example"))
        assert minted.status_code == 200
        start_path = minted.json()["start_path"]
        async with other_browser(h) as victim:
            s = await victim.get(start_path)
            # Refused before reaching the provider: no state, nothing to approve.
            assert s.status_code == 303, s.text
            assert dict(parse_qsl(urlsplit(s.headers["location"]).fragment)) == {
                "error": "invalid_state",
                "provider": provider,
                "from": "settings",
            }
            assert "set-cookie" not in s.headers or f"remembra_oauth_{provider}=" not in s.headers["set-cookie"]
            # The victim then signs in with their own Google / GitHub account normally.
            if provider == "google":
                await h.create_user("person@gmail.com", verified=True)
            victim_start = await victim.get(f"/api/v1/auth/oauth/{provider}/start")
            providers.authorize(victim_start.headers["location"], "victim-code")
            cb = await victim.get(
                f"/api/v1/auth/oauth/{provider}/callback",
                params={"code": "victim-code", "state": query_of(victim_start)["state"]},
            )
            frag = dict(parse_qsl(urlsplit(cb.headers["location"]).fragment))
            assert "linked" not in frag
            body = (await victim.post("/api/v1/auth/oauth/exchange", json={"code": frag["code"]})).json()
            assert body["user"]["id"] != attacker
        # The ticket is burned: even the attacker's own browser cannot use it now.
        assert (await h.client.get(start_path)).status_code == 303
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", attacker) == 0
        assert await count(h, "SELECT COUNT(*) FROM oauth_link_tickets") == 0
        assert all(to != "attacker@evil.example" for to, _name, _email in notices)


async def test_link_ticket_needs_its_own_cookie_not_any_link_cookie(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        attacker = await h.create_user("attacker@evil.example", verified=True)
        victim_id = await h.create_user("victim@company.example", verified=True)
        attacker_path = (
            await h.client.post("/api/v1/auth/oauth/github/link", headers=h.jwt(attacker, "attacker@evil.example"))
        ).json()["start_path"]
        async with other_browser(h) as victim:
            # The victim's browser holds a live link cookie of its own (they were connecting GitHub too).
            own = await victim.post("/api/v1/auth/oauth/github/link", headers=h.jwt(victim_id, "victim@company.example"))
            assert own.status_code == 200
            refused = await victim.get(attacker_path)
            assert refused.status_code == 303
            assert "invalid_state" in refused.headers["location"]
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_states WHERE link_user_id = ?", attacker) == 0


async def test_link_cookie_attributes_and_cleanup(tmp_path, providers, notices, monkeypatch) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("alice@company.example", verified=True)
        r = await h.client.post("/api/v1/auth/oauth/github/link", headers=h.jwt(uid))
        cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("remembra_oauth_link_github="))
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=120" in cookie and "Path=/" in cookie
        assert r.headers["cache-control"] == "no-store"
        # Only a hash of the cookie secret is stored with the ticket.
        secret = h.client.cookies.get("remembra_oauth_link_github")
        assert secret and secret not in str(await (await h.db.conn.execute("SELECT * FROM oauth_link_tickets")).fetchall())
        s = await h.client.get(r.json()["start_path"])
        assert s.status_code == 302
        assert any(c.startswith("remembra_oauth_link_github=") and "Max-Age=0" in c for c in s.headers.get_list("set-cookie"))
        cursor = await h.db.conn.execute("SELECT from_page, link_user_id FROM oauth_login_states")
        assert [tuple(row) for row in await cursor.fetchall()] == [("settings", uid)]

    import tests.test_social_login as fake_module

    monkeypatch.setattr(fake_module, "API", "https://api.example.test")
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings(public_url="https://api.example.test")) as h:
        uid = await h.create_user("bob@company.example", verified=True)
        r = await h.client.post("/api/v1/auth/oauth/google/link", headers=h.jwt(uid, "bob@company.example"))
        cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("__Host-remembra_oauth_link_google="))
        assert "Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie


async def test_link_ticket_issued_before_browser_binding_is_refused(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("alice@company.example", verified=True)
        # A deployment that ran the previous version: the table has no browser_hash column.
        await h.db.conn.executescript(
            """
            CREATE TABLE oauth_link_tickets (ticket_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL,
                expires_at REAL NOT NULL);
            """
        )
        await h.db.conn.execute(
            "INSERT INTO oauth_link_tickets VALUES (?, ?, 'github', ?)", (social._hash("legacy-ticket"), uid, time.time() + 60)
        )
        await h.db.conn.commit()
        social._initialized.discard(h.db.conn)
        legacy = await h.client.get("/api/v1/auth/oauth/github/start", params={"link": "legacy-ticket"})
        assert legacy.status_code == 303 and "invalid_state" in legacy.headers["location"]
        assert await count(h, "SELECT COUNT(*) FROM oauth_link_tickets") == 0
        # The migrated table issues bound tickets that work.
        assert (await connect(h, providers, "github", h.jwt(uid), "link-code-9")).get("linked") == "1"


async def test_connect_refuses_a_provider_account_used_elsewhere(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        other = (await exchange(h, (await sign_in(h, providers, "github"))["code"])).json()["user"]["id"]
        uid = await h.create_user("alice@company.example", verified=True)
        frag = await connect(h, providers, "github", h.jwt(uid), "link-code-2")
        assert frag == {"error": "identity_in_use", "provider": "github", "from": "settings"}
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", uid) == 0
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", other) == 1
        # Connecting the SAME GitHub account again is a no-op success.
        assert (await connect(h, providers, "github", h.jwt(other), "link-code-3")).get("linked") == "1"
        # A second GitHub account cannot be added next to a connected one.
        providers.github_user = {"id": 7, "login": "second"}
        frag = await connect(h, providers, "github", h.jwt(other), "link-code-4")
        assert frag == {"error": "identity_conflict", "provider": "github", "from": "settings"}
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", other) == 1


async def test_google_email_link_still_works_and_notifies(tmp_path, providers, notices) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        body = (await exchange(h, (await sign_in(h, providers, "google"))["code"])).json()
        assert body["user"]["id"] == uid
        assert notices == [("person@gmail.com", "Google", "person@gmail.com")]


async def test_link_notice_failure_never_breaks_sign_in(tmp_path, providers, monkeypatch) -> None:
    async def boom(*_: Any) -> None:
        raise RuntimeError("smtp down")

    monkeypatch.setattr(social, "link_notifier", boom)
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        body = (await exchange(h, (await sign_in(h, providers, "google"))["code"])).json()
        assert body["user"]["id"] == uid


# ---------------------------------------------------------------------------
# 4. One free account per verified email, both directions
# ---------------------------------------------------------------------------


async def verified_tenant(h: Any, email: str, user_id: str = "tenant_1") -> None:
    meter = UsageMeter(h.db)
    await meter.register_tenant(user_id, email=email)
    await meter.mark_tenant_signup(user_id)
    await meter.set_tenant_email_verified(user_id)


async def test_verified_api_tenant_blocks_a_second_account_via_social(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await verified_tenant(h, "person@gmail.com")
        assert await UsageMeter(h.db).email_has_verified_account("person@gmail.com", exclude_user_id="someone-else")
        frag = await sign_in(h, providers, "google")
        assert frag == {"error": "email_in_use", "provider": "google", "from": "login"}
        providers.github_emails = [{"email": "Person@Gmail.com", "primary": True, "verified": True}]
        assert (await sign_in(h, providers, "github", code="auth-code-2"))["error"] == "email_in_use"
        assert await count(h, "SELECT COUNT(*) FROM users") == 0


async def test_verified_api_tenant_blocks_dashboard_verification(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await verified_tenant(h, "person@gmail.com")
        uid = await h.create_user("person@gmail.com", verified=False)
        token = await security_state.create_email_verification(h.db, uid, "person@gmail.com")
        r = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": token}, headers=h.jwt(uid))
        assert r.status_code == 409 and "already verified on another" in r.json()["detail"]
        assert not (await h.db.get_user_by_id(uid))["email_verified"]
        # A wrong token is still a 400, not a hint about other accounts.
        bad = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": "x" * 32}, headers=h.jwt(uid))
        assert bad.status_code == 400

        # The reset still works (and clears pre-verification credentials) but does not verify.
        reset_token, _ = await h.users.create_password_reset_token("person@gmail.com")
        assert (await h.users.reset_password("person@gmail.com", reset_token, "N3w!Passw0rdX"))[0]
        assert not (await h.db.get_user_by_id(uid))["email_verified"]


async def test_dashboard_verification_still_works_without_a_conflict(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await verified_tenant(h, "someone-else@gmail.com")
        uid = await h.create_user("person@gmail.com", verified=False)
        raw_key, _ = await h.api_key(uid)
        token = await security_state.create_email_verification(h.db, uid, "person@gmail.com")
        r = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": token}, headers=h.jwt(uid))
        assert r.status_code == 200 and r.json()["email_verified"] is True
        # The signed-in owner confirming their own mailbox keeps their keys.
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": raw_key})).status_code == 200
        again = await h.client.post("/api/v1/auth/verify-email/confirm", json={"token": token}, headers=h.jwt(uid))
        assert again.status_code == 400  # single use


async def test_email_check_tolerates_deployments_without_tenant_tables(tmp_path) -> None:
    from remembra.auth.users import email_verified_on_another_account

    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await h.db.conn.execute("DROP TABLE cloud_tenants")
        await h.db.conn.commit()
        assert await email_verified_on_another_account(h.db, "a@b.example", exclude_user_id="x") is False
        uid = await h.create_user("a@b.example", verified=True)
        assert await email_verified_on_another_account(h.db, " A@B.example ", exclude_user_id="x") is True
        assert await email_verified_on_another_account(h.db, "a@b.example", exclude_user_id=uid) is False
        assert await email_verified_on_another_account(h.db, "", exclude_user_id="x") is False
