"""F29 follow-up: the over-match fix must not open holes the old rules closed.

Each row here was redacted before the F29 fix and has to stay redacted after it:

* a card, account or phone number is not a "developer number" because a ``/`` or a
  common word (run, issue, release, PR) happens to sit next to it;
* a 22 to 31 character token in a URL (an OAuth code, a reset or magic link) is
  still cut out, and a base64 query value is judged whole;
* a short numeric password in a real ``*_PASS`` variable is not a test count, and
  a name with the credential word in the middle (``SECRETKEYBASE``) is still a secret's;
* a generated token with a prefix is not a "name".

The negatives the F29 fix exists for are repeated beside them.
"""

from __future__ import annotations

import base64
import secrets

import pytest

from remembra.crew.redact import outbound, redact_text
from remembra.security.pii_detector import PIIDetector, redact_pii, scan_for_pii
from remembra.security.secrets import redact_secrets

# ---------------------------------------------------------------------------
# 1. Personal numbers beside a slash or a common word
# ---------------------------------------------------------------------------

PERSONAL_NUMBERS = [
    ("card 4111111111111111/123 exp 12/27", "4111111111111111"),
    ("cc: 4111111111111111/12/27", "4111111111111111"),
    ("run card 4111111111111111 for $50", "4111111111111111"),
    ("issue card 4111111111111111", "4111111111111111"),
    ("workflow run 4111111111111111 finished", "4111111111111111"),
    ("https://pay.example.com/charge/4111111111111111/confirm", "4111111111111111"),
    ("wire/ach 12345678901/021000021", "12345678901"),
    ("wire/ach 12345678901/021000021", "021000021"),
    ("release funds 123456789012", "123456789012"),
    ("pull from 12345678901", "12345678901"),
    ("commit deposit 123456789012", "123456789012"),
    ("phone/fax 2035551234/2035551235", "2035551234"),
    ("phone/fax 2035551234/2035551235", "2035551235"),
    ("cell/home: 2035551234/2035559876", "2035559876"),
    ("PR contact 2035551234", "2035551234"),
    ("jobs: call 2035551234", "2035551234"),
    ("call 2035551234/ext 5", "2035551234"),
    ("check id 123456789", "123456789"),
    ("build site phone 2035551234", "2035551234"),
    ("my job number is 2035551234", "2035551234"),
    ("run to the bank 12345678901", "12345678901"),
    ("deploy fee card 4111111111111111", "4111111111111111"),
]


@pytest.mark.parametrize(("text", "number"), PERSONAL_NUMBERS)
def test_a_slash_or_a_common_word_does_not_exempt_a_personal_number(text, number):
    redacted = redact_pii(text)
    assert number not in redacted and "[REDACTED_" in redacted, redacted


DEVELOPER_NUMBERS = [
    "gh run view 42664995945",
    "gh run watch 42664995945 --exit-status",
    "Release run 40233615394 is waiting",
    "kill-9 soak job passed in CI run 40233615394",
    "run id 42664995945, job #71234567890",
    "run_id=42664995945",
    "TODO: Banking redesign UI B1-B8 + PR (migrations 590000000-599999)",
    "www.facebook.com/100063512345678 (redirects heartandsoledancestudio)",
    "https://github.com/acme/widget/actions/runs/42664995945/job/71234567890",
    "App Store Connect app ID 1234567890, bundle com.example.app",
    "app_id=6810035908 team id 1234567890",
    "epoch 1762735701",
    "build 1234567890 deployed to staging",
    "see artifacts/20261110/1234567890123/report.html",
    "https://www.facebook.com/profile/1000635123456789 is the page",
]


@pytest.mark.parametrize("text", DEVELOPER_NUMBERS)
def test_developer_identifiers_are_still_kept(text):
    assert redact_pii(text) == text
    assert scan_for_pii(text).matches == []


