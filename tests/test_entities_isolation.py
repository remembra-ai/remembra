"""TI-H1: entity relationships never cross the project (or user) boundary.

Two halves, both exercised on the real code:

1. The sleep-time entity resolution pass merged look-alike entities across a
   user's projects: it re-pointed project p2's relationships and memory links at
   a p1 entity and deleted the p2 entity. It now merges only within a project.
2. /entities/{id}/relationships and /entities/relationship-search returned every
   edge touching the entity and filled in names without a scope, so a key limited
   to p2 read p1 entity names (and a cross-user edge echoed the other user's
   entity name). They now return only edges whose both ends belong to the caller's
   user and resolved project.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from remembra.api.v1 import entities
from remembra.models.memory import Entity, Relationship
from remembra.services.sleep_time import SleepTimeWorker
from remembra.storage.database import Database
from remembra.core.time import utcnow
from tests.security_harness import secure_app

ROUTERS = [entities.router]


def _worker(db: Database) -> SleepTimeWorker:
    service = SimpleNamespace(db=db, qdrant=None, embeddings=None, consolidator=None, entity_matcher=None)
    return SleepTimeWorker(settings=SimpleNamespace(consolidation_threshold=0.9), memory_service=service)


async def _entity(db: Database, name: str, kind: str, user_id: str, project_id: str) -> Entity:
    entity = Entity(canonical_name=name, type=kind)
    await db.save_entity(entity, user_id=user_id, project_id=project_id)
    return entity


async def _memory(db: Database, memory_id: str, user_id: str, project_id: str, entity: Entity) -> None:
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id=user_id,
        project_id=project_id,
        content=f"content {memory_id}",
        extracted_facts=[],
        metadata={},
        created_at=utcnow(),
    )
    await db.link_memory_to_entity(memory_id, entity.id)


async def _count(db: Database, sql: str, *params: Any) -> int:
    cursor = await db.conn.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def _cross_project_graph(db: Database, uid: str) -> dict[str, Entity]:
    """The sweep's repro: a confidential p1 company, and a p2 person who works at a similar name."""
    target = await _entity(db, "Falcon Acquisition Target", "company", uid, "p1")
    person = await _entity(db, "Dana Whistle", "person", uid, "p2")
    falcon = await _entity(db, "Falcon", "company", uid, "p2")
    await db.save_relationship(Relationship(from_entity_id=person.id, to_entity_id=falcon.id, type="works_at"))
    await _memory(db, "mem-p2", uid, "p2", falcon)
    return {"target": target, "person": person, "falcon": falcon}


# ---------------------------------------------------------------------------
# 1. Sleep-time merges stay within one project
# ---------------------------------------------------------------------------


