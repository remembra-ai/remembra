"""gatecore paths, globs, zones, the command trie and the real filesystem (§8.2, §13.1, D38)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import gatecore as G
from remembra.crew import schemas as S
from tests.crew.gatecore_support import snapshot
from tests.crew.vectors.loader import run_command_grammar

# ---------------------------------------------------------------------------
# Globs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "path", "ci", "expect"),
    [
        ("src/app/pos/**", "src/app/pos/cart.ts", False, True),
        ("src/app/pos/**", "src/app/pos/a/b/c.ts", False, True),
        ("src/app/pos/**", "src/app/pos", False, True),
        ("src/app/pos/**", "src/app/posx/a.ts", False, False),
        ("src/app/pos/**", "src/App/POS/cart.ts", False, False),
        ("src/app/pos/**", "src/App/POS/cart.ts", True, True),
        ("src/app/café/**", "src/app/café/menu.ts", False, True),  # NFD path, NFC glob
        ("src/**/*.test.ts", "src/a/b/x.test.ts", False, True),
        ("src/**/*.test.ts", "src/x.test.ts", False, True),
        ("src/**/*.test.ts", "src/x.ts", False, False),
        ("src/*.ts", "src/a/b.ts", False, False),
        ("src/?.ts", "src/a.ts", False, True),
        ("src/[ab].ts", "src/b.ts", False, True),
        ("src/[!ab].ts", "src/b.ts", False, False),
        ("src/[a-c]x.ts", "src/cx.ts", False, True),
        ("src/{pos,reports}/**", "src/reports/a.ts", False, True),
        ("src/{pos,reports}/**", "src/billing/a.ts", False, False),
        ("docs", "docs/guide.md", False, True),  # a literal also covers everything below
        ("docs/", "docs/a/b.md", False, True),
        ("package.json", "package.json", False, True),
        ("package.json", "apps/web/package.json", False, False),
        ("./db/**", "db/x.sql", False, True),
        ("/db/**", "db/x.sql", False, True),
        ("**", "anything/at/all", False, True),
        ("a*b*c*d*e*f*g*h*i*j*k", "a" * 60 + "k", False, False),  # no backtracking blow-up
        ("a*b*k", "a" * 30 + "b" + "a" * 30 + "k", False, True),
    ],
)
def test_glob_match(glob: str, path: str, ci: bool, expect: bool) -> None:
    assert G.glob_match(glob, path, case_insensitive=ci) is expect


def test_glob_directory_relations() -> None:
    g = G.compile_glob("src/app/pos/**")
    assert g.overlaps_dir("src") and g.overlaps_dir("src/app") and g.overlaps_dir("src/app/pos/x")
    assert not g.overlaps_dir("src/lib")
    assert g.contains_dir("src/app/pos") and g.contains_dir("src/app/pos/components")
    assert not g.contains_dir("src/app") and not g.contains_dir("src")
    assert not G.compile_glob("src/app/pos/*.ts").contains_dir("src/app/pos")
    assert G.compile_glob("src/**/gen/**").overlaps_dir("src/a/b")
    assert G.compile_glob("src/app/pos/**").static_prefix() == "src/app/pos"


def test_pathological_globs_stay_fast() -> None:
    import time

    g = G.compile_glob("**/" + "*a" * 20 + "/**")
    t = time.perf_counter()
    for _ in range(200):
        g.match("/".join(["a" * 30] * 20) + "/b")
    assert time.perf_counter() - t < 1.0


def test_path_helpers() -> None:
    assert G.normalize_rel("./src//app/../app/pos/") == "src/app/pos"
    assert G.normalize_rel("") == "." and G.normalize_rel("./") == "."
    assert G.fold("Café", True) == "café"
    assert G.is_under("/w/A/b", "/w/a", case_insensitive=True)
    assert not G.is_under("/w/A/b", "/w/a")
    assert not G.is_under("/w/ab", "/w/a")
    assert G.rel_to("/w/a/x/y", "/w/a") == "x/y" and G.rel_to("/w/a", "/w/a") == "."


# ---------------------------------------------------------------------------
# Command trie (D38)
# ---------------------------------------------------------------------------


def _trie_match(pattern: str, argv: list[str]) -> bool:
    trie = G.CommandTrie()
    trie.add(pattern, "z")
    return trie.match(argv) == {"z"}


def test_trie_conforms_to_the_grammar_vectors() -> None:
    assert run_command_grammar(S.validate_command_pattern, S.validate_command_patterns, _trie_match) == []


def test_trie_with_many_patterns_matches_like_the_reference() -> None:
    patterns = {
        "supabase db push *": "db",
        "prisma migrate dev": "db",
        "vercel deploy": "deploy",
        "vercel * --prod": "deploy_prod",
        "npm run *": "npm",
        "git push origin main": "main",
    }
    trie = G.CommandTrie()
    for p, v in patterns.items():
        trie.add(p, v)
    argvs = [
        ["supabase", "db", "push"],
        ["supabase", "db", "push", "--linked"],
        ["supabase", "--debug", "db", "push"],
        ["prisma", "migrate", "dev", "--name", "x"],
        ["vercel", "deploy", "--prod"],
        ["vercel", "promote", "--prod"],
        ["npm", "run"],
        ["npm", "run", "build"],
        ["git", "push", "origin", "main"],
        ["git", "push"],
        [],
    ]
    for argv in argvs:
        expect = {v for p, v in patterns.items() if S.command_pattern_matches(p, argv)}
        assert trie.match(argv) == expect, argv
    with pytest.raises(ValueError):
        trie.add("rm -rf (.*)", "x")


def test_invalid_zone_patterns_are_skipped_not_fatal() -> None:
    zones = [
        {"id": "zn_a", "slug": "a", "include_globs": ["a/**"], "command_patterns": ["deploy.*", "fly deploy"], "is_leaf": True}
    ]
    zi = G.ZoneIndex(zones, [], [])
    assert zi.bad_patterns == ["deploy.*"]
    assert zi.trie.match(["fly", "deploy", "--now"]) == {"zn_a"}


def test_zone_index_nesting_and_excludes() -> None:
    zones = [
        {"id": "zn_app", "slug": "app", "include_globs": ["src/app/**"], "is_leaf": False},
        {
            "id": "zn_pos",
            "slug": "pos",
            "parent_id": "zn_app",
            "include_globs": ["src/app/pos/**"],
            "exclude_globs": ["src/app/pos/**/*.md"],
        },
        {"id": "zn_old", "slug": "old", "include_globs": ["legacy/**"], "archived_at": "2026-01-01T00:00:00Z"},
    ]
    zi = G.ZoneIndex(zones, [{"glob": "src/app/pos/shared.ts", "kind": "plain"}], ["**/*.snap"])
    assert {z["slug"] for z in zi.match("src/app/pos/a.ts", False)} == {"app", "pos"}
    assert {z["slug"] for z in zi.match("src/app/pos/README.md", False)} == {"app"}
    assert zi.match("legacy/x.ts", False) == []
    assert zi.commons_entry("src/app/pos/shared.ts", False) == {"glob": "src/app/pos/shared.ts", "kind": "plain"}
    assert zi.ignored("src/x/__snapshots__/a.snap", False)
    assert [z["slug"] for z in zi.ancestors("zn_pos")] == ["app"]


# ---------------------------------------------------------------------------
# The real filesystem: symlinks, hard links, case-insensitive volumes
# ---------------------------------------------------------------------------


def _real_snapshot(root: Path, home: Path) -> dict[str, Any]:
    snap = snapshot()
    for co in snap["checkouts"]:
        name = co["toplevel"].rsplit("/", 1)[1]
        co["toplevel"] = str(root / name)
        co["git_common_dir"] = str(root / "yaadbooks" / ".git")
    return snap


@pytest.fixture()
def real_tree(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = Path(os.path.realpath(tmp_path))
    home = root / "home"
    for name in ("yaadbooks-a", "yaadbooks-b", "yaadbooks-c", "yaadbooks-p"):
        (root / name / "src" / "app" / "pos").mkdir(parents=True)
        (root / name / "src" / "lib").mkdir(parents=True)
        (root / name / ".remembra").mkdir()
    (root / "yaadbooks" / ".git" / "hooks").mkdir(parents=True)
    (home / ".remembra" / "crew" / "bin").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    return root, home, _real_snapshot(root, home)


def _real_run(
    root: Path, home: Path, snap: dict[str, Any], tool: str, tool_input: dict[str, Any], caller: str = "cs_c"
) -> G.Verdict:
    return G.evaluate(
        tool,
        tool_input,
        snapshot=snap,
        caller=caller,
        cwd=str(root / "yaadbooks-c"),
        home=str(home),
        now="2026-09-25T20:01:00Z",
        mode="enforce",
        fs=G.OsFs(),
        human="Mani",
    )


def test_real_symlink_into_another_checkout_is_denied(real_tree: tuple[Path, Path, dict[str, Any]]) -> None:
    root, home, snap = real_tree
    link = root / "yaadbooks-c" / "vendor"
    link.symlink_to(root / "yaadbooks-a" / "src" / "app")
    v = _real_run(root, home, snap, "Write", {"file_path": str(link / "pos" / "cart.ts"), "content": "x"})
    assert (v.rule, v.decision) == (4, "deny")
    v = _real_run(root, home, snap, "Bash", {"command": "echo x > vendor/pos/cart.ts"})
    assert (v.rule, v.decision) == (4, "deny")


def test_real_hard_link_to_another_sessions_dirty_file(real_tree: tuple[Path, Path, dict[str, Any]]) -> None:
    root, home, snap = real_tree
    theirs = root / "yaadbooks-b" / "src" / "lib" / "format.ts"
    theirs.write_text("x")
    mine = root / "yaadbooks-c" / "src" / "lib" / "alias.ts"
    os.link(theirs, mine)
    v = _real_run(root, home, snap, "Edit", {"file_path": str(mine), "old_string": "x", "new_string": "y"})
    assert (v.rule, v.decision) == (4, "deny")


def test_real_crew_policy_locations(real_tree: tuple[Path, Path, dict[str, Any]]) -> None:
    root, home, snap = real_tree
    for path in (
        root / "yaadbooks-c" / ".remembra" / "zones.yml",
        home / ".remembra" / "crew" / "bin" / "crew-gate.py",
        root / "yaadbooks" / ".git" / "hooks" / "pre-push",
        home / ".REMEMBRA" / "crew" / "x.json",  # case variants of crew-policy paths are denied on any volume
    ):
        v = _real_run(root, home, snap, "Write", {"file_path": str(path), "content": "x"})
        assert (v.rule, v.variant) == (2, "crew_policy"), path


def test_real_settings_file_protection(real_tree: tuple[Path, Path, dict[str, Any]]) -> None:
    root, home, snap = real_tree
    settings = home / ".claude" / "settings.json"
    entry = {"hooks": [{"type": "command", "command": f"py gate pretool {S.CREW_HOOK_MARKER}"}]}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [entry]}, "theme": "dark"}))
    drop = _real_run(root, home, snap, "Write", {"file_path": str(settings), "content": json.dumps({"theme": "dark"})})
    assert (drop.rule, drop.variant) == (2, "tamper")
    keep = _real_run(
        root, home, snap, "Edit", {"file_path": str(settings), "old_string": '"theme": "dark"', "new_string": '"theme": "light"'}
    )
    assert (keep.rule, keep.decision) == (0, "allow")


def test_real_append_only_uses_existence_on_disk(real_tree: tuple[Path, Path, dict[str, Any]]) -> None:
    root, home, snap = real_tree
    mig = root / "yaadbooks-c" / "supabase" / "migrations"
    mig.mkdir(parents=True)
    (mig / "0001_init.sql").write_text("create table x();")
    edit = _real_run(root, home, snap, "Write", {"file_path": str(mig / "0001_init.sql"), "content": "drop table x;"})
    assert (edit.rule, edit.decision) == (11, "deny")
    new = _real_run(root, home, snap, "Write", {"file_path": str(mig / "0002_more.sql"), "content": "x"})
    assert (new.rule, new.decision) == (12, "allow")
    assert "micro_lease" in new.effects and "schema_claim" in new.effects


def test_osfs_never_raises() -> None:
    fs = G.OsFs()
    assert fs.exists("/definitely/not/here") is False
    assert fs.is_dir("\x00bad") is False
    assert fs.inode("/definitely/not/here") is None
    assert fs.read_text("/definitely/not/here") is None
    assert fs.realpath("/a/../b") == os.path.realpath("/b")
