"""MCP tool map (§8.2) and zone command grammar (D38)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from remembra.crew import schemas as S
from tests.crew.vectors.loader import load, run_command_grammar, run_mcp_tool_map


def _classify(name: str, tool_input: dict[str, Any], rules: list[Any]) -> dict[str, Any]:
    return S.classify_mcp_tool(name, tool_input, rules).as_dict()


def test_reference_tool_map_satisfies_vectors() -> None:
    assert run_mcp_tool_map(_classify) == []


def test_tool_map_runner_catches_a_map_without_ddl_detection() -> None:
    def no_ddl(name: str, tool_input: dict[str, Any], rules: list[Any]) -> dict[str, Any]:
        out = _classify(name, tool_input, rules)
        return {**out, "kind": "other", "services": []} if "execute_sql" in name else out

    assert len(run_mcp_tool_map(no_ddl)) == 2


def test_tool_map_covers_every_category() -> None:
    kinds = {c["expect"]["kind"] for c in load("mcp/tool_map.json")["cases"]}
    assert kinds == {"read", "services", "paths", "other", "zone"}


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("mcp__stripe__*", "mcp__stripe__create_invoice", True),
        ("mcp__stripe__*", "mcp__stripes__x", False),
        ("*apply_migration*", "mcp__supabase__apply_migration", True),
        ("*", "", True),
        ("", "", True),
        ("", "x", False),
        ("a*b*c", "aXXbYYc", True),
        ("a*b*c", "aXXbYY", False),
        ("mcp__*__write_file", "mcp__fs__write_file", True),
    ],
)
def test_star_match(pattern: str, text: str, expected: bool) -> None:
    assert S.star_match(pattern, text) is expected


def test_star_match_has_no_catastrophic_backtracking() -> None:
    start = time.perf_counter()
    assert not S.star_match("*a*a*a*a*a*a*a*b", "a" * 4000)
    assert time.perf_counter() - start < 2.0


def test_split_mcp_name() -> None:
    assert S.split_mcp_name("mcp__supabase__apply_migration") == ("supabase", "apply_migration")
    assert S.split_mcp_name("mcp__a__b__c") == ("a", "b__c")
    assert S.split_mcp_name("Edit") == ("", "Edit")
    assert S.split_mcp_name("mcp__weird") == ("weird", "")


def test_reference_grammar_satisfies_vectors() -> None:
    assert run_command_grammar(S.validate_command_pattern, S.validate_command_patterns, S.command_pattern_matches) == []


def test_grammar_runner_catches_a_regex_matcher() -> None:
    import re

    def regex_match(pattern: str, argv: list[str]) -> bool:
        return re.match(pattern.replace("*", ".*"), " ".join(argv)) is not None

    def accept_all(pattern: Any) -> list[str]:
        return []

    failures = run_command_grammar(accept_all, S.validate_command_patterns, regex_match)
    assert any("invalid pattern" in f for f in failures)
    assert any(f.startswith("match") for f in failures)


def test_grammar_rejects_non_strings_and_matching_is_linear() -> None:
    assert S.validate_command_pattern(None) == ["pattern must be a string"]
    argv = ["x"] * 10_000
    start = time.perf_counter()
    assert S.command_pattern_matches("x * x * x", argv)
    assert time.perf_counter() - start < 0.5
