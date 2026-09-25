"""Bash parser corpus (§8.2, §13.1): size, coverage, internal consistency and the conformance runner."""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any

import pytest

from remembra.crew import schemas as S
from tests.crew.vectors.loader import bash_expected, load, run_bash_corpus

CORPUS = load("bash/corpus.json")
ENTRIES = CORPUS["entries"]
DEFAULTS = CORPUS["defaults"]


def test_corpus_size_and_uniqueness() -> None:
    assert len(ENTRIES) >= 250
    assert len({e["id"] for e in ENTRIES}) == len(ENTRIES)
    assert len({e["cmd"] for e in ENTRIES}) == len(ENTRIES)
    assert dict(S.BASH_PARSE_DEFAULTS) == DEFAULTS


def test_required_forms_are_covered() -> None:
    tags = {t for e in ENTRIES for t in e["tags"]}
    assert set(CORPUS["required_tags"]) <= tags
    tamper = {k for e in ENTRIES for k in e["expect"].get("tamper", [])}
    # settings_hook_edit is an Edit/Write of settings.json, covered by guard/concrete.json
    assert tamper == set(S.TAMPER_KINDS) - {"settings_hook_edit"}
    ops = {e["expect"].get("git_tree_op") for e in ENTRIES} - {None}
    assert ops == set(S.GIT_TREE_OPS)
    assert sum(1 for e in ENTRIES if e["expect"].get("read_only")) >= 50
    assert sum(1 for e in ENTRIES if e["expect"].get("tree_writer")) >= 25
    assert sum(1 for e in ENTRIES if e["expect"].get("tamper")) >= 40
    assert sum(1 for e in ENTRIES if e["expect"].get("opaque")) >= 40


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["id"])
def test_entry_is_well_formed(entry: dict[str, Any]) -> None:
    exp = entry["expect"]
    assert set(exp) <= set(DEFAULTS), exp
    full = bash_expected(entry, DEFAULTS)
    assert isinstance(full["read_only"], bool) and isinstance(full["opaque"], bool) and isinstance(full["tree_writer"], bool)
    for key in ("writes", "tree_scope", "tamper"):
        assert full[key] == sorted(set(full[key])), key
    assert set(full["tamper"]) <= set(S.TAMPER_KINDS)
    assert full["git_tree_op"] in (None, *S.GIT_TREE_OPS)
    assert full["tree_writer"] == bool(full["tree_scope"])
    shlex.split(entry["cmd"])  # balanced quotes: a typo in the corpus fails here


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["id"])
def test_entry_is_internally_consistent(entry: dict[str, Any]) -> None:
    exp = bash_expected(entry, DEFAULTS)
    cmd = entry["cmd"]
    if exp["read_only"]:
        others = {k: v for k, v in exp.items() if k != "read_only"}
        assert others == {k: v for k, v in DEFAULTS.items() if k != "read_only"}
        assert ">" not in cmd.replace("2>/dev/null", "").replace("> /dev/null", "").replace("2>&1", "")
        words = [w for w in shlex.split(cmd.split("|")[0]) if "=" not in w or w.startswith("-")]
        words = [w for w in words if w not in ("time", "npx")]
        head = words[0]
        assert head in S.BASH_READ_ONLY_COMMANDS or head in ("git", "npm", "pnpm", "yarn", "python", "go", "cargo"), head
        if head == "git":
            assert any(cmd.split("|")[0].strip().startswith(f"git {sub}") for sub in S.BASH_READ_ONLY_GIT), cmd
    for w in exp["writes"]:
        base = posixpath.basename(w) or w
        implied = {"prisma/migrations": "prisma migrate", "~/x": "cd && rm x"}  # targets the parser derives
        assert base in cmd or implied.get(w, "\0") in cmd, (w, cmd)
    for w in exp["tree_scope"]:
        assert w == "." or w in cmd, (w, cmd)
    markers = {
        "no_verify": r"--no-verify|\s-[a-z]*n[a-z]*\s",
        "hooks_path": r"(?i)hookspath",
        "husky_off": r"HUSKY",
        "lefthook_off": r"LEFTHOOK",
        "env_crew_var": r"REMEMBRA_",
        "crewd_kill": r"crewd",
        "crew_files_removed": r"\.remembra|\.git/hooks",
    }
    for kind in exp["tamper"]:
        assert re.search(markers[kind], cmd), (kind, cmd)
    if "not_tamper" in entry["tags"]:
        assert exp["tamper"] == []


def test_heredoc_bodies_are_data_unless_fed_to_a_shell() -> None:
    heredocs = [e for e in ENTRIES if "heredoc" in e["tags"]]
    assert len(heredocs) >= 4
    for e in heredocs:
        exp = bash_expected(e, DEFAULTS)
        if e["cmd"].startswith(("bash", "sh", "zsh")):
            assert exp["opaque"] and exp["tamper"]
        else:
            assert exp["tamper"] == [] and exp["writes"], e["cmd"]


def test_runner_mechanics() -> None:
    def oracle(cmd: str) -> dict[str, Any]:
        entry = next(e for e in ENTRIES if e["cmd"] == cmd)
        full = bash_expected(entry, DEFAULTS)
        return {**full, "writes": list(reversed(full["writes"]))}  # order must not matter

    assert run_bash_corpus(oracle) == []

    def naive(cmd: str) -> dict[str, Any]:
        return {**DEFAULTS, "read_only": cmd.split()[0] in S.BASH_READ_ONLY_COMMANDS}

    failures = run_bash_corpus(naive)
    assert len(failures) >= 200
