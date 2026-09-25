"""SEC-4: global embedding provider switching is superadmin-only and rolls back completely."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from remembra.api.v1 import embeddings
from remembra.storage.embeddings import EmbeddingService
from tests.security_harness import make_settings, secure_app


class _FakeEmbedder:
    def __init__(self) -> None:
        self.closed = False

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1536

    async def close(self) -> None:
        self.closed = True


def _service(settings) -> tuple[EmbeddingService, _FakeEmbedder]:
    service = EmbeddingService(settings)
    live = _FakeEmbedder()
    service._embedder = live  # the currently serving client
    return service, live


def _reindex():
    return SimpleNamespace(
        start_reindex=AsyncMock(),
        get_status=AsyncMock(return_value=None),
        list_jobs=AsyncMock(return_value=[]),
        cancel=AsyncMock(return_value=False),
    )


async def test_regular_users_cannot_touch_global_embedding_config(tmp_path):
    settings = make_settings(openai_api_key="sk-original-server-key-000000000000")
    service, live = _service(settings)
    reindex = _reindex()
    async with secure_app(
        tmp_path, [embeddings.router], settings=settings, state={"embeddings": service, "reindex_manager": reindex}
    ) as h:
        uid = await h.create_user("tenant@example.com", verified=True)
        hdr = h.jwt(uid, "tenant@example.com")
        r = await h.client.post(
            "/api/v1/embeddings/switch",
            json={"provider": "openai", "model": "text-embedding-3-small", "api_key": "sk-attacker"},
            headers=hdr,
        )
        assert r.status_code == 403
        for method, path in [
            ("GET", "/api/v1/embeddings/reindex/status"),
            ("POST", "/api/v1/embeddings/reindex/cancel"),
            ("GET", "/api/v1/embeddings/reindex/history"),
        ]:
            assert (await h.client.request(method, path, headers=hdr)).status_code == 403
        reindex.cancel.assert_not_awaited()
        assert service.settings.openai_api_key == "sk-original-server-key-000000000000"
        assert service._embedder is live
        # Read-only info stays available to everyone.
        assert (await h.client.get("/api/v1/embeddings/info", headers=hdr)).status_code == 200


async def test_superadmin_cannot_inject_keys_and_failed_switch_restores_everything(tmp_path):
    settings = make_settings(openai_api_key="sk-original-server-key-000000000000", ollama_url="http://127.0.0.1:9")
    service, live = _service(settings)
    reindex = _reindex()
    async with secure_app(
        tmp_path, [embeddings.router], settings=settings, state={"embeddings": service, "reindex_manager": reindex}
    ) as h:
        owner = await h.create_user("owner@example.com", verified=True)
        hdr = h.jwt(owner, "owner@example.com")

        r = await h.client.post("/api/v1/embeddings/switch", json={"provider": "openai", "api_key": "sk-injected"}, headers=hdr)
        assert r.status_code == 400
        assert service.settings.openai_api_key == "sk-original-server-key-000000000000"

        before = (service.provider, service.model)
        # Real provider path that fails at the test embedding (nothing listens on port 9).
        r = await h.client.post(
            "/api/v1/embeddings/switch",
            json={"provider": "ollama", "model": "nomic-embed-text", "force": True, "auto_reindex": False},
            headers=hdr,
        )
        assert r.status_code == 400, r.text
        assert "sk-" not in r.text
        assert (service.provider, service.model) == before
        assert service._embedder is live  # the serving client itself is restored
        assert service.settings.openai_api_key == "sk-original-server-key-000000000000"
        reindex.start_reindex.assert_not_awaited()
