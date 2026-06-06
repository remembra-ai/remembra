"""get() must return None (a clean 404 upstream) for malformed memory IDs.

Qdrant point IDs must be a UUID or unsigned int. Passing a malformed id (e.g. a
non-UUID slug) used to reach the Qdrant fallback, which raised and surfaced to
API callers as a 500 (e.g. POST /memories/{garbage}/pin). The lookup now skips
Qdrant for ids it could never hold.
"""

from remembra.services.memory import MemoryService, _is_qdrant_point_id


def test_is_qdrant_point_id_accepts_uuid_and_int():
    assert _is_qdrant_point_id("00000000-0000-4000-8000-000000000000")
    assert _is_qdrant_point_id("ffcdcd1f-7f0c-4602-9517-060b474dcbbe")
    assert _is_qdrant_point_id("123")


def test_is_qdrant_point_id_rejects_garbage():
    assert not _is_qdrant_point_id("nonexistent-smoke-test-id")
    assert not _is_qdrant_point_id("../etc/passwd")
    assert not _is_qdrant_point_id("abc")
    assert not _is_qdrant_point_id("")


async def test_get_returns_none_for_malformed_id_without_touching_qdrant():
    class FakeDB:
        async def get_memory(self, mid):
            return None

    class FakeQdrant:
        async def get_by_id(self, mid):
            raise AssertionError("Qdrant must not be queried for a malformed id")

    svc = MemoryService.__new__(MemoryService)  # bypass heavy __init__
    svc.db = FakeDB()
    svc.qdrant = FakeQdrant()

    assert await svc.get("nonexistent-smoke-test-id") is None