def test_a_bare_phone_number_is_named_a_phone_number():
    """Two patterns match the same ten digits; the specific one names the placeholder."""
    assert redact_pii("2035551234") == "[REDACTED_PHONE_US]"
    assert redact_pii("my job number is 2035551234") == "my job number is [REDACTED_PHONE_US]"
    assert redact_pii("4111111111111111") == "[REDACTED_CREDIT_CARD]"
    # An account label keeps the account name; so does a number no other pattern matches.
    assert redact_pii("acct 1234567890") == "acct [REDACTED_BANK_ACCOUNT]"
    assert redact_pii("wire to 12345678901 today") == "wire to [REDACTED_BANK_ACCOUNT] today"
    detector = PIIDetector(enabled=True, mode="redact")
    assert detector.hash_pii("2035551234").startswith("[HASH_PHONE_US:")


# ---------------------------------------------------------------------------
# 2. Short tokens in a URL
# ---------------------------------------------------------------------------

URL_TOKENS = [
    ("https://example.com/cb?code=a8Kd92LmQz71XyPv03RtUw5Nq2Zp1", "a8Kd92LmQz71XyPv03RtUw5Nq2Zp1"),
    ("https://example.com/cb#access_token=a8Kd92LmQz71XyPv03RtUw5Nq2Zp1", "a8Kd92LmQz71XyPv03RtUw5Nq2Zp1"),
    ("https://example.com/reset/a8Kd92LmQz71XyPv03RtUw5N/Zq3Lp8Xw21MnBv5Tr9Kd0Aa", "a8Kd92LmQz71XyPv03RtUw5N"),
    ("https://example.com/reset/a8Kd92LmQz71XyPv03RtUw5N/Zq3Lp8Xw21MnBv5Tr9Kd0Aa", "Zq3Lp8Xw21MnBv5Tr9Kd0Aa"),
    ("https://api.example.com/v1/a8Kd92LmQz71XyPv03RtUw5Nq2Zp/data", "a8Kd92LmQz71XyPv03RtUw5Nq2Zp"),
    ("https://h.example.com/magic/Qz71XyPv03RtUw5Nq2Zp1a/confirm123", "Qz71XyPv03RtUw5Nq2Zp1a"),
    (
        "https://h.example.com/x?k=wJalrXUtnFEMI/K7MDENG/bPxRfiCYzQ3vT9kLm2X/a8Kd92LmQz71Xy+Pv03RtUw==",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzQ3vT9kLm2X/a8Kd92LmQz71Xy+Pv03RtUw",
    ),
    ("https://h.example.com/x?k=wJalrXUtnFEMI/K7MDENG/bPxRfiCYzQ3vT9kLm2X", "K7MDENG"),
]


@pytest.mark.parametrize(("text", "secret"), URL_TOKENS)
def test_a_token_in_a_url_is_still_redacted(text, secret):
    out = redact_secrets(text).text
    assert secret not in out and "[REDACTED:" in out, out
    assert secret not in redact_text(text)


def test_only_the_token_is_cut_out_of_the_url():
    out = redact_secrets("https://h.example.com/magic/Qz71XyPv03RtUw5Nq2Zp1a/confirm123").text
    assert out == "https://h.example.com/magic/[REDACTED:high_entropy_token]/confirm123"
    out = redact_secrets("https://example.com/cb?code=a8Kd92LmQz71XyPv03RtUw5Nq2Zp1&state=ok").text
    assert out == "https://example.com/cb?code=[REDACTED:high_entropy_token]&state=ok"


def _miss_rate(make, n=3000):
    missed = 0
    for _ in range(n):
        url, secret = make()
        if secret in redact_secrets(f"value {url} end").text:
            missed += 1
    return missed / n


def test_random_url_tokens_are_caught_at_the_old_rate():
    """Generated tokens, as a reset link, a magic link and a signed query value carry them."""

    def reset():
        token = secrets.token_urlsafe(21)
        return f"https://h.example.com/reset?token2={token}", token

    def magic():
        token = secrets.token_urlsafe(16)
        return f"https://h.example.com/magic/{token}/confirm123", token

    def signed():
        token = base64.b64encode(secrets.token_bytes(48)).decode()
        return f"https://h.example.com/x?k={token}", token.rstrip("=")

    assert _miss_rate(reset) < 0.05
    assert _miss_rate(magic) < 0.08
    assert _miss_rate(signed) < 0.05


