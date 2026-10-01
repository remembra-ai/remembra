"""Keep the capacity fixture from connecting to a real owner service or reusing its collection."""

from types import SimpleNamespace
import asyncio
import sys

import pytest

from tests.crew.e2e import server


def test_fixture_runs_the_installed_production_auto_loop():
    pytest.importorskip("uvloop")

    async def probe():
        return type(asyncio.get_running_loop()).__module__

    policy = asyncio.get_event_loop_policy()
    assert server.run_fixture(probe()).startswith("uvloop")
    assert asyncio.get_event_loop_policy() is policy


def test_fixture_labels_portable_asyncio_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvloop", None)

    async def probe():
        return type(asyncio.get_running_loop()).__module__

    assert server.run_fixture(probe()).startswith("asyncio")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:6333",
        "http://production.example:6333",
        "http://127.0.0.1:6333/private",
        "http://user:password@127.0.0.1:6333",
        "http://127.0.0.1:6333?token=synthetic",
        "http://127.0.0.1:6333#fragment",
    ],
)
async def test_load_backend_rejects_unsafe_routes_before_connect(monkeypatch, url):
    import qdrant_client

    monkeypatch.setenv("REMEMBRA_E2E_QDRANT_URL", url)

    def unexpected_connect(*args, **kwargs):
        pytest.fail("unsafe fixture route reached the client")

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", unexpected_connect)
    with pytest.raises(ValueError, match="isolated credential-free"):
        await server.memory_backend(SimpleNamespace(), None)


@pytest.mark.asyncio
async def test_server_backends_use_distinct_owned_collections(monkeypatch):
    import qdrant_client
    from remembra.storage import qdrant

    collections = []

    class Store:
        def __init__(self, settings):
            self.settings = settings

        async def init_collection(self, dim):
            collections.append(self.settings.qdrant_collection)

    monkeypatch.setenv("REMEMBRA_E2E_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", lambda **kwargs: object())
    monkeypatch.setattr(qdrant, "QdrantStore", Store)
    monkeypatch.setattr(server, "MemoryService", lambda **kwargs: SimpleNamespace())
    for _ in range(2):
        await server.memory_backend(SimpleNamespace(qdrant_collection="memories"), None)
    assert len(set(collections)) == 2
    assert all(name.startswith("e2e_load_") and name != "memories" for name in collections)
