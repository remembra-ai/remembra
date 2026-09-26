"""Server-computed trust fields cannot be set by clients (Phase 0 security).

``health``, ``quality`` and ``confidence`` on a memory are server-computed
(handoff health, state quality, confidence). A client that could write them
could promote its own handoff to "verified". They are reserved next to the
relay block: every client write path drops them, and the relay close never
copies them from the request.

Production routes over a real SQLite ``Database`` and a real ``MemoryService``
(``agent_api_harness``; only the vector store and embedder are fakes).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.services.relay import RESERVED_METADATA_KEYS, strip_reserved_metadata
from tests.agent_api_harness import build_api, row

TRUST = {"health": "ready", "quality": "verified", "confidence": 0.99}


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _as(api, **kwargs):
    user = AuthenticatedUser(user_id="default_user", api_key_id="k1", rate_limit_tier="standard", **kwargs)
    api["app"].dependency_overrides[get_current_user] = lambda: user
    return user


def _post(api, path, body, status=200):
    res = api["http"].post(f"/api/v1{path}", json=body)
    assert res.status_code == status, res.text
    return res.json()


def _meta(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value or "{}")


def _metas_by_content(api, needle: str) -> list[dict[str, Any]]:
    async def _q() -> list[Any]:
        cursor = await api["app"].state.db.conn.execute(
            "SELECT metadata FROM memories WHERE content LIKE ? AND superseded_by IS NULL", (f"%{needle}%",)
        )
        return [r[0] for r in await cursor.fetchall()]

    rows = api["http"].portal.call(_q)
    assert rows, f"no memory stored for {needle!r}"
    return [_meta(r) for r in rows]


def _assert_clean(meta: dict[str, Any], keep: dict[str, Any] | None = None) -> None:
    for key in TRUST:
        assert key not in meta, (key, meta)
    for key, value in (keep or {}).items():
        assert meta.get(key) == value, (key, meta)


def test_reserved_keys_cover_the_trust_fields():
    assert {"relay", "relay_key", "health", "quality", "confidence"} <= set(RESERVED_METADATA_KEYS)
    assert strip_reserved_metadata(None) is None
    assert strip_reserved_metadata({}) == {}
    src = {"quality": "verified", "topic": "x"}
    assert strip_reserved_metadata(src) == {"topic": "x"}
    assert src == {"quality": "verified", "topic": "x"}  # input is not mutated


def test_store_drops_client_trust_fields(api):
    _as(api)
    out = _post(api, "/memories", {"content": "Store trust probe alpha", "metadata": {**TRUST, "topic": "t1"}}, status=201)
    _assert_clean(_meta(row(api, out["id"])["metadata"]), keep={"topic": "t1"})
    # Atomic stores (skip_extraction) and typed handoffs take the same path.
    out = _post(
        api,
        "/memories",
        {"content": "Handoff trust probe beta", "memory_type": "handoff", "skip_extraction": True, "metadata": TRUST},
        status=201,
    )
    _assert_clean(_meta(row(api, out["id"])["metadata"]))


def test_batch_and_bulk_drop_client_trust_fields(api):
    _as(api)
    _post(api, "/memories/batch", {"items": [{"content": "Batch trust probe gamma", "metadata": {**TRUST, "k": 1}}]}, status=201)
    for meta in _metas_by_content(api, "Batch trust probe gamma"):
        _assert_clean(meta, keep={"k": 1})

    async def embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    api["app"].state.memory_service.embeddings.embed_batch = embed_batch  # the harness fake has no batch call
    _post(api, "/memories/bulk", {"items": [{"content": "Bulk trust probe delta", "metadata": {**TRUST, "k": 2}}]}, status=201)
    for meta in _metas_by_content(api, "Bulk trust probe delta"):
        _assert_clean(meta, keep={"k": 2})


def test_patch_update_cannot_set_or_overwrite_trust_fields(api):
    async def extract(content: str, reference_date: Any = None) -> list[str]:
        return [content]

    api["app"].state.memory_service.extractor.extract = extract  # the harness fake predates reference_date
    _as(api)
    out = _post(api, "/memories", {"content": "Patch trust probe epsilon", "metadata": {"topic": "p"}}, status=201)
    res = api["http"].patch(
        f"/api/v1/memories/{out['id']}",
        json={"content": "Patch trust probe epsilon edited", "metadata": {**TRUST, "extra": "yes"}},
    )
    assert res.status_code == 200, res.text
    for meta in _metas_by_content(api, "Patch trust probe epsilon edited"):
        _assert_clean(meta, keep={"extra": "yes"})


def test_supersede_drops_client_trust_fields(api):
    _as(api)
    out = _post(api, "/memories", {"content": "Supersede trust probe zeta"}, status=201)
    _post(
        api,
        f"/memories/{out['id']}/supersede",
        {"new_content": "Supersede trust probe zeta replaced", "reason": "test", "metadata": {**TRUST, "why": "r"}},
    )
    for meta in _metas_by_content(api, "zeta replaced"):
        _assert_clean(meta, keep={"why": "r"})


def test_status_upsert_drops_client_trust_fields(api):
    _as(api)
    _post(
        api, "/session/status", {"key": "deploy:web", "value": "green", "project_id": "widget", "metadata": {**TRUST, "by": "ci"}}
    )
    for meta in _metas_by_content(api, "green"):
        _assert_clean(meta, keep={"by": "ci"})


def test_import_drops_relay_block_and_trust_fields(api):
    _as(api)
    data = json.dumps(
        [
            {
                "content": "Imported trust probe eta with enough words",
                "metadata": {**TRUST, "relay": {"agent_verified": True}, "relay_key": "x", "origin": "export"},
            }
        ]
    )
    out = _post(api, "/transfer/import", {"format": "json", "data": data, "project_id": "widget"})
    assert out["imported"] == 1, out
    for meta in _metas_by_content(api, "Imported trust probe eta"):
        _assert_clean(meta, keep={"origin": "export"})
        assert "relay" not in meta and "relay_key" not in meta


def test_session_close_never_copies_client_trust_fields(api):
    _as(api)
    body = {
        "agent_id": "claude-code",
        "session_id": "s-trust",
        "project_id": "widget",
        # Extra fields a client might try at every level of the close request.
        "facts": {"branch": "main", "next_step": "ship it", **TRUST},
        **TRUST,
        "metadata": TRUST,
    }
    out = _post(api, "/session/close", body)
    meta = _meta(row(api, out["handoff_id"])["metadata"])
    _assert_clean(meta)
    relay = dict(meta["relay"])
    # R-21: the relay block's "health" is the server's own grade of the facts,
    # never the client's value ("ready" above).
    health = relay.pop("health")
    assert health == out["health"] and health["rules_version"] == 1
    assert health["status"] == "ready" and health["missing"] == []  # graded from the facts: nothing left open
    _assert_clean(relay)
    assert relay["agent_id"] == "claude-code"
