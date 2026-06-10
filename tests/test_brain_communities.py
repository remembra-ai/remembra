"""Correctness tests for the pure-Python community detection engine.

The brain layer's value depends entirely on this partition being right, so we
test it on graphs with a known ground-truth community structure rather than just
asserting "it runs."
"""

from remembra.brain.communities import detect_communities


def _two_triangles_with_bridge():
    """Two triangles (A,B,C) and (X,Y,Z) joined by a single C–X edge.

    Ground truth: two communities. The bridge is the only cross-theme link.
    """
    nodes = ["A", "B", "C", "X", "Y", "Z"]
    edges = [
        ("A", "B", 1.0), ("B", "C", 1.0), ("A", "C", 1.0),  # triangle 1
        ("X", "Y", 1.0), ("Y", "Z", 1.0), ("X", "Z", 1.0),  # triangle 2
        ("C", "X", 1.0),  # bridge
    ]
    return nodes, edges


def test_finds_two_communities_in_barbell():
    nodes, edges = _two_triangles_with_bridge()
    result = detect_communities(nodes, edges)
    assert result.num_communities == 2
    # The two triangles must each be wholly within one community.
    assert result.communities["A"] == result.communities["B"] == result.communities["C"]
    assert result.communities["X"] == result.communities["Y"] == result.communities["Z"]
    assert result.communities["A"] != result.communities["X"]


def test_bridge_is_detected_as_surprising_link():
    nodes, edges = _two_triangles_with_bridge()
    result = detect_communities(nodes, edges)
    # The C–X edge spans the two communities; intra-triangle edges do not.
    bridge_pairs = {(a, b) for a, b, _ in result.bridges}
    assert ("C", "X") in bridge_pairs
    assert ("A", "B") not in bridge_pairs


def test_modularity_is_positive_for_clear_structure():
    nodes, edges = _two_triangles_with_bridge()
    result = detect_communities(nodes, edges)
    # A good partition of clearly-clustered data has solidly positive modularity.
    assert result.modularity > 0.3


def test_determinism_same_input_same_output():
    nodes, edges = _two_triangles_with_bridge()
    a = detect_communities(nodes, edges)
    b = detect_communities(nodes, edges)
    assert a.communities == b.communities
    assert a.bridges == b.bridges


def test_centrality_flags_the_hub():
    """A star graph's center must be the most central node."""
    nodes = ["hub", "a", "b", "c", "d"]
    edges = [("hub", n, 1.0) for n in ["a", "b", "c", "d"]]
    result = detect_communities(nodes, edges)
    assert result.centrality["hub"] == max(result.centrality.values())
    assert result.centrality["hub"] > result.centrality["a"]


def test_empty_and_edgeless_graphs_are_safe():
    assert detect_communities([], []).num_communities == 0
    # Three entities, no relationships → three singleton communities, no crash.
    edgeless = detect_communities(["a", "b", "c"], [])
    assert edgeless.num_communities == 3
    assert edgeless.modularity == 0.0


def test_weighted_edges_pull_communities():
    """Heavy intra-cluster weights beat a light cross edge."""
    nodes = ["a", "b", "c", "d"]
    edges = [
        ("a", "b", 10.0),
        ("c", "d", 10.0),
        ("b", "c", 0.5),  # weak bridge
    ]
    result = detect_communities(nodes, edges)
    assert result.communities["a"] == result.communities["b"]
    assert result.communities["c"] == result.communities["d"]
    assert result.communities["a"] != result.communities["c"]
