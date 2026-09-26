"""
Tests for PII detector.

Guards the expected redaction behavior and — critically — guards the
*non*-redaction of IPv4 addresses. IPv4 redaction was removed because it
destroyed legitimate user-owned infra config (deploy IPs, server addresses)
that Remembra must preserve verbatim for recall.
"""

import pytest

from remembra.security.pii_detector import PIIDetector, scan_for_pii, redact_pii


# ---------------------------------------------------------------------------
# Regression: IPv4 addresses must NOT be treated as PII
# ---------------------------------------------------------------------------


class TestIPv4NotRedacted:
    """IPv4 addresses must flow through unredacted in both scan and redact."""

    def test_bare_ipv4_not_flagged(self):
        result = scan_for_pii("Server IP is 178.156.226.84")
        assert not result.has_pii, f"IPv4 should not be flagged, got: {result.matches}"

    def test_bare_ipv4_not_redacted(self):
        content = "Coolify server IP is 178.156.226.84 — deploy via ssh coolify"
        assert redact_pii(content) == content

    def test_localhost_and_private_ranges_not_redacted(self):
        for ip in ("127.0.0.1", "192.168.1.100", "10.0.0.1", "172.16.0.5"):
            content = f"host {ip} ok"
            assert redact_pii(content) == content, f"IP {ip} was redacted"

    def test_ipv4_in_url_not_redacted(self):
        content = "http://178.156.226.84:8000/health"
        assert redact_pii(content) == content

    def test_detector_redact_mode_leaves_ipv4_intact(self):
        detector = PIIDetector(enabled=True, mode="redact")
        result = detector.scan("deploy to 178.156.226.84")
        # No PII → no redacted_content substitution
        assert not result.has_pii
        assert result.redacted_content is None


# ---------------------------------------------------------------------------
# Real PII must still be redacted (we did not break the rest)
# ---------------------------------------------------------------------------


class TestRealPIIStillRedacted:
    def test_ssn_still_redacted(self):
        out = redact_pii("My SSN is 123-45-6789")
        assert "123-45-6789" not in out
        assert "REDACTED_SSN" in out

    def test_password_still_redacted(self):
        out = redact_pii("password: hunter2longpass")
        assert "hunter2longpass" not in out
        assert "REDACTED_PASSWORD" in out

    def test_api_key_still_redacted(self):
        out = redact_pii("use token_abcdef0123456789xyz to auth")
        assert "token_abcdef0123456789xyz" not in out

    def test_aws_key_still_redacted(self):
        out = redact_pii("key=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_email_still_redacted(self):
        out = redact_pii("contact me at foo@example.com")
        assert "foo@example.com" not in out

    def test_credit_card_still_redacted(self):
        out = redact_pii("card 4111 1111 1111 1111")
        assert "4111 1111 1111 1111" not in out

    def test_mixed_ip_and_password_only_redacts_password(self):
        content = "server 178.156.226.84 password: s3cretValue99"
        out = redact_pii(content)
        assert "178.156.226.84" in out, "IP should survive"
        assert "s3cretValue99" not in out, "Password should be redacted"


# ---------------------------------------------------------------------------
# Round-trip: store-path-equivalent behavior
# ---------------------------------------------------------------------------


class TestStorePathRoundTrip:
    """Simulates the write-path logic in api/v1/memories.py."""

    def test_owner_deploy_config_survives(self):
        detector = PIIDetector(enabled=True, mode="redact")
        original = "Coolify server IP is 178.156.226.84 — deploy via ssh coolify"

        pii_result = detector.scan(original, source="user_input")
        # Mirror the branch in memories.py: only substitute when has_pii+redacted_content
        stored = pii_result.redacted_content if (pii_result.has_pii and pii_result.redacted_content) else original

        assert stored == original
        assert "178.156.226.84" in stored


def test_compact_dates_are_not_bank_accounts():
    """Live 2026-09-25: '~/.remembra-config-backups-20260925' came back as
    '[REDACTED_BANK_ACCOUNT]'."""
    text = "backups in ~/.remembra-config-backups-20260925 and snapshot 19991231"
    assert redact_pii(text) == text


def test_long_account_numbers_still_redacted():
    assert "4000123456789" not in redact_pii("acct 4000123456789 on file")


@pytest.mark.parametrize(
    "text",
    [
        # Live 2026-09-26: a Google OAuth client id lost its project number.
        "GOOGLE_CLIENT_ID=629134551556-8v3kq0abcdefghijklmnopq.apps.googleusercontent.com",
        "client 629134551556-8v3kq0abcdefghij.apps.googleusercontent.com is the web one",
        "request 123e4567-e89b-12d3-a456-426614174000 and 12345678-1234-5678-1234-567812345678",
        "image tag 20260926123456-a1b2c3 and build-1234567890-rc1",
    ],
)
def test_hyphenated_identifiers_are_not_bank_accounts(text):
    assert not any(m.type == "bank_account" for m in scan_for_pii(text).matches)
    if "googleusercontent" in text:
        assert redact_pii(text) == text  # nothing else in a client id looks like PII either


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("acct 4000123456789 on file", "4000123456789"),
        ("account number: 000123456789.", "000123456789"),
        ("routing 021000021, account 12345678901234", "12345678901234"),
        ("branch-split 0012-345678901", "345678901"),
        ("acct-12345678 (checking)", "12345678"),
        ("wire to 9876543210123-", "9876543210123"),
    ],
)
def test_real_bank_account_numbers_are_still_redacted(text, number):
    redacted = redact_pii(text)
    assert number not in redacted and "[REDACTED_BANK_ACCOUNT]" in redacted
