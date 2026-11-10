"""F3: the PreToolUse gate must not judge Remembra's own coordination tools as file writes.

``crew_claim`` and ``crew_guard`` take a ``paths`` argument that names what to claim, release, adopt or
check; it is not a list of files the call writes. The server answers those calls (GRANTED / QUEUED /
REFUSED) and it alone honours ``action`` and ``mode``.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from remembra.crew import gatecore as G
from remembra.crew import schemas as S
from tests.crew import gatecore_support as GS

CLAIM = "mcp__remembra__crew_claim"
GUARD = "mcp__remembra__crew_guard"


def _run(tool: str, tool_input: dict[str, Any], **kw: Any) -> tuple[G.Verdict, list[G.ClaimRequest]]:
    calls: list[G.ClaimRequest] = []

    def claim(req: G.ClaimRequest) -> str:
        calls.append(req)
        return "granted"

    return GS.run(tool, tool_input, claim=claim, **kw), calls


def _capped_snapshot() -> dict[str, Any]:
    """cs_a holds three exclusive zones: the gate's cap for its own auto-claims."""
    snap = GS.snapshot()
    third = copy.deepcopy(GS.find(snap["claims"], "clm_pos"))
    third.update({"id": "clm_third", "zone_id": "zn_payroll"})
    snap["claims"].append(third)
    return snap


# -- classifier -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        (CLAIM, {"action": "release", "paths": ["app/not-found.tsx"]}),
        (CLAIM, {"action": "adopt", "paths": ["tests/sales-plan-map-ui.test.mjs"]}),
        (CLAIM, {"action": "claim", "mode": "shared", "paths": ["deploy:backtohello-production"]}),
        (CLAIM, {"paths": ["app/admin", "app/portal", "app/client"]}),
        (CLAIM, {"action": "claim", "paths": "src/app/cart.ts"}),
        (GUARD, {"paths": ["a.py"]}),
        (GUARD, {"paths": [], "command": "git push"}),
        ("mcp__remembra-mcp__crew_claim", {"action": "release", "paths": ["a.py"]}),
        ("mcp__Remembra__crew_claim", {"action": "release", "paths": ["a.py"]}),
        ("mcp__plugin_remembra_remembra__crew_claim", {"action": "release", "paths": ["a.py"]}),
    ],
)
def test_remembra_coordination_tools_are_not_file_writes(name, tool_input):
    cls = S.classify_mcp_tool(name, tool_input)
    assert cls.kind == "other", (name, cls)
    assert cls.paths == () and cls.services == () and cls.zone_slug is None


def test_every_crew_tool_name_is_covered():
    assert {spec.name for spec in S.MCP_TOOLS} == set(S.CREW_MCP_TOOL_NAMES)
    for tool in S.CREW_MCP_TOOL_NAMES:
        assert S.classify_mcp_tool(f"mcp__remembra__{tool}", {"paths": ["a.py"]}).kind == "other", tool


def test_a_zone_rule_cannot_gate_the_coordination_tools():
    """A zones.yml mcp_tools wildcard must not turn release/adopt into a gated zone command."""
    rules = [("ops", {"tool": "mcp__*"}), ("db", {"tool": "mcp__remembra__*", "service": "schema:main"})]
    cls = S.classify_mcp_tool(CLAIM, {"action": "release", "paths": ["a.py"]}, rules)
    assert cls.kind == "other" and cls.zone_slug is None and cls.services == ()


@pytest.mark.parametrize(
    "name",
    [
        "mcp__otherserver__crew_claim",  # a foreign server that happens to reuse the tool name
        "mcp__notremembra__crew_claim",
        "mcp__remembrax__crew_claim",
        "mcp____crew_claim",
        "crew_claim",  # not an MCP tool name at all
    ],
)
def test_foreign_servers_stay_gated(name):
    cls = S.classify_mcp_tool(name, {"paths": ["a.py"]})
    assert cls.kind == "paths" and cls.paths == ("a.py",)


