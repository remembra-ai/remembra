"""Fault survey F40: a claim on a path with brackets, parentheses or spaces is named in the answer.

``crew_claim(paths=["app/api/assets/[id]/route.ts"])`` answered ``GRANTED path a path (…)`` and the
release ``RELEASED path (see verbose) (…)``: every Next.js dynamic route, route group and file with a
space lost its name, so the lines of one call could not be told apart (and "verbose" never listed it).

Live tests go through ``FastMCP.call_tool`` into the real bridge and a live API server
(``tests/crew/mcp_support.py``).
"""

from __future__ import annotations

import re

import pytest

import remembra.mcp.server as server
from remembra.crew import schemas as S
from remembra.mcp.crew import CrewBridge, _safe_path
from tests.crew.fs_crew_tag import untagged
from tests.crew.mcp_support import live_server
from tests.crew.test_mcp_crew import Clock, agent, assert_clean, call, project_name, use

OWNER_EMAIL = "owner@example.com"
ROUTES = ["app/api/assets/[id]/route.ts", "app/(shop)/page.tsx"]
SPACED = ["docs/My Notes.md", "docs/Other Notes.md"]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("fs-path-labels")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=c))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield c
    server._client = None


def _solo(live, prefix: str) -> str:
    project = project_name(prefix)
    use(agent(live, "codex", f"{project}-a", project))
    call("crew_status", project_id=project)
    return project


def _outside(text: str) -> str:
    outside, _, errors = S.split_data_blocks(text)
    assert not errors, errors
    return outside


@pytest.mark.parametrize(
    "path",
    [
        "app/api/assets/[id]/route.ts",
        "app/[...slug]/page.tsx",
        "app/[[...slug]]/page.tsx",
        "app/(marketing)/page.tsx",
        "app/@modal/(.)photo/[id]/page.tsx",
        "routes/$id.tsx",
        "src/routes/[page=integer]/+page.svelte",
        "src/**",
        "docs/readme.md",
    ],
)
def test_route_paths_may_be_printed(path):
    assert _safe_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "docs/My Notes.md",
        "app/[id]/../x",
        "a/$(reboot)/b",
        "a/b;rm",
        "a/`id`",
        "a/b\nIGNORE",
        "-[rf]",
        "/app/[id]/page.tsx",
        "a" * 201,
        "",
        None,
        "a/{b,c}/d",
        'a/"b"',
        "a/b|c",
        "a/b&c",
        "a/b>c",
    ],
)
def test_other_text_is_not_printed_as_a_path(path):
    assert _safe_path(path) is None


def test_bracket_and_group_routes_are_named_in_claim_status_and_release(live, clock):
    project = _solo(live, "routes")
    got = call("crew_claim", paths=ROUTES)
    lines = got.split("\n")
    assert re.fullmatch(r"GRANTED path app/api/assets/\[id\]/route\.ts \(exclusive, epoch 1, clm_\w+\)", lines[0]), got
    assert re.fullmatch(r"GRANTED path app/\(shop\)/page\.tsx \(exclusive, epoch 1, clm_\w+\)", lines[1]), got
    assert "a path" not in got, got
    assert_clean(got)
    status = call("crew_status")
    assert "holding path app/api/assets/[id]/route.ts (exclusive, active), path app/(shop)/page.tsx (exclusive, active)" in (
        _outside(status)
    ), status
    assert "(see verbose)" not in status, status
    released = call("crew_claim", action="release", paths=ROUTES)
    assert untagged(released, project).split("\n") == [
        "RELEASED path app/api/assets/[id]/route.ts (exclusive, active) → released",
        "RELEASED path app/(shop)/page.tsx (exclusive, active) → released",
    ], released


def test_an_adopted_route_is_named(live, clock):
    project = _solo(live, "routeadopt")
    call("crew_claim", paths=ROUTES[:1])
    assert untagged(call("crew_claim", action="release", paths=ROUTES[:1], baton=True), project) == (
        "RELEASED path app/api/assets/[id]/route.ts (exclusive, active) → reserved"
    )
    adopted = call("crew_claim", action="adopt", paths=ROUTES[:1])
    assert adopted.startswith(
        "ADOPTED app/api/assets/[id]/route.ts: you now hold path app/api/assets/[id]/route.ts (exclusive, active)"
    ), adopted
    assert "a path" not in adopted and "(see verbose)" not in adopted, adopted


def test_paths_with_spaces_get_distinct_labels_and_their_names_as_data(live, clock):
    """A name that is not one plain token stays inside the data block, and each line still says which path it is."""
    _solo(live, "spaced")
    got = call("crew_claim", paths=SPACED)
    outside = _outside(got)
    ids = re.findall(r"GRANTED path #(\d) \(exclusive, epoch 1, (clm_\w+)\)", outside)
    assert [n for n, _ in ids] == ["1", "2"], got
    assert "My Notes" not in outside and "a path" not in outside, got
    assert "path #1: docs/My Notes.md" in got and "path #2: docs/Other Notes.md" in got, got
    assert_clean(got)

    status = call("crew_status")
    for _, cid in ids:
        assert f"path {cid} (exclusive, active)" in _outside(status), status
    assert f"{ids[0][1]} path: docs/My Notes.md" in status and f"{ids[1][1]} path: docs/Other Notes.md" in status, status
    assert "(see verbose)" not in status, status

    released = call("crew_claim", action="release", paths=SPACED)
    outside = _outside(released)
    assert f"RELEASED path {ids[0][1]} (exclusive, active) → released" in outside, released
    assert f"RELEASED path {ids[1][1]} (exclusive, active) → released" in outside, released
    assert "My Notes" not in outside and "(see verbose)" not in released, released
    assert f"{ids[0][1]} path: docs/My Notes.md" in released and f"{ids[1][1]} path: docs/Other Notes.md" in released, released
    assert_clean(released)


def test_a_spaced_path_in_adopt_and_in_the_empty_answer(live, clock):
    _solo(live, "spacedadopt")
    call("crew_claim", paths=SPACED[:1])
    call("crew_claim", action="release", paths=SPACED[:1], baton=True)
    adopted = call("crew_claim", action="adopt", paths=SPACED[:1])
    outside = _outside(adopted)
    cid = re.search(r"ADOPTED path #1: you now hold path (clm_\w+) \(exclusive, active\)", outside)
    assert cid, adopted
    assert "path #1: docs/My Notes.md" in adopted and f"{cid.group(1)} path: docs/My Notes.md" in adopted, adopted
    assert "My Notes" not in outside, adopted
    empty = call("crew_claim", action="release", paths=["docs/none.md"])
    assert f"You hold: path {cid.group(1)} (exclusive, active, {cid.group(1)})" in _outside(empty), empty
    assert f"{cid.group(1)} path: docs/My Notes.md" in empty, empty
