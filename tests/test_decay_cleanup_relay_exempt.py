"""POST /temporal/cleanup?include_decayed=true never archives relay handoffs or pinned rows.

The TTL path already skipped relay-structured handoffs/checkpoints
(tests/test_relay_record_ttl.py). The decay path did not: an owner running
``POST /api/v1/temporal/cleanup?include_decayed=true&dry_run=false`` archived
every handoff nobody had re-read for a few months, which is most of the trail.
Pinned rows were also archived, because the decay query never selected the
``pinned`` column the loop checked.

Real routes, real SQLite ``Database`` and ``MemoryService``
(``agent_api_harness``; only the vector store and the embedder are fakes).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.core.time import utcnow
from tests.agent_api_harness import build_api, seed

PROJECT = "widget"
RELAY_META = {
    "relay_key": "claude-code\x1fs-old",
    "agent_id": "claude-code",
    "session_id": "s-old",
    "source": "relay",
    "relay": {"v": 1, "agent_id": "claude-code", "done": ["shipped"], "next": "deploy"},
}
PROTECTED = {"relay_handoff", "relay_checkpoint", "pinned_fact"}
DECAYED = {"stale_fact", "free_handoff", "relay_observation", "malformed_handoff"}


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _owner(api) -> None:
    user = AuthenticatedUser(user_id="default_user", api_key_id="k1", rate_limit_tier="standard")
    api["app"].dependency_overrides[get_current_user] = lambda: user


def _call(api, fn):
    return api["http"].portal.call(fn)


def _seed_old_rows(api) -> None:
    """A year old and never accessed: far below the 0.1 prune threshold."""
    old = utcnow() - timedelta(days=365)
    kw = {"project_id": PROJECT}
    seed(api, "relay_handoff", "Handoff: shipped", old, memory_type="handoff", metadata=RELAY_META, **kw)
    seed(api, "relay_checkpoint", "Checkpoint: halfway", old, memory_type="checkpoint", metadata=RELAY_META, **kw)
    seed(api, "pinned_fact", "the prod DB is /data/remembra.db", old, metadata={}, pinned=True, **kw)
    seed(api, "stale_fact", "an old passing thought", old, metadata={}, **kw)
    # Not continuity records (same definition as the TTL exemption):
    seed(api, "free_handoff", "[SESSION END] notes", old, memory_type="handoff", metadata={}, **kw)
    seed(api, "relay_observation", "observation", old, memory_type="observation", metadata=RELAY_META, **kw)
    seed(api, "malformed_handoff", "bad", old, memory_type="handoff", metadata={}, **kw)

    async def _corrupt() -> None:
        db = api["app"].state.db
        await db.conn.execute("UPDATE memories SET metadata = 'not json' WHERE id = 'malformed_handoff'")
        await db.conn.commit()

    _call(api, _corrupt)


def _alive(api) -> set[str]:
    async def _ids() -> set[str]:
        cursor = await api["app"].state.db.conn.execute("SELECT id FROM memories")
        return {r[0] for r in await cursor.fetchall()}

    return _call(api, _ids)


def _archived(api) -> set[str]:
    async def _ids() -> set[str]:
        cursor = await api["app"].state.db.conn.execute("SELECT id FROM archived_memories")
        return {r[0] for r in await cursor.fetchall()}

    return _call(api, _ids)


def test_decay_cleanup_archives_stale_rows_but_never_relay_records_or_pinned(api):
    _owner(api)
    _seed_old_rows(api)

    preview = api["http"].post(f"/api/v1/temporal/cleanup?project_id={PROJECT}&include_decayed=true&dry_run=true")
    assert preview.status_code == 200, preview.text
    assert preview.json()["decayed_found"] == len(DECAYED)
    assert _alive(api) == PROTECTED | DECAYED  # a dry run changes nothing

    res = api["http"].post(f"/api/v1/temporal/cleanup?project_id={PROJECT}&include_decayed=true&dry_run=false")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["errors"] == []
    assert body["decayed_found"] == len(DECAYED)
    assert body["decayed_archived"] == len(DECAYED)
    assert _alive(api) == PROTECTED
    assert _archived(api) == DECAYED

    # The surviving handoff is intact and still reads as the relay record.
    async def _meta() -> dict:
        row = await api["app"].state.db.get_memory("relay_handoff")
        meta = row["metadata"]
        return meta if isinstance(meta, dict) else json.loads(meta)

    assert _call(api, _meta)["relay"]["next"] == "deploy"

    # A second run finds nothing more to prune.
    again = api["http"].post(f"/api/v1/temporal/cleanup?project_id={PROJECT}&include_decayed=true&dry_run=false")
    assert again.status_code == 200 and again.json()["decayed_found"] == 0


def test_decay_report_does_not_flag_protected_rows(api):
    _owner(api)
    _seed_old_rows(api)
    res = api["http"].get(f"/api/v1/temporal/decay/report?project_id={PROJECT}")
    assert res.status_code == 200, res.text
    flagged = {m["id"] for m in res.json()["memories"] if m["should_prune"]}
    assert flagged == DECAYED
    assert res.json()["prune_candidates"] == len(DECAYED)


def test_empty_project_decay_cleanup_is_a_no_op(api):
    _owner(api)
    res = api["http"].post("/api/v1/temporal/cleanup?project_id=nothing-here&include_decayed=true&dry_run=false")
    assert res.status_code == 200, res.text
    assert res.json()["decayed_found"] == 0 and res.json()["errors"] == []
