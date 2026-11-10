"""Fault survey F40 (adjacent): a claim on a literal path with brackets covers that file at the gate.

A path claim is sent as the literal file path, and the glob engine reads ``[id]`` as a character
class. The stored claim on ``routes/[id]/page.tsx`` therefore matched ``routes/i/page.tsx`` and not
the file it names, so the gate saw no claim on it: the holder's own write was not recognised as
claimed and another session's write was not blocked.
"""

from __future__ import annotations

from remembra.crew import gatecore as G
from tests.crew.gatecore_support import find, run, snapshot

ROUTE = "routes/[id]/page.tsx"


def _snap(glob: str = ROUTE) -> dict:
    snap = snapshot()
    snap["claims"].append({**find(snap["claims"], "clm_cafe"), "id": "clm_route", "zone_id": None, "path_glob": glob})
    return snap


def test_the_glob_engine_reads_the_literal_route_as_a_character_class() -> None:
    assert not G.glob_match(ROUTE, ROUTE)  # why the equality check is needed
    assert G.glob_match(ROUTE, "routes/i/page.tsx")


def test_another_sessions_claim_on_a_bracket_route_blocks_the_write() -> None:
    v = run("Write", {"file_path": f"/w/yaadbooks-c/{ROUTE}", "content": "x"}, snap=_snap())
    assert (v.rule, v.decision) == (9, "deny"), (v.rule, v.decision, v.reason)
    assert "cc-1" in (v.reason or "")


def test_the_holders_own_claim_on_a_bracket_route_is_recognised() -> None:
    own = run("Write", {"file_path": f"/w/yaadbooks-a/{ROUTE}", "content": "x"}, caller="cs_a", snap=_snap())
    assert (own.rule, own.decision) == (7, "allow"), (own.rule, own.decision, own.reason)


def test_a_plain_path_claim_and_an_unclaimed_route_are_unchanged() -> None:
    plain = run("Write", {"file_path": "/w/yaadbooks-c/README.md", "content": "x"}, snap=_snap("README.md"))
    assert (plain.rule, plain.decision) == (9, "deny")
    other = run("Write", {"file_path": "/w/yaadbooks-c/routes/[slug]/page.tsx", "content": "x"}, snap=_snap())
    assert (other.rule, other.decision) == (19, "allow"), (other.rule, other.decision, other.reason)
