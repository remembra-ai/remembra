"""Persistence for the connector's OAuth 2.1 authorization server.

Tables (created by :meth:`ConnectorStore.init_schema`, like the inbox/spaces
managers do):

- ``oauth_clients``        dynamically registered clients (RFC 7591)
- ``oauth_auth_requests``  in-flight browser authorizations (login/consent)
- ``oauth_grants``         one row per approved connection: user, client,
                           scopes, resource, projects and agent label
- ``oauth_codes``          single-use authorization codes (hashed)
- ``oauth_tokens``         access and refresh tokens (hashed), per grant

Every secret (client secret, CSRF value, code, access and refresh token) is
stored only as a SHA-256 hash; the raw value exists once, in the response
that hands it out. Times are epoch seconds (REAL) so expiry checks are plain
comparisons.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.connector.policy import format_scope, token_hash, verify_pkce

log = structlog.get_logger(__name__)

CODE_TTL_SECONDS = 300
AUTH_REQUEST_TTL_SECONDS = 900
CLIENT_IDLE_PRUNE_SECONDS = 30 * 86400
# A rotated-out refresh token presented again within this window is treated as
# a client retry (lost response, or a proactive and a reactive refresh racing
# — Claude does both), not theft: it gets the SAME successor pair the first
# rotation issued (never an extra one, so a grant has one refresh chain), instead
# of ending the connection. Outside the window, reuse revokes the whole grant.
REFRESH_REUSE_GRACE_SECONDS = 30
_LAST_USED_WRITE_INTERVAL = 60.0

ACCESS_PREFIX = "rmc_at_"
REFRESH_PREFIX = "rmc_rt_"
CODE_PREFIX = "rmc_ac_"
CLIENT_PREFIX = "rmc_client_"


class OAuthError(Exception):
    """An RFC 6749 error response (``error`` + ``error_description``)."""

    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status_code = status_code


@dataclass(frozen=True)
class Grant:
    """An approved connection. Tokens are bound to exactly this grant."""

    grant_id: str
    user_id: str
    client_id: str
    scopes: list[str]
    resource: str
    project_ids: list[str]
    agent_id: str
    created_at_ms: int
    client_name: str = ""
    # When the user proved their password for this connection (login on the
    # sign-in page), which can be up to AUTH_REQUEST_TTL_SECONDS before consent.
    authenticated_at_ms: int | None = None

    @property
    def default_project(self) -> str:
        return self.project_ids[0]

    @property
    def signed_in_at_ms(self) -> int:
        """The moment the account's credentials authorized this grant.

        Session invalidation (password change/reset) is compared against this,
        not the consent time: a sign-in made with the old password must not
        survive a reset that happens before its consent step.
        """
        return self.authenticated_at_ms if self.authenticated_at_ms is not None else self.created_at_ms


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: list[str]

    def response(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": format_scope(self.scopes),
        }


@dataclass
class AuthRequest:
    request_id: str
    client_id: str
    redirect_uri: str
    state: str | None
    code_challenge: str
    scopes: list[str]
    resource: str
    csrf_hash: str
    user_id: str | None
    expires_at: float
    authenticated_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def successor_key(server_secret: str) -> bytes:
    """HMAC key for refresh rotation, derived from a server secret shared by every worker."""
    if not server_secret:
        raise ValueError("a server secret is required to derive the refresh rotation key")
    return hmac.new(server_secret.encode("utf-8"), b"remembra-connector-refresh-successor-v1", hashlib.sha256).digest()


def _derived_secret(key: bytes, prefix: str, label: bytes, parent: str) -> str:
    digest = hmac.new(key, label + b"\0" + parent.encode("utf-8"), hashlib.sha256).digest()
    return prefix + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret_hash TEXT,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    grant_types TEXT NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL
);

CREATE TABLE IF NOT EXISTS oauth_auth_requests (
    request_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    state TEXT,
    code_challenge TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    csrf_hash TEXT NOT NULL,
    user_id TEXT,
    authenticated_at REAL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_grants (
    grant_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    project_ids TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    authenticated_at REAL,
    last_used_at REAL,
    revoked_at REAL,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_grants_user ON oauth_grants(user_id, revoked_at);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_grant ON oauth_tokens(grant_id);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_expiry ON oauth_tokens(expires_at);
"""

