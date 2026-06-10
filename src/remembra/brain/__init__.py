"""Remembra brain layer — GraphRAG-style community understanding.

Turns the flat entity graph into themed communities with summaries, plus
graph-level insights (central entities, surprising cross-theme links). This is
the "higher-level understanding" layer that lets recall and the dashboard reason
about *what the memory is about*, not just retrieve matching facts.
"""

from remembra.brain.communities import GraphAnalysis, detect_communities

__all__ = ["GraphAnalysis", "detect_communities"]
