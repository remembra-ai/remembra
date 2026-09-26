"""Hardening of the one-time account review (review of the verified-provider-linking hotfix).

Real routers on SQLite with auth on (tests.security_harness), and the real
production app for the MCP connector (tests.connector_harness); only the
Google / GitHub HTTP edge is faked (tests.test_social_login.FakeProviders).

1. A Settings connect armed by a squatter before the owner proves the mailbox
   (Google sign-in or emailed reset) never lands, and review trust belongs to
   the exact provider account that proved the mailbox.
2. "Keep all" keeps exactly the list the owner was shown.
3. The squatter's sessions end at the Google link; disconnecting a sign-in
   method is gated during the review and always audited.
4. An owner address gets no owner plan while its review is open.
5. 2FA set up before verification is kept only with a current code.
6. Trust changes and trusted sign-ins are audited.
7. The review never disconnects app connections (MCP grants).
8. A review with nothing to list finishes by itself: no screen, no email.
9. GitHub (verified primary) signs in to a never-verified account under the same review.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
import pyotp
import pytest

from remembra.api.v1 import account_review as review_api
from remembra.api.v1 import auth, keys, social_auth, webhooks
from remembra.auth import account_review, social
from remembra.auth.users import UserManager
from remembra.cloud.metering import UsageMeter
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter
from remembra.connector.oauth import account_allows_grant
from remembra.cloud.plans import PlanTier
from remembra.security import state as security_state
from remembra.webhooks.manager import WebhookManager
from tests.connector_harness import connector_app, pkce
from tests.security_harness import JWT_SECRET, secure_app
from tests.test_social_login import FakeProviders, count, exchange, oauth_settings, query_of, sign_in
from tests.test_social_review_fixes import other_browser

ROUTERS = [auth.router, social_auth.router, review_api.router, keys.router, webhooks.router]
PASSWORD = "Str0ng!Passw0rd"


@pytest.fixture()
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProviders]:
    fake = FakeProviders()
    monkeypatch.setattr(social, "http_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    social.google_jwks.reset()
    set_cloud_rate_limiter(CloudRateLimiter())
    yield fake
    social.google_jwks.reset()
    set_cloud_rate_limiter(None)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook hosts resolve to a public address (no network in tests)."""
    from remembra.webhooks import manager as manager_mod

    async def fake_resolve(hostname: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(manager_mod, "_resolve", fake_resolve)


@pytest.fixture()
def mail(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    sent: dict[str, list[Any]] = {"linked": [], "review": []}

    async def linked(to: str, provider_name: str, provider_email: str) -> None:
        sent["linked"].append((to, provider_name, provider_email))

    async def review(to: str, kept: list[str], removed: list[str]) -> None:
        sent["review"].append((to, kept, removed))

    monkeypatch.setattr(social, "link_notifier", linked)
    monkeypatch.setattr(account_review, "review_notifier", review)
    return sent


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def claims_of(token: str) -> dict[str, Any]:
    return dict(jwt.decode(token, JWT_SECRET, algorithms=["HS256"]))


async def google_session(h: Any, fake: FakeProviders, email: str, code: str = "g-1", totp: str | None = None) -> dict[str, Any]:
    fake.google_claims = {"email": email}
    frag = await sign_in(h, fake, "google", code=code)
    assert "code" in frag, frag
    r = await exchange(h, frag["code"], totp)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def github_login(client: httpx.AsyncClient, fake: FakeProviders, code: str) -> dict[str, str]:
    """GitHub sign-in in the browser ``client`` (start -> consent -> callback). Returns the fragment."""
    s = await client.get("/api/v1/auth/oauth/github/start", params={"from": "login"})
    assert s.status_code == 302, s.text
    fake.authorize(s.headers["location"], code)
    r = await client.get("/api/v1/auth/oauth/github/callback", params={"code": code, "state": query_of(s)["state"]})
    return dict(parse_qsl(urlsplit(r.headers["location"]).fragment))


async def review_of(h: Any, token: str) -> dict[str, Any]:
    r = await h.client.get("/api/v1/auth/review", headers=bearer(token))
    assert r.status_code == 200, r.text
    return dict(r.json())


async def keep_all(h: Any, token: str, version: str | None = None) -> httpx.Response:
    if version is None:
        version = (await review_of(h, token))["version"]
    return await h.client.post("/api/v1/auth/review/complete", headers=bearer(token), json={"version": version})


async def audit_rows(h: Any, uid: str, action: str) -> list[dict[str, Any]]:
    cursor = await h.db.conn.execute(
        "SELECT error_message FROM audit_log WHERE user_id = ? AND action = ? ORDER BY timestamp", (uid, action)
    )
    return [json.loads(row[0] or "{}") for row in await cursor.fetchall()]


async def with_totp(h: Any, uid: str) -> pyotp.TOTP:
    secret, _, _ = await h.users.setup_totp(uid)
    totp = pyotp.TOTP(secret)
    assert (await h.users.enable_totp(uid, totp.now()))[0]
    return totp


def next_code(totp: pyotp.TOTP) -> str:
    """A code the server still accepts (it allows one step either side) that was not used yet."""
    return str(totp.at(time.time() + 30))


async def arm_github_connect(browser: httpx.AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    """Squatter: ask for a connect ticket and open /start (but do not finish the provider consent yet)."""
    r = await browser.post("/api/v1/auth/oauth/github/link", headers=headers)
    assert r.status_code == 200, r.text
    s = await browser.get(r.json()["start_path"])
    assert s.status_code == 302, s.text
    return s.headers["location"], query_of(s)["state"]


# ---------------------------------------------------------------------------
# 1. A connect armed before the mailbox was proven never lands
# ---------------------------------------------------------------------------


async def test_github_connect_armed_before_the_google_sign_in_never_lands(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        email = "victim@gmail.com"
        uid = await h.create_user(email, verified=False)
        squatter_totp = await with_totp(h, uid)
        async with other_browser(h) as sq:
            authorize_at, state = await arm_github_connect(sq, h.jwt(uid, email))
            assert await count(h, "SELECT COUNT(*) FROM oauth_login_states WHERE link_user_id = ?", uid) == 1

            # The owner signs in with Google: the review opens and the armed connect is cancelled.
            owner = (await google_session(h, providers, email))["access_token"]
            assert claims_of(owner)["rvw"]
            assert await count(h, "SELECT COUNT(*) FROM oauth_login_states WHERE link_user_id = ?", uid) == 0

            # The squatter finishes the held GitHub consent: refused, nothing linked.
            providers.github_user = {"id": 777, "login": "squatter"}
            providers.github_emails = [{"email": "sq@evil.example", "primary": True, "verified": True}]
            providers.authorize(authorize_at, "gh-1")
            r = await sq.get("/api/v1/auth/oauth/github/callback", params={"code": "gh-1", "state": state})
            assert dict(parse_qsl(urlsplit(r.headers["location"]).fragment))["error"] == "invalid_state"
            assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ? AND provider = 'github'", uid) == 0

            # A GitHub sign-in by the squatter is a separate, new account: no way into this one.
            frag = await github_login(sq, providers, "gh-2")
            ex = (await sq.post("/api/v1/auth/oauth/exchange", json={"code": frag["code"]})).json()
            assert (
                ex["user"]["id"] != uid
                and ex["user"]["email"] == "sq@evil.example"
                and "rvw" not in claims_of(ex["access_token"])
            )

        # The owner keeps the list; the squatter's 2FA does not lock them out afterwards.
        assert (await keep_all(h, owner)).status_code == 200
        again = await google_session(h, providers, email, code="g-2")
        assert again["requires_2fa"] is False and again["user"]["id"] == uid
        assert squatter_totp  # (their authenticator no longer matters)


async def test_github_connect_armed_before_an_emailed_reset_never_lands(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        email = "victim@gmail.com"
        uid = await h.create_user(email, verified=False)
        await with_totp(h, uid)
        await h.api_key(uid)
        async with other_browser(h) as sq:
            sq_session = h.jwt(uid, email)
            authorize_at, state = await arm_github_connect(sq, sq_session)
            token, _ = await h.users.create_password_reset_token(email)
            assert (await h.users.reset_password(email, token, "N3w!Passw0rd-owner"))[0]
            assert (await sq.get("/api/v1/auth/me", headers=sq_session)).status_code == 401

            providers.github_user = {"id": 777, "login": "squatter"}
            providers.github_emails = [{"email": "sq@evil.example", "primary": True, "verified": True}]
            providers.authorize(authorize_at, "gh-1")
            r = await sq.get("/api/v1/auth/oauth/github/callback", params={"code": "gh-1", "state": state})
            assert "error=invalid_state" in r.headers["location"]
            assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", uid) == 0
        # Only the owner's reset password carries review authority.
        r = await h.client.post("/api/v1/auth/login", json={"email": email, "password": "N3w!Passw0rd-owner"})
        assert r.status_code == 200 and claims_of(r.json()["access_token"])["rvw"]


async def test_the_callback_rechecks_the_session_and_the_review(tmp_path, providers, mail) -> None:
    """Even a flow row that survived is refused unless its session still counts and proved the mailbox."""
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        await h.api_key(uid)
        owner = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        review = await account_review.pending_review(h.db, uid)
        assert review is not None
        identity = social.ProviderIdentity(provider="github", subject="31", email="me@example.org", name=None)
        now_ms = int(time.time() * 1000) + 1

        def flow(review_id: str | None, session_ms: int | None) -> social.LoginState:
            return social.LoginState(
                provider="github",
                code_verifier="v",
                nonce="n",
                from_page="settings",
                link_user_id=uid,
                link_review_id=review_id,
                link_session_ms=session_ms,
            )

        for bad in (flow(None, now_ms), flow("another-review", now_ms), flow(review.review_id, None), flow(review.review_id, 1)):
            with pytest.raises(social.SocialLoginError) as refused:
                await social.link_identity(h.db, identity, bad)
            assert refused.value.code == "invalid_state"
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE provider = 'github'") == 0

        # The owner's own connect (their session, their review) lands, and that exact account is trusted.
        await social.link_identity(h.db, identity, flow(review.review_id, now_ms))
        refreshed = await account_review.get_review(h.db, uid)
        assert refreshed is not None and "github:31" in refreshed.trusted_identities
        assert (await review_of(h, owner))["items"]["identities"] == []
        claims, _ = await account_review.login_claims(h.db, uid, provider="github", subject="31")
        assert claims == {"rvw": review.review_id}
        # Another account at the same provider proves nothing.
        assert (await account_review.login_claims(h.db, uid, provider="github", subject="32")) == ({}, False)
        assert (await account_review.login_claims(h.db, uid, provider="github", subject=None)) == ({}, False)
        updated = await audit_rows(h, uid, "account_review_updated")
        assert updated == [{"change": "identity_trusted", "provider": "github", "subject": "31"}]
        linked = await audit_rows(h, uid, "identity_linked")
        assert {"provider": "github", "subject": "31", "email": "me@example.org", "by": "settings"} in linked


# ---------------------------------------------------------------------------
# 2. "Keep all" keeps exactly what was shown
# ---------------------------------------------------------------------------


async def test_keep_all_refuses_a_list_that_changed_since_it_was_shown(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await WebhookManager(h.db).init_schema()
        h.app.state.webhook_manager = WebhookManager(h.db)
        uid = await h.create_user("victim@gmail.com", verified=False)
        admin_key, _ = await h.api_key(uid, "admin")
        owner = (await google_session(h, providers, "victim@gmail.com"))["access_token"]
        first = await review_of(h, owner)
        assert len(first["items"]["keys"]) == 1 and first["version"]
        # Using a key does not change the list (last-used times are not part of it).
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": admin_key})).status_code == 200
        assert (await review_of(h, owner))["version"] == first["version"]

        # Meanwhile a key the squatter holds makes another key (keys keep working during the review).
        made = await h.client.post("/api/v1/keys", headers={"X-API-Key": admin_key}, json={"name": "backup"})
        assert made.status_code == 201, made.text
        stale = await keep_all(h, owner, first["version"])
        assert stale.status_code == 409 and stale.json()["detail"] == "The list changed. Check it again."
        assert await account_review.is_pending(h.db, uid) and mail["review"] == []

        second = await review_of(h, owner)
        assert {k["name"] for k in second["items"]["keys"]} == {"admin-key", "backup"}
        # A webhook pointed elsewhere is a different item too.
        hook = await h.client.post(
            "/api/v1/webhooks",
            headers={"X-API-Key": admin_key},
            json={"url": "https://hooks.example.com/a", "events": ["memory.stored"]},
        )
        assert hook.status_code == 201, hook.text
        third = await review_of(h, owner)
        await h.db.conn.execute("UPDATE webhooks SET url = 'https://evil.example/b' WHERE user_id = ?", (uid,))
        await h.db.conn.commit()
        assert (await keep_all(h, owner, third["version"])).status_code == 409

        done = await keep_all(h, owner)
        assert done.status_code == 200, done.text
        assert "API key 'backup' (editor, all projects)" in done.json()["kept"]
        assert "Webhook to evil.example" in done.json()["kept"]

        # Once the check is done nothing is gated any more.
        assert (
            await h.client.post("/api/v1/keys", headers=h.jwt(uid, "victim@gmail.com"), json={"name": "after"})
        ).status_code == 201


async def test_untrusted_sessions_add_no_keys_or_webhooks_during_the_review(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await WebhookManager(h.db).init_schema()
        h.app.state.webhook_manager = WebhookManager(h.db)
        uid = await h.create_user("victim@gmail.com", verified=False)
        await h.api_key(uid)
        owner = (await google_session(h, providers, "victim@gmail.com"))["access_token"]
        squatter = h.jwt(uid, "victim@gmail.com")  # a fresh password sign-in: not trusted
        blocked = "Sign in with Google and finish checking your account first."
        r = await h.client.post("/api/v1/keys", headers=squatter, json={"name": "backup"})
        assert r.status_code == 403 and r.json()["detail"] == blocked
        body = {"url": "https://hooks.example.com/a", "events": ["memory.stored"]}
        r = await h.client.post("/api/v1/webhooks", headers=squatter, json=body)
        assert r.status_code == 403 and r.json()["detail"] == blocked
        # The owner's session may.
        assert (await h.client.post("/api/v1/keys", headers=bearer(owner), json={"name": "mine"})).status_code == 201
        assert (await h.client.post("/api/v1/webhooks", headers=bearer(owner), json=body)).status_code == 201


# ---------------------------------------------------------------------------
# 3. Sessions end at the link; disconnecting is gated and audited
# ---------------------------------------------------------------------------


async def test_sessions_from_before_the_link_end_and_disconnects_are_gated_and_audited(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("victim@gmail.com", verified=False)
        raw_key, _ = await h.api_key(uid)
        before = h.jwt(uid, "victim@gmail.com")
        owner = (await google_session(h, providers, "victim@gmail.com"))["access_token"]
        assert (await h.client.get("/api/v1/auth/me", headers=before)).status_code == 401
        assert (await h.client.get("/api/v1/auth/me", headers=bearer(owner))).status_code == 200
        # Agents are not disconnected.
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": raw_key})).status_code == 200

        squatter = h.jwt(uid, "victim@gmail.com")
        r = await h.client.delete("/api/v1/auth/identities/google", headers=squatter)
        assert r.status_code == 403
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", uid) == 1

        # The owner may, and it is on the record (with the provider account).
        r = await h.client.delete("/api/v1/auth/identities/google", headers=bearer(owner))
        assert r.status_code == 200 and r.json() == {"removed": True}
        assert await audit_rows(h, uid, "identity_unlinked") == [
            {"provider": "google", "subject": "google-sub-1", "email": "victim@gmail.com", "by": "settings"}
        ]
        refreshed = await account_review.get_review(h.db, uid)
        assert refreshed is not None and refreshed.trusted_identities == ()


async def test_disconnect_is_audited_outside_a_review_too(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        await google_session(h, providers, "person@gmail.com")
        assert await audit_rows(h, uid, "identity_linked") == [
            {"provider": "google", "subject": "google-sub-1", "email": "person@gmail.com", "by": "email_match"}
        ]
        r = await h.client.delete("/api/v1/auth/identities/google", headers=h.jwt(uid, "person@gmail.com"))
        assert r.status_code == 200
        assert [row["provider"] for row in await audit_rows(h, uid, "identity_unlinked")] == ["google"]


# ---------------------------------------------------------------------------
# 4. Owner plan
# ---------------------------------------------------------------------------


async def test_owner_address_gets_no_owner_plan_until_the_review_is_done(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings(owner_emails=["boss@gmail.com"])) as h:
        uid = await h.create_user("boss@gmail.com", verified=False)
        await h.api_key(uid, "admin")
        meter = UsageMeter(h.db)
        assert await meter.get_tenant_plan(uid) == PlanTier.FREE  # unverified (unchanged)
        owner = (await google_session(h, providers, "boss@gmail.com"))["access_token"]
        assert await meter.get_tenant_plan(uid) == PlanTier.FREE  # verified, but the review is open
        assert (await keep_all(h, owner)).status_code == 200
        assert await meter.get_tenant_plan(uid) == PlanTier.ENTERPRISE


# ---------------------------------------------------------------------------
# 5. 2FA from before verification
# ---------------------------------------------------------------------------


async def test_keep_all_turns_off_2fa_from_before_unless_kept_with_a_code(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("victim@gmail.com", verified=False)
        await with_totp(h, uid)  # the squatter's authenticator
        owner = (await google_session(h, providers, "victim@gmail.com"))["access_token"]
        assert (await review_of(h, owner))["items"]["two_factor"] is True
        done = await keep_all(h, owner)
        assert done.status_code == 200, done.text
        assert done.json() == {"done": True, "kept": ["Password"], "removed": ["Two-factor sign-in"]}
        assert not (await h.db.get_user_by_id(uid))["totp_enabled"]
        assert mail["review"] == [("victim@gmail.com", ["Password"], ["Two-factor sign-in"])]
        # The owner is not locked out by an authenticator they never had.
        again = await google_session(h, providers, "victim@gmail.com", code="g-2")
        assert again["requires_2fa"] is False
        r = await h.client.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": PASSWORD})
        assert r.status_code == 200 and r.json()["access_token"]


async def test_2fa_from_before_is_kept_only_with_a_current_code(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        totp = await with_totp(h, uid)  # a real legacy owner's own authenticator
        owner = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        keep = "/api/v1/auth/review/keep"
        wrong = await h.client.post(keep, headers=bearer(owner), json={"kind": "two_factor", "code": "000000"})
        assert wrong.status_code == 400 and wrong.json()["detail"] == "That code did not work. Try the current one."
        ok = await h.client.post(keep, headers=bearer(owner), json={"kind": "two_factor", "code": next_code(totp)})
        assert ok.status_code == 200, ok.text
        assert ok.json()["kept"] == "Two-factor sign-in" and ok.json()["review"]["items"]["two_factor"] is False
        again = await h.client.post(keep, headers=bearer(owner), json={"kind": "two_factor", "code": next_code(totp)})
        assert again.status_code in (400, 404)  # not listed any more (or the step was used)

        done = (await keep_all(h, owner)).json()
        assert done["kept"] == ["Password", "Two-factor sign-in"] and done["removed"] == []
        assert (await h.db.get_user_by_id(uid))["totp_enabled"]
        assert await audit_rows(h, uid, "account_review_kept") == [
            {"kind": "two_factor", "label": "Two-factor sign-in", "by": "dashboard_session", "proof": "current_code"}
        ]
        # Kept means it applies again.
        providers.google_claims = {"email": "legacy@gmail.com"}
        frag = await sign_in(h, providers, "google", code="g-2")
        assert (await exchange(h, frag["code"])).json()["requires_2fa"] is True


# ---------------------------------------------------------------------------
# 6. Audit trail
# ---------------------------------------------------------------------------


async def test_trusted_sign_ins_and_trust_changes_are_audited(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        await h.api_key(uid)
        owner = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        setup = await h.client.post("/api/v1/auth/2fa/setup", headers=bearer(owner))
        assert setup.status_code == 200, setup.text
        code = pyotp.TOTP(setup.json()["secret"]).now()
        assert (await h.client.post("/api/v1/auth/2fa/enable", headers=bearer(owner), json={"code": code})).status_code == 200
        token, _ = await h.users.create_password_reset_token("legacy@gmail.com")
        assert (await h.users.reset_password("legacy@gmail.com", token, "N3w!Passw0rdX"))[0]
        # A password sign-in with the reset password (the owner's 2FA applies to it).
        r = await h.client.post(
            "/api/v1/auth/login",
            json={
                "email": "legacy@gmail.com",
                "password": "N3w!Passw0rdX",
                "totp_code": next_code(pyotp.TOTP(setup.json()["secret"])),
            },
        )
        assert r.status_code == 200 and claims_of(r.json()["access_token"])["rvw"], r.text

        assert await audit_rows(h, uid, "account_review_session") == [
            {"method": "google", "subject": "google-sub-1"},
            {"method": "password_reset"},
        ]
        assert await audit_rows(h, uid, "account_review_updated") == [
            {"change": "two_factor_trusted", "by": "two_factor_setup"},
            {"change": "password_trusted", "by": "password_reset"},
        ]
        opened = await audit_rows(h, uid, "account_review_opened")
        assert opened[0]["origin"] == "google" and opened[0]["trusted"] == ["google:google-sub-1"]
        # An untrusted password sign-in is not recorded as review authority.
        other = await h.create_user("other@gmail.com", verified=True)
        await h.client.post("/api/v1/auth/login", json={"email": "other@gmail.com", "password": PASSWORD})
        assert await audit_rows(h, other, "account_review_session") == []


# ---------------------------------------------------------------------------
# 7. App connections are never cut by the review
# ---------------------------------------------------------------------------


async def _grant_of(c: Any, uid: str) -> Any:
    store = c.app.state.connector_store
    grant = (await store.list_grants(uid))[0]
    found = await store._grant_row(grant["connection_id"])
    assert found is not None
    return found[0]


async def _mcp_ok(c: Any, token: str) -> bool:
    resp = await c.mcp_post(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return resp.status_code == 200


async def test_reset_and_review_revokes_keep_app_connections_working(tmp_path, mail) -> None:
    async with connector_app(tmp_path) as c:
        uid = await c.create_user("legacy@gmail.com")
        assert not (await c.db.get_user_by_id(uid))["email_verified"]
        conn = await c.connect("legacy@gmail.com", ["alpha"])
        assert await _mcp_ok(c, conn.access_token)

        # "Forgot password" on the never-verified account: the review opens, Claude keeps working.
        users = UserManager(c.db, c.settings.jwt_secret)
        token, _ = await users.create_password_reset_token("legacy@gmail.com")
        assert (await users.reset_password("legacy@gmail.com", token, "N3w!Passw0rdX"))[0]
        assert await account_allows_grant(c.db, await _grant_of(c, uid))
        assert await _mcp_ok(c, conn.access_token)

        r = await c.http.post("/api/v1/auth/login", json={"email": "legacy@gmail.com", "password": "N3w!Passw0rdX"})
        assert r.status_code == 200, r.text
        owner = r.json()["access_token"]
        review = (await c.http.get("/api/v1/auth/review", headers=bearer(owner))).json()
        assert [x["name"] for x in review["items"]["connections"]] == ["Claude"]

        # Removing a sign-in method signs out dashboard sessions only.
        await security_state.invalidate_user_sessions(c.db, uid, keep_app_connections=True)
        assert (await c.http.get("/api/v1/auth/me", headers=bearer(owner))).status_code == 401
        assert await _mcp_ok(c, conn.access_token)
        owner = (await c.http.post("/api/v1/auth/login", json={"email": "legacy@gmail.com", "password": "N3w!Passw0rdX"})).json()[
            "access_token"
        ]
        done = await c.http.post(
            "/api/v1/auth/review/complete",
            headers=bearer(owner),
            json={"version": (await c.http.get("/api/v1/auth/review", headers=bearer(owner))).json()["version"]},
        )
        assert done.status_code == 200 and done.json()["kept"] == ["App connection 'Claude'"], done.text
        assert await account_allows_grant(c.db, await _grant_of(c, uid))
        assert await _mcp_ok(c, conn.access_token)

        # A reset of a VERIFIED account still ends app connections (its old password may be known).
        token, _ = await users.create_password_reset_token("legacy@gmail.com")
        assert (await users.reset_password("legacy@gmail.com", token, "An0ther!Passw0rd"))[0]
        assert not await account_allows_grant(c.db, await _grant_of(c, uid))
        assert not await _mcp_ok(c, conn.access_token)


async def test_review_password_removal_keeps_app_connections_working(tmp_path, providers, mail) -> None:
    async with connector_app(tmp_path) as c:
        uid = await c.create_user("legacy@gmail.com")
        conn = await c.connect("legacy@gmail.com", ["alpha"])
        # A Google review (the only way in during it is the Google session).
        review = await account_review.open_review(
            c.db,
            uid,
            origin=account_review.ORIGIN_GOOGLE,
            verified_at=account_review.now_iso(),
            totp_enabled=False,
            proof_identity=("google", "g-1"),
        )
        owner = UserManager(c.db, c.settings.jwt_secret).create_jwt_token(
            uid, "legacy@gmail.com", extra_claims={"rvw": review.review_id}
        )
        r = await c.http.post("/api/v1/auth/review/revoke", headers=bearer(owner), json={"kind": "password"})
        assert r.status_code == 200 and r.json()["access_token"], r.text
        assert [x["name"] for x in r.json()["review"]["items"]["connections"]] == ["Claude"]
        assert await account_allows_grant(c.db, await _grant_of(c, uid))
        assert await _mcp_ok(c, conn.access_token)

        # While the check is open, the (untrusted) password cannot connect another app.
        await c.db.update_user_password(uid, UserManager.hash_password(PASSWORD))
        client_id = (await c.register()).json()["client_id"]
        _verifier, challenge = pkce()
        rid = c.request_id(await c.authorize(client_id, challenge))
        page = await c.login(rid, "legacy@gmail.com")
        assert page.status_code == 403 and "finish checking your account first" in page.text


# ---------------------------------------------------------------------------
# 8. Nothing to check: no screen, no email
# ---------------------------------------------------------------------------


async def test_reset_with_nothing_to_check_finishes_silently(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacynokeys@gmail.com", verified=False)
        token, _ = await h.users.create_password_reset_token("legacynokeys@gmail.com")
        assert (await h.users.reset_password("legacynokeys@gmail.com", token, "N3w!Passw0rdX"))[0]
        assert (await h.db.get_user_by_id(uid))["email_verified"]
        assert not await account_review.is_pending(h.db, uid)
        r = await h.client.post("/api/v1/auth/login", json={"email": "legacynokeys@gmail.com", "password": "N3w!Passw0rdX"})
        session = r.json()["access_token"]
        assert "rvw" not in claims_of(session)
        assert (await review_of(h, session))["pending"] is False
        assert mail["review"] == []
        completed = await audit_rows(h, uid, "account_review_completed")
        assert completed == [{"kept": [], "removed": [], "by": "auto_nothing_to_check"}]


async def test_a_review_emptied_by_revokes_finishes_and_says_what_was_removed(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        owner = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        review = await review_of(h, owner)
        assert review["pending"] is True and review["items"]["password"] is True  # the only item
        r = await h.client.post("/api/v1/auth/review/revoke", headers=bearer(owner), json={"kind": "password"})
        assert r.status_code == 200 and r.json()["review"]["pending"] is False
        assert not await account_review.is_pending(h.db, uid)
        assert mail["review"] == [("legacy@gmail.com", [], ["Password"])]


# ---------------------------------------------------------------------------
# 9. GitHub into a never-verified account
# ---------------------------------------------------------------------------


async def test_github_verified_primary_signs_in_to_a_legacy_account_under_review(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@example.org", verified=False)
        raw_key, key_id = await h.api_key(uid)
        providers.github_user = {"id": 4242, "login": "legacy", "name": "Legacy"}
        providers.github_emails = [{"email": "legacy@example.org", "primary": True, "verified": True}]
        frag = await github_login(h.client, providers, "gh-1")
        body = (await exchange(h, frag["code"])).json()
        assert body["user"]["id"] == uid and body["user"]["email_verified"] is True and body["requires_2fa"] is False
        token = body["access_token"]
        assert claims_of(token)["rvw"]
        assert mail["linked"] == [("legacy@example.org", "GitHub", "legacy@example.org")]
        review = await review_of(h, token)
        assert review["origin"] == "github" and review["items"]["identities"] == []
        assert [k["id"] for k in review["items"]["keys"]] == [key_id] and review["items"]["password"] is True
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": raw_key})).status_code == 200
        # A password session is not the proof here.
        mine = await review_of(h, h.jwt(uid, "legacy@example.org")["Authorization"][7:])
        assert mine["can_review"] is False and mine["message"] == "Sign in with GitHub and finish checking your account first."
        assert (await keep_all(h, token)).status_code == 200