# (table, column, declaration) for columns added to existing deployments.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("oauth_auth_requests", "authenticated_at", "REAL"),
    ("oauth_grants", "authenticated_at", "REAL"),
)


class ConnectorStore:
    """OAuth state on the application's SQLite database."""

    def __init__(
        self,
        db: Any,
        *,
        rotation_key: bytes,
        access_ttl_seconds: int = 3600,
        refresh_ttl_seconds: int = 30 * 86400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(rotation_key) < 32:
            raise ValueError("rotation_key must be at least 32 bytes")
        self._db = db
        self._rotation_key = rotation_key
        self.access_ttl = int(access_ttl_seconds)
        self.refresh_ttl = int(refresh_ttl_seconds)
        self._clock = clock

    def now(self) -> float:
        return float(self._clock())

    async def init_schema(self) -> None:
        await self._db.conn.executescript(SCHEMA_SQL)
        # Columns added after the first schema; CREATE TABLE IF NOT EXISTS
        # leaves an existing table as it was.
        for table, column, decl in _ADDED_COLUMNS:
            cursor = await self._db.conn.execute(f"PRAGMA table_info({table})")
            if column not in {row[1] for row in await cursor.fetchall()}:
                await self._db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await self._db.conn.commit()

    # ------------------------------------------------------------------
    # Clients (RFC 7591)
    # ------------------------------------------------------------------

    async def register_client(
        self,
        *,
        client_name: str,
        redirect_uris: list[str],
        grant_types: list[str],
        token_endpoint_auth_method: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Create a client; returns (client row, raw secret or None for public clients)."""
        await self.prune()
        client_id = _new_secret(CLIENT_PREFIX)
        secret = None if token_endpoint_auth_method == "none" else secrets.token_urlsafe(32)
        now = self.now()
        await self._db.conn.execute(
            """
            INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, redirect_uris,
                                       grant_types, token_endpoint_auth_method, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                token_hash(secret) if secret else None,
                client_name,
                json.dumps(redirect_uris),
                json.dumps(grant_types),
                token_endpoint_auth_method,
                now,
            ),
        )
        await self._db.conn.commit()
        client = await self.get_client(client_id)
        assert client is not None
        return client, secret

    async def get_client(self, client_id: str | None) -> dict[str, Any] | None:
        if not client_id:
            return None
        cursor = await self._db.conn.execute(
            """
            SELECT client_id, client_secret_hash, client_name, redirect_uris, grant_types,
                   token_endpoint_auth_method, created_at
            FROM oauth_clients WHERE client_id = ?
            """,
            (client_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "client_id": row[0],
            "client_secret_hash": row[1],
            "client_name": row[2],
            "redirect_uris": json.loads(row[3]),
            "grant_types": json.loads(row[4]),
            "token_endpoint_auth_method": row[5],
            "client_id_issued_at": int(row[6]),
        }

    @staticmethod
    def client_secret_valid(client: dict[str, Any], secret: str | None) -> bool:
        """Public clients need no secret; confidential ones must present theirs."""
        stored = client.get("client_secret_hash")
        if not stored:
            return True
        if not secret:
            return False
        return hmac.compare_digest(stored, token_hash(secret))

    async def _touch_client(self, client_id: str) -> None:
        await self._db.conn.execute("UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?", (self.now(), client_id))

    # ------------------------------------------------------------------
    # Browser authorization requests
    # ------------------------------------------------------------------

    async def create_auth_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str | None,
        code_challenge: str,
        scopes: list[str],
        resource: str,
    ) -> tuple[str, str]:
        """Persist an authorization request; returns (request_id, raw CSRF value)."""
        request_id = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(32)
        now = self.now()
        # Unauthenticated callers create these rows; drop expired ones as we go.
        await self._db.conn.execute("DELETE FROM oauth_auth_requests WHERE expires_at <= ?", (now,))
        await self._db.conn.execute(
            """
            INSERT INTO oauth_auth_requests (request_id, client_id, redirect_uri, state, code_challenge,
                                             scope, resource, csrf_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                client_id,
                redirect_uri,
                state,
                code_challenge,
                format_scope(scopes),
                resource,
                token_hash(csrf),
                now,
                now + AUTH_REQUEST_TTL_SECONDS,
            ),
        )
        await self._db.conn.commit()
        return request_id, csrf

    async def get_auth_request(self, request_id: str | None) -> AuthRequest | None:
        if not request_id or len(request_id) > 128:
            return None
        cursor = await self._db.conn.execute(
            """
            SELECT request_id, client_id, redirect_uri, state, code_challenge, scope, resource,
                   csrf_hash, user_id, expires_at, authenticated_at
            FROM oauth_auth_requests WHERE request_id = ? AND expires_at > ?
            """,
            (request_id, self.now()),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return AuthRequest(
            request_id=row[0],
            client_id=row[1],
            redirect_uri=row[2],
            state=row[3],
            code_challenge=row[4],
            scopes=row[5].split(),
            resource=row[6],
            csrf_hash=row[7],
            user_id=row[8],
            expires_at=float(row[9]),
            authenticated_at=float(row[10]) if row[10] is not None else None,
        )

    @staticmethod
    def csrf_valid(req: AuthRequest, csrf: str | None) -> bool:
        return bool(csrf) and hmac.compare_digest(req.csrf_hash, token_hash(csrf or ""))

    async def set_auth_request_user(self, request_id: str, user_id: str) -> None:
        """Bind the request to the user who just signed in, and when they did."""
        await self._db.conn.execute(
            "UPDATE oauth_auth_requests SET user_id = ?, authenticated_at = ? WHERE request_id = ?",
            (user_id, self.now(), request_id),
        )
        await self._db.conn.commit()

    async def delete_auth_request(self, request_id: str) -> bool:
        """Consume a request. False when another submission already consumed it."""
        cursor = await self._db.conn.execute("DELETE FROM oauth_auth_requests WHERE request_id = ?", (request_id,))
        await self._db.conn.commit()
        return bool(cursor.rowcount)

    # ------------------------------------------------------------------
    # Grants and authorization codes
    # ------------------------------------------------------------------

    async def create_grant_with_code(
        self,
        *,
        user_id: str,
        client_id: str,
        scopes: list[str],
        resource: str,
        project_ids: list[str],
        agent_id: str,
        redirect_uri: str,
        code_challenge: str,
        authenticated_at: float,
    ) -> str:
        """Record an approved connection and return a single-use authorization code.

        ``authenticated_at`` is when the user signed in for this request; it is
        what session invalidation is checked against for the grant's lifetime.
        """
        grant_id = "grant_" + secrets.token_hex(12)
        code = _new_secret(CODE_PREFIX)
        now = self.now()
        async with self._db.transaction():
            await self._db.conn.execute(
                """
                INSERT INTO oauth_grants (grant_id, user_id, client_id, scope, resource, project_ids,
                                          agent_id, created_at, authenticated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    user_id,
                    client_id,
                    format_scope(scopes),
                    resource,
                    json.dumps(project_ids),
                    agent_id,
                    now,
                    authenticated_at,
                ),
            )
            await self._db.conn.execute(
                """
                INSERT INTO oauth_codes (code_hash, grant_id, client_id, redirect_uri, code_challenge, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token_hash(code), grant_id, client_id, redirect_uri, code_challenge, now + CODE_TTL_SECONDS),
            )
        return code

    async def _grant_row(self, grant_id: str) -> tuple[Grant, float | None] | None:
        cursor = await self._db.conn.execute(
            """
            SELECT g.grant_id, g.user_id, g.client_id, g.scope, g.resource, g.project_ids, g.agent_id,
                   g.created_at, g.revoked_at, c.client_name, g.authenticated_at
            FROM oauth_grants g LEFT JOIN oauth_clients c ON c.client_id = g.client_id
            WHERE g.grant_id = ?
            """,
            (grant_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        grant = Grant(
            grant_id=row[0],
            user_id=row[1],
            client_id=row[2],
            scopes=row[3].split(),
            resource=row[4],
            project_ids=json.loads(row[5]),
            agent_id=row[6],
            created_at_ms=int(float(row[7]) * 1000),
            client_name=row[9] or "",
            authenticated_at_ms=int(float(row[10]) * 1000) if row[10] is not None else None,
        )
        return grant, (float(row[8]) if row[8] is not None else None)

    def _successor_of(self, refresh_token: str) -> tuple[str, str]:
        """The (access, refresh) pair a rotation of ``refresh_token`` issues.

        Derived with a server-side HMAC from the presented token, so a retry of
        the same refresh returns exactly the same pair: rotation stays a single
        chain per grant while nothing but hashes is stored. Unpredictable
        without both the server key and the parent refresh token.
        """
        key = self._rotation_key
        return (
            _derived_secret(key, ACCESS_PREFIX, b"access", refresh_token),
            _derived_secret(key, REFRESH_PREFIX, b"refresh", refresh_token),
        )

    async def _issue_pair(self, grant: Grant, pair: tuple[str, str] | None = None) -> TokenPair:
        """Insert an access + refresh token for ``grant`` (caller holds the transaction).

        Random unless ``pair`` supplies the (access, refresh) values to store.
        """
        access, refresh = pair or (_new_secret(ACCESS_PREFIX), _new_secret(REFRESH_PREFIX))
        now = self.now()
        await self._db.conn.execute(
            "INSERT INTO oauth_tokens (token_hash, grant_id, client_id, kind, created_at, expires_at) "
            "VALUES (?, ?, ?, 'access', ?, ?)",
            (token_hash(access), grant.grant_id, grant.client_id, now, now + self.access_ttl),
        )
        await self._db.conn.execute(
            "INSERT INTO oauth_tokens (token_hash, grant_id, client_id, kind, created_at, expires_at) "
            "VALUES (?, ?, ?, 'refresh', ?, ?)",
            (token_hash(refresh), grant.grant_id, grant.client_id, now, now + self.refresh_ttl),
        )
        return TokenPair(access_token=access, refresh_token=refresh, expires_in=self.access_ttl, scopes=grant.scopes)

    async def _revoke_grant_tx(self, grant_id: str, reason: str) -> None:
        now = self.now()
        await self._db.conn.execute(
            "UPDATE oauth_grants SET revoked_at = COALESCE(revoked_at, ?), revoke_reason = COALESCE(revoke_reason, ?) "
            "WHERE grant_id = ?",
            (now, reason, grant_id),
        )
        await self._db.conn.execute(
            "UPDATE oauth_tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
            (now, grant_id),
        )
        await self._db.conn.execute("UPDATE oauth_codes SET used_at = COALESCE(used_at, ?) WHERE grant_id = ?", (now, grant_id))

    async def exchange_code(
        self, *, code: str, client_id: str, redirect_uri: str | None, code_verifier: str | None
    ) -> tuple[Grant, TokenPair]:
        """authorization_code grant. Single use: replaying a code revokes its grant."""
        now = self.now()
        error: OAuthError | None = None
        result: tuple[Grant, TokenPair] | None = None
        # Errors are raised only after the transaction block so the revocation
        # a replay triggers is committed, not rolled back with the exception.
        async with self._db.transaction():
            cursor = await self._db.conn.execute(
                "SELECT grant_id, client_id, redirect_uri, code_challenge, expires_at, used_at "
                "FROM oauth_codes WHERE code_hash = ?",
                (token_hash(code),),
            )
            row = await cursor.fetchone()
            if not row:
                error = OAuthError("invalid_grant", "Unknown authorization code.")
            else:
                grant_id, code_client, code_redirect, challenge, expires_at, used_at = row
                if used_at is not None:
                    # RFC 6749 4.1.2 / OAuth 2.1: a replayed code means it leaked.
                    await self._revoke_grant_tx(grant_id, "authorization_code_replayed")
                    log.warning("oauth_code_replay_revoked_grant", grant_id=grant_id)
                    error = OAuthError("invalid_grant", "Authorization code was already used.")
                elif code_client != client_id:
                    error = OAuthError("invalid_grant", "Authorization code was issued to another client.")
                elif float(expires_at) <= now:
                    error = OAuthError("invalid_grant", "Authorization code expired.")
                elif redirect_uri is not None and redirect_uri != code_redirect:
                    error = OAuthError("invalid_grant", "redirect_uri does not match the authorization request.")
                elif not verify_pkce(code_verifier, challenge):
                    error = OAuthError("invalid_grant", "PKCE verification failed.")
                else:
                    await self._db.conn.execute("UPDATE oauth_codes SET used_at = ? WHERE code_hash = ?", (now, token_hash(code)))
                    found = await self._grant_row(grant_id)
                    if found is None or found[1] is not None:
                        error = OAuthError("invalid_grant", "The authorization was revoked.")
                    else:
                        grant = found[0]
                        result = (grant, await self._issue_pair(grant))
                        await self._touch_client(client_id)
        if error is not None:
            raise error
        assert result is not None
        return result

    async def refresh(self, *, refresh_token: str, client_id: str, scope: list[str] | None) -> tuple[Grant, TokenPair]:
        """refresh_token grant with rotation and reuse detection."""
        now = self.now()
        error: OAuthError | None = None
        result: tuple[Grant, TokenPair] | None = None
        async with self._db.transaction():
            cursor = await self._db.conn.execute(
                "SELECT grant_id, client_id, expires_at, used_at, revoked_at "
                "FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                (token_hash(refresh_token),),
            )
            row = await cursor.fetchone()
            if not row:
                error = OAuthError("invalid_grant", "Unknown refresh token.")
            else:
                grant_id, token_client, expires_at, used_at, revoked_at = row
                if token_client != client_id:
                    error = OAuthError("invalid_grant", "Refresh token was issued to another client.")
                elif used_at is not None and now - float(used_at) > REFRESH_REUSE_GRACE_SECONDS:
                    # A rotated-out token came back: the client or an attacker holds
                    # a stale copy. End the whole connection (OAuth 2.1 4.3.1).
                    await self._revoke_grant_tx(grant_id, "refresh_token_reused")
                    log.warning("oauth_refresh_reuse_revoked_grant", grant_id=grant_id)
                    error = OAuthError("invalid_grant", "Refresh token was already used.")
                elif revoked_at is not None or float(expires_at) <= now:
                    error = OAuthError("invalid_grant", "Refresh token expired or revoked.")
                else:
                    found = await self._grant_row(grant_id)
                    if found is None or found[1] is not None:
                        error = OAuthError("invalid_grant", "The authorization was revoked.")
                    elif scope is not None and not set(scope) <= set(found[0].scopes):
                        error = OAuthError("invalid_scope", "Requested scope exceeds the original grant.")
                    else:
                        grant = found[0]
                        successor = self._successor_of(refresh_token)
                        if used_at is None:
                            await self._db.conn.execute(
                                "UPDATE oauth_tokens SET used_at = ? WHERE token_hash = ?", (now, token_hash(refresh_token))
                            )
                            result = (grant, await self._issue_pair(grant, successor))
                            await self._touch_client(client_id)
                        else:
                            retried = await self._reissue_successor(grant, successor, now)
                            if retried is None:
                                # The successor was already rotated (or is gone): the
                                # presenter holds a stale token beside a live chain.
                                await self._revoke_grant_tx(grant_id, "refresh_token_reused")
                                log.warning("oauth_refresh_reuse_revoked_grant", grant_id=grant_id, within_grace=True)
                                error = OAuthError("invalid_grant", "Refresh token was already used.")
                            else:
                                log.warning("oauth_refresh_retry_within_grace", grant_id=grant_id)
                                result = (grant, retried)
                                await self._touch_client(client_id)
        if error is not None:
            raise error
        assert result is not None
        return result

    async def _reissue_successor(self, grant: Grant, successor: tuple[str, str], now: float) -> TokenPair | None:
        """The pair the first rotation issued, for a retry inside the grace window.

        None when that pair's refresh token has since been used, revoked, or is
        missing: the retry can't be answered without forking the chain.
        """
        access, refresh = successor
        cursor = await self._db.conn.execute(
            "SELECT token_hash, kind, expires_at, used_at, revoked_at FROM oauth_tokens "
            "WHERE token_hash IN (?, ?) AND grant_id = ?",
            (token_hash(access), token_hash(refresh), grant.grant_id),
        )
        rows = {row[1]: row for row in await cursor.fetchall()}
        live_refresh = rows.get("refresh")
        live_access = rows.get("access")
        if (
            live_refresh is None
            or live_access is None
            or live_refresh[3] is not None
            or live_refresh[4] is not None
            or float(live_refresh[2]) <= now
        ):
            return None
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=max(1, int(float(live_access[2]) - now)),
            scopes=grant.scopes,
        )

    async def grant_for_access_token(self, access_token: str) -> Grant | None:
        """Grant behind a live access token (not expired, token and grant not revoked)."""
        if not access_token.startswith(ACCESS_PREFIX):
            return None
        now = self.now()
        cursor = await self._db.conn.execute(
            "SELECT grant_id, expires_at, revoked_at FROM oauth_tokens WHERE token_hash = ? AND kind = 'access'",
            (token_hash(access_token),),
        )
        row = await cursor.fetchone()
        if not row or row[2] is not None or float(row[1]) <= now:
            return None
        found = await self._grant_row(row[0])
        if found is None or found[1] is not None:
            return None
        return found[0]

    async def touch_grant(self, grant_id: str) -> None:
        """Record use, at most once a minute per grant (avoids a write per MCP call)."""
        now = self.now()
        await self._db.conn.execute(
            "UPDATE oauth_grants SET last_used_at = ? WHERE grant_id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
            (now, grant_id, now - _LAST_USED_WRITE_INTERVAL),
        )
        await self._db.conn.commit()

    async def revoke_token(self, token: str, client_id: str) -> None:
        """RFC 7009. Revoking a refresh token ends the whole connection; an access
        token only itself. Tokens of other clients are ignored silently."""
        cursor = await self._db.conn.execute(
            "SELECT grant_id, client_id, kind FROM oauth_tokens WHERE token_hash = ?",
            (token_hash(token),),
        )
        row = await cursor.fetchone()
        if not row or row[1] != client_id:
            return
        async with self._db.transaction():
            if row[2] == "refresh":
                await self._revoke_grant_tx(row[0], "revoked_by_client")
            else:
                await self._db.conn.execute(
                    "UPDATE oauth_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                    (self.now(), token_hash(token)),
                )

    # ------------------------------------------------------------------
    # User-facing connection management
    # ------------------------------------------------------------------

    async def list_grants(self, user_id: str) -> list[dict[str, Any]]:
        cursor = await self._db.conn.execute(
            """
            SELECT g.grant_id, g.client_id, c.client_name, g.scope, g.project_ids, g.agent_id,
                   g.created_at, g.last_used_at
            FROM oauth_grants g LEFT JOIN oauth_clients c ON c.client_id = g.client_id
            WHERE g.user_id = ? AND g.revoked_at IS NULL
            ORDER BY g.created_at DESC
            """,
            (user_id,),
        )
        return [
            {
                "connection_id": r[0],
                "client_id": r[1],
                "client_name": r[2] or "",
                "scopes": r[3].split(),
                "project_ids": json.loads(r[4]),
                "agent_id": r[5],
                "created_at": _iso(r[6]),
                "last_used_at": _iso(r[7]),
            }
            for r in await cursor.fetchall()
        ]

    async def revoke_grant(self, user_id: str, grant_id: str, reason: str = "revoked_by_user") -> bool:
        cursor = await self._db.conn.execute(
            "SELECT 1 FROM oauth_grants WHERE grant_id = ? AND user_id = ? AND revoked_at IS NULL",
            (grant_id, user_id),
        )
        if not await cursor.fetchone():
            return False
        async with self._db.transaction():
            await self._revoke_grant_tx(grant_id, reason)
        return True

    async def user_projects(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Projects the user has memories in, most recently active first."""
        cursor = await self._db.conn.execute(
            """
            SELECT project_id, COUNT(*) AS n, MAX(created_at) AS last
            FROM memories WHERE user_id = ?
            GROUP BY project_id ORDER BY last DESC LIMIT ?
            """,
            (user_id, limit),
        )
        return [{"project_id": r[0], "memories": int(r[1]), "last_activity": r[2]} for r in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    async def prune(self) -> None:
        """Delete expired requests/codes/tokens and idle clients with no live grant."""
        now = self.now()
        conn = self._db.conn
        await conn.execute("DELETE FROM oauth_auth_requests WHERE expires_at <= ?", (now,))
        await conn.execute("DELETE FROM oauth_codes WHERE expires_at <= ?", (now - 86400,))
        await conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now - 86400,))
        await conn.execute(
            """
            DELETE FROM oauth_clients
            WHERE COALESCE(last_used_at, created_at) < ?
              AND client_id NOT IN (SELECT client_id FROM oauth_grants WHERE revoked_at IS NULL)
            """,
            (now - CLIENT_IDLE_PRUNE_SECONDS,),
        )
        await conn.commit()
