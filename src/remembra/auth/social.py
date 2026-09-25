"""Sign in with GitHub / Google for the dashboard (OAuth 2.0 authorization code + PKCE).

Flow (the API is the OAuth client; the dashboard never sees provider tokens):

1. ``GET /api/v1/auth/oauth/{provider}/start`` creates a single-use login
   state (random ``state``, PKCE ``code_verifier``, OIDC ``nonce``) stored
   hashed in ``oauth_login_states`` and bound to the browser with an HttpOnly
   cookie, then redirects to the provider.
2. ``GET /api/v1/auth/oauth/{provider}/callback`` consumes the state (it must
   match the cookie), exchanges the code with the PKCE verifier, and reads a
   VERIFIED email:

   * GitHub: ``GET /user`` for the stable numeric id and ``GET /user/emails``
     (scope ``user:email``, so private addresses are visible) for the primary
     address, which must be ``verified``. ``users.noreply.github.com``
     addresses are refused (they are not mailboxes).
   * Google: the ``id_token`` is verified against Google's JWKS (RS256),
     ``iss``, ``aud``, ``exp``/``iat`` and the ``nonce``; ``email_verified``
     must be true, and the address must be one Google is authoritative for
     (``@gmail.com`` or a Workspace ``hd``), per Google's guidance.

3. The identity is resolved to ONE account: an existing identity link signs
   in; otherwise an existing account with that email is linked only if that
   account's email is already verified (a verified provider email never
   takes over an unverified password account); otherwise a new account is
   created with ``email_verified = true`` under the normal signup limits.
   ``user_identities`` is unique on ``(provider, provider_user_id)`` and on
   ``(user_id, provider)``, and ``users.email`` is unique, so a verified
   email or a provider account can back at most one Remembra account.
4. The callback redirects to ``<public_dashboard_url>/oauth/callback`` with a
   single-use, 5-minute login code in the URL fragment; the dashboard trades
   it at ``POST /api/v1/auth/oauth/exchange`` for the normal dashboard JWT
   (a TOTP code is required there when the account has 2FA on).

Provider access tokens, codes, states, verifiers and nonces are never logged
and never stored in plaintext beyond the few minutes a flow is open.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import sqlite3
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import jwt
import structlog

from remembra.config import get_settings

log = structlog.get_logger(__name__)

STATE_TTL_SECONDS = 600  # GitHub codes expire after 10 minutes; so does our state
LOGIN_CODE_TTL_SECONDS = 300
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_NOREPLY_SUFFIX = "@users.noreply.github.com"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
ID_TOKEN_LEEWAY_SECONDS = 60
JWKS_TTL_SECONDS = 3600
JWKS_MIN_REFRESH_SECONDS = 30
FROM_PAGES = ("login", "signup")


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    authorize_url: str
    token_url: str
    scope: str


PROVIDERS: dict[str, ProviderSpec] = {
    "github": ProviderSpec(
        id="github",
        name="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scope="user:email",
    ),
    "google": ProviderSpec(
        id="google",
        name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="openid email profile",
    ),
}


class SocialLoginError(Exception):
    """A sign-in that must stop; ``code`` is the stable error shown by the dashboard."""

    CODES = frozenset(
        {
            "access_denied",
            "invalid_state",
            "provider_error",
            "email_unverified",
            "email_not_authoritative",
            "account_exists_unverified",
            "identity_conflict",
            "account_disabled",
            "rate_limited",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            code = "provider_error"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    subject: str
    email: str
    name: str | None


@dataclass(frozen=True)
class LoginState:
    provider: str
    code_verifier: str
    nonce: str
    from_page: str


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


# Factory for provider HTTP calls (tests swap in an httpx.MockTransport client).
http_client_factory: Callable[[], httpx.AsyncClient] = _default_http_client


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def provider_credentials(provider: str) -> tuple[str, str] | None:
    settings = get_settings()
    if provider == "github":
        cid, secret = settings.github_client_id, settings.github_client_secret
    elif provider == "google":
        cid, secret = settings.google_client_id, settings.google_client_secret
    else:
        return None
    if not (cid and cid.strip() and secret and secret.strip()):
        return None
    return cid.strip(), secret.strip()


def provider_enabled(provider: str) -> bool:
    """A provider is live only with its credentials, both public origins and a real JWT secret."""
    settings = get_settings()
    if provider not in PROVIDERS or provider_credentials(provider) is None:
        return False
    if not settings.public_url or not settings.public_dashboard_url or not settings.jwt_secret:
        return False
    # RS256 (Google ID tokens) needs the `cryptography` package.
    return provider != "google" or bool(jwt.algorithms.has_crypto)


def enabled_providers() -> list[ProviderSpec]:
    return [spec for pid, spec in PROVIDERS.items() if provider_enabled(pid)]


def callback_url(provider: str) -> str:
    public_url = get_settings().public_url
    assert public_url, "provider_enabled() guarantees public_url"
    return f"{public_url}/api/v1/auth/oauth/{provider}/callback"


def dashboard_url(path: str, fragment: dict[str, str] | None = None) -> str:
    """An absolute URL on the configured dashboard origin (never request-derived)."""
    base = get_settings().public_dashboard_url
    assert base, "provider_enabled() guarantees public_dashboard_url"
    return f"{base}{path}" + (f"#{urlencode(fragment)}" if fragment else "")


def cookie_name(provider: str) -> str:
    """``__Host-`` prefixed (Secure, Path=/, host-only) whenever the API is served over HTTPS."""
    secure = (get_settings().public_url or "").startswith("https://")
    return f"{'__Host-' if secure else ''}remembra_oauth_{provider}"


def cookie_secure() -> bool:
    return (get_settings().public_url or "").startswith("https://")


# ---------------------------------------------------------------------------
# Storage (created lazily, like the security-state tables)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    UNIQUE (provider, provider_user_id),
    UNIQUE (user_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);

CREATE TABLE IF NOT EXISTS oauth_login_states (
    state_hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    browser_hash TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    nonce TEXT NOT NULL,
    from_page TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_login_codes (
    code_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    new_account INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL
);
"""

