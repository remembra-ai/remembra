"""Sign-in trust rules (truth audit P-297, P-296, P-254).

Real routers on SQLite with auth on (tests.security_harness), and the real
production app for the MCP connector (tests.connector_harness); only the
Google / GitHub HTTP edge is faked (tests.test_social_login.FakeProviders).

1. A provider identity is linked by email only into an account whose email is
   verified, and only when the provider says its email is verified. An account
   nobody has proven the mailbox of is never taken over by a sign-in with a
   provider (a GitHub primary address can be stale).
2. Every sign-in that issues a dashboard session asks for the TOTP code when
   2FA is on, account check or not. The emailed reset that proves the mailbox
   turns off 2FA that was set up before the email was verified, so an
   authenticator the owner never had cannot lock them out.
3. A password reset during an open account check disconnects the apps that
   were connected since the check opened (they were connected with a password
   the reset replaces). Apps connected before the email was verified stay
   listed in the check.
4. The PKCE verifier and the OpenID nonce are never stored in plaintext.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pyotp
import pytest

from remembra.api.v1 import account_review as review_api
from remembra.api.v1 import auth, keys, social_auth
from remembra.auth import account_review, social
from remembra.auth.users import UserManager
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter
from remembra.security import state as security_state
from tests.connector_harness import PASSWORD as CONNECTOR_PASSWORD
from tests.connector_harness import connector_app
from tests.security_harness import JWT_SECRET, secure_app
from tests.test_social_login import FakeProviders, callback, count, exchange, oauth_settings, query_of, sign_in, start

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


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def with_totp(h: Any, uid: str) -> pyotp.TOTP:
    secret, _, _ = await h.users.setup_totp(uid)
    totp = pyotp.TOTP(secret)
    assert (await h.users.enable_totp(uid, totp.now()))[0]
    return totp


def next_code(totp: pyotp.TOTP) -> str:
    """A code the server has not seen yet (the current step was used to turn 2FA on)."""
    return str(totp.at(time.time() + 30))


async def audit_rows(h: Any, uid: str, action: str) -> list[dict[str, Any]]:
    cursor = await h.db.conn.execute(
        "SELECT error_message FROM audit_log WHERE user_id = ? AND action = ? ORDER BY timestamp", (uid, action)
    )
    return [json.loads(row[0] or "{}") for row in await cursor.fetchall()]


async def legacy_provider_review(
    db: Any, uid: str, email: str, *, provider: str = "google", subject: str = "google-sub-1"
) -> account_review.Review:
    """The state a Google / GitHub sign-in into a never-verified account left before this change.

    That sign-in linked the provider account, verified the email, ended the
    dashboard sessions and opened an account check trusting that provider
    account. Sign-in no longer does this, but such checks can still be open
    in existing databases, and everything after the link is live code.
    """
    await social.ensure_schema(db)
    now = account_review.now_iso()
    await db.conn.execute(
        "INSERT INTO user_identities (provider, provider_user_id, user_id, email, created_at, last_login_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (provider, subject, uid, email, now, now),
    )
    await db.conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (uid,))
    await db.conn.commit()
    user = await db.get_user_by_id(uid)
    review = await account_review.open_review(
        db, uid, origin=provider, verified_at=now, totp_enabled=bool(user["totp_enabled"]), proof_identity=(provider, subject)
    )
    await security_state.invalidate_user_sessions(db, uid, keep_app_connections=True)
    await account_review.audit_opened(db, review, ip=None)
    await account_review.finish_if_empty(db, review, ip=None)
    return review


async def reset(h: Any, email: str, new_password: str) -> None:
    token, _ = await h.users.create_password_reset_token(email)
    r = await h.client.post("/api/v1/auth/reset-password", json={"email": email, "token": token, "new_password": new_password})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 1. No provider takes over an account whose email was never verified
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["github", "google"])
async def test_a_verified_provider_email_never_links_into_an_unverified_account(tmp_path, providers, mail, provider) -> None:
    """The owner signed up with a password and has not clicked the link yet; a provider account shows up with that address."""
    email = "owner@example.org" if provider == "github" else "owner@gmail.com"
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user(email, verified=False)
        raw_key, _ = await h.api_key(uid)
        owner_session = h.jwt(uid, email)
        # A GitHub account that still lists the address as its verified primary
        # (GitHub never re-verifies), or a Google account for it.
        providers.github_user = {"id": 666, "login": "someone-else"}
        providers.github_emails = [{"email": email, "primary": True, "verified": True}]
        providers.google_claims = {"email": email, "sub": "google-sub-other"}

        frag = await sign_in(h, providers, provider)

        assert frag == {"error": "account_exists_unverified", "provider": provider, "from": "login"}
        assert not (await h.db.get_user_by_id(uid))["email_verified"]
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_codes") == 0
        assert await count(h, "SELECT COUNT(*) FROM account_reviews") == 0
        assert await count(h, "SELECT COUNT(*) FROM users") == 1
        assert mail["linked"] == []
        # The owner is not signed out, and their key keeps working.
        assert (await h.client.get("/api/v1/auth/me", headers=owner_session)).status_code == 200
        assert (await h.client.get("/api/v1/keys", headers={"X-API-Key": raw_key})).status_code == 200


async def test_once_the_owner_verifies_the_email_google_links_and_github_still_needs_settings(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("owner@gmail.com", verified=False)
        providers.google_claims = {"email": "owner@gmail.com"}
        assert (await sign_in(h, providers, "google"))["error"] == "account_exists_unverified"

        await h.db.update_user_email_verified(uid, True)  # the owner clicked the verification link
        body = (await exchange(h, (await sign_in(h, providers, "google", code="auth-code-2"))["code"])).json()
        assert body["user"]["id"] == uid and body["new_account"] is False
        assert mail["linked"] == [("owner@gmail.com", "Google", "owner@gmail.com")]
        assert await count(h, "SELECT COUNT(*) FROM account_reviews") == 0

        providers.github_emails = [{"email": "owner@gmail.com", "primary": True, "verified": True}]
        frag = await sign_in(h, providers, "github", code="auth-code-3")
        assert frag == {"error": "account_exists_link_required", "provider": "github", "from": "login"}


async def test_resolve_account_refuses_an_identity_whose_email_the_provider_did_not_verify(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        for email in ("person@gmail.com", "brand.new@gmail.com"):
            identity = social.ProviderIdentity(
                provider="google", subject="google-sub-9", email=email, name=None, email_verified=False
            )
            with pytest.raises(social.SocialLoginError) as refused:
                await social.resolve_account(h.db, identity, "203.0.113.9")
            assert refused.value.code == "email_unverified"
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0
        assert await count(h, "SELECT COUNT(*) FROM users") == 1
        assert (await h.db.get_user_by_id(uid))["email_verified"]


# ---------------------------------------------------------------------------
# 2. 2FA on every sign-in that issues a session
# ---------------------------------------------------------------------------


async def test_provider_sign_in_asks_for_2fa_even_for_the_identity_that_proved_the_mailbox(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("owner@gmail.com", verified=False)
        totp = await with_totp(h, uid)
        await h.api_key(uid)  # something to check, so the check stays open
        review = await legacy_provider_review(h.db, uid, "owner@gmail.com")
        assert review.totp_status == account_review.TOTP_PREDATES

        providers.google_claims = {"email": "owner@gmail.com"}
        frag = await sign_in(h, providers, "google")
        first = await exchange(h, frag["code"])
        assert first.status_code == 200, first.text
        assert first.json()["requires_2fa"] is True and first.json()["access_token"] is None

        done = await exchange(h, frag["code"], next_code(totp))
        assert done.status_code == 200, done.text
        assert claims_of(done.json()["access_token"])["rvw"] == review.review_id


async def test_password_sign_in_asks_for_2fa_even_with_a_password_set_by_emailed_reset(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("owner@gmail.com", verified=False)
        totp = await with_totp(h, uid)
        await h.api_key(uid)
        # A check opened by an emailed reset before this change: its password is the owner's, its 2FA predates it.
        review = await account_review.open_review(
            h.db,
            uid,
            origin=account_review.ORIGIN_PASSWORD_RESET,
            verified_at=account_review.now_iso(),
            totp_enabled=True,
        )
        assert review.password_status == account_review.PASSWORD_TRUSTED

        r = await h.client.post("/api/v1/auth/login", json={"email": "owner@gmail.com", "password": PASSWORD})
        assert r.status_code == 200, r.text
        assert r.json()["requires_2fa"] is True and r.json()["access_token"] is None

        r = await h.client.post(
            "/api/v1/auth/login", json={"email": "owner@gmail.com", "password": PASSWORD, "totp_code": next_code(totp)}
        )
        assert r.status_code == 200 and r.json()["access_token"], r.text
        assert claims_of(r.json()["access_token"])["rvw"] == review.review_id


async def test_the_reset_that_proves_the_mailbox_turns_off_2fa_set_before_the_email_was_verified(
    tmp_path, providers, mail
) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("victim@gmail.com", verified=False)
        await with_totp(h, uid)  # possibly the squatter's authenticator
        await h.api_key(uid)

        await reset(h, "victim@gmail.com", "N3w!Passw0rdX")

        assert not (await h.db.get_user_by_id(uid))["totp_enabled"]
        r = await h.client.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": "N3w!Passw0rdX"})
        assert r.status_code == 200 and r.json()["requires_2fa"] is False and r.json()["access_token"], r.text
        owner = r.json()["access_token"]
        shown = (await h.client.get("/api/v1/auth/review", headers=bearer(owner))).json()
        assert shown["can_review"] is True and shown["items"]["two_factor"] is False
        removed = [d for d in await audit_rows(h, uid, "account_review_revoked") if d["kind"] == "two_factor"]
        assert removed == [
            {"kind": "two_factor", "label": "Two-factor sign-in", "by": "password_reset", "reason": "set_before_email_verified"}
        ]
        # Finishing the check reports it as removed.
        done = await h.client.post("/api/v1/auth/review/complete", headers=bearer(owner), json={"version": shown["version"]})
        assert done.status_code == 200 and "Two-factor sign-in" in done.json()["removed"], done.text


async def test_an_owner_locked_out_by_a_squatters_2fa_gets_in_through_the_emailed_reset(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("victim@gmail.com", verified=False)
        await with_totp(h, uid)  # the squatter's; the owner never had it
        await h.api_key(uid)
        review = await legacy_provider_review(h.db, uid, "victim@gmail.com")

        providers.google_claims = {"email": "victim@gmail.com"}
        frag = await sign_in(h, providers, "google")
        assert (await exchange(h, frag["code"])).json()["requires_2fa"] is True

        await reset(h, "victim@gmail.com", "N3w!Passw0rdX")
        assert not (await h.db.get_user_by_id(uid))["totp_enabled"]

        frag = await sign_in(h, providers, "google", code="auth-code-2")
        body = (await exchange(h, frag["code"])).json()
        assert body["requires_2fa"] is False and claims_of(body["access_token"])["rvw"] == review.review_id


async def test_2fa_turned_on_after_the_mailbox_was_proven_survives_a_later_reset(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("owner@gmail.com", verified=False)
        await h.api_key(uid)
        await reset(h, "owner@gmail.com", "N3w!Passw0rdX")
        owner = (
            await h.client.post("/api/v1/auth/login", json={"email": "owner@gmail.com", "password": "N3w!Passw0rdX"})
        ).json()["access_token"]
        secret = (await h.client.post("/api/v1/auth/2fa/setup", headers=bearer(owner))).json()["secret"]
        totp = pyotp.TOTP(secret)
        assert (
            await h.client.post("/api/v1/auth/2fa/enable", headers=bearer(owner), json={"code": totp.now()})
        ).status_code == 200

        await reset(h, "owner@gmail.com", "An0ther!Passw0rd")

        assert (await h.db.get_user_by_id(uid))["totp_enabled"]
        r = await h.client.post("/api/v1/auth/login", json={"email": "owner@gmail.com", "password": "An0ther!Passw0rd"})
        assert r.json()["requires_2fa"] is True and r.json()["access_token"] is None


# ---------------------------------------------------------------------------
# 3. A reset during an open check disconnects apps connected since it opened
# ---------------------------------------------------------------------------


async def _mcp_ok(c: Any, token: str) -> bool:
    resp = await c.mcp_post(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return resp.status_code == 200


async def test_a_reset_during_the_check_disconnects_apps_connected_since_it_opened(tmp_path, mail) -> None:
    async with connector_app(tmp_path) as c:
        uid = await c.create_user("legacy@gmail.com")
        assert not (await c.db.get_user_by_id(uid))["email_verified"]
        before = await c.connect("legacy@gmail.com", ["alpha"])
        users = UserManager(c.db, c.settings.jwt_secret)

        # The emailed reset proves the mailbox and opens the check; the password is the owner's now.
        token, _ = await users.create_password_reset_token("legacy@gmail.com")
        assert (await users.reset_password("legacy@gmail.com", token, CONNECTOR_PASSWORD))[0]
        assert await account_review.is_pending(c.db, uid)
        during = await c.connect("legacy@gmail.com", ["beta"])  # connected with that password while the check is open
        assert await _mcp_ok(c, before.access_token) and await _mcp_ok(c, during.access_token)

        # The owner resets again: the password it replaces may be known to someone else.
        token, _ = await users.create_password_reset_token("legacy@gmail.com")
        assert (await users.reset_password("legacy@gmail.com", token, "An0ther!Passw0rd"))[0]

        assert not await _mcp_ok(c, during.access_token)
        refreshed = await c.token(
            {"grant_type": "refresh_token", "refresh_token": during.refresh_token, "client_id": during.client_id}
        )
        assert refreshed.status_code == 400, refreshed.text
        # The app connected before the email was verified is the owner's call, in the check.
        assert await _mcp_ok(c, before.access_token)
        assert await account_review.is_pending(c.db, uid)
        cursor = await c.db.conn.execute(
            "SELECT project_ids, revoke_reason FROM oauth_grants WHERE user_id = ? ORDER BY created_at", (uid,)
        )
        assert [(json.loads(p), r) for p, r in await cursor.fetchall()] == [(["alpha"], None), (["beta"], "password_reset")]
        cursor = await c.db.conn.execute(
            "SELECT error_message FROM audit_log WHERE user_id = ? AND action = 'account_review_revoked'", (uid,)
        )
        revoked = [json.loads(row[0]) for row in await cursor.fetchall()]
        assert revoked == [
            {
                "kind": "connection",
                "label": "App connection 'Claude'",
                "by": "password_reset",
                "reason": "signed_in_during_review",
            }
        ]


async def test_a_reset_during_a_check_with_nothing_connected_since_revokes_nothing(tmp_path, mail) -> None:
    async with connector_app(tmp_path) as c:
        uid = await c.create_user("legacy@gmail.com")
        before = await c.connect("legacy@gmail.com", ["alpha"])
        users = UserManager(c.db, c.settings.jwt_secret)
        for new_password in ("N3w!Passw0rdX", "An0ther!Passw0rd"):
            token, _ = await users.create_password_reset_token("legacy@gmail.com")
            assert (await users.reset_password("legacy@gmail.com", token, new_password))[0]
        assert await account_review.is_pending(c.db, uid)
        assert await _mcp_ok(c, before.access_token)
        cursor = await c.db.conn.execute("SELECT COUNT(*) FROM oauth_grants WHERE revoked_at IS NOT NULL")
        assert (await cursor.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# 4. PKCE verifier and nonce at rest
# ---------------------------------------------------------------------------


async def _stored_flow(h: Any) -> tuple[str, str]:
    cursor = await h.db.conn.execute("SELECT code_verifier, nonce FROM oauth_login_states")
    rows = await cursor.fetchall()
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1])


@pytest.mark.parametrize("provider", ["github", "google"])
async def test_the_pkce_verifier_and_nonce_are_stored_only_as_hashes(tmp_path, providers, mail, provider) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, provider)
        sent = query_of(r)
        stored_verifier, stored_nonce = await _stored_flow(h)

        code = providers.authorize(r.headers["location"])
        frag = await callback(h, provider, {"code": code, "state": sent["state"]})
        assert "code" in frag, frag
        verifier = providers.token_calls[-1]["code_verifier"]

        assert stored_verifier == sha256(verifier) and verifier not in stored_verifier
        if provider == "google":
            assert stored_nonce == sha256(sent["nonce"]) and sent["nonce"] not in stored_nonce
        else:
            assert len(stored_nonce) == 64 and all(ch in "0123456789abcdef" for ch in stored_nonce)
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_states") == 0


async def test_a_flow_whose_verifier_does_not_match_the_stored_hash_is_refused(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        await h.db.conn.execute("UPDATE oauth_login_states SET code_verifier = ?", (sha256("some-other-verifier"),))
        await h.db.conn.commit()
        code = providers.authorize(r.headers["location"])
        frag = await callback(h, "github", {"code": code, "state": query_of(r)["state"]})
        assert frag["error"] == "invalid_state"
        assert providers.token_calls == []  # never sent to the provider


async def test_flows_stored_in_plaintext_before_the_upgrade_are_deleted(tmp_path, providers, mail) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await social.ensure_schema(h.db)
        legacy_verifier, legacy_nonce = "v" * 86, "n" * 43
        await h.db.conn.execute(
            "INSERT INTO oauth_login_states (state_hash, provider, browser_hash, code_verifier, nonce, from_page, expires_at)"
            " VALUES (?, 'google', ?, ?, ?, 'login', ?)",
            (sha256("legacy-state"), sha256("legacy-browser"), legacy_verifier, legacy_nonce, time.time() + 600),
        )
        await h.db.conn.commit()
        social._initialized.discard(h.db.conn)  # the next start after an upgrade

        await start(h, "github")

        cursor = await h.db.conn.execute("SELECT code_verifier, nonce FROM oauth_login_states")
        rows = await cursor.fetchall()
        assert len(rows) == 1 and legacy_verifier not in rows[0] and legacy_nonce not in rows[0]
