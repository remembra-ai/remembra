"""Verified-provider linking into unverified accounts, and the one-time account review.

Real routers on SQLite with auth on (tests.security_harness); only the
Google / GitHub HTTP edge is faked (tests.test_social_login.FakeProviders).

* A legacy account (email never verified) + Sign in with Google: linked,
  verified, signed in, a review opens, every API key keeps working.
* An unverified provider email is never linked.
* Squatter: someone registered the victim's address with a password, made
  keys, 2FA, a webhook, an app connection and linked their own GitHub. The
  victim's Google sign-in is not stopped by the squatter's 2FA, the review
  lists all of it, only the victim's session can act on it, and each revoke
  kills that credential (password and sign-in links sign out every session).
* Forgot password on an unverified account revokes nothing; the reset
  verifies the email and the next password sign-in opens the review.
* Verified accounts never get a review; the link-CSRF protections hold.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest

from remembra.api.v1 import account_review as review_api
from remembra.api.v1 import auth, keys, social_auth
from remembra.auth import account_review, social
from remembra.auth.middleware import AuthenticatedUser
from remembra.auth.superadmin import is_superadmin
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter
from tests.security_harness import JWT_SECRET, secure_app
from tests.test_social_login import FakeProviders, count, exchange, oauth_settings, sign_in
from tests.test_social_review_fixes import _squatter_credentials, connect, other_browser, verified_tenant

ROUTERS = [auth.router, social_auth.router, review_api.router, keys.router]
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


@pytest.fixture()
def mail(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Capture the link notice and the review notice instead of emailing."""
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


async def google_session(h: Any, fake: FakeProviders, email: str, code: str = "auth-code-1") -> dict[str, Any]:
    fake.google_claims = {"email": email}
    frag = await sign_in(h, fake, "google", code=code)
    assert "code" in frag, frag
    r = await exchange(h, frag["code"])
    assert r.status_code == 200, r.text
    return r.json()


async def key_works(h: Any, raw_key: str) -> bool:
    r = await h.client.get("/api/v1/keys", headers={"X-API-Key": raw_key})
    assert r.status_code in (200, 401), r.text
    return r.status_code == 200


