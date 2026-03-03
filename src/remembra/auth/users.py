"""User model and password hashing for authentication."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
import structlog

from remembra.storage.database import Database

log = structlog.get_logger(__name__)

# JWT settings
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 days
PASSWORD_RESET_EXPIRATION_HOURS = 24


@dataclass
class User:
    """Represents a registered user."""
    
    id: str
    email: str
    name: str | None
    created_at: datetime
    email_verified: bool = False
    is_active: bool = True


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
    
    def create_jwt_token(self, user_id: str, email: str) -> str:
        """Create a JWT access token."""
        payload = {
            "sub": user_id,
            "email": email,
            "iat": datetime.now(UTC),
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
        
        log.info("user_created", user_id=user_id, email=email)
        
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
            log.warning("login_failed_user_not_found", email=email)
            return None, None, "Invalid email or password"
        
        if not user_data.get("is_active", True):
            log.warning("login_failed_user_inactive", email=email)
            return None, None, "Account is deactivated"
        
        if not self.verify_password(password, user_data["password_hash"]):
            log.warning("login_failed_wrong_password", email=email)
            return None, None, "Invalid email or password"
        
        # Update last login
        await self.db.update_user_last_login(user_data["id"])
        
        user = User(
            id=user_data["id"],
            email=user_data["email"],
            name=user_data.get("name"),
            created_at=datetime.fromisoformat(user_data["created_at"]) if isinstance(user_data["created_at"], str) else user_data["created_at"],
            email_verified=user_data.get("email_verified", False),
            is_active=user_data.get("is_active", True),
        )
        
        token = self.create_jwt_token(user.id, user.email)
        
        log.info("user_logged_in", user_id=user.id, email=email)
        
        return user, token, None
    
    async def get_user_by_id(self, user_id: str) -> User | None:
        """Get user by ID."""
        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return None
        
        return User(
            id=user_data["id"],
            email=user_data["email"],
            name=user_data.get("name"),
            created_at=datetime.fromisoformat(user_data["created_at"]) if isinstance(user_data["created_at"], str) else user_data["created_at"],
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
            log.debug("password_reset_requested_unknown_email", email=email)
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
        expires_at = datetime.fromisoformat(reset_record["expires_at"]) if isinstance(reset_record["expires_at"], str) else reset_record["expires_at"]
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
        
        # Add to blacklist
        await self.db.add_token_to_blacklist(
            token_hash=self.hash_password(token),
            user_id=user_id,
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
        
        log.info("user_logged_out", user_id=user_id)
        return True
    
    async def is_token_blacklisted(self, token: str) -> bool:
        """Check if a token is blacklisted."""
        token_hash = self.hash_password(token)
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
        
        log.info("password_changed", user_id=user_id)
        
        return True, None
    
    async def delete_account(
        self,
        user_id: str,
        password: str,
    ) -> tuple[bool, str | None]:
        """
        Deactivate user account (soft delete).
        
        Requires password confirmation for security.
        Returns (True, None) on success, (False, error_message) on failure.
        """
        user_data = await self.db.get_user_by_id(user_id)
        if not user_data:
            return False, "User not found"
        
        # Verify password
        if not self.verify_password(password, user_data["password_hash"]):
            log.warning("account_deletion_failed_wrong_password", user_id=user_id)
            return False, "Password is incorrect"
        
        # Deactivate account
        success = await self.db.deactivate_user(user_id)
        if not success:
            return False, "Failed to deactivate account"
        
        log.info("account_deactivated", user_id=user_id)
        
        return True, None
