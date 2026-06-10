"""Dependency-free community detection over the entity graph.

This is the engine behind Remembra's "brain" layer. GraphRAG systems (Microsoft
GraphRAG, graphify, LightRAG) all converge on the same move: cluster the entity
graph into communities, then summarize each community so the system can reason at
a *theme* level, not just a fact level. Those systems reach for igraph/leidenalg
or NetworkX. We deliberately do not — a native graph dependency is a deployment
liability, and our per-tenant graphs are small (hundreds–low-thousands of
entities), so a clean pure-Python Louvain pass is both sufficient and portable.

The public surface is intentionally pure (no I/O): it takes nodes + weighted
edges and returns community assignments, centrality, and cross-community bridges.
The orchestration layer (analyzer.py) handles loading from the DB, summarizing,
and persistence.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class GraphAnalysis:
    """Result of analyzing an entity graph."""

    communities: dict[str, int]  # node_id -> community index
    centrality: dict[str, float]  # node_id -> normalized weighted degree (0..1)
    modularity: float  # quality of the partition (-0.5..1; higher is better)
    bridges: list[tuple[str, str, float]] = field(default_factory=list)  # cross-community edges
    community_members: dict[int, list[str]] = field(default_factory=dict)  # community -> node_ids

    @property
    def num_communities(self) -> int:
        return len({c for c in self.communities.values()})


def _normalize_edges(
    edges: list[tuple[str, str, float]],
) -> dict[str, dict[str, float]]:
    """Build a symmetric weighted adjacency map, collapsing duplicate/edge pairs.

    Self-loops are dropped (an entity related to itself carries no community
    signal). Parallel edges between the same pair are summed.
    """
    adj: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for u, v, w in edges:
        if u == v:
            continue
        weight = float(w) if w and w > 0 else 1.0
        adj[u][v] += weight
        adj[v][u] += weight
    return {n: dict(neighbors) for n, neighbors in adj.items()}


def detect_communities(
    nodes: list[str],
    edges: list[tuple[str, str, float]],
    *,
    max_passes: int = 20,
) -> GraphAnalysis:
    """Partition the graph into communities via Louvain modularity optimization.

    Single-level local-moving Louvain: every node starts in its own community,
    then we repeatedly move each node into the neighboring community that yields
    the largest positive modularity gain until the partition is stable. Nodes are
    visited in sorted order and ties break toward the lower community index, so
    the result is deterministic for a given input (important for tests and for
    stable community ids across consolidation runs).

    Isolated entities (no relationships) each form a singleton community; the
    analyzer treats those as "unclustered" rather than surfacing them as themes.
    """
    adj = _normalize_edges(edges)
    # Include every requested node even if it has no edges.
    all_nodes = sorted(set(nodes) | set(adj.keys()))
    if not all_nodes:
        return GraphAnalysis(communities={}, centrality={}, modularity=0.0)

    # Weighted degree per node and total edge weight m.
    k: dict[str, float] = {n: sum(adj.get(n, {}).values()) for n in all_nodes}
    two_m = sum(k.values())  # = 2m (each edge counted from both ends)
    if two_m == 0:
        # No edges at all — every node is its own singleton community.
        return GraphAnalysis(
            communities={n: i for i, n in enumerate(all_nodes)},
            centrality={n: 0.0 for n in all_nodes},
            modularity=0.0,
            community_members={i: [n] for i, n in enumerate(all_nodes)},
        )

    comm: dict[str, int] = {n: i for i, n in enumerate(all_nodes)}
    # Σ_tot per community: total degree of nodes in the community.
    sigma_tot: dict[int, float] = {i: k[n] for i, n in enumerate(all_nodes)}

    def gain_components(node: str) -> dict[int, float]:
        """Sum of edge weights from `node` into each neighboring community."""
        weights: dict[int, float] = defaultdict(float)
        for nbr, w in adj.get(node, {}).items():
            weights[comm[nbr]] += w
        return weights

    for _ in range(max_passes):
        moved = False
        for node in all_nodes:
            node_k = k[node]
            current = comm[node]
            # Remove node from its community.
            sigma_tot[current] -= node_k
            links = gain_components(node)

            # Best target: maximize ΔQ ∝ k_i_in - (Σ_tot * k_i) / (2m).
            best_comm = current
            best_gain = links.get(current, 0.0) - (sigma_tot[current] * node_k) / two_m
            for cand, k_i_in in links.items():
                if cand == current:
                    continue
                cand_gain = k_i_in - (sigma_tot[cand] * node_k) / two_m
                if cand_gain > best_gain + 1e-12 or (abs(cand_gain - best_gain) <= 1e-12 and cand < best_comm):
                    best_gain = cand_gain
                    best_comm = cand

            sigma_tot[best_comm] += node_k
            comm[node] = best_comm
            if best_comm != current:
                moved = True
        if not moved:
            break

    # Relabel communities to dense 0..K-1 in order of first appearance.
    relabel: dict[int, int] = {}
    members: dict[int, list[str]] = defaultdict(list)
    for n in all_nodes:
        c = comm[n]
        if c not in relabel:
            relabel[c] = len(relabel)
        members[relabel[c]].append(n)
    communities = {n: relabel[comm[n]] for n in all_nodes}

    centrality = _degree_centrality(k, two_m)
    bridges = _cross_community_bridges(adj, communities)
    modularity = _modularity(adj, communities, k, two_m)

    return GraphAnalysis(
        communities=communities,
        centrality=centrality,
        modularity=modularity,
        bridges=bridges,
        community_members=dict(members),
    )


def _degree_centrality(k: dict[str, float], two_m: float) -> dict[str, float]:
    """Weighted degree normalized to 0..1 (the 'god node' signal)."""
    if not k:
        return {}
    max_k = max(k.values()) or 1.0
    return {n: (kv / max_k) for n, kv in k.items()}


def _cross_community_bridges(
    adj: dict[str, dict[str, float]],
    communities: dict[str, int],
    *,
    limit: int = 25,
) -> list[tuple[str, str, float]]:
    """Edges whose endpoints sit in different communities.

    These are the "surprising links" — connections that span otherwise separate
    themes (e.g. a person who ties your trading work to your accounting work).
    Returned heaviest-first, de-duplicated by unordered pair.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, float]] = []
    for u, neighbors in adj.items():
        for v, w in neighbors.items():
            if communities.get(u) == communities.get(v):
                continue
            key = (u, v) if u < v else (v, u)
            if key in seen:
                continue
            seen.add(key)
            out.append((key[0], key[1], w))
    out.sort(key=lambda t: t[2], reverse=True)
    return out[:limit]


def _modularity(
    adj: dict[str, dict[str, float]],
    communities: dict[str, int],
    k: dict[str, float],
    two_m: float,
) -> float:
    """Newman modularity Q of the partition. Used to report partition quality."""
    if two_m == 0:
        return 0.0
    # Σ_in: intra-community edge weight (counted twice); Σ_tot per community.
    sigma_in: dict[int, float] = defaultdict(float)
    sigma_tot: dict[int, float] = defaultdict(float)
    for n, c in communities.items():
        sigma_tot[c] += k.get(n, 0.0)
        for nbr, w in adj.get(n, {}).items():
            if communities.get(nbr) == c:
                sigma_in[c] += w  # each intra edge counted from both ends → 2w total
    q = 0.0
    for c in sigma_tot:
        q += (sigma_in[c] / two_m) - (sigma_tot[c] / two_m) ** 2
    return q