async def test_resolution_pass_never_merges_across_projects(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("a@example.com")
        g = await _cross_project_graph(h.db, uid)

        assert await _worker(h.db)._entity_resolution_pass(uid) == 0

        # Both companies survive, each in its own project.
        assert await h.db.get_entity(g["target"].id, user_id=uid, project_id="p1") is not None
        assert await h.db.get_entity(g["falcon"].id, user_id=uid, project_id="p2") is not None
        # p2's edge and memory link still point at p2's entity.
        edges = await h.db.get_entity_relationships(g["person"].id)
        assert [(e.from_entity_id, e.to_entity_id) for e in edges] == [(g["person"].id, g["falcon"].id)]
        assert await _count(h.db, "SELECT COUNT(*) FROM memory_entities WHERE entity_id = ?", g["falcon"].id) == 1
        assert await _count(h.db, "SELECT COUNT(*) FROM memory_entities WHERE entity_id = ?", g["target"].id) == 0


async def test_resolution_pass_still_merges_within_a_project(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("b@example.com")
        full = await _entity(h.db, "John Smith", "person", uid, "p1")
        short = await _entity(h.db, "John", "person", uid, "p1")
        other = await _entity(h.db, "John", "person", uid, "p2")

        assert await _worker(h.db)._entity_resolution_pass(uid) == 1

        survivors = {e.id for e in await h.db.get_user_entities(uid)}
        assert other.id in survivors  # p2's John is not p1's
        assert len(survivors & {full.id, short.id}) == 1


async def test_a_merge_never_lands_on_an_entity_an_earlier_merge_removed(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("chain@example.com")
        await _entity(h.db, "John Smith", "person", uid, "p1")
        john = await _entity(h.db, "John", "person", uid, "p1")  # merged into John Smith first
        johnny = await _entity(h.db, "Johnny", "person", uid, "p1")  # then "John" ~ "Johnny"
        await _memory(h.db, "mem-johnny", uid, "p1", johnny)

        await _worker(h.db)._entity_resolution_pass(uid)

        assert await h.db.get_entity(john.id) is None
        # Johnny was not merged into the deleted John: it and its memory link remain.
        assert await h.db.get_entity(johnny.id) is not None
        assert await _count(h.db, "SELECT COUNT(*) FROM memory_entities WHERE memory_id = 'mem-johnny'") == 1
        assert await _count(h.db, "SELECT COUNT(*) FROM memory_entities WHERE entity_id NOT IN (SELECT id FROM entities)") == 0


async def test_merge_refuses_entities_of_different_projects_or_users(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        a = await h.create_user("c@example.com")
        b = await h.create_user("d@example.com")
        keep = await _entity(h.db, "Acme", "company", a, "p1")
        other_project = await _entity(h.db, "Acme", "company", a, "p2")
        other_user = await _entity(h.db, "Acme", "company", b, "p1")
        worker = _worker(h.db)
        await worker._merge_entities(keep.id, other_project.id)
        await worker._merge_entities(keep.id, other_user.id)
        assert await _count(h.db, "SELECT COUNT(*) FROM entities WHERE canonical_name = 'Acme'") == 3


# ---------------------------------------------------------------------------
# 2. The relationship endpoints return only edges inside the caller's scope
# ---------------------------------------------------------------------------


async def test_a_p2_key_never_sees_a_p1_entity_through_relationships(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("e@example.com")
        g = await _cross_project_graph(h.db, uid)
        # The edge the old cross-project merge produced (p2 person -> p1 company).
        await h.db.save_relationship(Relationship(from_entity_id=g["person"].id, to_entity_id=g["target"].id, type="works_at"))
        p2_key, _ = await h.api_key(uid, "editor", project_ids=["p2"])
        hdr = {"X-API-Key": p2_key}

        # Control: the p1 entity itself is invisible to this key.
        assert (await h.client.get(f"/api/v1/entities/{g['target'].id}", headers=hdr)).status_code == 404

        for r in (
            await h.client.get(f"/api/v1/entities/{g['person'].id}/relationships", headers=hdr),
            await h.client.get("/api/v1/entities/relationship-search", params={"entity_name": "Dana Whistle"}, headers=hdr),
        ):
            assert r.status_code == 200, r.text
            assert g["target"].id not in r.text and "Falcon Acquisition Target" not in r.text
            rels = r.json()["relationships"]
            assert [(x["from_entity_name"], x["type"], x["to_entity_name"]) for x in rels] == [
                ("Dana Whistle", "works_at", "Falcon")
            ]
            assert r.json()["total"] == 1

        # An unrestricted key asking for p2 gets the same scope.
        full_key, _ = await h.api_key(uid, "editor")
        r = await h.client.get(
            f"/api/v1/entities/{g['person'].id}/relationships", params={"project_id": "p2"}, headers={"X-API-Key": full_key}
        )
        assert r.status_code == 200 and "Falcon Acquisition Target" not in r.text


async def test_a_cross_user_edge_is_never_returned_or_named(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        alice = await h.create_user("alice@example.com")
        bob = await h.create_user("bob@example.com")
        secret = await _entity(h.db, "AliceSecretCorp", "company", alice, "default")
        bobco = await _entity(h.db, "BobCo", "company", bob, "default")
        partner = await _entity(h.db, "BobPartner", "company", bob, "default")
        await h.db.save_relationship(Relationship(from_entity_id=bobco.id, to_entity_id=partner.id, type="partner_of"))
        # No write path creates this; it is here to prove the read side holds anyway.
        await h.db.save_relationship(Relationship(from_entity_id=bobco.id, to_entity_id=secret.id, type="partner_of"))
        bob_key, _ = await h.api_key(bob, "editor")
        hdr = {"X-API-Key": bob_key}

        for r in (
            await h.client.get(f"/api/v1/entities/{bobco.id}/relationships", headers=hdr),
            await h.client.get("/api/v1/entities/relationship-search", params={"entity_name": "BobCo"}, headers=hdr),
        ):
            assert r.status_code == 200, r.text
            assert "AliceSecretCorp" not in r.text and secret.id not in r.text and "Unknown" not in r.text
            assert [x["to_entity_name"] for x in r.json()["relationships"]] == ["BobPartner"]

        # Alice's own view is scoped too: the edge's other end is not hers.
        r = await h.client.get(f"/api/v1/entities/{secret.id}/relationships", headers={"X-API-Key": (await h.api_key(alice))[0]})
        assert r.status_code == 200 and r.json()["relationships"] == []


async def test_database_scope_keeps_only_edges_with_both_ends_in_scope(tmp_path) -> None:
    async with secure_app(tmp_path, ROUTERS) as h:
        uid = await h.create_user("f@example.com")
        g = await _cross_project_graph(h.db, uid)
        await h.db.save_relationship(Relationship(from_entity_id=g["person"].id, to_entity_id=g["target"].id, type="knows"))
        everything = await h.db.get_entity_relationships(g["person"].id)
        assert len(everything) == 2  # unscoped callers (graph recall, account export) are unchanged
        scoped = await h.db.get_entity_relationships(g["person"].id, user_id=uid, project_id="p2")
        assert [e.to_entity_id for e in scoped] == [g["falcon"].id]
        all_projects = await h.db.get_entity_relationships(g["person"].id, user_id=uid)
        assert len(all_projects) == 2
        assert await h.db.get_entity_relationships(g["person"].id, user_id="someone-else") == []
