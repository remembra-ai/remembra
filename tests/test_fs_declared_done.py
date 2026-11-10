"""F17: declared ``done`` items in the handoff sections, and the alias reading of the normalizer."""

from __future__ import annotations

from remembra.relay.declared_facts import normalize_close_facts
from remembra.relay.handoff import build_sections, sections_have_substance


def test_declared_done_items_lead_the_done_section():
    sections = build_sections(
        {"done": ["verified live deploy", "  ", "x" * 400], "commits": [{"sha": "abc1234", "subject": "fix"}]}
    )
    assert sections["done"][0] == "verified live deploy"
    assert sections["done"][1].startswith("xxx") and len(sections["done"][1]) <= 201
    assert sections["done"][2] == "abc1234 fix"
    assert sections["headline"].startswith("1 commit(s), last: fix")


def test_a_close_with_no_git_facts_is_headed_by_what_was_declared():
    sections = build_sections({"done": ["a", "b"], "todos_open": ["c"]})
    assert sections["done"] == ["a", "b"]
    assert sections["headline"] == "2 item(s) declared done (no commits or file changes recorded) [1 open]"
    assert sections_have_substance(sections)


def test_more_than_ten_declared_items_are_counted_not_listed():
    sections = build_sections({"done": [f"item {i}" for i in range(14)]})
    assert sections["done"][:10] == [f"item {i}" for i in range(10)]
    assert sections["done"][10] == "(+4 more declared done)"
    assert sections["headline"].startswith("14 item(s) declared done")


def test_nothing_declared_is_unchanged():
    for facts in ({}, {"done": None}, {"done": []}, {"done": ["", "  "]}):
        sections = build_sections(facts)
        assert sections["done"] == [] and sections["headline"] == "no commits or file changes", facts


def test_aliases_are_read_as_the_field_they_mean_and_add_to_it():
    facts, changes = normalize_close_facts(
        {
            "todos_open": ["a"],
            "open": ["b"],
            "open_work": "c",
            "not_done": ["a"],
            "next": "ship",
            "failing": ["e"],
            "unresolved_failures": None,
            "completed": ["z"],
            "done": ["y"],
        }
    )
    assert facts == {"todos_open": ["a", "b", "c"], "next_step": "ship", "errors": ["e"], "done": ["y", "z"]}
    assert any(line.startswith("open: read as todos_open") for line in changes)
    # the field's own key wins for a single value
    assert normalize_close_facts({"next_step": "mine", "next": "other"})[0] == {"next_step": "mine"}
    # idempotent
    assert normalize_close_facts(facts) == (facts, [])


def test_unknown_keys_go_to_the_notes_only_for_agent_declared_facts():
    facts, changes = normalize_close_facts({"deployed": True, "bogus": {"a": [1, 2]}, "empty": None, "notes": "n"})
    assert facts == {"notes": 'n\nOther declared facts: deployed: true; bogus: {"a": [1, 2]}'}
    assert changes == ["deployed, bogus: no handoff field for these, kept in notes"]
    assert normalize_close_facts(facts) == (facts, [])
    cli = {"facts_source": "relay-cli:git", "commit_range": "x", "branch": "main"}
    assert normalize_close_facts(cli) == (cli, [])
    declared = normalize_close_facts({"facts_source": "agent-declared", "pushed": False})[0]
    assert declared == {"facts_source": "agent-declared", "notes": "Other declared facts: pushed: false"}


def test_the_known_keys_are_the_close_route_fields():
    from remembra.api.v1.relay import FactsIn
    from remembra.relay.declared_facts import KNOWN_KEYS

    assert set(FactsIn.model_fields) == KNOWN_KEYS
