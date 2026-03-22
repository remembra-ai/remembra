"""Tests for AES-256-GCM field-level encryption."""


from remembra.security.encryption import FieldEncryptor, _ENCRYPTED_PREFIX


class TestFieldEncryptor:
    """Tests for the FieldEncryptor class."""

    def test_passthrough_when_no_key(self):
        """Without a key, content passes through unmodified."""
        enc = FieldEncryptor(key=None)
        assert not enc.enabled
        assert enc.encrypt("hello world") == "hello world"
        assert enc.decrypt("hello world") == "hello world"

    def test_passthrough_empty_key(self):
        """Empty string key means passthrough."""
        enc = FieldEncryptor(key="")
        assert not enc.enabled
        assert enc.encrypt("test") == "test"

    def test_encrypt_decrypt_roundtrip(self):
        """Content survives encrypt → decrypt roundtrip."""
        enc = FieldEncryptor(key="test-secret-key-for-unit-tests")
        assert enc.enabled

        plaintext = "The user's favorite color is blue."
        ciphertext = enc.encrypt(plaintext)

        # Ciphertext should be different from plaintext
        assert ciphertext != plaintext
        # Ciphertext should have the version prefix
        assert ciphertext.startswith(_ENCRYPTED_PREFIX)

        # Decrypt should recover original
        recovered = enc.decrypt(ciphertext)
        assert recovered == plaintext

    def test_different_nonces_produce_different_ciphertext(self):
        """Each encryption uses a fresh random nonce."""
        enc = FieldEncryptor(key="test-key-123")

        ct1 = enc.encrypt("same content")
        ct2 = enc.encrypt("same content")

        # Same plaintext should produce different ciphertext (random nonce)
        assert ct1 != ct2

        # Both should decrypt to the same value
        assert enc.decrypt(ct1) == "same content"
        assert enc.decrypt(ct2) == "same content"

    def test_no_double_encryption(self):
        """Already-encrypted content should not be re-encrypted."""
        enc = FieldEncryptor(key="test-key")

        ct = enc.encrypt("hello")
        ct2 = enc.encrypt(ct)

        # Should be identical — no double encryption
        assert ct == ct2

    def test_decrypt_plaintext_passthrough(self):
        """Decrypting plaintext (no prefix) returns it unchanged."""
        enc = FieldEncryptor(key="test-key")

        # Legacy unencrypted content should pass through
        assert enc.decrypt("plain text content") == "plain text content"

    def test_decrypt_without_key_returns_ciphertext(self):
        """Decrypting without a key returns the ciphertext unchanged."""
        enc_with_key = FieldEncryptor(key="secret")
        enc_without_key = FieldEncryptor(key=None)

        ct = enc_with_key.encrypt("sensitive data")

        # Without key, should return ciphertext as-is
        result = enc_without_key.decrypt(ct)
        assert result == ct

    def test_wrong_key_fails_gracefully(self):
        """Decrypting with wrong key returns ciphertext (doesn't crash)."""
        enc1 = FieldEncryptor(key="correct-key")
        enc2 = FieldEncryptor(key="wrong-key")

        ct = enc1.encrypt("secret message")
        result = enc2.decrypt(ct)

        # Should not crash — returns the ciphertext on failure
        assert result == ct

    def test_empty_string(self):
        """Empty string passes through."""
        enc = FieldEncryptor(key="test")
        assert enc.encrypt("") == ""
        assert enc.decrypt("") == ""

    def test_unicode_content(self):
        """Unicode content survives roundtrip."""
        enc = FieldEncryptor(key="unicode-test-key")

        texts = [
            "日本語テスト",
            "Ñoño España",
            "Привет мир",
            "🎉🔒🧠💾",
            "Mixed: hello 世界 مرحبا",
        ]

        for text in texts:
            ct = enc.encrypt(text)
            assert enc.decrypt(ct) == text

    def test_large_content(self):
        """Large content encrypts and decrypts correctly."""
        enc = FieldEncryptor(key="large-content-key")

        large_text = "A" * 100_000  # 100KB
        ct = enc.encrypt(large_text)
        assert enc.decrypt(ct) == large_text

    def test_deterministic_key_derivation(self):
        """Same passphrase produces same derived key (for restarts)."""
        enc1 = FieldEncryptor(key="my-persistent-key")
        enc2 = FieldEncryptor(key="my-persistent-key")

        # Encrypt with enc1, decrypt with enc2 — should work
        ct = enc1.encrypt("cross-instance test")
        assert enc2.decrypt(ct) == "cross-instance test"


class TestDictEncryption:
    """Tests for dict-level encryption (metadata fields)."""

    def test_encrypt_dict_roundtrip(self):
        """Dictionary encryption preserves structure and values."""
        enc = FieldEncryptor(key="dict-test-key")

        metadata = {
            "source": "user_input",
            "tags": ["memory", "test"],
            "count": 42,
            "active": True,
            "nested": {"key": "value"},
        }

        encrypted = enc.encrypt_dict(metadata)
        assert encrypted is not None

        # String values should be encrypted
        assert encrypted["source"] != "user_input"
        assert encrypted["source"].startswith(_ENCRYPTED_PREFIX)

        # Non-string values should be untouched
        assert encrypted["tags"] == ["memory", "test"]
        assert encrypted["count"] == 42
        assert encrypted["active"] is True

        # Nested dict string values should be encrypted
        assert encrypted["nested"]["key"].startswith(_ENCRYPTED_PREFIX)

        # Decrypt should recover original
        decrypted = enc.decrypt_dict(encrypted)
        assert decrypted == metadata

    def test_encrypt_dict_none(self):
        """None input returns None."""
        enc = FieldEncryptor(key="test")
        assert enc.encrypt_dict(None) is None
        assert enc.decrypt_dict(None) is None

    def test_encrypt_dict_passthrough(self):
        """Without key, dict passes through."""
        enc = FieldEncryptor(key=None)
        data = {"key": "value"}
        assert enc.encrypt_dict(data) is data

    def test_decrypt_mixed_dict(self):
        """Decrypt handles mix of encrypted and plaintext values."""
        enc = FieldEncryptor(key="mixed-test")

        mixed = {
            "encrypted_field": enc.encrypt("secret"),
            "plain_field": "not encrypted",
            "number": 123,
        }

        result = enc.decrypt_dict(mixed)
        assert result["encrypted_field"] == "secret"
        assert result["plain_field"] == "not encrypted"
        assert result["number"] == 123