def test_other_remembra_tools_keep_their_classification():
    """Only the coordination tools are exempt: nothing else on the server changes."""
    assert S.classify_mcp_tool("mcp__remembra__recall_memories", {"query": "x"}).kind == "read"
    assert S.classify_mcp_tool("mcp__remembra__store_memory", {"content": "x"}).kind == "other"
    assert S.classify_mcp_tool("mcp__remembra__write_file", {"path": "a.py"}).kind == "paths"


@pytest.mark.parametrize("tool_input", [None, {}, {"paths": None}, {"paths": []}, {"paths": [None, 3, ""]}])
def test_empty_and_malformed_input(tool_input):
    assert S.classify_mcp_tool(CLAIM, tool_input).kind == "other"
    assert S.classify_mcp_tool(GUARD, tool_input).kind == "other"


# -- the gate decision (the real evaluate path) --------------------------------------------


def test_adopt_of_an_offered_baton_is_not_blocked_by_its_own_reservation():
    """Rule 10's deny text recommends this exact call."""
    v, calls = _run(CLAIM, {"action": "adopt", "paths": ["src/app/reports/x.ts"]}, caller="cs_c")
    assert (v.decision, v.rule) == ("allow", 0), (v.rule, v.decision, v.reason)
    assert calls == [] and v.post_tool_check is True and v.claims == []


def test_release_is_not_blocked_by_another_seats_claim():
    v, calls = _run(CLAIM, {"action": "release", "paths": ["src/app/pos/Cart.tsx"]}, caller="cs_c")
    assert (v.decision, v.rule) == ("allow", 0), (v.rule, v.decision, v.reason)
    assert calls == []


def test_guard_check_reaches_the_server():
    v, calls = _run(GUARD, {"paths": ["src/app/pos/Cart.tsx"]}, caller="cs_c")
    assert (v.decision, v.rule) == ("allow", 0), (v.rule, v.decision, v.reason)
    assert calls == []


def test_shared_claim_makes_no_exclusive_auto_claim():
    """Under the cap the gate used to take the whole zone exclusively for the hook seat before the tool ran."""
    v, calls = _run(CLAIM, {"action": "claim", "mode": "shared", "paths": ["src/app/invoices/list.tsx"]}, caller="cs_a")
    assert (v.decision, v.rule) == ("allow", 0), (v.rule, v.decision, v.variant)
    assert calls == [], calls


def test_shared_claim_is_not_denied_by_the_exclusive_cap():
    snap = _capped_snapshot()
    # control: a real file write there is still held to the cap
    edit, _ = _run("Write", {"file_path": "/w/yaadbooks-a/src/app/invoices/list.tsx", "content": "x"}, caller="cs_a", snap=snap)
    assert edit.decision == "deny" and "you hold 3 of 3 exclusive claims, the per-session limit" in (edit.reason or "")
    v, calls = _run(
        CLAIM, {"action": "claim", "mode": "shared", "paths": ["src/app/invoices/list.tsx"]}, caller="cs_a", snap=snap
    )
    assert (v.decision, v.rule) == ("allow", 0), (v.rule, v.decision, v.reason)
    assert calls == []


def test_resource_name_is_not_read_as_a_file_in_another_checkout():
    """``deploy:backtohello-production`` joined to a foreign cwd became "deploy?backtohello-production is inside
    the checkout of cc-1"."""
    v, calls = _run(
        CLAIM,
        {"action": "claim", "paths": ["deploy:backtohello-production"]},
        caller="cs_c",
        cwd="/w/yaadbooks-a",
    )
    assert (v.decision, v.rule) == ("allow", 0), (v.rule, v.decision, v.reason)
    assert calls == [] and v.facts.get("footprints") in (None, [], ())


def test_a_foreign_servers_crew_claim_is_still_judged_as_a_write():
    v, _ = _run("mcp__otherserver__crew_claim", {"paths": ["src/app/pos/Cart.tsx"]}, caller="cs_c")
    assert v.decision == "deny" and v.rule == 9


def test_real_edits_are_still_gated():
    v, _ = _run("Edit", {"file_path": "/w/yaadbooks-c/src/app/pos/Cart.tsx", "old_string": "a", "new_string": "b"})
    assert v.decision == "deny" and v.rule == 9
