"""Brain analyzer — turns a tenant's entity graph into themed communities + insights.

Orchestration around the pure detection engine: load the graph, detect
communities, label and summarize each, persist, and surface graph-level insights
(central "god node" entities and surprising cross-theme links). Runs inside the
sleep-time consolidation worker and on demand via the /v1/brain API.

The summary layer is pluggable: pass an async `summarizer(label, names, types)`
to use an LLM, otherwise a deterministic extractive summary is generated so the
feature always works with zero external calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from remembra.brain.communities import detect_communities

log = structlog.get_logger(__name__)

Summarizer = Callable[[str, list[str], list[str]], Awaitable[str]]

# Communities smaller than this are singletons/noise, not themes worth surfacing.
_MIN_THEME_SIZE = 2
_TOP_ENTITIES_PER_COMMUNITY = 8


@dataclass
class BrainResult:
    """Structured output of a brain analysis run."""

    num_entities: int
    num_relationships: int
    num_communities: int
    modularity: float
    communities: list[dict[str, Any]] = field(default_factory=list)
    god_nodes: list[dict[str, Any]] = field(default_factory=list)  # most central entities
    surprising_links: list[dict[str, Any]] = field(default_factory=list)  # cross-theme bridges

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_entities": self.num_entities,
            "num_relationships": self.num_relationships,
            "num_communities": self.num_communities,
            "modularity": round(self.modularity, 4),
            "communities": self.communities,
            "god_nodes": self.god_nodes,
            "surprising_links": self.surprising_links,
        }


def _extractive_summary(label: str, names: list[str], types: list[str]) -> str:
    """Deterministic, LLM-free summary of a community.

    Reads like a one-line theme description: what it's centered on, who/what else
    is in it, and the dominant kind of thing. Good enough to be useful on its own
    and as a fallback when no LLM summarizer is configured.
    """
    others = [n for n in names if n != label]
    type_counts = Counter(t for t in types if t)
    kind = ""
    if type_counts:
        top_kind, _ = type_counts.most_common(1)[0]
        kind = f" mostly {top_kind}s" if top_kind else ""
    if not others:
        return f"A theme centered on {label}{kind}."
    shown = ", ".join(others[:4])
    extra = f" and {len(others) - 4} more" if len(others) > 4 else ""
    return f"Theme centered on {label}, connecting {shown}{extra}{kind}."


class BrainAnalyzer:
    """Computes and persists the brain layer for a tenant."""

    def __init__(self, db: Any, summarizer: Summarizer | None = None) -> None:
        self.db = db
        self.summarizer = summarizer

    async def analyze(
        self,
        user_id: str,
        project_id: str = "default",
        *,
        persist: bool = True,
    ) -> BrainResult:
        """Run the full brain pipeline for one (user, project)."""
        entities = await self.db.get_entities_for_graph(user_id, project_id)
        edges = await self.db.get_relationships_for_graph(user_id, project_id)

        if not entities:
            return BrainResult(0, 0, 0, 0.0)

        name_by_id = {e["id"]: e["name"] for e in entities}
        type_by_id = {e["id"]: (e.get("type") or "") for e in entities}
        node_ids = [e["id"] for e in entities]

        analysis = detect_communities(node_ids, edges)

        # Build per-community theme objects (only communities with ≥2 members).
        themes: list[dict[str, Any]] = []
        for comm_idx, member_ids in sorted(analysis.community_members.items()):
            if len(member_ids) < _MIN_THEME_SIZE:
                continue
            # Rank members by centrality to pick the label + representative set.
            ranked = sorted(
                member_ids,
                key=lambda mid: analysis.centrality.get(mid, 0.0),
                reverse=True,
            )
            label = name_by_id.get(ranked[0], f"Theme {comm_idx}")
            top = [
                {
                    "id": mid,
                    "name": name_by_id.get(mid, mid),
                    "type": type_by_id.get(mid, ""),
                    "centrality": round(analysis.centrality.get(mid, 0.0), 4),
                }
                for mid in ranked[:_TOP_ENTITIES_PER_COMMUNITY]
            ]
            names = [t["name"] for t in top]
            types = [t["type"] for t in top]
            summary = await self._summarize(label, names, types)
            themes.append(
                {
                    "community_index": comm_idx,
                    "label": label,
                    "summary": summary,
                    "size": len(member_ids),
                    "top_entities": top,
                    "central_entity": label,
                }
            )
        themes.sort(key=lambda t: t["size"], reverse=True)

        # God nodes: the most central entities across the whole graph.
        god_nodes = [
            {
                "id": eid,
                "name": name_by_id.get(eid, eid),
                "type": type_by_id.get(eid, ""),
                "centrality": round(score, 4),
                "community_index": analysis.communities.get(eid),
            }
            for eid, score in sorted(analysis.centrality.items(), key=lambda kv: kv[1], reverse=True)[:10]
            if score > 0
        ]

        # Surprising links: heaviest cross-community edges, mapped to names.
        surprising = [
            {
                "from": name_by_id.get(a, a),
                "to": name_by_id.get(b, b),
                "weight": round(w, 3),
                "from_community": analysis.communities.get(a),
                "to_community": analysis.communities.get(b),
            }
            for a, b, w in analysis.bridges[:12]
        ]

        if persist:
            try:
                await self.db.set_entity_communities(analysis.communities)
                await self.db.save_communities(user_id, project_id, themes)
            except Exception as exc:  # persistence must never crash consolidation
                log.warning("brain_persist_failed", error=str(exc), user_id=user_id)

        log.info(
            "brain_analyzed",
            user_id=user_id,
            project_id=project_id,
            entities=len(entities),
            communities=len(themes),
            modularity=round(analysis.modularity, 3),
        )

        return BrainResult(
            num_entities=len(entities),
            num_relationships=len(edges),
            num_communities=len(themes),
            modularity=analysis.modularity,
            communities=themes,
            god_nodes=god_nodes,
            surprising_links=surprising,
        )

    async def _summarize(self, label: str, names: list[str], types: list[str]) -> str:
        if self.summarizer is not None:
            try:
                return await self.summarizer(label, names, types)
            except Exception as exc:
                log.warning("brain_summarizer_failed", error=str(exc))
        return _extractive_summary(label, names, types)
