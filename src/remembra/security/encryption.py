"""
AES-256-GCM field-level encryption for memory content at rest.

Provides transparent encrypt/decrypt for the text fields of a memory's
Qdrant payload (see ``storage/qdrant.py``: content, metadata, extracted
facts and entities) and for TOTP secrets. The SQLite database is not
field-encrypted.

Usage:
    from remembra.security.encryption import FieldEncryptor

    encryptor = FieldEncryptor(key="your-secret-key")
    ciphertext = encryptor.encrypt("sensitive memory content")
    plaintext = encryptor.decrypt(ciphertext)

The encryption key is derived from REMEMBRA_ENCRYPTION_KEY using
PBKDF2-HMAC-SHA256 with 480,000 iterations and a static salt
(derived from the key itself to ensure deterministic derivation
across restarts without storing additional state).

When REMEMBRA_ENCRYPTION_KEY is not set, the encryptor operates in
passthrough mode — content is stored and returned unmodified. This
allows zero-config development while enforcing encryption in production.
"""

import base64
import hashlib
import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Marker prefix for encrypted content (used to detect already-encrypted data)
_ENCRYPTED_PREFIX = "enc:v1:"

# PBKDF2 iterations — OWASP 2023 recommendation for HMAC-SHA256
_PBKDF2_ITERATIONS = 480_000

# AES-256-GCM nonce size in bytes (96-bit = 12 bytes, NIST recommended)
_NONCE_SIZE = 12

# AES-256-GCM tag size in bytes
_TAG_SIZE = 16


def _derive_key(passphrase: str) -> bytes:
    """
    Derive a 256-bit AES key from a passphrase using PBKDF2-HMAC-SHA256.

    Uses a deterministic salt derived from the passphrase itself (SHA-256
    of the passphrase). This avoids needing to store a random salt while
    still providing key stretching against brute-force attacks.

    The 480,000 iteration count follows OWASP 2023 recommendations.
    """
    # Deterministic salt from passphrase — ensures same key across restarts
    salt = hashlib.sha256(passphrase.encode("utf-8")).digest()[:16]

    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=32,  # 256-bit key
    )


