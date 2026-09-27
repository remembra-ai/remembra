"""DELETE /api/v1/memories?entity=<name> deletes only that entity's memories, never the account.

Before 0.16.1 the route passed the caller's user_id on every call and
``MemoryService.forget`` checked "user_id given" (wipe the account) before
"entity given" (a TODO), so a delete by entity erased every memory, entity,
relationship and decision log in the account and reported the wrong count.

Production routes over real SQLite (tests/agent_api_harness.py) and the real
QdrantStore on an in-memory Qdrant, so the tests also see what a delete
removes from the vector store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from qdrant_client import AsyncQdrantClient

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.client.memory import MemoryError
from remembra.config import Settings
from remembra.models.memory import Entity, Memory, Relationship
from remembra.storage.qdrant import QdrantStore
from tests.agent_api_harness import build_api

USER = "default_user"  # the caller when auth is disabled
THEIRS_POINT = "00000000-0000-4000-8000-000000000001"


async def _in_memory_qdrant() -> QdrantStore:
    store = QdrantStore(Settings(openai_api_key="test", embedding_dimensions=3, qdrant_collection="forget_entity"))
    store._client = AsyncQdrantClient(location=":memory:")
    await store.init_collection(3)
    return store


@pytest.fixture()
def api(tmp_path):
    for env in build_api(tmp_path):
        store = env["http"].portal.call(_in_memory_qdrant)
        env["app"].state.memory_service.qdrant = store
        env["vectors"] = store
        yield env


def _run(api: dict[str, Any], fn: Any, *args: Any) -> Any:
    return api["http"].portal.call(fn, *args)


def _db(api: dict[str, Any]) -> Any:
    return api["app"].state.db


def _store(api: dict[str, Any], content: str, project: str = "alpha") -> str:
    res = api["http"].post("/api/v1/memories", json={"content": content, "project_id": project, "skip_extraction": True})
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


def _entity(api: dict[str, Any], name: str, memory_ids: list[str], project: str = "alpha", **kw: Any) -> str:
    """Persist an entity and its memory links the way entity extraction does."""
    user_id = kw.pop("user_id", USER)
    entity = Entity(canonical_name=name, type=kw.pop("type", "person"), aliases=kw.pop("aliases", []), **kw)

    async def _save() -> None:
        await _db(api).save_entity(entity, user_id, project)
        for mid in memory_ids:
            await _db(api).link_memory_to_entity(mid, entity.id)

    _run(api, _save)
    return entity.id


def _rows(api: dict[str, Any], sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    async def _q() -> list[tuple[Any, ...]]:
        cursor = await _db(api).conn.execute(sql, params)
        return [tuple(r) for r in await cursor.fetchall()]

    return _run(api, _q)


def _memory_ids(api: dict[str, Any], user_id: str = USER) -> set[str]:
    return {r[0] for r in _rows(api, "SELECT id FROM memories WHERE user_id = ?", (user_id,))}


def _vector_ids(api: dict[str, Any], user_id: str = USER) -> set[str]:
    async def _scroll() -> set[str]:
        client = await api["vectors"]._get_client()
        points, _ = await client.scroll(collection_name=api["vectors"].collection_name, limit=1000, with_payload=True)
        return {str(p.id) for p in points if (p.payload or {}).get("user_id") == user_id}

    return _run(api, _scroll)


def _decision_log(api: dict[str, Any], user_id: str = USER) -> None:
    async def _log() -> None:
        await _db(api).log_decision("entity_coref", "shadow", user_id, "alpha", None, "John", "new", "new", None, True, 1.0)

    _run(api, _log)


def test_delete_by_entity_does_not_wipe_the_account(api):
    john = [_store(api, "John ships the invoices on Friday"), _store(api, "John prefers Postgres")]
    others = [
        _store(api, "Alice owns the roadmap"),
        _store(api, "The deploy runs at 9"),
        _store(api, "Beta release is in March", project="beta"),
        _store(api, "Beta uses Redis", project="beta"),
    ]
    _entity(api, "John", john)
    _entity(api, "Alice", others[:1])
    _decision_log(api)
    assert _memory_ids(api) == {*john, *others}
    assert _vector_ids(api) == {*john, *others}

    res = api["http"].delete("/api/v1/memories", params={"entity": "John"})

    assert res.status_code == 200, res.text
    # The account is still there: every memory not linked to John, the other entity, the decision log.
    assert _memory_ids(api) == set(others)
    assert _vector_ids(api) == set(others)
    assert _rows(api, "SELECT canonical_name FROM entities WHERE user_id = ?", (USER,)) == [("Alice",)]
    assert _rows(api, "SELECT count(*) FROM decision_log WHERE user_id = ?", (USER,)) == [(1,)]
    assert res.json()["deleted_memories"] == 2


def _as(api: dict[str, Any], **kwargs: Any) -> None:
    user = AuthenticatedUser(user_id=USER, api_key_id="k1", rate_limit_tier="standard", **kwargs)
    api["app"].dependency_overrides[get_current_user] = lambda: user


def _forget(api: dict[str, Any], **params: str) -> Any:
    return api["http"].delete("/api/v1/memories", params=params)


def _relationship(api: dict[str, Any], rel: Relationship) -> str:
    _run(api, _db(api).save_relationship, rel)
    return rel.id


def test_entity_delete_takes_the_entity_and_the_relationships_its_memories_carried(api):
    john = [_store(api, "John ships the invoices on Friday"), _store(api, "John and Alice run the Friday review")]
    alice_only = _store(api, "Alice owns the roadmap")
    plain = _store(api, "The deploy runs at 9")
    john_id = _entity(api, "John", john, aliases=["Johnny"])
    alice_id = _entity(api, "Alice", [john[1], alice_only])
    acme_id = _entity(api, "Acme", [alice_only], type="organization")
    extracted = _relationship(
        api, Relationship(from_entity_id=john_id, to_entity_id=alice_id, type="works_with", source_memory_id=john[1])
    )
    unsourced = _relationship(api, Relationship(from_entity_id=john_id, to_entity_id=acme_id, type="works_at"))
    older = _relationship(
        api, Relationship(from_entity_id=alice_id, to_entity_id=acme_id, type="works_at", source_memory_id=alice_only)
    )
    # A newer edge taken from a John memory supersedes an edge that stays (FK on superseded_by).
    newer = _relationship(
        api, Relationship(from_entity_id=alice_id, to_entity_id=acme_id, type="leads", source_memory_id=john[1])
    )
    _run(api, _db(api).supersede_relationship, older, newer)

    # An alias, in another case.
    res = _forget(api, entity="johnny")

    assert res.status_code == 200, res.text
    assert res.json() == {"deleted_memories": 2, "deleted_entities": 1, "deleted_relationships": 3}
    assert _memory_ids(api) == {alice_only, plain} and _vector_ids(api) == {alice_only, plain}
    assert {r[0] for r in _rows(api, "SELECT canonical_name FROM entities")} == {"Alice", "Acme"}
    # Alice was also named in a deleted memory: she stays, linked to what is left.
    assert _rows(api, "SELECT memory_id FROM memory_entities WHERE entity_id = ?", (alice_id,)) == [(alice_only,)]
    assert _rows(api, "SELECT id, superseded_by FROM relationships") == [(older, None)]
    assert {extracted, unsourced, newer}.isdisjoint(r[0] for r in _rows(api, "SELECT id FROM relationships"))
    marks = ",".join("?" * len(john))
    assert _rows(api, f"SELECT count(*) FROM memories_fts WHERE id IN ({marks})", tuple(john)) == [(0,)]
    assert _rows(api, "SELECT count(*) FROM memories_fts WHERE id = ?", (plain,)) == [(1,)]


def test_the_entity_name_matches_exactly_never_as_a_pattern_or_substring(api):
    john = _store(api, "John ships the invoices")
    rest = [_store(api, "Alice owns the roadmap"), _store(api, "Joan runs payroll")]
    _entity(api, "John", [john], aliases=["J. Smith"])
    _entity(api, "Alice", rest[:1])
    _entity(api, "Joan", rest[1:])

    for name in ("Jo", "%", "_ohn", "Joh%", "John Smith", '"John"', "J.%", "*"):
        res = _forget(api, entity=name)
        assert res.status_code == 200, res.text
        assert res.json()["deleted_memories"] == 0, name
    assert _memory_ids(api) == {john, *rest}

    assert _forget(api, entity="  JOHN ").json()["deleted_memories"] == 1
    assert _memory_ids(api) == set(rest)


def test_project_id_limits_an_entity_delete_to_that_project_and_never_wipes_it(api):
    alpha_john = _store(api, "John reviews alpha PRs", project="alpha")
    beta_john = _store(api, "John reviews beta PRs", project="beta")
    beta_other = _store(api, "Beta uses Redis", project="beta")
    _entity(api, "John", [alpha_john], project="alpha")
    _entity(api, "John", [beta_john], project="beta")

    res = _forget(api, entity="John", project_id="beta")

    assert res.status_code == 200, res.text
    assert res.json() == {"deleted_memories": 1, "deleted_entities": 1, "deleted_relationships": 0}
    assert _memory_ids(api) == {alpha_john, beta_other} and _vector_ids(api) == {alpha_john, beta_other}
    assert _rows(api, "SELECT project_id FROM entities") == [("alpha",)]


def test_another_account_keeps_its_memories_about_the_same_entity(api):
    mine = _store(api, "John ships the invoices")
    _entity(api, "John", [mine])

    async def _theirs() -> None:
        created = datetime(2026, 9, 1, tzinfo=UTC)
        await _db(api).save_memory_metadata(
            "theirs", "other_user", "alpha", "John is my brother", ["John is my brother"], {}, created
        )
        await api["vectors"].upsert(
            Memory(id=THEIRS_POINT, user_id="other_user", project_id="alpha", content="x", embedding=[0.1, 0.2, 0.3])
        )

    _run(api, _theirs)
    _entity(api, "John", ["theirs"], user_id="other_user")

    assert _forget(api, entity="John").json()["deleted_memories"] == 1
    assert _memory_ids(api) == set() and _memory_ids(api, "other_user") == {"theirs"}
    assert _vector_ids(api, "other_user") == {THEIRS_POINT}
    assert _rows(api, "SELECT user_id FROM entities") == [("other_user",)]


def test_only_all_memories_true_deletes_the_account(api):
    notes = [_store(api, f"note {i}") for i in range(3)]
    _entity(api, "John", notes[:1])
    _decision_log(api)

    for params in (
        {},
        {"all_memories": "false"},
        {"entity": "   "},
        {"entity": "John", "all_memories": "true"},
        {"memory_id": notes[0], "entity": "John"},
        {"memory_id": notes[0], "all_memories": "true"},
        {"entity": "John", "project_id": ""},
        {"project_id": " "},
    ):
        res = _forget(api, **params)
        assert res.status_code == 422, (params, res.text)
    assert _memory_ids(api) == set(notes) and _vector_ids(api) == set(notes)

    res = _forget(api, all_memories="true")

    assert res.status_code == 200, res.text
    assert res.json()["deleted_memories"] == 3 and res.json()["deleted_entities"] == 1
    assert _memory_ids(api) == set() and _vector_ids(api) == set()
    assert _rows(api, "SELECT count(*) FROM decision_log WHERE user_id = ?", (USER,)) == [(0,)]


def test_a_project_scoped_key_deletes_by_entity_only_inside_its_projects(api):
    alpha = _store(api, "John reviews alpha PRs", project="alpha")
    beta = _store(api, "John reviews beta PRs", project="beta")
    _entity(api, "John", [alpha], project="alpha")
    _entity(api, "John", [beta], project="beta")

    _as(api, project_ids=["beta"])  # a single-project key: its project is the scope
    assert _forget(api, entity="John").json()["deleted_memories"] == 1
    assert _memory_ids(api) == {alpha}

    _as(api, project_ids=["alpha", "beta"])
    assert _forget(api, entity="John").status_code == 400  # which project? never all of them
    assert _forget(api, entity="John", project_id="gamma").status_code == 403
    assert _forget(api, all_memories="true").status_code == 400
    assert _memory_ids(api) == {alpha}
    assert _forget(api, entity="John", project_id="alpha").json()["deleted_memories"] == 1
    assert _memory_ids(api) == set()


def test_the_python_sdk_deletes_by_entity_and_never_wipes_by_default(api):
    client = api["make_client"](project="alpha")
    john = [_store(api, "John ships the invoices"), _store(api, "John reviews beta PRs", project="beta")]
    others = [_store(api, "Alice owns the roadmap"), _store(api, "Beta uses Redis", project="beta")]
    _entity(api, "John", john[:1], project="alpha")
    _entity(api, "John", john[1:], project="beta")

    with pytest.raises(MemoryError):
        client.forget()
    assert _memory_ids(api) == {*john, *others}

    assert client.forget(entity="John", project_id="beta").deleted_memories == 1
    assert _memory_ids(api) == {john[0], *others}
    result = client.forget(entity="John")  # every project
    assert (result.deleted_memories, result.deleted_entities) == (1, 1)
    assert _memory_ids(api) == set(others)

    assert client.forget(all_memories=True).deleted_memories == 2
    assert _memory_ids(api) == set()


def test_the_service_never_widens_a_delete_because_user_id_is_set(api):
    notes = [_store(api, "John ships the invoices"), _store(api, "Alice owns the roadmap")]
    _entity(api, "John", notes[:1])
    service = api["app"].state.memory_service

    for kwargs in ({"user_id": USER}, {"user_id": USER, "entity": "  "}, {"entity": "John"}, {"all_memories": True}):
        with pytest.raises(ValueError):
            _run(api, lambda kw=kwargs: service.forget(**kw))
    assert _memory_ids(api) == set(notes) and _vector_ids(api) == set(notes)
