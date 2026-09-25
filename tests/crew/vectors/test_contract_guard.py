"""Guard decision table (§5.2): table vectors, first-match order, ask rule, and the concrete vectors for gatecore."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from remembra.crew import schemas as S
from tests.crew.vectors.loader import load, run_guard_concrete, run_guard_table

TABLE = load("guard/table.json")["cases"]
CONCRETE = load("guard/concrete.json")


def _decide(facts: dict[str, Any], mode: str, **kw: Any) -> dict[str, Any]:
    return S.guard_decide(facts, mode, **kw).as_dict()


@pytest.mark.parametrize("case", TABLE, ids=lambda c: c["name"])
def test_table_vector(case: dict[str, Any]) -> None:
    got = S.guard_decide(
        case["facts"],
        case["mode"],
        interactive_override=case.get("interactive_override", False),
        permission_mode=case.get("permission_mode", "default"),
    )
    assert got.as_dict() == case["expect"]


def test_runner_passes_reference_and_catches_a_wrong_table() -> None:
    assert run_guard_table(_decide) == []

    def swapped(facts: dict[str, Any], mode: str, **kw: Any) -> dict[str, Any]:
        out = _decide(facts, mode, **kw)
        if out["rule"] == 6:  # a gate that lets ignore win over crew-policy would be a hole
            out["rule"] = 2
        return out

    assert run_guard_table(swapped) != []


def test_every_rule_is_covered_in_both_modes() -> None:
    for rule in range(1, 20):
        for mode in ("enforce", "observe"):
            assert any(c["expect"]["rule"] == rule and c["mode"] == mode for c in TABLE) or rule in (12, 16, 19, 10, 17), (
                rule,
                mode,
            )
    covered = {(c["expect"]["rule"], c["mode"]) for c in TABLE}
    assert {(r, m) for r in (10, 17) for m in ("enforce", "observe")} <= covered
    assert all(any(c["expect"]["rule"] == r and c["expect"]["decision"] == "ask" for c in TABLE) for r in (9, 10, 13, 14, 15))


def _single(pred: str) -> dict[str, Any]:
    facts: dict[str, Any] = {pred: True}
    if pred == "leaf_zone_unclaimed":
        facts["auto_claim_result"] = "granted"
    return facts


def test_first_match_order_for_every_pair_of_rows() -> None:
    rule_of = {p: r.number for r in S.GUARD_RULES for p in r.any_of}
    for a, b in itertools.combinations(S.GUARD_PREDICATES, 2):
        facts = {**_single(a), **_single(b)}
        for mode in ("enforce", "observe"):
            assert S.guard_decide(facts, mode).rule == min(rule_of[a], rule_of[b]), (a, b, mode)


def test_rows_1_to_5_never_ask_and_observe_never_asks() -> None:
    for rule in S.GUARD_RULES:
        facts = _single(rule.any_of[0])
        out = S.guard_decide(facts, "enforce", interactive_override=True)
        assert (out.decision == "ask") == rule.ask_eligible, rule.number
        assert S.guard_decide(facts, "observe", interactive_override=True).decision != "ask"


def test_observe_mode_only_denies_rows_1_to_5() -> None:
    for rule in S.GUARD_RULES:
        out = S.guard_decide(_single(rule.any_of[0]), "observe")
        assert (out.decision == "deny") == (rule.number <= 5), rule.number


def test_bad_inputs_raise() -> None:
    with pytest.raises(ValueError):
        S.guard_decide({"made_up": True})
    with pytest.raises(ValueError):
        S.guard_decide({"no_zone_match": True}, "off")
    with pytest.raises(ValueError):
        S.guard_decide({"leaf_zone_unclaimed": True})  # needs the auto-claim result
    with pytest.raises(ValueError):
        S.guard_decide({"no_zone_match": True, "undeclared_policy": "lock_everything"})


# ---------------------------------------------------------------------------
# Concrete vectors (consumed by gatecore, WP-3)
# ---------------------------------------------------------------------------


def test_concrete_snapshot_is_a_valid_signed_local_snapshot() -> None:
    snap = CONCRETE["snapshot"]
    assert S.validate(snap, S.LOCAL_SNAPSHOT) == []
    assert S.verify_snapshot_hmac(CONCRETE["hmac_key"].encode(), snap)
    session_ids = {s["id"] for s in snap["sessions"]}
    checkout_sessions = {c["session_id"] for c in snap["checkouts"]}
    for case in CONCRETE["cases"]:
        assert case["caller"] in session_ids and case["caller"] in checkout_sessions, case["name"]


@pytest.mark.parametrize("case", CONCRETE["cases"], ids=lambda c: c["name"])
def test_concrete_facts_lead_to_the_expected_outcome(case: dict[str, Any]) -> None:
    exp = case["expect"]
    if exp["rule"] == 0:
        assert case["facts"] == {} and exp["decision"] == "allow"
        assert exp["variant"] in ("read_only", "outside_checkouts", "post_tool_check")
        return
    got = S.guard_decide(case["facts"], case["mode"])
    assert (got.rule, got.decision, got.variant) == (exp["rule"], exp["decision"], exp["variant"])


def test_concrete_vectors_cover_every_rule() -> None:
    assert {c["expect"]["rule"] for c in CONCRETE["cases"]} == set(range(0, 20))


def test_concrete_runner_mechanics() -> None:
    def oracle(case: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        assert ctx["snapshot"]["crew"]["id"].startswith("crw_")
        return {**case["expect"], "facts": case["facts"]}

    assert run_guard_concrete(oracle) == []

    def allow_all(case: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        return {"rule": 19, "decision": "allow", "variant": "footprint"}

    failures = run_guard_concrete(allow_all)
    assert len(failures) >= 30

    def wrong_facts(case: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        return {**case["expect"], "facts": {}}

    assert any("facts expected" in f for f in run_guard_concrete(wrong_facts))
