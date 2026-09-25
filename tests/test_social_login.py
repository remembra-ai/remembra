"""Sign in with GitHub / Google: full flows against mocked provider endpoints.

The real routers run on SQLite with auth enabled (tests.security_harness);
only the provider HTTP edge is faked with an ``httpx.MockTransport`` that
behaves like GitHub (token endpoint, /user, /user/emails) and Google (token
endpoint returning an RS256 ``id_token`` signed by a key published on a fake
JWKS endpoint). The fake checks PKCE the way the providers do: the
``code_verifier`` sent at the token endpoint must hash to the
``code_challenge`` sent on the authorize redirect.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from structlog.testing import capture_logs

from remembra.api.v1 import auth, social_auth
from remembra.auth import social
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter
from remembra.core.limiter import limiter
from tests.security_harness import make_settings, secure_app

ROUTERS = [auth.router, social_auth.router]
API = "http://localhost:8787"
DASH = "https://app.example.test"
GH_ID, GH_SECRET = "gh-client-id", "gh-client-secret-value"
GO_ID, GO_SECRET = "go-client-id.apps.googleusercontent.com", "go-client-secret-value"


def oauth_settings(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "public_url": API,
        "public_dashboard_url": DASH,
        "github_client_id": GH_ID,
        "github_client_secret": GH_SECRET,
        "google_client_id": GO_ID,
        "google_client_secret": GO_SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class FakeProviders:
    key: Any = field(default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048))
    kid: str = "test-kid-1"
    challenges: dict[str, str] = field(default_factory=dict)  # auth code -> code_challenge
    nonces: dict[str, str] = field(default_factory=dict)  # auth code -> nonce
    token_calls: list[dict[str, str]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    github_user: dict[str, Any] = field(default_factory=lambda: {"id": 4242, "login": "octo", "name": "Octo Cat", "email": None})
    github_emails: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"email": "octo@private.example", "primary": True, "verified": True, "visibility": "private"},
            {"email": "other@example.com", "primary": False, "verified": False, "visibility": None},
        ]
    )
    github_token_error: str | None = None
    google_claims: dict[str, Any] = field(default_factory=dict)
    google_signing_key: Any = None  # sign with a different key than the JWKS publishes
    google_alg: str = "RS256"
    jwks_hits: int = 0

    def authorize(self, location: str, code: str = "auth-code-1") -> str:
        """Pretend the user approved: remember the PKCE challenge/nonce for this code."""
        query = dict(parse_qsl(urlsplit(location).query))
        self.challenges[code] = query["code_challenge"]
        self.nonces[code] = query.get("nonce", "")
        return code

    def id_token(self, code: str) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": GO_ID,
            "azp": GO_ID,
            "sub": "google-sub-1",
            "email": "person@gmail.com",
            "email_verified": True,
            "name": "Pat Person",
            "iat": now,
            "exp": now + 3600,
            "nonce": self.nonces.get(code, ""),
        }
        claims.update(self.google_claims)
        claims = {k: v for k, v in claims.items() if v is not None}
        if self.google_alg == "HS256":
            return jwt.encode(claims, "shared-secret-attack-0123456789abcdef", algorithm="HS256", headers={"kid": self.kid})
        return jwt.encode(claims, self.google_signing_key or self.key, algorithm="RS256", headers={"kid": self.kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == "https://github.com/login/oauth/access_token":
            form = dict(parse_qsl(request.content.decode()))
            self.token_calls.append(form)
            assert request.headers["accept"] == "application/json"
            ok = (
                form.get("client_id") == GH_ID
                and form.get("client_secret") == GH_SECRET
                and form.get("redirect_uri") == f"{API}/api/v1/auth/oauth/github/callback"
                and form.get("code") in self.challenges
                and _s256(form.get("code_verifier", "")) == self.challenges[form["code"]]
            )
            if self.github_token_error or not ok:  # GitHub reports errors with HTTP 200
                return httpx.Response(200, json={"error": self.github_token_error or "bad_verification_code"})
            token = {"access_token": "gho_SECRETACCESSTOKEN123", "token_type": "bearer", "scope": "user:email"}
            return httpx.Response(200, json=token)
        if url == "https://api.github.com/user":
            assert request.headers["authorization"] == "Bearer gho_SECRETACCESSTOKEN123"
            assert request.headers["x-github-api-version"] == "2022-11-28"
            return httpx.Response(200, json=self.github_user)
        if url.startswith("https://api.github.com/user/emails"):
            assert request.headers["authorization"] == "Bearer gho_SECRETACCESSTOKEN123"
            return httpx.Response(200, json=self.github_emails)
        if url == "https://oauth2.googleapis.com/token":
            form = dict(parse_qsl(request.content.decode()))
            self.token_calls.append(form)
            ok = (
                form.get("grant_type") == "authorization_code"
                and form.get("client_id") == GO_ID
                and form.get("client_secret") == GO_SECRET
                and form.get("redirect_uri") == f"{API}/api/v1/auth/oauth/google/callback"
                and form.get("code") in self.challenges
                and _s256(form.get("code_verifier", "")) == self.challenges[form["code"]]
            )
            if not ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "ya29.SECRET", "id_token": self.id_token(form["code"])})
        if url == social.GOOGLE_JWKS_URL:
            self.jwks_hits += 1
            jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            jwk.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(404, json={"message": "unexpected " + url})


@pytest.fixture()
def providers(monkeypatch: pytest.MonkeyPatch) -> FakeProviders:
    fake = FakeProviders()
    monkeypatch.setattr(social, "http_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    social.google_jwks.reset()
    set_cloud_rate_limiter(CloudRateLimiter())
    yield fake
    social.google_jwks.reset()
    set_cloud_rate_limiter(None)


async def start(h: Any, provider: str, from_page: str = "login") -> httpx.Response:
    r = await h.client.get(f"/api/v1/auth/oauth/{provider}/start", params={"from": from_page})
    assert r.status_code == 302, r.text
    return r


def query_of(r: httpx.Response) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(r.headers["location"]).query))


async def callback(h: Any, provider: str, params: dict[str, str]) -> dict[str, str]:
    r = await h.client.get(f"/api/v1/auth/oauth/{provider}/callback", params=params)
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.startswith(f"{DASH}/oauth/callback#"), location
    assert r.headers["cache-control"] == "no-store"
    return dict(parse_qsl(urlsplit(location).fragment))


async def sign_in(
    h: Any, fake: FakeProviders, provider: str, code: str = "auth-code-1", from_page: str = "login"
) -> dict[str, str]:
    """Start -> provider approval -> callback. Returns the dashboard fragment."""
    r = await start(h, provider, from_page)
    fake.authorize(r.headers["location"], code)
    return await callback(h, provider, {"code": code, "state": query_of(r)["state"]})


async def exchange(h: Any, code: str, totp: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"code": code}
    if totp:
        body["totp_code"] = totp
    return await h.client.post("/api/v1/auth/oauth/exchange", json=body)


async def count(h: Any, sql: str, *args: Any) -> int:
    await social.ensure_schema(h.db)
    cursor = await h.db.conn.execute(sql, args)
    return int((await cursor.fetchone())[0])


# ---------------------------------------------------------------------------
# Configuration: providers listing, disabled providers are 404
# ---------------------------------------------------------------------------


async def test_providers_lists_only_configured_ones_and_the_turnstile_site_key(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=make_settings()) as h:
        body = (await h.client.get("/api/v1/auth/providers")).json()
        assert body == {"providers": [], "turnstile_site_key": None}
        assert (await h.client.get("/api/v1/auth/oauth/github/start")).status_code == 404
        assert (await h.client.get("/api/v1/auth/oauth/google/callback?code=x&state=y")).status_code == 404

    settings = oauth_settings(google_client_id=None, turnstile_site_key="0x4AAA-site", turnstile_secret="ts-secret")
    async with secure_app(tmp_path, ROUTERS, settings=settings) as h:
        body = (await h.client.get("/api/v1/auth/providers")).json()
        assert body["providers"] == [{"id": "github", "name": "GitHub", "start_path": "/api/v1/auth/oauth/github/start"}]
        assert body["turnstile_site_key"] == "0x4AAA-site"
        assert (await h.client.get("/api/v1/auth/oauth/google/start")).status_code == 404
        assert (await h.client.get("/api/v1/auth/oauth/gitlab/start")).status_code == 404

    # A site key without the secret is never published (the server would not verify it).
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings(turnstile_site_key="0x4AAA-site")) as h:
        body = (await h.client.get("/api/v1/auth/providers")).json()
        assert [p["id"] for p in body["providers"]] == ["github", "google"]
        assert body["turnstile_site_key"] is None


async def test_providers_need_both_public_origins(tmp_path) -> None:
    for missing in ({"public_dashboard_url": None}, {"public_url": None}, {"github_client_secret": "  "}):
        async with secure_app(tmp_path, ROUTERS, settings=oauth_settings(google_client_id=None, **missing)) as h:
            assert (await h.client.get("/api/v1/auth/providers")).json()["providers"] == []
            assert (await h.client.get("/api/v1/auth/oauth/github/start")).status_code == 404


def test_dashboard_url_must_be_https_origin() -> None:
    with pytest.raises(ValueError, match="public_dashboard_url"):
        make_settings(public_dashboard_url="http://app.example.com")
    with pytest.raises(ValueError, match="public_dashboard_url"):
        make_settings(public_dashboard_url="https://app.example.com/evil?next=x")
    assert make_settings(public_dashboard_url="https://App.Example.com/").public_dashboard_url == "https://app.example.com"
    assert make_settings(public_dashboard_url="").public_dashboard_url is None


# ---------------------------------------------------------------------------
# Start: state + PKCE + nonce, strict redirect URI, browser-bound cookie
# ---------------------------------------------------------------------------


async def test_start_redirects_with_state_pkce_and_nonce(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        loc = urlsplit(r.headers["location"])
        q = query_of(r)
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://github.com/login/oauth/authorize"
        assert q["client_id"] == GH_ID and q["scope"] == "user:email"
        assert q["redirect_uri"] == f"{API}/api/v1/auth/oauth/github/callback"
        assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
        assert len(q["state"]) >= 43
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("remembra_oauth_github=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert r.headers["cache-control"] == "no-store"

        r2 = await start(h, "google", "signup")
        g = query_of(r2)
        assert urlsplit(r2.headers["location"]).netloc == "accounts.google.com"
        assert g["response_type"] == "code" and g["scope"] == "openid email profile"
        assert g["redirect_uri"] == f"{API}/api/v1/auth/oauth/google/callback"
        assert g["code_challenge_method"] == "S256" and len(g["nonce"]) >= 43
        assert g["state"] != q["state"] and g["code_challenge"] != q["code_challenge"]
        # Nothing secret is stored in plaintext: only hashes of state and browser binding.
        cursor = await h.db.conn.execute("SELECT state_hash, browser_hash FROM oauth_login_states")
        rows = await cursor.fetchall()
        assert len(rows) == 2 and all(q["state"] not in row[0] for row in rows)


async def test_https_deployments_use_a_host_prefixed_secure_cookie(tmp_path, providers) -> None:
    settings = oauth_settings(public_url="https://api.example.test")
    async with secure_app(tmp_path, ROUTERS, settings=settings) as h:
        r = await start(h, "github")
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-remembra_oauth_github=")
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie and "Domain" not in cookie
        assert query_of(r)["redirect_uri"] == "https://api.example.test/api/v1/auth/oauth/github/callback"


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


async def test_github_new_account_with_private_email(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        frag = await sign_in(h, providers, "github", from_page="signup")
        assert set(frag) == {"code", "provider"} and frag["provider"] == "github"
        # PKCE was actually used at the token endpoint.
        assert providers.token_calls[-1]["code_verifier"]
        assert _s256(providers.token_calls[-1]["code_verifier"]) == providers.challenges["auth-code-1"]

        r = await exchange(h, frag["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["new_account"] is True and body["provider"] == "github"
        assert body["user"]["email"] == "octo@private.example" and body["user"]["email_verified"] is True
        assert body["user"]["name"] == "Octo Cat"
        me = await h.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200 and me.json()["email_verified"] is True

        # The login code is single use.
        again = await exchange(h, frag["code"])
        assert again.status_code == 400 and "expired or was already used" in again.json()["detail"]

        user = await h.db.get_user_by_email("octo@private.example")
        assert user["email_verified"]
        # No usable password: the password endpoint cannot be used to get in.
        assert not h.users.verify_password("", user["password_hash"])
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE provider='github' AND provider_user_id='4242'") == 1


async def test_github_second_sign_in_uses_the_identity_not_the_email(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        first = (await exchange(h, (await sign_in(h, providers, "github"))["code"])).json()
        # The user changes their primary email on GitHub: same GitHub id -> same account.
        providers.github_emails = [{"email": "octo-new@example.com", "primary": True, "verified": True}]
        second = (await exchange(h, (await sign_in(h, providers, "github", code="auth-code-2"))["code"])).json()
        assert second["user"]["id"] == first["user"]["id"] and second["new_account"] is False
        assert await count(h, "SELECT COUNT(*) FROM users") == 1
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 1
        cursor = await h.db.conn.execute("SELECT email FROM user_identities")
        assert (await cursor.fetchone())[0] == "octo-new@example.com"


@pytest.mark.parametrize(
    "emails",
    [
        [{"email": "octo@example.com", "primary": True, "verified": False}],
        # A verified secondary address is not enough: only the primary counts.
        [
            {"email": "octo@example.com", "primary": True, "verified": False},
            {"email": "alt@example.com", "primary": False, "verified": True},
        ],
        [{"email": "4242+octo@users.noreply.github.com", "primary": True, "verified": True}],
        [],
    ],
)
async def test_github_unverified_or_noreply_primary_is_refused(tmp_path, providers, emails) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        await h.create_user("octo@example.com", verified=True)
        providers.github_emails = emails
        frag = await sign_in(h, providers, "github")
        assert frag == {"error": "email_unverified", "provider": "github", "from": "login"}
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0
        assert await count(h, "SELECT COUNT(*) FROM users") == 1


async def test_github_token_error_is_a_provider_error(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        providers.github_token_error = "bad_verification_code"
        frag = await sign_in(h, providers, "github")
        assert frag["error"] == "provider_error"
        assert not any("api.github.com" in u for u in providers.requests)


# ---------------------------------------------------------------------------
# State / cookie / PKCE failures
# ---------------------------------------------------------------------------


async def test_state_must_match_the_browser_that_started_the_flow(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        code = providers.authorize(r.headers["location"])
        state = query_of(r)["state"]
        h.client.cookies.clear()  # login CSRF: the victim's browser has no binding cookie
        frag = await callback(h, "github", {"code": code, "state": state})
        assert frag["error"] == "invalid_state"
        assert providers.token_calls == []  # never exchanged the attacker's code

        # The state was burned by that attempt: replaying it with the right cookie fails too.
        r = await start(h, "github")
        frag = await callback(h, "github", {"code": code, "state": state})
        assert frag["error"] == "invalid_state"


async def test_forged_replayed_crossed_and_expired_states_are_rejected(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        code = providers.authorize(r.headers["location"])
        assert (await callback(h, "github", {"code": code, "state": "forged-state-value"}))["error"] == "invalid_state"
        assert (await callback(h, "github", {"code": code}))["error"] == "invalid_state"

        # Replay: the first use succeeds, the second is refused.
        r = await start(h, "github")
        code = providers.authorize(r.headers["location"], "auth-code-2")
        state = query_of(r)["state"]
        assert "code" in await callback(h, "github", {"code": code, "state": state})
        await start(h, "github")  # fresh cookie in the jar
        assert (await callback(h, "github", {"code": code, "state": state}))["error"] == "invalid_state"

        # A GitHub state cannot finish a Google flow.
        r = await start(h, "github")
        g = await start(h, "google")
        code = providers.authorize(g.headers["location"], "auth-code-3")
        assert (await callback(h, "google", {"code": code, "state": query_of(r)["state"]}))["error"] == "invalid_state"

        # Expired.
        r = await start(h, "github")
        await h.db.conn.execute("UPDATE oauth_login_states SET expires_at = ?", (time.time() - 1,))
        await h.db.conn.commit()
        code = providers.authorize(r.headers["location"], "auth-code-4")
        assert (await callback(h, "github", {"code": code, "state": query_of(r)["state"]}))["error"] == "invalid_state"


async def test_wrong_pkce_verifier_is_refused_by_the_provider(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "github")
        code = providers.authorize(r.headers["location"])
        providers.challenges[code] = _s256("some-other-verifier")  # code was issued for another verifier
        frag = await callback(h, "github", {"code": code, "state": query_of(r)["state"]})
        assert frag["error"] == "provider_error"
        assert await count(h, "SELECT COUNT(*) FROM users") == 0


async def test_user_cancel_returns_access_denied_to_the_right_page(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        r = await start(h, "google", "signup")
        frag = await callback(h, "google", {"error": "access_denied", "state": query_of(r)["state"]})
        assert frag == {"error": "access_denied", "provider": "google", "from": "signup"}
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_states") == 0


# ---------------------------------------------------------------------------
# Google: id_token validation
# ---------------------------------------------------------------------------


async def test_google_new_account_with_verified_id_token(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        frag = await sign_in(h, providers, "google")
        r = await exchange(h, frag["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["new_account"] is True and body["user"]["email"] == "person@gmail.com"
        assert body["user"]["email_verified"] is True and body["user"]["name"] == "Pat Person"
        assert providers.jwks_hits == 1
        # JWKS is cached across sign-ins.
        await sign_in(h, providers, "google", code="auth-code-2")
        assert providers.jwks_hits == 1


async def test_google_workspace_address_is_accepted(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        providers.google_claims = {"email": "Dev@Company.example", "hd": "company.example", "iss": "accounts.google.com"}
        body = (await exchange(h, (await sign_in(h, providers, "google"))["code"])).json()
        assert body["user"]["email"] == "dev@company.example"


@pytest.mark.parametrize(
    ("claims", "error"),
    [
        ({"aud": "someone-elses-client"}, "provider_error"),
        ({"iss": "https://evil.example"}, "provider_error"),
        ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "provider_error"),
        ({"nonce": "not-the-nonce-we-sent"}, "invalid_state"),
        ({"nonce": None}, "invalid_state"),
        ({"sub": None}, "provider_error"),
        ({"email_verified": False}, "email_unverified"),
        ({"email_verified": "false"}, "email_unverified"),
        # Verified once by Google, but Google is not authoritative for it now.
        ({"email": "me@custom-domain.example"}, "email_not_authoritative"),
        ({"aud": [GO_ID, "other"], "azp": "other"}, "provider_error"),
    ],
)
async def test_google_bad_id_tokens_are_refused(tmp_path, providers, claims, error) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        providers.google_claims = claims
        frag = await sign_in(h, providers, "google")
        assert frag["error"] == error, frag
        assert await count(h, "SELECT COUNT(*) FROM users") == 0


async def test_google_token_signed_by_another_key_or_hs256_is_refused(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        providers.google_signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert (await sign_in(h, providers, "google"))["error"] == "provider_error"
        providers.google_signing_key = None
        providers.google_alg = "HS256"
        assert (await sign_in(h, providers, "google", code="auth-code-2"))["error"] == "provider_error"
        assert await count(h, "SELECT COUNT(*) FROM users") == 0


# ---------------------------------------------------------------------------
# Linking and account-takeover protection
# ---------------------------------------------------------------------------


async def test_verified_account_is_linked_not_duplicated(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        body = (await exchange(h, (await sign_in(h, providers, "google"))["code"])).json()
        assert body["user"]["id"] == uid and body["new_account"] is False
        # Password sign-in still works for the same account.
        r = await h.client.post("/api/v1/auth/login", json={"email": "person@gmail.com", "password": "Str0ng!Passw0rd"})
        assert r.status_code == 200 and r.json()["user"]["id"] == uid
        # GitHub with the same verified email is NOT linked by email (GitHub never
        # re-verifies addresses) and does not create a second account either.
        providers.github_emails = [{"email": "person@gmail.com", "primary": True, "verified": True}]
        frag = await sign_in(h, providers, "github", code="auth-code-2")
        assert frag == {"error": "account_exists_link_required", "provider": "github", "from": "login"}
        assert await count(h, "SELECT COUNT(*) FROM users") == 1
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", uid) == 1


async def test_unverified_password_account_is_never_taken_over(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        # Someone pre-registered the victim's address with a password and never verified it.
        await h.create_user("person@gmail.com", verified=False)
        frag = await sign_in(h, providers, "google")
        assert frag["error"] == "account_exists_unverified"
        assert await count(h, "SELECT COUNT(*) FROM user_identities") == 0
        assert await count(h, "SELECT COUNT(*) FROM oauth_login_codes") == 0


async def test_password_reset_verifies_the_email_so_linking_then_works(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=False)
        token, _ = await h.users.create_password_reset_token("person@gmail.com")
        ok, err = await h.users.reset_password("person@gmail.com", token, "N3w!Passw0rdX")
        assert ok, err
        assert (await h.db.get_user_by_id(uid))["email_verified"]
        body = (await exchange(h, (await sign_in(h, providers, "google"))["code"])).json()
        assert body["user"]["id"] == uid


async def test_second_provider_account_cannot_attach_to_a_linked_account(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        first = (await exchange(h, (await sign_in(h, providers, "github"))["code"])).json()
        # A DIFFERENT GitHub account that also lists this verified primary email.
        providers.github_user = {"id": 9999, "login": "impostor", "name": None}
        frag = await sign_in(h, providers, "github", code="auth-code-2")
        assert frag["error"] == "identity_conflict"
        assert await count(h, "SELECT COUNT(*) FROM user_identities WHERE user_id = ?", first["user"]["id"]) == 1
        assert await count(h, "SELECT COUNT(*) FROM users") == 1


async def test_deactivated_account_cannot_sign_in(tmp_path, providers) -> None:
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        body = (await exchange(h, (await sign_in(h, providers, "github"))["code"])).json()
        await h.db.deactivate_user(body["user"]["id"])
        assert (await sign_in(h, providers, "github", code="auth-code-2"))["error"] == "account_disabled"
        await h.create_user("person@gmail.com", verified=True, active=False)
        assert (await sign_in(h, providers, "google", code="auth-code-3"))["error"] == "account_disabled"


async def test_two_factor_is_still_required_after_social_sign_in(tmp_path, providers) -> None:
    import pyotp

    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        uid = await h.create_user("person@gmail.com", verified=True)
        secret, _, _ = await h.users.setup_totp(uid)
        totp = pyotp.TOTP(secret)
        ok, _ = await h.users.enable_totp(uid, totp.now())
        assert ok
        frag = await sign_in(h, providers, "google")
        r = await exchange(h, frag["code"])
        assert r.status_code == 200 and r.json()["requires_2fa"] is True and r.json()["access_token"] is None
        assert (await exchange(h, frag["code"], "000000")).status_code == 401
        good = totp.at(time.time() + 30)  # next step (the current one was used to enable 2FA)
        r = await exchange(h, frag["code"], good)
        assert r.status_code == 200 and r.json()["access_token"] and r.json()["user"]["id"] == uid
        assert (await exchange(h, frag["code"], good)).status_code == 400  # code consumed


async def test_new_social_accounts_are_under_the_signup_limits(tmp_path, providers) -> None:
    settings = oauth_settings(rate_limit_enabled=True, signup_ip_rate_limit="1/hour")
    async with secure_app(tmp_path, ROUTERS, settings=settings) as h:
        assert "code" in await sign_in(h, providers, "github")
        providers.github_user = {"id": 5, "login": "second"}
        providers.github_emails = [{"email": "second@example.com", "primary": True, "verified": True}]
        assert (await sign_in(h, providers, "github", code="auth-code-2"))["error"] == "rate_limited"
        # Signing back in to an existing account is not a signup.
        providers.github_user = {"id": 4242, "login": "octo"}
        assert "code" in await sign_in(h, providers, "github", code="auth-code-3")


async def test_start_and_callback_are_rate_limited(tmp_path, providers) -> None:
    limiter.enabled = True
    limiter.reset()
    try:
        async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
            codes = [(await h.client.get("/api/v1/auth/oauth/github/start")).status_code for _ in range(21)]
            assert codes[:20] == [302] * 20 and codes[20] == 429
            codes = [
                (await h.client.get("/api/v1/auth/oauth/github/callback", params={"state": "x"})).status_code for _ in range(31)
            ]
            assert codes[:30] == [303] * 30 and codes[30] == 429
    finally:
        limiter.reset()
        limiter.enabled = False


# ---------------------------------------------------------------------------
# Secrets stay out of logs
# ---------------------------------------------------------------------------


async def test_tokens_codes_and_states_are_never_logged(tmp_path, providers, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with secure_app(tmp_path, ROUTERS, settings=oauth_settings()) as h:
        with capture_logs() as events:
            r = await start(h, "google")
            code = providers.authorize(r.headers["location"])
            state = query_of(r)["state"]
            frag = await callback(h, "google", {"code": code, "state": state})
            session = (await exchange(h, frag["code"])).json()["access_token"]
            gh = await sign_in(h, providers, "github", code="auth-code-gh")
            await exchange(h, gh["code"])
        # Every server-side log line (structlog events + stdlib records), minus
        # aiosqlite's DEBUG statement echo and the test client's own request log.
        server_lines = [
            r.getMessage() for r in caplog.records if not r.name.startswith("aiosqlite") and "http://test/" not in r.getMessage()
        ]
        text = repr(events) + "\n".join(server_lines)
        for secret in (
            state,
            code,
            frag["code"],
            gh["code"],
            session,
            "gho_SECRETACCESSTOKEN123",
            "ya29.SECRET",
            GH_SECRET,
            GO_SECRET,
            query_of(r)["nonce"],
        ):
            assert secret not in text
        assert any(e.get("event") == "oauth_account_created" for e in events)


def test_access_log_redacts_oauth_codes() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/api/v1/auth/oauth/github/callback?code=SECRETCODE&state=SECRETSTATE&x=1", "1.1", 303),
        None,
    )
    assert social.AccessLogRedactor().filter(record)
    line = record.getMessage()
    assert "SECRETCODE" not in line and "SECRETSTATE" not in line and "x=1" in line and "code=REDACTED" in line
    assert social.redact_url("/api/v1/memories?q=hi") == "/api/v1/memories?q=hi"
    social.install_access_log_redaction()
    social.install_access_log_redaction()
    assert sum(isinstance(f, social.AccessLogRedactor) for f in logging.getLogger("uvicorn.access").filters) == 1