_initialized: weakref.WeakSet[Any] = weakref.WeakSet()


async def ensure_schema(db: Any) -> None:
    conn = db.conn
    if conn in _initialized:
        return
    await conn.executescript(_SCHEMA)
    await conn.commit()
    _initialized.add(conn)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now_iso() -> str:
    from remembra.core.time import utcnow

    return utcnow().isoformat()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def create_login_state(db: Any, provider: str, from_page: str) -> tuple[str, str, str, str]:
    """New flow: returns ``(state, browser_secret, code_verifier, nonce)``; stores hashes."""
    await ensure_schema(db)
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 chars of [A-Za-z0-9_-]: valid RFC 7636 verifier
    nonce = secrets.token_urlsafe(32)
    now = time.time()
    await db.conn.execute("DELETE FROM oauth_login_states WHERE expires_at < ?", (now,))
    await db.conn.execute(
        "INSERT INTO oauth_login_states (state_hash, provider, browser_hash, code_verifier, nonce, from_page, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_hash(state), provider, _hash(browser_secret), verifier, nonce, from_page, now + STATE_TTL_SECONDS),
    )
    await db.conn.commit()
    return state, browser_secret, verifier, nonce


async def consume_login_state(db: Any, provider: str, state: str | None, browser_secret: str | None) -> LoginState:
    """Single use: the row is deleted whether or not it validates. Raises ``invalid_state``."""
    await ensure_schema(db)
    if not state or len(state) > 512:
        raise SocialLoginError("invalid_state")
    key = _hash(state)
    cursor = await db.conn.execute(
        "SELECT provider, browser_hash, code_verifier, nonce, from_page, expires_at FROM oauth_login_states WHERE state_hash = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise SocialLoginError("invalid_state")
    deleted = await db.conn.execute("DELETE FROM oauth_login_states WHERE state_hash = ?", (key,))
    await db.conn.commit()
    if (deleted.rowcount or 0) != 1:  # a concurrent callback already used it
        raise SocialLoginError("invalid_state")
    row_provider, browser_hash, verifier, nonce, from_page, expires_at = row
    if row_provider != provider or float(expires_at) < time.time():
        raise SocialLoginError("invalid_state")
    if not browser_secret or not secrets.compare_digest(str(browser_hash), _hash(browser_secret)):
        # Login CSRF: the callback did not come from the browser that started the flow.
        raise SocialLoginError("invalid_state")
    return LoginState(provider=provider, code_verifier=str(verifier), nonce=str(nonce), from_page=str(from_page))


async def peek_login_state_page(db: Any, state: str | None) -> str:
    """Which page (login / signup) a flow started from, for error redirects. Never raises."""
    if not state or len(state) > 512:
        return "login"
    try:
        await ensure_schema(db)
        cursor = await db.conn.execute("SELECT from_page FROM oauth_login_states WHERE state_hash = ?", (_hash(state),))
        row = await cursor.fetchone()
    except Exception:
        return "login"
    return str(row[0]) if row and row[0] in FROM_PAGES else "login"


async def discard_login_state(db: Any, state: str | None) -> None:
    if not state or len(state) > 512:
        return
    await ensure_schema(db)
    await db.conn.execute("DELETE FROM oauth_login_states WHERE state_hash = ?", (_hash(state),))
    await db.conn.commit()


async def issue_login_code(db: Any, user_id: str, provider: str, *, new_account: bool) -> str:
    await ensure_schema(db)
    code = secrets.token_urlsafe(32)
    now = time.time()
    await db.conn.execute("DELETE FROM oauth_login_codes WHERE expires_at < ?", (now,))
    await db.conn.execute(
        "INSERT INTO oauth_login_codes (code_hash, user_id, provider, new_account, expires_at) VALUES (?, ?, ?, ?, ?)",
        (_hash(code), user_id, provider, int(new_account), now + LOGIN_CODE_TTL_SECONDS),
    )
    await db.conn.commit()
    return code


async def peek_login_code(db: Any, code: str) -> dict[str, Any] | None:
    await ensure_schema(db)
    cursor = await db.conn.execute(
        "SELECT user_id, provider, new_account, expires_at FROM oauth_login_codes WHERE code_hash = ?", (_hash(code),)
    )
    row = await cursor.fetchone()
    if row is None or float(row[3]) < time.time():
        return None
    return {"user_id": str(row[0]), "provider": str(row[1]), "new_account": bool(row[2])}


async def consume_login_code(db: Any, code: str) -> bool:
    """Delete the code; True only for the one caller that removed it."""
    await ensure_schema(db)
    cursor = await db.conn.execute("DELETE FROM oauth_login_codes WHERE code_hash = ?", (_hash(code),))
    await db.conn.commit()
    return (cursor.rowcount or 0) == 1


async def list_identities(db: Any, user_id: str) -> list[dict[str, Any]]:
    await ensure_schema(db)
    cursor = await db.conn.execute(
        "SELECT provider, email, created_at, last_login_at FROM user_identities WHERE user_id = ? ORDER BY provider",
        (user_id,),
    )
    return [dict(zip(("provider", "email", "created_at", "last_login_at"), row, strict=True)) for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------


def authorize_url(provider: str, state: str, verifier: str, nonce: str) -> str:
    spec = PROVIDERS[provider]
    creds = provider_credentials(provider)
    assert creds is not None
    params = {
        "client_id": creds[0],
        "redirect_uri": callback_url(provider),
        "scope": spec.scope,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if provider == "github":
        params["allow_signup"] = "true"
    else:
        params["response_type"] = "code"
        params["nonce"] = nonce
        params["prompt"] = "select_account"
    return f"{spec.authorize_url}?{urlencode(params)}"


async def _exchange_code(client: httpx.AsyncClient, provider: str, code: str, verifier: str) -> dict[str, Any]:
    spec = PROVIDERS[provider]
    creds = provider_credentials(provider)
    assert creds is not None
    form = {
        "client_id": creds[0],
        "client_secret": creds[1],
        "code": code,
        "redirect_uri": callback_url(provider),
        "code_verifier": verifier,
    }
    if provider == "google":
        form["grant_type"] = "authorization_code"
    response = await client.post(spec.token_url, data=form, headers={"Accept": "application/json"})
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        log.warning("oauth_token_exchange_failed", provider=provider, status=response.status_code)
        raise SocialLoginError("provider_error")
    # GitHub answers errors with HTTP 200 and an ``error`` field.
    if response.status_code != 200 or payload.get("error"):
        log.warning(
            "oauth_token_exchange_failed",
            provider=provider,
            status=response.status_code,
            error=str(payload.get("error"))[:64] if payload.get("error") else None,
        )
        raise SocialLoginError("provider_error")
    return payload


async def _github_identity(client: httpx.AsyncClient, access_token: str) -> ProviderIdentity:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "Remembra-SignIn",
    }
    user_resp = await client.get(f"{GITHUB_API}/user", headers=headers)
    emails_resp = await client.get(f"{GITHUB_API}/user/emails", headers=headers, params={"per_page": 100})
    if user_resp.status_code != 200 or emails_resp.status_code != 200:
        log.warning("github_profile_fetch_failed", user_status=user_resp.status_code, emails_status=emails_resp.status_code)
        raise SocialLoginError("provider_error")
    try:
        user = user_resp.json()
        emails = emails_resp.json()
    except ValueError as e:
        raise SocialLoginError("provider_error") from e
    if not isinstance(user, dict) or not isinstance(emails, list) or user.get("id") is None:
        raise SocialLoginError("provider_error")
    primary = next((e for e in emails if isinstance(e, dict) and e.get("primary") is True), None)
    email = str((primary or {}).get("email") or "").strip().lower()
    # Only the PRIMARY address, and only when GitHub says it is verified. A
    # private address is fine (it is listed here with scope user:email); a
    # noreply relay address is not a mailbox.
    if not primary or primary.get("verified") is not True or "@" not in email or email.endswith(GITHUB_NOREPLY_SUFFIX):
        raise SocialLoginError("email_unverified")
    name = user.get("name") or user.get("login")
    return ProviderIdentity(provider="github", subject=str(user["id"]), email=email, name=_clean_name(name))


class _JwksCache:
    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.fetched_at = 0.0

    def reset(self) -> None:
        self.keys = {}
        self.fetched_at = 0.0

    async def key_for(self, client: httpx.AsyncClient, kid: str) -> Any:
        now = time.time()
        stale = now - self.fetched_at > JWKS_TTL_SECONDS
        if stale or (kid not in self.keys and now - self.fetched_at > JWKS_MIN_REFRESH_SECONDS):
            await self._refresh(client)
        key = self.keys.get(kid)
        if key is None:
            raise SocialLoginError("provider_error")
        return key

    async def _refresh(self, client: httpx.AsyncClient) -> None:
        response = await client.get(GOOGLE_JWKS_URL)
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200 or not isinstance(data, dict):
            log.warning("google_jwks_fetch_failed", status=response.status_code)
            raise SocialLoginError("provider_error")
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not jwk.get("kid"):
                continue
            try:
                keys[str(jwk["kid"])] = jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
            except jwt.PyJWTError:
                continue
        if not keys:
            raise SocialLoginError("provider_error")
        self.keys = keys
        self.fetched_at = time.time()


google_jwks = _JwksCache()


async def verify_google_id_token(client: httpx.AsyncClient, id_token: str, nonce: str) -> dict[str, Any]:
    """Validate a Google ID token: RS256 signature (JWKS), iss, aud (+azp), exp/iat, nonce."""
    creds = provider_credentials("google")
    assert creds is not None
    client_id = creds[0]
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise SocialLoginError("provider_error") from e
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise SocialLoginError("provider_error")
    key = await google_jwks.key_for(client, str(header["kid"]))
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=client_id,
            leeway=ID_TOKEN_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        log.warning("google_id_token_invalid", reason=type(e).__name__)
        raise SocialLoginError("provider_error") from e
    if claims.get("iss") not in GOOGLE_ISSUERS:
        log.warning("google_id_token_invalid", reason="issuer")
        raise SocialLoginError("provider_error")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != client_id:
        log.warning("google_id_token_invalid", reason="azp")
        raise SocialLoginError("provider_error")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        log.warning("google_id_token_invalid", reason="nonce")
        raise SocialLoginError("invalid_state")
    return claims


def _google_identity(claims: dict[str, Any]) -> ProviderIdentity:
    email = str(claims.get("email") or "").strip().lower()
    if "@" not in email or claims.get("email_verified") not in (True, "true"):
        raise SocialLoginError("email_unverified")
    # Google is authoritative only for Gmail and Workspace (hd) addresses; for
    # any other address "ownership of the third party email account may have
    # since changed", so it cannot prove who owns that mailbox today.
    if not (email.endswith("@gmail.com") or claims.get("hd")):
        raise SocialLoginError("email_not_authoritative")
    return ProviderIdentity(
        provider="google",
        subject=str(claims["sub"]),
        email=email,
        name=_clean_name(claims.get("name")),
    )


async def fetch_identity(provider: str, code: str, state: LoginState) -> ProviderIdentity:
    """Exchange the code (with the PKCE verifier) and return the verified identity."""
    if not code or len(code) > 2048:
        raise SocialLoginError("provider_error")
    try:
        async with http_client_factory() as client:
            tokens = await _exchange_code(client, provider, code, state.code_verifier)
            if provider == "github":
                access_token = tokens.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise SocialLoginError("provider_error")
                return await _github_identity(client, access_token)
            id_token = tokens.get("id_token")
            if not isinstance(id_token, str) or not id_token:
                raise SocialLoginError("provider_error")
            claims = await verify_google_id_token(client, id_token, state.nonce)
            return _google_identity(claims)
    except httpx.HTTPError as e:
        log.warning("oauth_provider_unreachable", provider=provider, error_type=type(e).__name__)
        raise SocialLoginError("provider_error") from e


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    import re

    clean = re.sub(r"<[^>]+>", "", value).strip()
    return clean[:100] or None


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


async def resolve_account(db: Any, identity: ProviderIdentity, client_ip: str) -> tuple[str, bool]:
    """Sign in, link, or create exactly one account for ``identity``. Returns ``(user_id, created)``."""
    await ensure_schema(db)
    for _attempt in range(2):
        try:
            return await _resolve_once(db, identity, client_ip)
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent callback for the same identity or
            # email; the winner's rows are committed now, so resolve again.
            log.info("oauth_resolve_retry", provider=identity.provider)
    raise SocialLoginError("identity_conflict")


async def _resolve_once(db: Any, identity: ProviderIdentity, client_ip: str) -> tuple[str, bool]:
    cursor = await db.conn.execute(
        "SELECT user_id FROM user_identities WHERE provider = ? AND provider_user_id = ?",
        (identity.provider, identity.subject),
    )
    row = await cursor.fetchone()
    if row is not None:
        user = await db.get_user_by_id(str(row[0]))
        if user is not None:
            if not user.get("is_active", True):
                raise SocialLoginError("account_disabled")
            await db.conn.execute(
                "UPDATE user_identities SET email = ?, last_login_at = ? WHERE provider = ? AND provider_user_id = ?",
                (identity.email, _now_iso(), identity.provider, identity.subject),
            )
            await db.conn.commit()
            log.info("oauth_login", provider=identity.provider, user_id=user["id"])
            return str(user["id"]), False
        # The account row is gone (hard-deleted by an operator): drop the dangling link.
        await db.conn.execute(
            "DELETE FROM user_identities WHERE provider = ? AND provider_user_id = ?",
            (identity.provider, identity.subject),
        )
        await db.conn.commit()

    existing = await db.get_user_by_email(identity.email)
    if existing is not None:
        if not existing.get("is_active", True):
            raise SocialLoginError("account_disabled")
        if not existing.get("email_verified"):
            # Never attach a provider to an account whose email nobody has
            # proven: that account may have been pre-registered by someone
            # else with this address (pre-account-takeover).
            log.warning("oauth_link_refused_unverified_account", provider=identity.provider, user_id=existing["id"])
            raise SocialLoginError("account_exists_unverified")
        cursor = await db.conn.execute(
            "SELECT 1 FROM user_identities WHERE user_id = ? AND provider = ?", (existing["id"], identity.provider)
        )
        if await cursor.fetchone() is not None:
            # The account is already linked to a DIFFERENT account at this provider.
            log.warning("oauth_link_refused_other_identity", provider=identity.provider, user_id=existing["id"])
            raise SocialLoginError("identity_conflict")
        now = _now_iso()
        await db.conn.execute(
            "INSERT INTO user_identities (provider, provider_user_id, user_id, email, created_at, last_login_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (identity.provider, identity.subject, existing["id"], identity.email, now, now),
        )
        await db.conn.commit()
        log.info("oauth_identity_linked", provider=identity.provider, user_id=existing["id"])
        return str(existing["id"]), False

    return await _create_account(db, identity, client_ip), True


async def _create_account(db: Any, identity: ProviderIdentity, client_ip: str) -> str:
    from fastapi import HTTPException

    from remembra.auth.users import UserManager
    from remembra.cloud.signup_guard import enforce_signup_rate_limits
    from remembra.core.time import utcnow

    try:
        # Same per-network / per-email-domain limits as password signup.
        enforce_signup_rate_limits(client_ip, identity.email)
    except HTTPException as e:
        raise SocialLoginError("rate_limited") from e

    user_id = UserManager.generate_user_id()
    # No password: a random bcrypt hash nobody knows. "Forgot password" can set one later.
    unusable_hash = UserManager.hash_password(secrets.token_urlsafe(48))
    created_at = utcnow()
    async with db.transaction():
        await db.create_user(
            user_id=user_id,
            email=identity.email,
            password_hash=unusable_hash,
            name=identity.name,
            created_at=created_at,
        )
        await db.conn.execute("UPDATE users SET email_verified = ? WHERE id = ?", (True, user_id))
        await db.conn.execute(
            "INSERT INTO user_identities (provider, provider_user_id, user_id, email, created_at, last_login_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (identity.provider, identity.subject, user_id, identity.email, created_at.isoformat(), created_at.isoformat()),
        )
    log.info("oauth_account_created", provider=identity.provider, user_id=user_id)
    return user_id


# ---------------------------------------------------------------------------
# Access-log redaction
# ---------------------------------------------------------------------------

_SENSITIVE_QUERY_KEYS = frozenset({"code", "state", "token", "access_token", "id_token", "code_verifier"})


def redact_url(url: str) -> str:
    """Replace sensitive query values (OAuth code/state, tokens) with ``REDACTED``."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in _SENSITIVE_QUERY_KEYS for k, _ in pairs):
        return url
    query = urlencode([(k, "REDACTED" if k.lower() in _SENSITIVE_QUERY_KEYS else v) for k, v in pairs])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class AccessLogRedactor(logging.Filter):
    """Strip OAuth codes/states from uvicorn access-log lines (the callback URL carries them)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_url(args[2]), *args[3:])
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactor) for f in logger.filters):
        logger.addFilter(AccessLogRedactor())
