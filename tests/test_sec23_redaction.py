"""SEC-23: credentials are detected and redacted; ordinary text is left alone.

All keys below are synthetic and assembled at runtime so no real-looking
credential literal is committed.
"""

from __future__ import annotations

import secrets
import string

import pytest

from remembra.security.secrets import redact_secrets


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    # Guarantee mixed classes so the synthetic key looks like a real one.
    body = "".join(secrets.choice(alphabet) for _ in range(n - 3))
    return body + "aZ7"


FAKE = {
    "remembra_key": "rem_" + secrets.token_urlsafe(32).replace("-", "a") + "Z9",
    "resend_key": "re_" + _rand(8) + "_" + _rand(24),
    "stripe_key": "sk_" + "live_" + _rand(24),
    "stripe_webhook_secret": "whsec_" + _rand(32),
    "anthropic_key": "sk-" + "ant-api03-" + _rand(40),
    "openai_key": "sk-" + "proj-" + _rand(40),
    "github_token": "gh" + "p_" + _rand(36),
    "slack_token": "xo" + "xb-" + "1234567890-" + _rand(24),
    "aws_access_key": "AK" + "IA" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
    "google_api_key": "AI" + "za" + _rand(35),
    "jwt": "eyJ" + _rand(20) + ".eyJ" + _rand(30) + "." + _rand(30),
}


@pytest.mark.parametrize("kind", sorted(FAKE))
def test_provider_keys_redacted(kind):
    value = FAKE[kind]
    text = f"Deploy note: the key is {value} (rotate quarterly)."
    result = redact_secrets(text)
    assert value not in result.text
    assert f"[REDACTED:{kind}]" in result.text, result.text
    assert result.text.startswith("Deploy note: the key is ")
    assert result.text.endswith(" (rotate quarterly).")


def test_structural_secrets_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + _rand(60) + "\n-----END RSA PRIVATE KEY-----"
    db_pw = "S3cr3t" + _rand(10)
    text = (
        f"key:\n{pem}\n"
        f"db: postgres://app:{db_pw}@db.internal:5432/main\n"
        "password: hunter22x\n"
        f"api_key = '{_rand(24)}'\n"
        f"Authorization: Bearer {_rand(40)}"
    )
    result = redact_secrets(text)
    assert "MIIEow" not in result.text
    assert db_pw not in result.text and "postgres://app:[REDACTED:url_credentials]@db.internal" in result.text
    assert "hunter22x" not in result.text
    assert "Bearer [REDACTED:bearer_token]" in result.text
    assert {"private_key", "url_credentials", "password", "secret", "bearer_token"} <= set(result.counts)


def test_unlabelled_high_entropy_token_redacted():
    token = _rand(48)
    assert token not in redact_secrets(f"use {token} for the cron job").text


@pytest.mark.parametrize(
    "text",
    [
        "Baseline commit f92e34d48005db6e70d450f9c37af320ecf5d622 fixed the auth bug.",
        "Memory id 3f2b8c1e-9a4d-4e2b-8f1a-2c3d4e5f6a7b was superseded.",
        "The password is required for login and the password reset link expires in 24h.",
        "Access token: refreshed hourly by the scheduler.",
        "We re_run the migration; call re_index_documents after deploy.",
        "Path /Users/dolphy/Projects/remembra/src/remembra/security/secrets.py was edited.",
        "sk-learn and scikit are libraries; rem_embedding_dimension_setting is a variable name.",
        "[REDACTED:resend_key] was already redacted.",
    ],
)
def test_ordinary_text_untouched(text):
    result = redact_secrets(text)
    assert result.text == text, result.text
    assert not result.redacted


def test_redaction_is_idempotent():
    once = redact_secrets(f"key {FAKE['resend_key']}").text
    assert redact_secrets(once).text == once


# Live 2026-09-25 dry run: most "high_entropy_token" hits were file paths with
# digits (/Volumes/T7/..., /Users/.../v2). Paths must survive; slash-bearing
# base64 secrets must not.
@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/T7/Projects/ChairTime2026/build42/ios/Runner",
        "/Users/dolphy/Projects/remembra-wt/ret/src/remembra/services",
        "~/Developer/CheckTheFridge/App/Sources/Views2",
        "clawd/projects/trademind/pxexec_practice/runs2026",
    ],
)
def test_file_paths_are_not_redacted(path: str) -> None:
    assert redact_secrets(f"repo lives at {path} now").text == f"repo lives at {path} now"


def test_slash_bearing_random_secret_is_still_redacted() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzk9sQ2Lm8Ew"
    assert secret not in redact_secrets(f"aws secret {secret}").text
