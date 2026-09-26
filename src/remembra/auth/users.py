"""User model and password hashing for authentication."""

import hashlib
import hmac
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import bcrypt
import jwt
import structlog

from remembra.security import state as security_state
from remembra.security.encryption import FieldEncryptor
from remembra.storage.database import Database

log = structlog.get_logger(__name__)

# JWT settings
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24  # 24 hours (tightened from 7 days - March 22, 2026)
PASSWORD_RESET_EXPIRATION_HOURS = 24

_TOTP_KEY_CONTEXT = "remembra-totp-secret-v1"


@lru_cache(maxsize=4)
def _totp_encryptor(key_material: str) -> FieldEncryptor:
    """AES-256-GCM encryptor for TOTP secrets (key derivation is expensive, so cache it)."""
    return FieldEncryptor(key=f"{_TOTP_KEY_CONTEXT}:{key_material}")


def _totp_key_material() -> str:
    """Key for TOTP-secret encryption: the at-rest encryption key if set, else the JWT secret."""
    from remembra.config import get_settings

    settings = get_settings()
    return settings.encryption_key or settings.jwt_secret


@dataclass
class User:
    """Represents a registered user."""

    id: str
    email: str
    name: str | None
    created_at: datetime
    email_verified: bool = False
    is_active: bool = True
    totp_enabled: bool = False


@dataclass
class UserWithPassword(User):
    """User with password hash (internal use only)."""

    password_hash: str = ""