URLS_KEPT = [
    "YouTube https://www.youtube.com/channel/UCa1B2c3D4e5F6g7H8i9J0kL",
    "https://github.com/acme/Widget-Service/actions/runs/42664995945/job/7Ab",
    "see www.example.com/docs/Getting-Started/Install-Guide-v2 for setup",
    "https://github.com/acme/widget/blob/main/src/components/UserProfileCardV2Container/index.tsx",
    "https://github.com/acme/widget/tree/feature/Add-OAuth2-Login-For-Enterprise-SSO",
    "https://example.com/docs/api/v2/getUserProfileById2Handler/overview-and-examples",
    "https://github.com/acme/widget/commit/0a00117637e3c478c7d0200ad37ef187736d3d9a",
    "https://example.com/login?next=/docs/Getting-Started/Install-Guide-v2/overview",
]


@pytest.mark.parametrize("text", URLS_KEPT)
def test_ordinary_urls_are_still_kept(text):
    result = redact_secrets(text)
    assert result.text == text and not result.redacted


def test_a_prefixed_generated_token_is_not_a_name():
    for token in (
        "sess_ABCDEFGHJKLMNPQRSTUVWXYZABCDEFGHJK9",
        "tok-abcdefghjklmnpqrstuvwxyzabcdefghjkXY9",
    ):
        assert token not in redact_secrets(f"the value {token} was pasted").text
    for name in (
        "cd T7/Codex-Worktrees/bth-ai-setup-20261107 && npm test",
        "branch feature/Add-OAuth2-Login-For-Enterprise-SSO",
        "node --test --test-name-pattern=Checkout2Flow_handlesRetries_3x",
    ):
        assert redact_secrets(name).text == name


# ---------------------------------------------------------------------------
# 3. Env assignments: a numeric password is not a count
# ---------------------------------------------------------------------------

NUMERIC_SECRETS = [
    ("DB_PASS=123456", "123456"),
    ("SSHPASS=123456 sshpass -e ssh host", "123456"),
    ("SMTP_PASS=4821", "4821"),
    ("ADMIN_PASS=0000", "0000"),
    ("PGPASS=1234567", "1234567"),
    ("API_KEYS=1234567", "1234567"),
    ("AUTH_TOKENS=9912345", "9912345"),
    ("password=123456", "123456"),
    ("DB_PASS=123456 retries=3 FAIL=0", "123456"),
    ("SECRETKEYBASE=abc123def", "abc123def"),
    ("APIKEYV2=abc123def", "abc123def"),
    ("tokenvalue=abc123def", "abc123def"),
    ("passwordhash=abc123def", "abc123def"),
    ("PASSWORDFILE=/etc/x", "/etc/x"),
    ("secretValue=abc123def", "abc123def"),
    ("dbPass=123456", "123456"),
]


@pytest.mark.parametrize(("text", "secret"), NUMERIC_SECRETS)
def test_a_credential_assignment_is_not_a_count(text, secret):
    name = text.split("=", 1)[0]
    assert redact_text(text).startswith(f"{name}=[REDACTED:env]"), redact_text(text)
    assert secret not in redact_text(text).split(" ")[0]


COUNTS = [
    "Test: node --test tests/*.test.mjs passed=41 failed=0",
    "Test: pytest -q passed=None failed=None",
    "PASSED=41 FAILED=0",
    "CI summary: PASS=120 FAIL=0 SKIP=3",
    "TESTS_PASS=128 TESTS_FAIL=2",
    "SUITE_PASS=128 SUITE_FAIL=2",
    "unit_pass=12 unit_fail=0",
    "max_tokens=4096 tokens=512",
    "maxTokens=4096 input_tokens=1200 outputTokens=300",
    "tokenizer=bpe keys=3 num_keys=3",
    "bypass=true compass=north",
]


@pytest.mark.parametrize("text", COUNTS)
def test_test_counts_and_token_counts_are_still_kept(text):
    assert redact_text(text) == text
    assert outbound("promotion", text) == text
