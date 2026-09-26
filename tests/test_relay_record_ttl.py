"""Relay-structured handoffs and checkpoints are never lost to TTL (Phase 0).

The structured handoff written by ``POST /session/close`` is the continuity
record the next agent picks up from. It must never get a TTL (not even the
server-wide ``default_ttl_days``), and every TTL cleanup path (the background
archive loop, the hard-delete job, ``POST /memories/cleanup-expired`` and the
database helper) must skip it even if a row somehow carries a past
``expires_at``. Free-text checkpoints keep their 7-day default.

Real SQLite ``Database`` and ``MemoryService`` (``agent_api_harness``; only the
vector store and the embedder are fakes).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.core.time import utcnow
from remembra.models.memory import StoreRequest
from remembra.services.memory import is_relay_record
from remembra.temporal.cleanup import TemporalCleanupJob
from tests.agent_api_harness import build_api, row, seed


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _owner(api) -> None:
    user = AuthenticatedUser(user_id="default_user", api_key_id="k1", rate_limit_tier="standard")
    api["app"].dependency_overrides[get_current_user] = lambda: user


def _call(api, fn):
    return api["http"].portal.call(fn)


RELAY_META = {
    "relay_key": "claude-code\x1fs-old",
    "agent_id": "claude-code",
    "session_id": "s-old",
    "source": "relay",
    "relay": {"v": 1, "agent_id": "claude-code", "done": ["shipped"], "next": "deploy"},
}


def _seed_expired_rows(api) -> None:
    """Rows whose expires_at is a week in the past."""
    past = utcnow() - timedelta(days=14)
    expired = utcnow() - timedelta(days=7)
    kw: dict[str, Any] = {"expires_at": expired, "project_id": "widget"}
    seed(api, "relay_handoff", "Handoff: shipped", past, memory_type="handoff", metadata=RELAY_META, **kw)
    seed(api, "relay_checkpoint", "Checkpoint: halfway", past, memory_type="checkpoint", metadata=RELAY_META, **kw)
    seed(api, "free_checkpoint", "progress note", past, memory_type="checkpoint", metadata={"agent_id": "codex"}, **kw)
    seed(api, "free_handoff", "[SESSION END] notes", past, memory_type="handoff", metadata={}, **kw)
    # A relay dict on a non-continuity type is not a continuity record.
    seed(api, "relay_observation", "observation", past, memory_type="observation", metadata=RELAY_META, **kw)
    # A non-dict relay value is not the server's block.
    seed(api, "fake_relay", "fake", past, memory_type="handoff", metadata={"relay": "yes"}, **kw)
    seed(api, "untyped", "plain fact", past, metadata={}, **kw)


def _set_raw_metadata(api, memory_id: str, raw: str | None) -> None:
    async def _upd() -> None:
        await api["app"].state.db.conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (raw, memory_id))
        await api["app"].state.db.conn.commit()

    _call(api, _upd)


def _alive(api) -> set[str]:
    async def _ids() -> set[str]:
        cursor = await api["app"].state.db.conn.execute("SELECT id FROM memories")
        return {r[0] for r in await cursor.fetchall()}

    return _call(api, _ids)


SURVIVORS = {"relay_handoff", "relay_checkpoint"}
CLEANED = {"free_checkpoint", "free_handoff", "relay_observation", "fake_relay", "untyped", "malformed"}


def _seed_all(api) -> None:
    _seed_expired_rows(api)
    past = utcnow() - timedelta(days=14)
    seed(api, "malformed", "bad", past, memory_type="handoff", metadata={}, expires_at=past, project_id="widget")
    _set_raw_metadata(api, "malformed", "not json")


def test_is_relay_record_edges():
    assert is_relay_record("handoff", {"relay": {}}) is True
    assert is_relay_record("checkpoint", {"relay": {"v": 2}}) is True
    assert is_relay_record("observation", {"relay": {}}) is False
    assert is_relay_record("handoff", {"relay": "x"}) is False
    assert is_relay_record("handoff", {}) is False
    assert is_relay_record("handoff", None) is False
    assert is_relay_record(None, {"relay": {}}) is False


def test_expired_looking_relay_handoff_survives_the_background_archive_job(api):
    _seed_all(api)
    job = TemporalCleanupJob(api["app"].state.db, api["app"].state.memory_service.qdrant, archive_expired=True)
    result = _call(api, lambda: job.run_cleanup())
    assert result["errors"] == []
    assert result["expired_found"] == len(CLEANED)
    alive = _alive(api)
    assert alive >= SURVIVORS
    assert not (CLEANED & alive)
    # The survivor is intact and still readable as the relay handoff.
    meta = row(api, "relay_handoff")["metadata"]
    meta = meta if isinstance(meta, dict) else json.loads(meta)
    assert meta["relay"]["next"] == "deploy"


def test_expired_looking_relay_rows_survive_hard_delete_paths(api):
    _seed_all(api)
    db = api["app"].state.db
    job = TemporalCleanupJob(db, api["app"].state.memory_service.qdrant, archive_expired=False)
    assert _call(api, lambda: job.run_cleanup(user_id="default_user"))["expired_deleted"] == len(CLEANED)
    assert _alive(api) >= SURVIVORS

    past = utcnow() - timedelta(days=3)
    seed(api, "free_again", "note", past, memory_type="checkpoint", metadata={}, expires_at=past)
    seed(api, "relay_again", "Handoff", past, memory_type="handoff", metadata=RELAY_META, expires_at=past)
    assert _call(api, lambda: db.cleanup_expired_memories()) == 1
    assert "relay_again" in _alive(api) and "free_again" not in _alive(api)

    seed(api, "free_third", "note", past, memory_type="checkpoint", metadata={}, expires_at=past)
    assert _call(api, lambda: api["app"].state.memory_service.cleanup_expired(user_id="default_user")) == 1
    assert {"relay_handoff", "relay_checkpoint", "relay_again"} <= _alive(api)


def test_cleanup_expired_endpoint_keeps_relay_handoffs(api):
    _owner(api)
    _seed_all(api)
    res = api["http"].post("/api/v1/memories/cleanup-expired")
    assert res.status_code == 200, res.text
    assert res.json()["deleted_count"] == len(CLEANED)
    assert _alive(api) >= SURVIVORS


def test_session_close_handoff_gets_no_ttl_even_with_a_server_default(api):
    _owner(api)
    service = api["app"].state.memory_service
    service.settings.default_ttl_days = 3
    res = api["http"].post(
        "/api/v1/session/close",
        json={"agent_id": "claude-code", "session_id": "s-1", "project_id": "widget", "facts": {"branch": "main"}},
    )
    assert res.status_code == 200, res.text
    handoff = row(api, res.json()["handoff_id"])
    assert handoff["expires_at"] is None
    # A plain memory still gets the server default; a free-text checkpoint its 7d default.
    plain = api["http"].post("/api/v1/memories", json={"content": "an ordinary fact", "project_id": "widget"})
    assert plain.status_code == 201 and row(api, plain.json()["id"])["expires_at"] is not None
    note = api["http"].post(
        "/api/v1/memories", json={"content": "free text progress", "memory_type": "checkpoint", "project_id": "widget"}
    )
    assert note.status_code == 201
    expires = row(api, note.json()["id"])["expires_at"]
    assert expires is not None
    left = (_as_dt(expires) - utcnow()).total_seconds()
    assert 6 * 86400 < left <= 7 * 86400


def test_relay_checkpoint_store_ignores_an_explicit_ttl(api):
    service = api["app"].state.memory_service

    async def _store() -> str:
        result = await service.store(
            StoreRequest(
                content="Checkpoint: halfway through the adapter",
                user_id="default_user",
                project_id="widget",
                memory_type="checkpoint",
                ttl="1h",
                metadata={"source": "relay", "relay": {"v": 1, "agent_id": "codex"}},
                skip_extraction=True,
            ),
            skip_extraction=True,
        )
        return str(result.id)

    memory_id = _call(api, _store)
    assert row(api, memory_id)["expires_at"] is None


def _as_dt(value: Any):
    from datetime import datetime

    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value)).replace(tzinfo=None)
