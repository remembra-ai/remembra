"""API key generation and management."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import bcrypt
import structlog

from remembra.storage.database import Database

log = structlog.get_logger(__name__)

# Cache for validated keys (key_hash -> key_data)
# TTL is managed by clearing on key changes
_key_cache: dict[str, dict[str, Any]] = {}
_KEY_CACHE_MAX_SIZE = 1000

# Key format: rem_<32 bytes base64> = rem_abc123...
KEY_PREFIX = "rem_"
KEY_BYTES = 32  # 256 bits of entropy


@dataclass
class APIKey:
    """Represents a generated API key."""
    
    id: str
    key: str  # Full key (only available at creation time)
    user_id: str
    name: str | None
    created_at: datetime
    rate_limit_tier: str = "standard"
    
    @property
    def key_preview(self) -> str:
        """Return first 8 chars of key for logging (never log full key)."""
        return self.key[:12] + "..." if len(self.key) > 12 else self.key


@dataclass
class APIKeyInfo:
    """API key info without the actual key (for listing)."""
    
    id: str
    user_id: str
    name: str | None
    created_at: str
    last_used_at: str | None
    active: bool
    rate_limit_tier: str
    project_ids: list[str] | None = None


class APIKeyManager:
    """
    Manages API key lifecycle: generation, validation, revocation.
    
    Keys are generated with 256 bits of entropy and hashed with bcrypt
    before storage. The raw key is only returned once at creation time.
    """
    
    def __init__(self, db: Database) -> None:
        self.db = db
    
    @staticmethod
    def generate_key() -> str:
        """Generate a new API key with prefix."""
        random_bytes = secrets.token_urlsafe(KEY_BYTES)
        return f"{KEY_PREFIX}{random_bytes}"
    
    @staticmethod
    def hash_key(key: str) -> str:
        """Hash an API key using bcrypt."""
        return bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()
    
    @staticmethod
    def verify_key(key: str, key_hash: str) -> bool:
        """Verify an API key against its hash."""
        try:
            return bcrypt.checkpw(key.encode(), key_hash.encode())
        except Exception:
            return False
    
    @staticmethod
    def generate_key_id() -> str:
        """Generate a unique key ID."""
        return f"key_{secrets.token_urlsafe(16)}"
    
    async def create_key(
        self,
        user_id: str,
        name: str | None = None,
        rate_limit_tier: str = "standard",
    ) -> APIKey:
        """
        Create a new API key for a user.
        
        Returns the APIKey with the raw key - this is the ONLY time
        the raw key is available. It's hashed before storage.
        """
        key_id = self.generate_key_id()
        raw_key = self.generate_key()
        key_hash = self.hash_key(raw_key)
        
        await self.db.save_api_key(
            key_id=key_id,
            key_hash=key_hash,
            user_id=user_id,
            name=name,
            rate_limit_tier=rate_limit_tier,
        )
        
        log.info(
            "api_key_created",
            key_id=key_id,
            user_id=user_id,
            name=name,
            key_preview=raw_key[:12] + "...",
        )
        
        return APIKey(
            id=key_id,
            key=raw_key,
            user_id=user_id,
            name=name,
            created_at=datetime.utcnow(),
            rate_limit_tier=rate_limit_tier,
        )
    
    async def validate_key(self, raw_key: str) -> dict | None:
        """
        Validate an API key and return key info if valid.
        
        Uses a SHA256 cache key for fast repeated lookups, falling back to
        O(n) bcrypt verification for uncached keys.
        
        Returns key record dict if valid, None otherwise.
        """
        global _key_cache
        
        if not raw_key.startswith(KEY_PREFIX):
            log.debug("api_key_invalid_format", key_preview=raw_key[:8] if raw_key else "empty")
            return None
        
        # Use SHA256 hash of the key as cache lookup (fast, constant time)
        cache_key = hashlib.sha256(raw_key.encode()).hexdigest()
        
        # Check cache first (O(1) lookup)
        if cache_key in _key_cache:
            cached = _key_cache[cache_key]
            # Verify the key is still active in DB
            cursor = await self.db.conn.execute(
                "SELECT active FROM api_keys WHERE id = ?",
                (cached["id"],)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                await self.db.update_api_key_last_used(cached["id"])
                log.debug("api_key_validated_cached", key_id=cached["id"])
                return cached
            else:
                # Key was deactivated, remove from cache
                del _key_cache[cache_key]
        
        # Cache miss - fall back to O(n) bcrypt check
        # Join with api_key_roles to get the role
        cursor = await self.db.conn.execute(
            """
            SELECT k.*, COALESCE(r.role, 'editor') as role, r.scopes, r.project_ids
            FROM api_keys k
            LEFT JOIN api_key_roles r ON k.id = r.api_key_id
            WHERE k.active = TRUE
            """
        )
        rows = await cursor.fetchall()
        
        for row in rows:
            key_data = dict(row)
            if self.verify_key(raw_key, key_data["key_hash"]):
                # Update last_used timestamp
                await self.db.update_api_key_last_used(key_data["id"])
                
                # Parse scopes from comma-separated string
                if key_data.get("scopes"):
                    key_data["scopes"] = [s for s in key_data["scopes"].split(",") if s]
                else:
                    key_data["scopes"] = None

                if key_data.get("project_ids"):
                    key_data["project_ids"] = [
                        project_id for project_id in key_data["project_ids"].split(",") if project_id
                    ]
                else:
                    key_data["project_ids"] = None
                
                # Cache the result (limit cache size)
                if len(_key_cache) >= _KEY_CACHE_MAX_SIZE:
                    # Remove oldest entry (simple eviction)
                    _key_cache.pop(next(iter(_key_cache)))
                _key_cache[cache_key] = key_data
                
                log.debug("api_key_validated", key_id=key_data["id"], user_id=key_data["user_id"], role=key_data.get("role"))
                return key_data
        
        log.warning("api_key_validation_failed", key_preview=raw_key[:12] + "...")
        return None
    
    async def list_keys(self, user_id: str) -> list[APIKeyInfo]:
        """List all API keys for a user (without actual keys)."""
        keys = await self.db.get_user_api_keys(user_id)
        return [
            APIKeyInfo(
                id=k["id"],
                user_id=k["user_id"],
                name=k["name"],
                created_at=k["created_at"],
                last_used_at=k["last_used_at"],
                active=k["active"],
                rate_limit_tier=k["rate_limit_tier"],
            )
            for k in keys
        ]
    
    async def revoke_key(self, key_id: str, user_id: str) -> bool:
        """
        Revoke an API key (soft delete). Returns True if found and revoked.
        
        The user_id is required to ensure users can only revoke their own keys.
        """
        global _key_cache
        
        success = await self.db.revoke_api_key(key_id, user_id)
        
        if success:
            # Invalidate cache entries for this key
            _key_cache = {k: v for k, v in _key_cache.items() if v.get("id") != key_id}
            log.info("api_key_revoked", key_id=key_id, user_id=user_id)
        else:
            log.warning("api_key_revoke_failed", key_id=key_id, user_id=user_id)
        
        return success
    
    async def delete_key_permanently(self, key_id: str, user_id: str) -> bool:
        """
        Permanently delete an API key from the database (hard delete).
        
        Use this for leaked keys or security incidents.
        The user_id is required to ensure users can only delete their own keys.
        
        Returns True if found and deleted.
        """
        global _key_cache
        
        success = await self.db.delete_api_key_permanently(key_id, user_id)
        
        if success:
            # Invalidate cache entries for this key
            _key_cache = {k: v for k, v in _key_cache.items() if v.get("id") != key_id}
            log.info("api_key_deleted_permanently", key_id=key_id, user_id=user_id)
        else:
            log.warning("api_key_delete_failed", key_id=key_id, user_id=user_id)
        
        return success
    
    async def get_key_info(self, key_id: str) -> APIKeyInfo | None:
        """Get info about a specific key (without the actual key)."""
        key = await self.db.get_api_key_by_id(key_id)
        if not key:
            return None
        
        return APIKeyInfo(
            id=key["id"],
            user_id=key["user_id"],
            name=key["name"],
            created_at=key["created_at"],
            last_used_at=key["last_used_at"],
            active=key["active"],
            rate_limit_tier=key["rate_limit_tier"],
            project_ids=None,
        )
    
    async def update_key_name(self, key_id: str, name: str) -> bool:
        """
        Update the name of an API key.
        
        Returns True if the key was updated successfully.
        """
        success = await self.db.update_api_key_name(key_id, name)
        
        if success:
            log.info("api_key_name_updated", key_id=key_id, new_name=name)
        else:
            log.warning("api_key_name_update_failed", key_id=key_id)
        
        return success
