"""Integration tests for the brain analyzer against a real SQLite database.

Verifies the full pipeline: load graph → detect communities → label/summarize →
persist communities + entity community ids → surface god nodes and bridges.
"""

import tempfile
from pathlib import Path

import pytest

from remembra.brain.analyzer import BrainAnalyzer
from remembra.models.memory import Entity, Relationship
from remembra.storage.database import Database


async def _db_with_two_themes() -> tuple[Database, dict[str, str]]:
    """Build a graph with two clear themes joined by one bridge person.

    Trading theme: TradeMind ↔ ChartHustle ↔ Mani.
    Accounting theme: YaadBooks ↔ GCT ↔ Stripe.
    Bridge: Mani ↔ YaadBooks (the person who ties both worlds).
    """
    tmp = Path(tempfile.mkdtemp()) / "brain.db"
    db = Database(str(tmp))
    await db.connect()
    await db.init_schema()

    names = ["TradeMind", "ChartHustle", "Mani", "YaadBooks", "GCT", "Stripe"]
    ids: dict[str, str] = {}
    for n in names:
        etype = "person" if n == "Mani" else ("concept" if n in ("GCT",) else "organization")
        ent = Entity(canonical_name=n, type=etype)
        await db.save_entity(ent, user_id="u1", project_id="default")
        ids[n] = ent.id

    def rel(a: str, b: str, conf: float = 1.0) -> Relationship:
        return Relationship(from_entity_id=ids[a], to_entity_id=ids[b], type="relates_to", confidence=conf)

    for a, b in [
        ("TradeMind", "ChartHustle"), ("ChartHustle", "Mani"), ("TradeMind", "Mani"),
        ("YaadBooks", "GCT"), ("GCT", "Stripe"), ("YaadBooks", "Stripe"),
    ]:
        await db.save_relationship(rel(a, b))
    # Bridge
    await db.save_relationship(rel("Mani", "YaadBooks", 0.5))
    return db, ids


@pytest.mark.asyncio
async def test_analyzer_finds_two_themes_and_persists():
    db, ids = await _db_with_two_themes()
    try:
        analyzer = BrainAnalyzer(db)
        result = await analyzer.analyze("u1", "default")

        assert result.num_entities == 6
        assert result.num_relationships == 7
        assert result.num_communities == 2  # trading + accounting
        assert result.modularity > 0.2

        # Persisted communities are reloadable.
        stored = await db.get_communities("u1", "default")
        assert len(stored) == 2
        assert all(c["summary"] for c in stored)  # every theme has a summary
        labels = {c["label"] for c in stored}
        assert labels  # central entities chosen as labels

        # Entity community ids were written back.
        entities = await db.get_entities_for_graph("u1", "default")
        assert all(e["community_id"] is not None for e in entities)
        # The two trading entities share a community; trading vs accounting differ.
        comm = {e["name"]: e["community_id"] for e in entities}
        assert comm["TradeMind"] == comm["ChartHustle"]
        assert comm["YaadBooks"] == comm["GCT"]
        assert comm["TradeMind"] != comm["YaadBooks"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_analyzer_surfaces_bridge_as_surprising_link():
    db, ids = await _db_with_two_themes()
    try:
        result = await BrainAnalyzer(db).analyze("u1", "default")
        pairs = {frozenset((s["from"], s["to"])) for s in result.surprising_links}
        # Mani↔YaadBooks crosses the two themes; it must be flagged.
        assert frozenset(("Mani", "YaadBooks")) in pairs
        # God nodes should be populated and centrality-ordered.
        assert result.god_nodes
        cents = [g["centrality"] for g in result.god_nodes]
        assert cents == sorted(cents, reverse=True)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_custom_summarizer_is_used():
    db, ids = await _db_with_two_themes()
    try:
        async def fake_llm(label, names, types):
            return f"LLM[{label}:{len(names)}]"

        result = await BrainAnalyzer(db, summarizer=fake_llm).analyze("u1", "default")
        assert all(c["summary"].startswith("LLM[") for c in result.communities)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_graph_is_safe():
    tmp = Path(tempfile.mkdtemp()) / "empty.db"
    db = Database(str(tmp))
    await db.connect()
    await db.init_schema()
    try:
        result = await BrainAnalyzer(db).analyze("nobody", "default")
        assert result.num_entities == 0
        assert result.num_communities == 0
        assert result.to_dict()["communities"] == []
    finally:
        await db.close()
