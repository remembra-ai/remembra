"""API key generation and management."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import bcrypt
import structlog

from remembra.core.time import utcnow
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
    def compute_lookup(raw_key: str) -> str:
        """
        Deterministic, non-reversible lookup hash for an API key.

        SHA-256 of the raw key. Because the key carries 256 bits of entropy,
        a plain SHA-256 is not brute-forceable, so this is safe to store and
        index — it turns validation into a single indexed read instead of an
        O(n) bcrypt scan over every active key. bcrypt remains the at-rest
        verifier (defense in depth).
        """
        return hashlib.sha256(raw_key.encode()).hexdigest()

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
        key_lookup = self.compute_lookup(raw_key)

        await self.db.save_api_key(
            key_id=key_id,
            key_hash=key_hash,
            user_id=user_id,
            name=name,
            rate_limit_tier=rate_limit_tier,
            key_lookup=key_lookup,
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
            created_at=utcnow(),
            rate_limit_tier=rate_limit_tier,
        )

    @staticmethod
    def _normalize_key_data(row: dict) -> dict:
        """Parse comma-separated scopes/project_ids into lists (or None)."""
        key_data = dict(row)
        scopes = key_data.get("scopes")
        key_data["scopes"] = [s for s in scopes.split(",") if s] if scopes else None
        project_ids = key_data.get("project_ids")
        key_data["project_ids"] = [p for p in project_ids.split(",") if p] if project_ids else None
        return key_data

    @staticmethod
    def _cache_put(cache_key: str, key_data: dict) -> None:
        """Insert into the in-memory validation cache with simple FIFO eviction."""
        if len(_key_cache) >= _KEY_CACHE_MAX_SIZE:
            _key_cache.pop(next(iter(_key_cache)))
        _key_cache[cache_key] = key_data

    async def validate_key(self, raw_key: str) -> dict | None:
        """
        Validate an API key and return its record if valid, else None.

        Lookup strategy (each step is O(1) except the bounded legacy fallback):
        1. In-memory cache keyed by sha256(raw_key).
        2. Indexed DB lookup on the deterministic `key_lookup` column, then a
           single bcrypt verification for defense in depth.
        3. Legacy fallback: only keys created before `key_lookup` existed are
           bcrypt-scanned, and each is backfilled on first match. Once no
           unmigrated keys remain, unknown/invalid keys are rejected in O(1) —
           removing the previous O(n)-bcrypt-per-request CPU-exhaustion vector.
        """
        global _key_cache

        if not raw_key.startswith(KEY_PREFIX):
            log.debug("api_key_invalid_format", key_preview=raw_key[:8] if raw_key else "empty")
            return None

        cache_key = self.compute_lookup(raw_key)

        # 1) In-memory cache (revalidate active flag against the DB)
        if cache_key in _key_cache:
            cached = _key_cache[cache_key]
            cursor = await self.db.conn.execute("SELECT active FROM api_keys WHERE id = ?", (cached["id"],))
            row = await cursor.fetchone()
            if row and row[0]:
                await self.db.update_api_key_last_used(cached["id"])
                log.debug("api_key_validated_cached", key_id=cached["id"])
                return cached
            del _key_cache[cache_key]

        # 2) O(1) indexed lookup by deterministic hash, then bcrypt verify
        row = await self.db.get_active_api_key_by_lookup(cache_key)
        if row:
            if self.verify_key(raw_key, row["key_hash"]):
                key_data = self._normalize_key_data(row)
                await self.db.update_api_key_last_used(key_data["id"])
                self._cache_put(cache_key, key_data)
                log.debug("api_key_validated", key_id=key_data["id"], user_id=key_data["user_id"], role=key_data.get("role"))
                return key_data
            # lookup collision without bcrypt match is effectively impossible; treat as invalid
            log.warning("api_key_lookup_bcrypt_mismatch")
            return None

        # 3) Bounded legacy fallback for keys predating the key_lookup column
        unmigrated = await self.db.get_unmigrated_active_api_keys()
        for legacy_row in unmigrated:
            if self.verify_key(raw_key, legacy_row["key_hash"]):
                await self.db.set_api_key_lookup(legacy_row["id"], cache_key)  # backfill → O(1) next time
                key_data = self._normalize_key_data(legacy_row)
                await self.db.update_api_key_last_used(key_data["id"])
                self._cache_put(cache_key, key_data)
                log.info("api_key_lookup_backfilled", key_id=key_data["id"])
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