class UserManager:
    """
    Manages user lifecycle: registration, authentication, password reset.
    """

    def __init__(self, db: Database, jwt_secret: str) -> None:
        self.db = db
        self.jwt_secret = jwt_secret

    @staticmethod
    def generate_user_id() -> str:
        """Generate a unique user ID."""
        return f"user_{secrets.token_urlsafe(16)}"

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Verify a password against its hash."""
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except Exception:
            return False

    @staticmethod
    def generate_reset_token() -> str:
        """Generate a password reset token."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_token_deterministic(token: str) -> str:
        """
        Create a deterministic hash for token blacklisting.
        Uses SHA-256 instead of bcrypt since we need consistent hashes.
        """
        return hashlib.sha256(token.encode()).hexdigest()

    def create_jwt_token(self, user_id: str, email: str) -> str:
        """Create a JWT access token."""
        payload = {
            "sub": user_id,
            "email": email,
            "iat": datetime.now(UTC),
            # Millisecond issue time so a password change/logout-all can reject
            # every token issued before it, even within the same second.
            "iat_ms": int(time.time() * 1000),
            "exp": datetime.now(UTC) + timedelta(hours=JWT_EXPIRATION_HOURS),
            "type": "access",
        }
        return jwt.encode(payload, self.jwt_secret, algorithm=JWT_ALGORITHM)

    def verify_jwt_token(self, token: str) -> dict[str, Any] | None:
        """Verify and decode a JWT token. Returns payload or None if invalid."""
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[JWT_ALGORITHM])
            if payload.get("type") != "access":
                return None
            return payload
        except jwt.ExpiredSignatureError:
            log.debug("jwt_token_expired")
            return None
        except jwt.InvalidTokenError as e:
            log.debug("jwt_token_invalid", error=str(e))
            return None

    async def create_user(
        self,
        email: str,
        password: str,
        name: str | None = None,
    ) -> tuple[User | None, str | None]:
        """
        Create a new user account.

        Returns (User, None) on success, (None, error_message) on failure.
        """
        # Check if email already exists
        existing = await self.db.get_user_by_email(email)
        if existing:
            return None, "Email already registered"

        # Validate password
        if len(password) < 8:
            return None, "Password must be at least 8 characters"

        user_id = self.generate_user_id()
        password_hash = self.hash_password(password)
        created_at = datetime.now(UTC)

        await self.db.create_user(
            user_id=user_id,
            email=email.lower().strip(),
            password_hash=password_hash,
            name=name,
            created_at=created_at,
        )

        log.info("user_created", user_id=user_id)

        return User(
            id=user_id,
            email=email.lower().strip(),
            name=name,
            created_at=created_at,
            email_verified=False,
            is_active=True,
        ), None

    async def authenticate(
        self,
        email: str,
        password: str,
    ) -> tuple[User | None, str | None, str | None]:
        """
        Authenticate a user with email and password.

        Returns (User, jwt_token, None) on success, (None, None, error_message) on failure.
        """
        user_data = await self.db.get_user_by_email(email.lower().strip())

        if not user_data:
            log.warning("login_failed_user_not_found")
            return None, None, "Invalid email or password"

        if not user_data.get("is_active", True):
            log.warning("login_failed_user_inactive", user_id=user_data["id"])
            return None, None, "Account is deactivated"

        if not self.verify_password(password, user_data["password_hash"]):
            log.warning("login_failed_wrong_password", user_id=user_data["id"])
            return None, None, "Invalid email or password"

        # Update last login
        await self.db.update_user_last_login(user_data["id"])

        created_at_raw = user_data["created_at"]
        created_at = datetime.fromisoformat(created_at_raw) if isinstance(created_at_raw, str) else created_at_raw
        user = User(
            id=user_data["id"],
            email=user_data["email"],
            name=user_data.get("name"),
            created_at=created_at,
            email_verified=user_data.get("email_verified", False),
            is_active=user_data.get("is_active", True),
        )

        token = self.create_jwt_token(user.id, user.email)

        log.info("user_logged_in", user_id=user.id)

        return user, token, None

    async def get_user_by_id(self, user_id: str) -> User | None:
        """Get user by ID."""
        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return None

        created_at_raw = user_data["created_at"]
        created_at = datetime.fromisoformat(created_at_raw) if isinstance(created_at_raw, str) else created_at_raw
        return User(
            id=user_data["id"],
            email=user_data["email"],
            name=user_data.get("name"),
            created_at=created_at,
            email_verified=user_data.get("email_verified", False),
            is_active=user_data.get("is_active", True),
        )

    async def create_password_reset_token(self, email: str) -> tuple[str | None, str | None]:
        """
        Create a password reset token for a user.

        Returns (reset_token, None) on success, (None, error_message) on failure.
        """
        user_data = await self.db.get_user_by_email(email.lower().strip())

        if not user_data:
            # Don't reveal if email exists or not for security
            log.debug("password_reset_requested_unknown_email")
            return None, None  # Return None for both - frontend shows generic message

        reset_token = self.generate_reset_token()
        expires_at = datetime.now(UTC) + timedelta(hours=PASSWORD_RESET_EXPIRATION_HOURS)

        # Hash the reset token before storing
        token_hash = self.hash_password(reset_token)

        await self.db.save_password_reset_token(
            user_id=user_data["id"],
            token_hash=token_hash,
            expires_at=expires_at,
        )

        log.info("password_reset_token_created", user_id=user_data["id"])

        return reset_token, None

    async def reset_password(
        self,
        email: str,
        token: str,
        new_password: str,
    ) -> tuple[bool, str | None]:
        """
        Reset a user's password using a reset token.

        Returns (True, None) on success, (False, error_message) on failure.
        """
        if len(new_password) < 8:
            return False, "Password must be at least 8 characters"

        user_data = await self.db.get_user_by_email(email.lower().strip())
        if not user_data:
            return False, "Invalid reset request"

        # Get the reset token record
        reset_record = await self.db.get_password_reset_token(user_data["id"])
        if not reset_record:
            return False, "Invalid or expired reset token"

        # Check expiration
        expires_raw = reset_record["expires_at"]
        expires_at = datetime.fromisoformat(expires_raw) if isinstance(expires_raw, str) else expires_raw
        if expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
            await self.db.delete_password_reset_token(user_data["id"])
            return False, "Reset token has expired"

        # Verify token
        if not self.verify_password(token, reset_record["token_hash"]):
            return False, "Invalid reset token"

        # Update password
        new_password_hash = self.hash_password(new_password)
        await self.db.update_user_password(user_data["id"], new_password_hash)

        # Delete the used reset token
        await self.db.delete_password_reset_token(user_data["id"])

        if not user_data.get("email_verified"):
            # The token was only ever sent to the account address, so using it
            # proves control of that mailbox. Nobody had proven that before, so
            # whoever created this account (and its keys, 2FA, connector
            # grants, webhooks) may not be the mailbox owner: someone can
            # pre-register a victim's address and wait. Treat the mailbox
            # owner as a new owner and clear every credential set up before.
            await reset_credentials_for_new_owner(self.db, user_data["id"])
            # The email is now verified (which also lets Sign in with Google
            # link to it), unless another account already verified the same
            # address: one free account per verified email.
            if await email_verified_on_another_account(self.db, user_data["email"], exclude_user_id=user_data["id"]):
                log.warning("password_reset_email_verified_elsewhere", user_id=user_data["id"])
            else:
                await self.db.update_user_email_verified(user_data["id"], True)

        # A reset means the old password may be compromised: kill every session.
        await security_state.invalidate_user_sessions(self.db, user_data["id"])

        log.info("password_reset_successful", user_id=user_data["id"])

        return True, None

    async def invalidate_token(self, user_id: str, token: str) -> bool:
        """
        Invalidate a JWT token (logout).

        For stateless JWT, we add it to a blacklist with expiration.
        """
        # Verify token first to get expiration
        payload = self.verify_jwt_token(token)
        if not payload:
            return False

        # Add to blacklist using deterministic hash (not bcrypt!)
        await self.db.add_token_to_blacklist(
            token_hash=self.hash_token_deterministic(token),
            user_id=user_id,
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

        log.info("user_logged_out", user_id=user_id)
        return True

    async def is_token_blacklisted(self, token: str) -> bool:
        """Check if a token is blacklisted."""
        token_hash = self.hash_token_deterministic(token)
        return await self.db.is_token_blacklisted(token_hash)

    async def update_profile(
        self,
        user_id: str,
        name: str | None = None,
    ) -> tuple[User | None, str | None]:
        """
        Update user profile information.

        Returns (User, None) on success, (None, error_message) on failure.
        """
        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return None, "User not found"

        success = await self.db.update_user_profile(user_id, name=name)
        if not success:
            return None, "Failed to update profile"

        # Return updated user
        updated_user = await self.get_user_by_id(user_id)

        log.info("user_profile_updated", user_id=user_id)

        return updated_user, None

    async def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> tuple[bool, str | None]:
        """
        Change user's password (requires current password verification).

        Returns (True, None) on success, (False, error_message) on failure.
        """
        if len(new_password) < 8:
            return False, "New password must be at least 8 characters"

        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return False, "User not found"

        # Verify current password
        if not self.verify_password(current_password, user_data["password_hash"]):
            log.warning("password_change_failed_wrong_password", user_id=user_id)
            return False, "Current password is incorrect"

        # Update password
        new_password_hash = self.hash_password(new_password)
        await self.db.update_user_password(user_id, new_password_hash)

        # Invalidate all previously issued tokens (including the caller's).
        await security_state.invalidate_user_sessions(self.db, user_id)

        log.info("password_changed", user_id=user_id)

        return True, None

    async def delete_account(
        self,
        user_id: str,
        password: str,
        before_deactivate: Callable[[], Awaitable[str | None]] | None = None,
    ) -> tuple[bool, str | None]:
        """
        Deactivate user account (soft delete).

        Requires password confirmation for security. ``before_deactivate``
        runs after the password is checked and before anything changes (the
        route cancels a paid subscription there); an error message it returns
        stops the deletion with that message.
        Returns (True, None) on success, (False, error_message) on failure.
        """
        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return False, "User not found"

        # Verify password
        if not self.verify_password(password, user_data["password_hash"]):
            log.warning("account_deletion_failed_wrong_password", user_id=user_id)
            return False, "Password is incorrect"

        if before_deactivate is not None:
            refusal = await before_deactivate()
            if refusal:
                return False, refusal

        # Deactivate account
        success = await self.db.deactivate_user(user_id)
        if not success:
            return False, "Failed to deactivate account"

        # Cut off API access too: revoke keys and invalidate every session.
        await revoke_user_access(self.db, user_id)

        log.info("account_deactivated", user_id=user_id)

        return True, None

    # -----------------------------------------------------------------------
    # Two-Factor Authentication (2FA) Methods
    # -----------------------------------------------------------------------

    async def setup_totp(self, user_id: str) -> tuple[str | None, str | None, str | None]:
        """
        Generate a TOTP secret for 2FA setup.

        Returns (secret, provisioning_uri, None) on success,
        (None, None, error_message) on failure.
        """
        try:
            import pyotp
        except ImportError:
            return None, None, "2FA not available - pyotp not installed"

        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return None, None, "User not found"

        # Generate new secret
        secret = pyotp.random_base32()

        # Generate provisioning URI for QR code
        totp = pyotp.TOTP(secret)
        provisioning_uri = totp.provisioning_uri(name=user_data["email"], issuer_name="Remembra")

        # Store secret encrypted at rest (not yet enabled)
        await self.db.save_totp_secret(user_id, self.encrypt_totp_secret(secret))

        log.info("totp_setup_initiated", user_id=user_id)

        return secret, provisioning_uri, None

    async def enable_totp(self, user_id: str, code: str) -> tuple[bool, str | None]:
        """
        Verify TOTP code and enable 2FA for user.

        Returns (True, None) on success, (False, error_message) on failure.
        """
        try:
            import pyotp
        except ImportError:
            return False, "2FA not available - pyotp not installed"

        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return False, "User not found"

        secret = self.decrypt_totp_secret(user_data.get("totp_secret"))
        if not secret:
            return False, "2FA setup not initiated. Call setup first."

        # Verify the code (single use: a code cannot be replayed)
        step = self._match_totp_step(pyotp.TOTP(secret), code)
        if step is None or not await security_state.claim_totp_step(self.db, user_id, step):
            return False, "Invalid verification code"

        # Enable 2FA
        await self.db.enable_totp(user_id)

        log.info("totp_enabled", user_id=user_id)

        return True, None

    async def disable_totp(self, user_id: str, password: str) -> tuple[bool, str | None]:
        """
        Disable 2FA for user (requires password confirmation).

        Returns (True, None) on success, (False, error_message) on failure.
        """
        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return False, "User not found"

        # Verify password
        if not self.verify_password(password, user_data["password_hash"]):
            return False, "Password is incorrect"

        # Disable 2FA
        await self.db.disable_totp(user_id)

        log.info("totp_disabled", user_id=user_id)

        return True, None

    async def verify_totp(self, user_id: str, code: str) -> bool:
        """
        Verify a TOTP code for login.

        Returns True if code is valid, False otherwise.
        """
        try:
            import pyotp
        except ImportError:
            return False

        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return False

        stored = user_data.get("totp_secret")
        secret = self.decrypt_totp_secret(stored)
        if not secret:
            return False

        step = self._match_totp_step(pyotp.TOTP(secret), code)
        if step is None:
            return False
        # Replay protection: each time-step's code is accepted at most once.
        if not await security_state.claim_totp_step(self.db, user_id, step):
            log.warning("totp_replay_rejected", user_id=user_id)
            return False
        # Opportunistically migrate a legacy plaintext secret to ciphertext.
        if stored and not str(stored).startswith("enc:v1:"):
            await self.db.save_totp_secret(user_id, self.encrypt_totp_secret(secret))
        return True

    @staticmethod
    def _match_totp_step(totp: Any, code: str) -> int | None:
        """Return the time-step (±1 window) whose code equals ``code``, else None."""
        if not code or not code.isdigit():
            return None
        now = datetime.now(UTC)
        current = int(totp.timecode(now))
        for offset in (0, -1, 1):
            step = current + offset
            if hmac.compare_digest(str(totp.generate_otp(step)), code):
                return step
        return None

    @staticmethod
    def encrypt_totp_secret(secret: str) -> str:
        return _totp_encryptor(_totp_key_material()).encrypt(secret)

    @staticmethod
    def decrypt_totp_secret(stored: str | None) -> str | None:
        """Decrypt a stored TOTP secret; legacy plaintext values are returned as-is."""
        if not stored:
            return None
        if not str(stored).startswith("enc:v1:"):
            return str(stored)
        try:
            return _totp_encryptor(_totp_key_material()).decrypt(stored)
        except Exception:
            log.error("totp_secret_decrypt_failed")
            return None

    async def is_totp_enabled(self, user_id: str) -> bool:
        """Check if 2FA is enabled for user."""
        user_data = await self.db.get_user_by_id(user_id)
        return user_data.get("totp_enabled", False) if user_data else False


async def revoke_user_access(db: Database, user_id: str) -> int:
    """Revoke every active API key for ``user_id`` and invalidate all JWTs.

    Used on account deactivation so neither dashboard sessions nor API keys keep
    working. Keys are soft-revoked (``active = FALSE``), never deleted.
    Returns the number of keys revoked.
    """
    from remembra.auth import keys as keys_module

    cursor = await db.conn.execute(
        "UPDATE api_keys SET active = FALSE WHERE user_id = ? AND active = TRUE",
        (user_id,),
    )
    await db.conn.commit()
    revoked: int = cursor.rowcount or 0
    keys_module.evict_user_from_cache(user_id)
    await security_state.invalidate_user_sessions(db, user_id)
    log.info("user_access_revoked", user_id=user_id, keys_revoked=revoked)
    return revoked


def _missing_table(error: Exception) -> bool:
    return "no such table" in str(error).lower()


async def email_verified_on_another_account(db: Any, email: str, *, exclude_user_id: str) -> bool:
    """True when an account other than ``exclude_user_id`` holds ``email`` as a VERIFIED address.

    The single check behind "one free account per verified email". It looks
    at both kinds of account: dashboard users (``users``) and API-signup
    tenants (``cloud_tenants``, which have no users row). Every path that
    marks an address verified or creates a pre-verified account calls it:
    dashboard verify-email, API-signup verify-email, password reset, and
    Sign in with Google / GitHub account creation.
    """
    import sqlite3

    address = email.strip().lower()
    if not address:
        return False
    queries = (
        "SELECT 1 FROM users WHERE lower(email) = ? AND email_verified AND id != ? LIMIT 1",
        "SELECT 1 FROM cloud_tenants WHERE lower(email) = ? AND email_verified = 1 AND user_id != ? LIMIT 1",
    )
    for sql in queries:
        try:
            cursor = await db.conn.execute(sql, (address, exclude_user_id))
        except sqlite3.OperationalError as e:
            if _missing_table(e) or "no such column" in str(e).lower():
                continue  # minimal deployments: no tenant table (or no verification column yet)
            raise
        if await cursor.fetchone():
            return True
    return False


async def reset_credentials_for_new_owner(db: Database, user_id: str) -> dict[str, int]:
    """Clear every credential set up on an account before its mailbox owner took it over.

    Called when control of the address is proven for the first time (a
    password reset on an unverified account). Revokes API keys and every
    dashboard session, turns 2FA off (the authenticator may be someone
    else's), revokes MCP connector grants and their tokens, pauses webhooks
    (they push memory contents to a URL someone else chose), and drops any
    provider identity links. Returns counts per kind, for the log.
    """
    import sqlite3

    counts = {"api_keys": await revoke_user_access(db, user_id)}
    await db.disable_totp(user_id)
    now = time.time()
    statements = (
        (
            "connector_tokens",
            "UPDATE oauth_tokens SET revoked_at = ? WHERE revoked_at IS NULL"
            " AND grant_id IN (SELECT grant_id FROM oauth_grants WHERE user_id = ?)",
            (now, user_id),
        ),
        (
            "connector_grants",
            "UPDATE oauth_grants SET revoked_at = ?, revoke_reason = 'email_owner_verified'"
            " WHERE user_id = ? AND revoked_at IS NULL",
            (now, user_id),
        ),
        ("webhooks", "UPDATE webhooks SET active = 0 WHERE user_id = ? AND active = 1", (user_id,)),
        ("identities", "DELETE FROM user_identities WHERE user_id = ?", (user_id,)),
    )
    for name, sql, args in statements:
        try:
            cursor = await db.conn.execute(sql, args)
        except sqlite3.OperationalError as e:
            if not _missing_table(e):
                raise
            counts[name] = 0
            continue
        counts[name] = cursor.rowcount or 0
    await db.conn.commit()
    log.warning("account_credentials_reset_for_verified_owner", user_id=user_id, **counts)
    return counts