async def audit_actions(h: Any, uid: str, *, sessions: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """The account-review audit rows (without the per-sign-in ``account_review_session`` rows unless asked)."""
    cursor = await h.db.conn.execute(
        "SELECT action, error_message FROM audit_log WHERE user_id = ? AND action LIKE 'account_review_%' ORDER BY timestamp",
        (uid,),
    )
    rows = [(row[0], json.loads(row[1] or "{}")) for row in await cursor.fetchall()]
    return [r for r in rows if sessions or r[0] != "account_review_session"]


async def shown(h: Any, token: str) -> dict[str, Any]:
    r = await h.client.get("/api/v1/auth/review", headers=bearer(token))
    assert r.status_code == 200, r.text
    return dict(r.json())


async def keep_all(h: Any, token: str) -> httpx.Response:
    """What the dashboard does: load the list, then "Keep all" with the version it showed."""
    review = await shown(h, token)
    return await h.client.post("/api/v1/auth/review/complete", headers=bearer(token), json={"version": review["version"]})


# ---------------------------------------------------------------------------
# Legacy account + Google
# ---------------------------------------------------------------------------


async def test_legacy_unverified_account_google_links_verifies_and_keeps_keys_working(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        raw_key, key_id = await h.api_key(uid)
        assert await key_works(h, raw_key)

        body = await google_session(h, providers, "legacy@gmail.com")
        assert body["user"]["id"] == uid and body["new_account"] is False
        assert body["user"]["email_verified"] is True
        assert (await h.db.get_user_by_id(uid))["email_verified"]
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ? AND provider = 'google'", uid) == 1
        assert mail["linked"] == [("legacy@gmail.com", "Google", "legacy@gmail.com")]
        # Agents are not disconnected.
        assert await key_works(h, raw_key)

        token = body["access_token"]
        assert jwt.decode(token, JWT_SECRET, algorithms=["HS256"])["rvw"]
        review = (await h.client.get("/api/v1/auth/review", headers=bearer(token))).json()
        assert review["pending"] is True and review["can_review"] is True and review["origin"] == "google"
        items = review["items"]
        assert [k["id"] for k in items["keys"]] == [key_id]
        assert items["keys"][0]["role"] == "editor" and items["keys"][0]["before_verification"] is True
        assert items["password"] is True and items["two_factor"] is False and items["identities"] == []

        # One click keeps everything and finishes the check.
        done = await h.client.post("/api/v1/auth/review/complete", headers=bearer(token), json={"version": review["version"]})
        assert done.status_code == 200, done.text
        assert done.json()["kept"] == ["API key 'editor-key' (editor, all projects)", "Password"]
        assert done.json()["removed"] == []
        assert await key_works(h, raw_key)
        assert (await h.client.get("/api/v1/auth/review", headers=bearer(token))).json() == {
            "pending": False,
            "can_review": False,
            "origin": None,
            "verified_at": None,
            "message": None,
            "items": None,
            "version": None,
        }
        again_done = await h.client.post("/api/v1/auth/review/complete", headers=bearer(token), json={"version": "x"})
        assert again_done.status_code == 409
        assert mail["review"] == [("legacy@gmail.com", done.json()["kept"], [])]
        actions = await audit_actions(h, uid, sessions=True)
        assert [a for a, _ in actions] == ["account_review_opened", "account_review_session", "account_review_completed"]
        assert actions[1][1] == {"method": "google", "subject": "google-sub-1"}
        assert actions[2][1]["kept"] == done.json()["kept"]

        # Next Google sign-in: a normal verified account, no claim, no review.
        again = await google_session(h, providers, "legacy@gmail.com", code="auth-code-2")
        assert "rvw" not in jwt.decode(again["access_token"], JWT_SECRET, algorithms=["HS256"])
        # The password still signs in too.
        r = await h.client.post("/api/v1/auth/login", json={"email": "legacy@gmail.com", "password": PASSWORD})
        assert r.status_code == 200 and r.json()["access_token"]


@pytest.mark.parametrize("claims", [{"email_verified": False}, {"email_verified": "false"}, {"email_verified": None}])
async def test_unverified_provider_email_is_never_linked(tmp_path, providers, mail, claims) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        providers.google_claims = {"email": "legacy@gmail.com", **claims}
        frag = await sign_in(h, providers, "google")
        assert frag == {"error": "email_unverified", "provider": "google", "from": "login"}
        assert not (await h.db.get_user_by_id(uid))["email_verified"]
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0
        assert await count(h, "SELECT COUNT(*) FROM account_reviews") == 0
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_codes") == 0
        assert mail["linked"] == []


async def test_google_email_verified_on_another_account_is_not_linked(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        await verified_tenant(h, "legacy@gmail.com")  # an API signup verified this address first
        providers.google_claims = {"email": "legacy@gmail.com"}
        frag = await sign_in(h, providers, "google")
        assert frag == {"error": "email_in_use", "provider": "google", "from": "login"}
        assert not (await h.db.get_user_by_id(uid))["email_verified"]
        assert await count(h, "SELECT COUNT(*) FROM account_reviews") == 0


# ---------------------------------------------------------------------------
# Squatter
# ---------------------------------------------------------------------------


async def _squatted_account(h: Any, fake: FakeProviders) -> dict[str, Any]:
    """The squatter registers victim@gmail.com with a password and sets up everything."""
    uid = await h.create_user("victim@gmail.com", password=PASSWORD, verified=False)
    creds = await _squatter_credentials(h, uid)  # key, 2FA, webhook, app connection
    second_key, second_id = await h.api_key(uid, "admin", project_ids=["proj-a"])
    squatter_jwt = h.jwt(uid, "victim@gmail.com")
    # The squatter also connects their own GitHub from Settings.
    fake.github_user = {"id": 777, "login": "squatter", "name": "Squatter"}
    fake.github_emails = [{"email": "squatter@example.org", "primary": True, "verified": True}]
    assert (await connect(h, fake, "github", squatter_jwt, "gh-link-1")) == {
        "linked": "1",
        "provider": "github",
        "from": "settings",
    }
    return {"uid": uid, "key": creds["key"], "admin_key": second_key, "admin_key_id": second_id, "jwt": squatter_jwt}


async def test_squatter_credentials_are_listed_and_die_when_revoked(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        sq = await _squatted_account(h, providers)
        uid = sq["uid"]

        # The victim signs in with Google. The squatter's 2FA does not lock them out.
        body = await google_session(h, providers, "victim@gmail.com")
        assert body["user"]["id"] == uid and body["requires_2fa"] is False
        victim = body["access_token"]
        # Until reviewed, everything still works (a real legacy owner's agents stay connected).
        assert await key_works(h, sq["key"]) and await key_works(h, sq["admin_key"])

        review = (await h.client.get("/api/v1/auth/review", headers=bearer(victim))).json()
        items = review["items"]
        assert review["can_review"] is True
        assert {k["id"] for k in items["keys"]} == {k.id for k in await h.keys.list_keys(uid)}
        admin = next(k for k in items["keys"] if k["id"] == sq["admin_key_id"])
        assert admin["role"] == "admin" and admin["project_ids"] == ["proj-a"] and admin["last_used_at"]
        assert [c["id"] for c in items["connections"]] == ["g1"]
        assert [w["url"] for w in items["webhooks"]] == ["https://attacker.example/hook"]
        assert [(i["provider"], i["email"]) for i in items["identities"]] == [("github", "squatter@example.org")]
        assert items["two_factor"] is True and items["password"] is True

        # The squatter's open dashboard session ended with the Google link.
        assert (await h.client.get("/api/v1/auth/me", headers=sq["jwt"])).status_code == 401
        # Signed in again (their password and their own 2FA still work), it sees
        # nothing and can do nothing to the review.
        sq_again = h.jwt(uid, "victim@gmail.com")
        mine = (await h.client.get("/api/v1/auth/review", headers=sq_again)).json()
        assert mine["pending"] is True and mine["can_review"] is False and mine["items"] is None
        assert mine["message"] == "Sign in with Google and finish checking your account first."
        for path, payload in (
            ("/api/v1/auth/review/complete", {"version": review["version"]}),
            ("/api/v1/auth/review/defer", None),
            ("/api/v1/auth/review/revoke", {"kind": "password"}),
            ("/api/v1/auth/review/keep", {"kind": "two_factor", "code": "123456"}),
        ):
            r = await h.client.post(path, headers=sq_again, json=payload)
            assert r.status_code == 403, (path, r.text)
        # ... nor add (or remove) a way in while the check is open.
        assert (await h.client.post("/api/v1/auth/2fa/setup", headers=sq_again)).status_code == 403
        assert (await h.client.post("/api/v1/auth/oauth/google/link", headers=sq_again)).status_code == 403
        assert (await h.client.delete("/api/v1/auth/identities/google", headers=sq_again)).status_code == 403
        assert (await h.client.post("/api/v1/keys", headers=sq_again, json={"name": "backup"})).status_code == 403
        # An API key cannot act on it either (dashboard sessions only).
        assert (await h.client.get("/api/v1/auth/review", headers={"X-API-Key": sq["key"]})).status_code == 401
        # The squatter's password sign-in is not trusted: no claim, and their own 2FA still applies.
        r = await h.client.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": PASSWORD})
        assert r.status_code == 200 and r.json()["requires_2fa"] is True and r.json()["access_token"] is None

        async def revoke(kind: str, item_id: str | None = None) -> dict[str, Any]:
            nonlocal victim
            r = await h.client.post("/api/v1/auth/review/revoke", headers=bearer(victim), json={"kind": kind, "id": item_id})
            assert r.status_code == 200, r.text
            data = r.json()
            if data["access_token"]:
                victim = data["access_token"]
            return data

        # Keys.
        await revoke("key", sq["admin_key_id"])
        assert not await key_works(h, sq["admin_key"]) and await key_works(h, sq["key"])
        other = next(k["id"] for k in items["keys"] if k["id"] != sq["admin_key_id"])
        await revoke("key", other)
        assert not await key_works(h, sq["key"])
        # A key id that is not on the account (or already revoked) is refused.
        r = await h.client.post("/api/v1/auth/review/revoke", headers=bearer(victim), json={"kind": "key", "id": other})
        assert r.status_code == 404

        # App connection, webhook, 2FA.
        await revoke("connection", "g1")
        assert await count(h, "SELECT COUNT(*) FROM oauth_grants WHERE user_id = ? AND revoked_at IS NULL", uid) == 0
        assert await count(h, "SELECT COUNT(*) FROM oauth_tokens WHERE revoked_at IS NULL") == 0
        await revoke("webhook", "wh1")
        assert await count(h, "SELECT COUNT(*) FROM webhooks WHERE user_id = ? AND active = 1", uid) == 0
        await revoke("two_factor")
        assert not (await h.db.get_user_by_id(uid))["totp_enabled"]

        # The squatter's GitHub link: removing it signs out every session; the victim gets a new one.
        data = await revoke("identity", "github")
        assert data["access_token"] and data["review"]["items"]["identities"] == []
        assert (await h.client.get("/api/v1/auth/review", headers=sq["jwt"])).status_code == 401
        frag = await sign_in(h, providers, "github", code="gh-login-1")
        assert "code" in frag  # a new, separate account for the squatter's own address
        other_account = (await exchange(h, frag["code"])).json()["user"]
        assert other_account["id"] != uid and other_account["email"] == "squatter@example.org"

        # Password: dead after removal, every old session too. It was the last
        # item, so the check finishes by itself (no empty screen).
        data = await revoke("password")
        assert data["access_token"] and data["review"]["pending"] is False
        r = await h.client.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": PASSWORD})
        assert r.status_code == 401
        assert (await h.client.get("/api/v1/auth/me", headers=bearer(body["access_token"]))).status_code == 401
        assert (await h.client.get("/api/v1/auth/me", headers=bearer(victim))).status_code == 200

        assert len(mail["review"]) == 1
        to, kept, removed = mail["review"][0]
        assert to == "victim@gmail.com" and kept == []
        assert len(removed) == 7 and "Password" in removed and "Two-factor sign-in" in removed
        assert "Webhook to attacker.example" in removed  # host only: a path can carry a secret
        actions = await audit_actions(h, uid)
        assert [a for a, _ in actions].count("account_review_revoked") == 7
        assert actions[-1][0] == "account_review_completed" and actions[-1][1]["removed"] == removed
        # The victim keeps a working session and Google sign-in; the account is theirs.
        again = await google_session(h, providers, "victim@gmail.com", code="auth-code-9")
        assert again["user"]["id"] == uid and again["requires_2fa"] is False


async def test_two_factor_turned_on_by_the_owner_during_the_review_applies(tmp_path, providers, mail) -> None:
    import pyotp

    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        token = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        setup = await h.client.post("/api/v1/auth/2fa/setup", headers=bearer(token))
        assert setup.status_code == 200, setup.text
        totp = pyotp.TOTP(setup.json()["secret"])
        enabled = await h.client.post("/api/v1/auth/2fa/enable", headers=bearer(token), json={"code": totp.now()})
        assert enabled.status_code == 200, enabled.text
        review = (await h.client.get("/api/v1/auth/review", headers=bearer(token))).json()
        assert review["items"]["two_factor"] is False  # the owner's own, not an item to review
        # And it is required at the next Google sign-in.
        providers.google_claims = {"email": "legacy@gmail.com"}
        frag = await sign_in(h, providers, "google", code="auth-code-2")
        assert (await exchange(h, frag["code"])).json()["requires_2fa"] is True
        assert uid


async def test_defer_is_recorded_and_the_review_stays_open(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        token = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        r = await h.client.post("/api/v1/auth/review/defer", headers=bearer(token))
        assert r.status_code == 200 and r.json() == {"deferred": True}
        assert (await h.client.get("/api/v1/auth/review", headers=bearer(token))).json()["pending"] is True
        # A later Google sign-in is trusted again and sees it.
        later = (await google_session(h, providers, "legacy@gmail.com", code="auth-code-2"))["access_token"]
        assert (await h.client.get("/api/v1/auth/review", headers=bearer(later))).json()["can_review"] is True
        assert [a for a, _ in await audit_actions(h, uid)] == ["account_review_opened", "account_review_deferred"]
        assert [a for a, _ in await audit_actions(h, uid, sessions=True)].count("account_review_session") == 2


async def test_owner_address_gets_no_superadmin_until_the_review_is_done(tmp_path, providers, mail) -> None:
    settings = oauth_settings(owner_emails=["boss@gmail.com"])
    async with secure_app(tmp_path, ROUTERS, settings=settings) as h:
        uid = await h.create_user("boss@gmail.com", verified=False)
        _, admin_key_id = await h.api_key(uid, "admin")
        request = SimpleNamespace(app=h.app)
        admin_key = AuthenticatedUser(user_id=uid, api_key_id=admin_key_id, rate_limit_tier="standard", role="admin")
        session = AuthenticatedUser(user_id=uid, api_key_id="jwt_auth", rate_limit_tier="standard")
        assert not await is_superadmin(request, admin_key)  # unverified: nothing (unchanged)

        body = await google_session(h, providers, "boss@gmail.com")
        # Verified now, but a key made before verification must not inherit platform rights.
        assert body["user"]["is_admin"] is False
        assert not await is_superadmin(request, admin_key) and not await is_superadmin(request, session)

        token = body["access_token"]
        assert (await keep_all(h, token)).status_code == 200
        assert await is_superadmin(request, admin_key) and await is_superadmin(request, session)
        assert (await h.client.get("/api/v1/auth/me", headers=bearer(token))).json()["is_admin"] is True


async def test_superadmin_listed_by_id_is_unchanged_during_a_review(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("root@gmail.com", verified=False)
        h.settings.superadmin_user_ids = [uid]
        await google_session(h, providers, "root@gmail.com")
        assert await account_review.is_pending(h.db, uid)
        session = AuthenticatedUser(user_id=uid, api_key_id="jwt_auth", rate_limit_tier="standard")
        assert await is_superadmin(SimpleNamespace(app=h.app), session)


# ---------------------------------------------------------------------------
# Forgot password on an unverified account
# ---------------------------------------------------------------------------


async def test_reset_on_unverified_account_revokes_nothing_and_opens_the_review(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        sq = await _squatted_account(h, providers)
        uid = sq["uid"]
        token, _ = await h.users.create_password_reset_token("victim@gmail.com")
        r = await h.client.post(
            "/api/v1/auth/reset-password",
            json={"email": "victim@gmail.com", "token": token, "new_password": "N3w!Passw0rdX"},
        )
        assert r.status_code == 200, r.text

        user = await h.db.get_user_by_id(uid)
        assert user["email_verified"] and user["totp_enabled"]
        assert await key_works(h, sq["key"]) and await key_works(h, sq["admin_key"])
        assert await count(h, "SELECT COUNT(*) FROM webhooks WHERE user_id = ? AND active = 1", uid) == 1
        assert await count(h, "SELECT COUNT(*) FROM oauth_grants WHERE user_id = ? AND revoked_at IS NULL", uid) == 1
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", uid) == 1
        # Every old session ends (a reset always did that).
        assert (await h.client.get("/api/v1/auth/me", headers=sq["jwt"])).status_code == 401

        # The new password is the mailbox owner's: no squatter 2FA, a trusted session.
        r = await h.client.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": "N3w!Passw0rdX"})
        assert r.status_code == 200 and r.json()["access_token"], r.text
        owner = r.json()["access_token"]
        review = (await h.client.get("/api/v1/auth/review", headers=bearer(owner))).json()
        assert review["can_review"] is True and review["origin"] == "password_reset"
        items = review["items"]
        assert len(items["keys"]) == 2 and items["two_factor"] is True and items["password"] is False
        assert [i["provider"] for i in items["identities"]] == ["github"]
        # The squatter's GitHub link signs in but is not trusted.
        frag = await sign_in(h, providers, "github", code="gh-login-1")
        gh = (await exchange(h, frag["code"])).json()
        assert gh["requires_2fa"] is True  # not the owner's proof: the old 2FA still applies to it
        assert [a for a, _ in await audit_actions(h, uid)] == ["account_review_opened"]
        assert (await audit_actions(h, uid, sessions=True))[-1] == ("account_review_session", {"method": "password_reset"})


async def test_reset_during_a_google_review_makes_the_password_trusted(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        await h.api_key(uid)  # something to review, so the check stays open
        await google_session(h, providers, "legacy@gmail.com")
        r = await h.client.post("/api/v1/auth/login", json={"email": "legacy@gmail.com", "password": PASSWORD})
        assert "rvw" not in jwt.decode(r.json()["access_token"], JWT_SECRET, algorithms=["HS256"])
        token, _ = await h.users.create_password_reset_token("legacy@gmail.com")
        assert (await h.users.reset_password("legacy@gmail.com", token, "N3w!Passw0rdX"))[0]
        r = await h.client.post("/api/v1/auth/login", json={"email": "legacy@gmail.com", "password": "N3w!Passw0rdX"})
        claims = jwt.decode(r.json()["access_token"], JWT_SECRET, algorithms=["HS256"])
        assert claims["rvw"] == (await account_review.get_review(h.db, uid)).review_id
        review = (await h.client.get("/api/v1/auth/review", headers=bearer(r.json()["access_token"]))).json()
        assert review["items"]["password"] is False
        # The password becoming the owner's is on the record.
        updated = [d for a, d in await audit_actions(h, uid) if a == "account_review_updated"]
        assert updated == [{"change": "password_trusted", "by": "password_reset"}]


# ---------------------------------------------------------------------------
# Verified accounts and the link-CSRF protections
# ---------------------------------------------------------------------------


async def test_verified_accounts_are_unaffected(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("verified@gmail.com", verified=True)
        raw_key, _ = await h.api_key(uid)
        body = await google_session(h, providers, "verified@gmail.com")
        assert body["user"]["id"] == uid and "rvw" not in jwt.decode(body["access_token"], JWT_SECRET, algorithms=["HS256"])
        assert (await h.client.get("/api/v1/auth/review", headers=bearer(body["access_token"]))).json()["pending"] is False
        token, _ = await h.users.create_password_reset_token("verified@gmail.com")
        assert (await h.users.reset_password("verified@gmail.com", token, "N3w!Passw0rdX"))[0]
        assert await count(h, "SELECT COUNT(*) FROM account_reviews") == 0
        assert await key_works(h, raw_key)
        # Nothing to act on.
        fresh = h.jwt(uid, "verified@gmail.com")
        assert (await h.client.post("/api/v1/auth/review/complete", headers=fresh, json={"version": "x"})).status_code == 409
        # A brand-new Google account never gets a review either.
        providers.google_claims = {"email": "brand.new@gmail.com", "sub": "google-sub-new"}
        new = (await exchange(h, (await sign_in(h, providers, "google", code="auth-code-3"))["code"])).json()
        assert new["new_account"] is True
        assert await count(h, "SELECT COUNT(*) FROM account_reviews") == 0


async def test_link_csrf_protections_hold_for_a_trusted_session(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        token = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        # The trusted session may connect GitHub, but the ticket stays bound to this browser.
        r = await h.client.post("/api/v1/auth/oauth/github/link", headers=bearer(token))
        assert r.status_code == 200, r.text
        async with other_browser(h) as victim_browser:
            s = await victim_browser.get(r.json()["start_path"])
            assert s.status_code == 303 and "error=invalid_state" in s.headers["location"]
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE provider = 'github'") == 0
        # In its own browser the flow completes, and a provider linked by the proven owner is trusted.
        providers.github_user = {"id": 31, "login": "legacy"}
        providers.github_emails = [{"email": "legacy@example.org", "primary": True, "verified": True}]
        frag = await connect(h, providers, "github", bearer(token), "gh-link-2")
        assert frag == {"linked": "1", "provider": "github", "from": "settings"}
        frag = await sign_in(h, providers, "github", code="gh-login-2")
        gh = (await exchange(h, frag["code"])).json()
        assert gh["user"]["id"] == uid
        assert jwt.decode(gh["access_token"], JWT_SECRET, algorithms=["HS256"])["rvw"]
        review = (await h.client.get("/api/v1/auth/review", headers=bearer(gh["access_token"]))).json()
        assert review["items"]["identities"] == []  # added after verification by the owner


async def test_keys_made_while_the_review_is_open_are_listed_too(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("legacy@gmail.com", verified=False)
        old_key, _ = await h.api_key(uid)
        token = (await google_session(h, providers, "legacy@gmail.com"))["access_token"]
        # E.g. a squatter's still-open password session mints a key after the link.
        _, late_id = await h.api_key(uid, "viewer")
        keys_listed = (await h.client.get("/api/v1/auth/review", headers=bearer(token))).json()["items"]["keys"]
        late = next(k for k in keys_listed if k["id"] == late_id)
        assert late["before_verification"] is False and late["role"] == "viewer"
        assert len(keys_listed) == 2 and await key_works(h, old_key)


async def test_migration_10_is_additive_and_reapplies_on_a_schema_9_database(tmp_path) -> None:
    from remembra.storage.database import VERSIONED_MIGRATIONS, Database

    assert [v for v, _, _ in VERSIONED_MIGRATIONS][-1] == 10
    assert 5 not in {v for v, _, _ in VERSIONED_MIGRATIONS}  # reserved by feat/crew
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    try:
        await db.init_schema()
        assert await db.get_schema_version() == 10
        # A production database at schema 9: the table and the version row are absent.
        await db.conn.execute("DROP TABLE account_reviews")
        await db.conn.execute("DELETE FROM schema_version WHERE version = 10")
        await db.conn.commit()
        assert await account_review.get_review(db, "u1") is None  # tolerated before the upgrade
        await db.init_schema()
        assert await db.get_schema_version() == 10
        cursor = await db.conn.execute("SELECT name FROM schema_version WHERE version = 10")
        assert (await cursor.fetchone())[0] == "account_reviews"
        review = await account_review.open_review(
            db,
            "u1",
            origin=account_review.ORIGIN_GOOGLE,
            verified_at=account_review.now_iso(),
            totp_enabled=False,
            proof_identity=("google", "sub-1"),
        )
        assert review.pending and (await account_review.get_review(db, "u1")) == review
        assert review.trusted_identities == ("google:sub-1",)
        # A provider review always names the provider account that proved the mailbox.
        with pytest.raises(ValueError):
            await account_review.open_review(
                db, "u2", origin=account_review.ORIGIN_GITHUB, verified_at=account_review.now_iso(), totp_enabled=False
            )
    finally:
        await db.close()
