"""Deployment env values that used to crash the boot (the container restart-looped).

Each case sets the variable exactly as an operator would in Coolify and builds
``Settings`` from the environment, the same way ``get_settings()`` does.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from remembra.config import Settings

DEFAULT_PROXIES = ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name, value in env.items():
        monkeypatch.setenv(f"REMEMBRA_{name.upper()}", value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_public_url_is_unset(monkeypatch, value):
    assert _settings(monkeypatch, public_url=value).public_url is None


def test_public_url_trailing_slash_normalized(monkeypatch):
    assert _settings(monkeypatch, public_url="https://api.remembra.dev/").public_url == "https://api.remembra.dev"


@pytest.mark.parametrize("value", ["api.remembra.dev", "http://api.remembra.dev", "https://api.remembra.dev/api"])
def test_invalid_public_url_still_rejected(monkeypatch, value):
    # The OAuth issuer is never guessed: a wrong origin must fail loudly.
    with pytest.raises(ValidationError, match="public_url"):
        _settings(monkeypatch, public_url=value)


def test_connector_without_public_url_still_rejected(monkeypatch):
    with pytest.raises(ValidationError, match="connector_enabled requires public_url"):
        _settings(monkeypatch, connector_enabled="true", auth_enabled="true", public_url="")


@pytest.mark.parametrize("name", ["memory_cap_notice_effective_at", "unverified_credit_cap_effective_at"])
def test_blank_effective_dates_are_unset(monkeypatch, name):
    assert getattr(_settings(monkeypatch, **{name: ""}), name) is None
    assert getattr(_settings(monkeypatch, **{name: "2026-10-25"}), name) == datetime(2026, 10, 25)
    with pytest.raises(ValidationError):
        _settings(monkeypatch, **{name: "Oct 25 2026"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["gmail.com", "outlook.com"]', ["gmail.com", "outlook.com"]),
        ("gmail.com, outlook.com", ["gmail.com", "outlook.com"]),
        ("gmail.com", ["gmail.com"]),
        ("gmail.com,,", ["gmail.com"]),
        ("[]", []),
    ],
)
def test_list_settings_accept_json_or_comma_lists(monkeypatch, raw, expected):
    s = _settings(
        monkeypatch,
        signup_domain_limit_exempt=raw,
        connector_redirect_uris=raw,
        superadmin_user_ids=raw,
        owner_emails=raw,
        pii_exclusions=raw,
    )
    assert s.signup_domain_limit_exempt == expected
    assert s.connector_redirect_uris == expected
    assert s.superadmin_user_ids == expected
    assert s.owner_emails == expected
    assert s.pii_exclusions == expected


def test_blank_list_settings_keep_their_defaults(monkeypatch):
    s = _settings(monkeypatch, trusted_proxies="", signup_domain_limit_exempt="", superadmin_user_ids=" ")
    # A blank value must not silently empty the proxy list: every client would
    # then share the proxy's rate-limit bucket.
    assert s.trusted_proxies == DEFAULT_PROXIES
    assert "gmail.com" in s.signup_domain_limit_exempt
    assert s.superadmin_user_ids == []


def test_explicit_empty_json_list_still_clears(monkeypatch):
    assert _settings(monkeypatch, trusted_proxies="[]").trusted_proxies == []
    assert _settings(monkeypatch, trusted_proxies="10.0.0.0/8, fc00::/7").trusted_proxies == ["10.0.0.0/8", "fc00::/7"]


def test_cors_origins_comma_list_still_filtered(monkeypatch):
    s = _settings(monkeypatch, cors_origins="https://app.remembra.dev, http://localhost:3000")
    assert s.cors_origins == ["https://app.remembra.dev"]


def test_broken_json_list_fails_with_the_setting_name(monkeypatch):
    with pytest.raises(ValidationError, match="signup_domain_limit_exempt: invalid JSON array"):
        _settings(monkeypatch, signup_domain_limit_exempt='["gmail.com"')


def test_list_settings_passed_in_code_are_untouched():
    s = Settings(_env_file=None, trusted_proxies=[], superadmin_user_ids=["u1"])  # type: ignore[call-arg]
    assert s.trusted_proxies == [] and s.superadmin_user_ids == ["u1"]
