"""Persistent security state: session invalidation, login lockout, TOTP replay, email verification.

The tables live in the main SQLite database but are owned by the security layer
and created lazily (``CREATE TABLE IF NOT EXISTS``) on first use, so no change to
the core schema/migration path is needed.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import weakref
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Per-account lockout policy for password + TOTP verification.
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60
LOCKOUT_DURATION_SECONDS = 15 * 60

EMAIL_VERIFICATION_TTL_HOURS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS security_user_state (
    user_id TEXT PRIMARY KEY,
    tokens_valid_after_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS security_login_attempts (
    account_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    first_failure_at REAL NOT NULL,
    locked_until REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS security_totp_used (
    user_id TEXT NOT NULL,
    time_step INTEGER NOT NULL,
    used_at REAL NOT NULL,
    PRIMARY KEY (user_id, time_step)
);

CREATE TABLE IF NOT EXISTS security_email_verifications (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

_initialized: weakref.WeakSet[Any] = weakref.WeakSet()


async def _ensure_schema(db: Any) -> None:
    conn = db.conn
    if conn in _initialized:
        return
    await conn.executescript(_SCHEMA)
    await conn.commit()
    _initialized.add(conn)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Session invalidation (password change / reset / deactivation)
# ---------------------------------------------------------------------------


async def get_tokens_valid_after_ms(db: Any, user_id: str) -> int:
    """JWTs issued (iat) strictly before this epoch-ms are rejected for the user."""
    await _ensure_schema(db)
    cursor = await db.conn.execute(
        "SELECT tokens_valid_after_ms FROM security_user_state WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def invalidate_user_sessions(db: Any, user_id: str) -> int:
    """Invalidate every JWT issued to ``user_id`` up to now. Returns the cutoff (epoch ms)."""
    await _ensure_schema(db)
    cutoff = int(time.time() * 1000)
    await db.conn.execute(
        """
        INSERT INTO security_user_state (user_id, tokens_valid_after_ms) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET tokens_valid_after_ms = excluded.tokens_valid_after_ms
        """,
        (user_id, cutoff),
    )
    await db.conn.commit()
    log.info("user_sessions_invalidated", user_id=user_id)
    return cutoff


def token_issued_at_ms(payload: dict[str, Any]) -> int:
    """Issue time of a decoded JWT in epoch ms (``iat_ms`` claim, else ``iat`` seconds)."""
    iat_ms = payload.get("iat_ms")
    if isinstance(iat_ms, int | float):
        return int(iat_ms)
    iat = payload.get("iat")
    if isinstance(iat, int | float):
        return int(iat * 1000)
    return 0


# ---------------------------------------------------------------------------
# Per-account lockout
# ---------------------------------------------------------------------------


def account_key(kind: str, identifier: str) -> str:
    """Stable, non-reversible key for an account (never stores raw emails)."""
    return f"{kind}:{_hash(identifier.strip().lower())}"


async def lockout_remaining_seconds(db: Any, key: str) -> int:
    """Seconds until ``key`` may try again (0 when not locked)."""
    await _ensure_schema(db)
    cursor = await db.conn.execute(
        "SELECT locked_until FROM security_login_attempts WHERE account_key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if not row:
        return 0
    remaining = float(row[0]) - time.time()
    return int(remaining) + 1 if remaining > 0 else 0


async def record_failure(db: Any, key: str) -> int:
    """Record a failed attempt; returns lockout seconds now in force (0 if not locked)."""
    await _ensure_schema(db)
    now = time.time()
    cursor = await db.conn.execute(
        "SELECT failures, first_failure_at FROM security_login_attempts WHERE account_key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if row is None or now - float(row[1]) > LOCKOUT_WINDOW_SECONDS:
        failures, first = 1, now
    else:
        failures, first = int(row[0]) + 1, float(row[1])
    locked_until = now + LOCKOUT_DURATION_SECONDS if failures >= LOCKOUT_THRESHOLD else 0.0
    await db.conn.execute(
        """
        INSERT INTO security_login_attempts (account_key, failures, first_failure_at, locked_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(account_key) DO UPDATE SET
            failures = excluded.failures,
            first_failure_at = excluded.first_failure_at,
            locked_until = excluded.locked_until
        """,
        (key, failures, first, locked_until),
    )
    await db.conn.commit()
    if locked_until:
        log.warning("account_locked_out", account_key=key, failures=failures)
        return LOCKOUT_DURATION_SECONDS
    return 0


async def clear_failures(db: Any, key: str) -> None:
    await _ensure_schema(db)
    await db.conn.execute("DELETE FROM security_login_attempts WHERE account_key = ?", (key,))
    await db.conn.commit()


# ---------------------------------------------------------------------------
# TOTP replay protection
# ---------------------------------------------------------------------------


async def claim_totp_step(db: Any, user_id: str, time_step: int) -> bool:
    """Mark a TOTP time-step as consumed. Returns False if it was already used (replay)."""
    await _ensure_schema(db)
    now = time.time()
    # Steps older than 10 minutes can never verify again; keep the table small.
    await db.conn.execute("DELETE FROM security_totp_used WHERE used_at < ?", (now - 600,))
    cursor = await db.conn.execute(
        "INSERT OR IGNORE INTO security_totp_used (user_id, time_step, used_at) VALUES (?, ?, ?)",
        (user_id, time_step, now),
    )
    await db.conn.commit()
    claimed: bool = cursor.rowcount > 0
    return claimed


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


async def create_email_verification(db: Any, user_id: str, email: str) -> str:
    """Create (or replace) a single-use verification token for ``email``; returns the raw token."""
    await _ensure_schema(db)
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS)).isoformat()
    await db.conn.execute(
        """
        INSERT INTO security_email_verifications (user_id, email, token_hash, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            email = excluded.email, token_hash = excluded.token_hash, expires_at = excluded.expires_at
        """,
        (user_id, email.strip().lower(), _hash(token), expires),
    )
    await db.conn.commit()
    return token


async def find_email_verification(db: Any, token: str) -> tuple[str, str] | None:
    """Return ``(user_id, email)`` for an unexpired verification token without consuming it.

    For the token-only confirm path (API-signup tenants have no dashboard
    session). The caller still consumes it with :func:`consume_email_verification`.
    """
    await _ensure_schema(db)
    cursor = await db.conn.execute(
        "SELECT user_id, email, expires_at FROM security_email_verifications WHERE token_hash = ?",
        (_hash(token),),
    )
    row = await cursor.fetchone()
    if not row or datetime.fromisoformat(row[2]) < datetime.now(UTC):
        return None
    return str(row[0]), str(row[1])


async def consume_email_verification(db: Any, user_id: str, email: str, token: str) -> bool:
    """Validate and consume a verification token. The email must still match the account."""
    await _ensure_schema(db)
    cursor = await db.conn.execute(
        "SELECT email, token_hash, expires_at FROM security_email_verifications WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return False
    stored_email, token_hash, expires_at = row[0], row[1], row[2]
    if datetime.fromisoformat(expires_at) < datetime.now(UTC):
        return False
    if stored_email != email.strip().lower():
        return False
    if not secrets.compare_digest(token_hash, _hash(token)):
        return False
    await db.conn.execute("DELETE FROM security_email_verifications WHERE user_id = ?", (user_id,))
    await db.conn.commit()
    return True
