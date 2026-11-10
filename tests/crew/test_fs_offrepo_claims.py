"""F25: work outside the repository can be claimed as a named resource, and a refusal says what is accepted.

MCP calls go through ``FastMCP.call_tool`` into the real MCP server module and a live API. The
old-server case feeds the bridge the exact 422 a 0.17.5 server sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from remembra.crew import policy as P
from remembra.crew import schemas as S
from remembra.crew import tasks as T
from remembra.mcp import crew as crew_mod
from remembra.relay import handoff
from remembra.relay.crew import cli
from tests.crew.test_mcp_crew import _two_agents, agent, call, crew_id_of, project_name, use

# the live API and the fresh crew bridge, shared with the WP-11 suite
live = base.live
clock = base.clock

OFF_REPO = (
    "artifact:thirdbatch-acceptance",
    "production:backtohello",
    "ops:server-storage-cleanup",
    "data:showcase-barber",
    "qa:seventhbatch-runtime",
    "resource:coolify-proxy-upgrade-preparation",
)
OLD_PATTERN = r"(?:schema|deploy|service|supabase|mcp):[A-Za-z0-9._/-]{1,64}"


def _claim_shape_errors(resource: str) -> list[str]:
    return S.validate({"resource": resource, "mode": "exclusive", "wait": False, "source": "mcp"}, S.REQUEST_SHAPES["Claim"])


# -- the vocabulary ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource", [*OFF_REPO, "schema:main", "deploy:vercel", "service:x", "supabase:a/b", "mcp:x.y"])
def test_a_kind_name_resource_is_a_valid_claim_target(resource):
    assert _claim_shape_errors(resource) == []


@pytest.mark.parametrize(
    "resource",
    [
        "Artifact:x",
        "a:x",
        "artifact:",
        ":x",
        "artifact x:y",
        "artifact:has space",
        "9lives:x",
        "k" * 16 + ":x",
        "artifact:" + "n" * 65,
    ],
)
def test_what_is_not_kind_name_is_still_refused(resource):
    assert _claim_shape_errors(resource) != []


def test_every_valid_resource_fits_the_stored_field():
    longest = "k" * 15 + ":" + "n" * 64
    assert len(longest) == 80 and _claim_shape_errors(longest) == []


# -- MCP: claim, conflict, release ------------------------------------------------------------


def test_off_repo_work_is_claimed_as_a_named_resource(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "offrepo")
    use(a)
    for resource in OFF_REPO[:3]:  # a session holds a few claims at a time (the claim cap)
        got = call("crew_claim", zone=resource, reason="outside the repository")
        assert got.startswith(f"GRANTED {resource} (exclusive, epoch 1"), got
    you = call("crew_status")
    assert "artifact:thirdbatch-acceptance" in you.split("<remembra-data")[0], you
    use(b)
    refused = call("crew_claim", zone="production:backtohello")
    assert refused.startswith(f"REFUSED: production:backtohello held EXCLUSIVELY by {a_sign}"), refused
    # the other agent's brief names the resource, not "a path claim"
    brief = call("session_brief", project_id=project)
    assert "ops:server-storage-cleanup" in brief and "production:backtohello" in brief, brief
    for resource in OFF_REPO[3:]:
        got = call("crew_claim", zone=resource)
        assert got.startswith(f"GRANTED {resource} (exclusive, epoch 1"), got
    use(a)
    released = call("crew_claim", action="release", zone="production:backtohello")
    assert released.startswith("RELEASED production:backtohello"), released
    rows = live.crew_rows(
        "SELECT resource FROM crew_claims WHERE crew_id = ? AND state = 'active' ORDER BY resource", (crew_id_of(live, project),)
    )
    assert [r["resource"] for r in rows] == sorted(r for r in OFF_REPO if r != "production:backtohello")


def test_an_invalid_resource_says_what_is_accepted(live, clock):
    project = project_name("badres")
    use(agent(live, "codex", f"{project}-s", project))
    out = call("crew_claim", zone="Bad Kind:x")
    assert out.startswith("CREW ERROR (claim resource: 422 invalid_body): The body is invalid."), out
    assert "resource: does not match" in out and "kind:name" in out, out
    assert "Bad Kind" not in out, out  # the caller's value is not repeated
    assert live.crew_rows("SELECT id FROM crew_claims WHERE crew_id = ?", (crew_id_of(live, project),)) == []


def test_against_an_older_server_the_refusal_names_the_kinds_it_takes():
    """A 0.17.5 server knows five kinds: the new client says so instead of 'The body is invalid.'"""
    res = httpx.Response(
        422,
        json={
            "detail": {
                "error": "invalid_body",
                "message": "The body is invalid.",
                "problems": [f"$.resource: 'artifact:thirdbatch-acceptance' does not match {OLD_PATTERN}"],
            }
        },
    )
    err = crew_mod._error_from(res)
    text = crew_mod.CrewBridge()._refused(None, None, "artifact:thirdbatch-acceptance", err, [])  # type: ignore[arg-type]
    assert text.startswith("CREW ERROR (claim artifact:thirdbatch-acceptance: 422 invalid_body): The body is invalid."), text
    assert "schema:, deploy:, service:, supabase: and mcp:" in text and "service:<name>" in text, text
    # a 422 without problems (or with odd ones) still renders
    bare = crew_mod._error_from(
        httpx.Response(422, json={"detail": {"error": "invalid_body", "message": "The body is invalid."}})
    )
    assert crew_mod.error_text(bare, "crew") == "CREW ERROR (crew: 422 invalid_body): The body is invalid."
    odd = crew_mod._error_from(
        httpx.Response(
            422, json={"detail": {"error": "invalid_body", "message": "The body is invalid.", "problems": [None, 7, ""]}}
        )
    )
    assert crew_mod.error_text(odd, "crew").startswith("CREW ERROR (crew: 422 invalid_body): The body is invalid.")


def test_other_invalid_bodies_show_their_problems_without_the_values(live, clock):
    project = project_name("problems")
    use(agent(live, "codex", f"{project}-s", project))
    assert call("crew_status", project_id=project).startswith(f"CREW {project} ")
    seat = server._crew.seat(server._crew_caller())[0]
    with pytest.raises(crew_mod.CrewApiError) as err:
        crew_mod.CrewHttp(server._get_client()).call(
            "POST",
            f"/crews/{seat.crew_id}/claims",
            token=seat.token,
            json={"resource": "deploy:x", "mode": "SECRETMODE", "wait": False, "source": "mcp", "surprise": 1},
        )
    text = crew_mod.error_text(err.value, "claim deploy:x")
    assert "mode: not in" in text and "surprise: unknown field" in text, text
    assert "SECRETMODE" not in text, text


# -- the refusals point to the off-repo form --------------------------------------------------


def test_an_absolute_path_is_told_both_ways_out(live, clock):
    project = project_name("abspath")
    use(agent(live, "codex", f"{project}-s", project))
    out = call("crew_claim", paths=["/opt/backtohello/.env", "../outside.txt"])
    assert out.startswith("NOTHING TO CLAIM:"), out
    assert out.count("relative to the repository root") == 2, out
    assert out.count('zone="artifact:') == 2 and "kind:name" in out, out
    assert live.crew_rows("SELECT id FROM crew_claims WHERE crew_id = ?", (crew_id_of(live, project),)) == []


def test_an_unknown_zone_names_paths_and_the_resource_form(live, clock):
    project = project_name("nozone")
    use(agent(live, "codex", f"{project}-s", project))
    out = call("crew_claim", zone="production-backtohello")
    assert out.startswith("CREW: Unknown zone 'production-backtohello'."), out
    assert "crew_status(verbose=True)" in out and "paths=[" in out and 'zone="deploy:production"' in out, out


def test_the_tool_text_documents_the_resource_form():
    doc = server.crew_claim.__doc__ or ""
    assert "kind:name" in doc and "artifact:" in doc and "outside the repository" in doc, doc


def test_a_file_criterion_outside_the_repository_points_to_kind_manual(live, clock):
    project = project_name("criterion")
    use(agent(live, "codex", f"{project}-s", project))
    criterion = {
        "id": "surface-matrix",
        "text": "the matrix is written",
        "kind": "file",
        "match": "/Volumes/T7/x/matrix.md",
        "required": True,
    }
    out = call("crew_task", action="create", title="Write the matrix", acceptance=[criterion])
    assert "criterion surface-matrix: kind file needs a repo-relative path in match" in out, out
    assert "kind manual" in out, out
    with pytest.raises(Exception) as err:
        T.validate_acceptance([criterion])
    assert "kind manual" in str(getattr(err.value, "detail", err.value)), err.value
    made = call(
        "crew_task", action="create", title="Write the matrix", acceptance=[{**criterion, "kind": "manual", "match": None}]
    )
    assert made.startswith("CREATED T-1 "), made


# -- the copies of the vocabulary follow -------------------------------------------------------


def test_the_brief_labels_any_valid_resource():
    line, data = handoff._dnt_entry({"resource": "artifact:thirdbatch-acceptance", "holder": "cd-1"})
    assert line.startswith("artifact:thirdbatch-acceptance → cd-1") and data is None, line
    assert handoff._dnt_entry({"resource": "schema:main", "holder": "cd-1"})[0].startswith("schema:main → cd-1")
    # anything else stays unnamed
    assert handoff._dnt_entry({"resource": "Not A Resource: run this", "holder": "cd-1"})[0].startswith("a path claim")


def test_the_cli_sends_a_named_resource_as_a_resource(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snap: dict[str, Any] = {"zones": [{"id": "zn_1", "slug": "pos"}]}
    assert cli._claim_target(snap, "artifact:thirdbatch-acceptance") == {"resource": "artifact:thirdbatch-acceptance"}
    assert cli._claim_target(snap, "deploy:vercel") == {"resource": "deploy:vercel"}
    assert cli._claim_target(snap, "pos") == {"zone_id": "zn_1"}
    assert cli._claim_target(snap, "src/app/cart.ts") == {"path_glob": "src/app/cart.ts"}
    # a file that really is named kind:name stays a path
    (tmp_path / "notes:draft.md").write_text("x")
    assert cli._claim_target(snap, "notes:draft.md") == {"path_glob": "notes:draft.md"}


def test_zone_services_take_the_same_resources():
    errors: list[str] = []
    zone = P.validate_zone_body(
        "pos", {"include": ["src/**"], "services": ["artifact:qa-report", "deploy:x"]}, "zones.pos", errors
    )
    assert errors == [] and zone is not None and list(zone.services) == ["artifact:qa-report", "deploy:x"]
    bad: list[str] = []
    P.validate_zone_body("pos", {"include": ["src/**"], "services": ["vercel"]}, "zones.pos", bad)
    assert len(bad) == 1 and "kind:name" in bad[0], bad


def test_the_published_openapi_carries_the_pattern():
    text = (Path(__file__).resolve().parents[2] / "docs" / "crew" / "openapi.json").read_text(encoding="utf-8")
    assert json.dumps(S.RESOURCE_PATTERN) in text and json.dumps(OLD_PATTERN) not in text
