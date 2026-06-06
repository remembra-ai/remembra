"""Tests for require_master_key hardening (fail-closed + constant-time).

Previously, if no master key was configured the dependency allowed the request
through ("fail open"), leaving master-key admin endpoints (tenant signup, promo
admin) unprotected. It also compared the key with `!=`, which is timing-attackable.
"""

import types

import pytest
from fastapi import HTTPException

import remembra.auth.middleware as mw


class _FakeURL:
    path = "/api/v1/cloud/signup"


class _FakeRequest:
    def __init__(self) -> None:
        self.url = _FakeURL()
        self.headers: dict[str, str] = {}
        self.client = types.SimpleNamespace(host="1.2.3.4")


def _settings(*, auth_enabled=True, auth_master_key=None, debug=False):
    return types.SimpleNamespace(
        auth_enabled=auth_enabled,
        auth_master_key=auth_master_key,
        debug=debug,
    )


async def test_fail_closed_when_unconfigured_in_prod(monkeypatch):
    monkeypatch.setattr(mw, "get_settings", lambda: _settings(auth_master_key=None, debug=False))
    with pytest.raises(HTTPException) as exc:
        await mw.require_master_key(_FakeRequest(), api_key="anything")
    assert exc.value.status_code == 403


async def test_dev_bypass_when_unconfigured(monkeypatch):
    monkeypatch.setattr(mw, "get_settings", lambda: _settings(auth_master_key=None, debug=True))
    assert await mw.require_master_key(_FakeRequest(), api_key=None) is None


async def test_valid_master_key_accepted(monkeypatch):
    monkeypatch.setattr(mw, "get_settings", lambda: _settings(auth_master_key="s3cret-master-key", debug=False))
    assert await mw.require_master_key(_FakeRequest(), api_key="s3cret-master-key") is None


async def test_invalid_master_key_rejected(monkeypatch):
    monkeypatch.setattr(mw, "get_settings", lambda: _settings(auth_master_key="s3cret-master-key", debug=False))
    with pytest.raises(HTTPException) as exc:
        await mw.require_master_key(_FakeRequest(), api_key="wrong-key")
    assert exc.value.status_code == 401


async def test_missing_master_key_when_configured(monkeypatch):
    monkeypatch.setattr(mw, "get_settings", lambda: _settings(auth_master_key="s3cret-master-key", debug=False))
    with pytest.raises(HTTPException) as exc:
        await mw.require_master_key(_FakeRequest(), api_key=None)
    assert exc.value.status_code == 401


async def test_auth_disabled_passes(monkeypatch):
    monkeypatch.setattr(mw, "get_settings", lambda: _settings(auth_enabled=False))
    assert await mw.require_master_key(_FakeRequest(), api_key=None) is None