class FieldEncryptor:
    """
    AES-256-GCM field-level encryptor for memory content.

    When initialized with a key, all encrypt/decrypt operations use
    AES-256-GCM with random nonces. When initialized without a key
    (passthrough mode), content passes through unmodified.

    Thread-safe: each encrypt() call generates a fresh random nonce.
    """

    def __init__(self, key: str | None = None) -> None:
        """
        Initialize the encryptor.

        Args:
            key: Encryption passphrase. If None, operates in passthrough
                 mode (no encryption). In production, always set
                 REMEMBRA_ENCRYPTION_KEY.
        """
        self._enabled = key is not None and len(key) > 0
        self._key: bytes | None = None

        if self._enabled:
            self._key = _derive_key(key)  # type: ignore[arg-type]
            log.info("encryption_enabled", algorithm="AES-256-GCM")
        else:
            log.info("encryption_disabled", reason="no REMEMBRA_ENCRYPTION_KEY set")

    @property
    def enabled(self) -> bool:
        """Whether encryption is active."""
        return self._enabled

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a string using AES-256-GCM.

        Returns a base64-encoded string prefixed with 'enc:v1:' containing
        the nonce + ciphertext + GCM tag. The prefix allows distinguishing
        encrypted content from plaintext (for migration / mixed-mode reads).

        Args:
            plaintext: Content to encrypt.

        Returns:
            Encrypted string in format: enc:v1:{base64(nonce + ciphertext + tag)}
            Or the original plaintext if encryption is disabled.
        """
        if not self._enabled or not plaintext:
            return plaintext

        # Already encrypted — don't double-encrypt
        if plaintext.startswith(_ENCRYPTED_PREFIX):
            return plaintext

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            assert self._key is not None  # guaranteed by the `self._enabled` check above
            nonce = os.urandom(_NONCE_SIZE)
            aesgcm = AESGCM(self._key)
            ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

            # Pack: nonce (12) + ciphertext + tag (appended by GCM)
            packed = nonce + ciphertext
            encoded = base64.b64encode(packed).decode("ascii")

            return f"{_ENCRYPTED_PREFIX}{encoded}"

        except ImportError:
            log.warning(
                "encryption_unavailable",
                reason="cryptography package not installed, pip install cryptography",
            )
            return plaintext
        except Exception as e:
            log.error("encryption_failed", error=str(e))
            # In production, fail closed - don't store unencrypted sensitive data
            if os.getenv("REMEMBRA_ENCRYPTION_STRICT", "true").lower() == "true":
                raise RuntimeError(f"Encryption failed: {e}. Set REMEMBRA_ENCRYPTION_STRICT=false to fail open.")
            # Fail open only if explicitly disabled — store unencrypted rather than lose data
            return plaintext

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt an AES-256-GCM encrypted string.

        Handles both encrypted (prefixed with 'enc:v1:') and plaintext
        content transparently. This allows reading legacy unencrypted
        memories alongside newly encrypted ones during migration.

        Args:
            ciphertext: Encrypted string or plaintext.

        Returns:
            Decrypted plaintext.
        """
        if not ciphertext:
            return ciphertext

        # Not encrypted — return as-is (supports mixed-mode reads)
        if not ciphertext.startswith(_ENCRYPTED_PREFIX):
            return ciphertext

        if not self._enabled:
            log.warning(
                "decrypt_without_key",
                reason="encrypted content found but no REMEMBRA_ENCRYPTION_KEY set",
            )
            return ciphertext  # Can't decrypt without key

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            # Strip prefix and decode
            encoded = ciphertext[len(_ENCRYPTED_PREFIX) :]
            packed = base64.b64decode(encoded)

            # Unpack: nonce (12 bytes) + ciphertext + tag
            nonce = packed[:_NONCE_SIZE]
            ct_with_tag = packed[_NONCE_SIZE:]

            assert self._key is not None  # guaranteed by the `self._enabled` check above
            aesgcm = AESGCM(self._key)
            plaintext_bytes = aesgcm.decrypt(nonce, ct_with_tag, None)

            return plaintext_bytes.decode("utf-8")

        except ImportError:
            log.warning(
                "decryption_unavailable",
                reason="cryptography package not installed",
            )
            return ciphertext
        except Exception as e:
            log.error("decryption_failed", error=str(e))
            # Decrypt fails gracefully by default — returning encrypted data is safe
            # (no data loss). Encrypt stays strict (to prevent storing unencrypted).
            if os.getenv("REMEMBRA_ENCRYPTION_STRICT", "false").lower() == "true":
                raise RuntimeError(f"Decryption failed: {e}. Data may be corrupted or key changed.")
            # Return ciphertext as-is — caller can retry with correct key
            return ciphertext

    def encrypt_value(self, value: Any) -> Any:
        """
        Encrypt every string in ``value``, at any depth.

        A string is encrypted; lists and dicts are rebuilt with their strings
        encrypted (a tuple becomes a list). Numbers, booleans and None pass
        through so their type survives a round trip, and dict keys stay plain.

        Args:
            value: A string, or a JSON-like structure of lists and dicts.

        Returns:
            The same shape with every string encrypted, or ``value`` itself
            when encryption is disabled.
        """
        if not self._enabled:
            return value
        if isinstance(value, str):
            return self.encrypt(value)
        if isinstance(value, dict):
            return {key: self.encrypt_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.encrypt_value(item) for item in value]
        return value

    def decrypt_value(self, value: Any) -> Any:
        """
        Decrypt every encrypted string in ``value``, at any depth.

        Plaintext strings (written before encryption covered them) come back
        unchanged, so old and new records read the same way.

        Args:
            value: A string, or a JSON-like structure of lists and dicts.

        Returns:
            The same shape with every encrypted string decrypted.
        """
        if isinstance(value, str):
            return self.decrypt(value)
        if isinstance(value, dict):
            return {key: self.decrypt_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.decrypt_value(item) for item in value]
        return value

    def encrypt_dict(self, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """
        Encrypt every string value in a metadata dictionary, at any depth.

        Strings inside nested dicts and lists are encrypted too. Numbers,
        booleans and None pass through unmodified, and keys stay plain.

        Args:
            data: Metadata dictionary or None.

        Returns:
            Dictionary with string values encrypted, or None.
        """
        if not self._enabled or data is None:
            return data
        result: dict[str, Any] = self.encrypt_value(data)
        return result

    def decrypt_dict(self, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """
        Decrypt all encrypted string values in a metadata dictionary, at any depth.

        Handles mixed encrypted/plaintext values transparently.

        Args:
            data: Dictionary possibly containing encrypted values.

        Returns:
            Dictionary with encrypted values decrypted, or None.
        """
        if data is None:
            return data
        result: dict[str, Any] = self.decrypt_value(data)
        return result
