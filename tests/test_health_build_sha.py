from __future__ import annotations

import importlib

import pytest
from httpx import ASGITransport, AsyncClient


def _reset_settings_cache() -> None:
    import remembra.config as config

    config._settings = None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_health_includes_build_sha_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("REMEMBRA_BUILD_SHA", "deadbeef")
    monkeypatch.setenv("REMEMBRA_AUTH_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_RATE_LIMIT_ENABLED", "false")

    _reset_settings_cache()

    import remembra.main as main

    importlib.reload(main)
    app = main.app

    async def _ok_qdrant(_url: str):
        return {"status": "ok"}

    # main.py imports check_qdrant into module scope, so patch there.
    monkeypatch.setattr(main, "check_qdrant", _ok_qdrant)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["build_sha"] == "deadbeef"
