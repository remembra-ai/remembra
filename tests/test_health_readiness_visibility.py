"""Public readiness must preserve probe semantics without exposing operator details."""

from copy import deepcopy

import httpx
import pytest

from remembra.config import Settings

pytestmark = pytest.mark.asyncio

REPORT = {
    "status": "degraded",
    "degraded_components": ["embeddings"],
    "components": {"embeddings": {"status": "degraded", "model": "private-model", "error": "provider quota"}},
}


async def request_ready(monkeypatch, *, debug=False, token=None, authorization=None):
    import remembra.main as main

    settings = Settings(auth_enabled=False, debug=debug, metrics_token=token, cors_origins=["https://app.remembra.dev"])
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()

    class Checker:
        async def check(self):
            return deepcopy(REPORT)

    app.state.readiness = Checker()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Origin": "https://app.remembra.dev"}
        if authorization:
            headers["Authorization"] = authorization
        return await client.get("/health/ready", headers=headers)


@pytest.mark.parametrize("authorization", [None, "Bearer incorrect", "Basic probe-token", "Bearer probe-token-extra"])
async def test_production_readiness_hides_details_without_operator_token(monkeypatch, authorization):
    response = await request_ready(monkeypatch, token="probe-token", authorization=authorization)
    assert response.status_code == 200  # preserve the existing readiness probe contract
    assert response.json() == {"status": "degraded"}
    assert response.headers["cache-control"] == "no-store"
    vary = {value.strip().lower() for value in response.headers["vary"].split(",")}
    assert {"authorization", "origin"} <= vary


async def test_unconfigured_token_does_not_authorize_an_empty_bearer(monkeypatch):
    response = await request_ready(monkeypatch, authorization="Bearer ")
    assert response.json() == {"status": "degraded"}


async def test_operator_token_preserves_detailed_diagnostics(monkeypatch):
    response = await request_ready(monkeypatch, token="probe-token", authorization="Bearer probe-token")
    assert response.status_code == 200
    assert response.json()["components"] == REPORT["components"]
    assert response.json()["degraded_components"] == ["embeddings"]
    assert response.headers["cache-control"] == "no-store"


async def test_explicit_debug_mode_preserves_local_diagnostics(monkeypatch):
    response = await request_ready(monkeypatch, debug=True)
    assert response.json()["components"] == REPORT["components"]
