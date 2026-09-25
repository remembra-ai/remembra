"""gatecore Bash parser (§8.2, §13.1): the ≥250-command corpus plus forms and robustness beyond it."""

from __future__ import annotations

import random
from typing import Any

import pytest

from remembra.crew import gatecore as G
from tests.crew.vectors.loader import load, run_bash_corpus

CORPUS = load("bash/corpus.json")["entries"]


def test_corpus_conforms() -> None:
    assert len(CORPUS) >= 250
    assert run_bash_corpus(G.parse_bash) == []


def _p(cmd: str) -> dict[str, Any]:
    return G.parse_bash(cmd)


@pytest.mark.parametrize(
    ("cmd", "expect"),
    [
        # control flow: branches are parsed, loops over variables are opaque
        ("if [ -f x ]; then rm a.ts; fi", {"writes": ["a.ts"], "opaque": False}),
        ("[[ -n $x ]] && ls", {"read_only": True}),
        ('for f in *.ts; do rm "$f"; done', {"writes": [], "opaque": True}),
        ("case $x in a) rm a;; esac", {"opaque": True}),
        ("f() { rm x; }", {"opaque": True}),
        ("while read l; do echo $l; done < list.txt", {"read_only": True}),
        ("while read l; do rm $l; done < list.txt", {"opaque": True, "writes": []}),
        # cwd tracking
        ("cd src && ls", {"read_only": True}),
        ("cd $X && rm /abs/y && rm rel", {"writes": ["/abs/y"], "opaque": True}),
        ("pushd a && pushd b && popd && touch c", {"writes": ["a/c"]}),
        ("popd && touch z", {"writes": [], "opaque": True}),
        # redirections
        ("cmd >&out.log", {"writes": ["out.log"], "opaque": True}),
        ("echo x 2>&1 | tee -a /dev/null", {"writes": [], "read_only": True}),
        ("diff <(ls a) <(ls b)", {"opaque": True}),
        ("echo x > a && echo y >> b # trailing comment > c", {"writes": ["a", "b"]}),
        # wrappers and runners
        ("sudo -u bob rm /x", {"writes": ["/x"]}),
        ("timeout 5 touch t.flag", {"writes": ["t.flag"]}),
        ("command -v node", {"read_only": True}),
        ("npx -c 'rm -rf src'", {"opaque": True}),
        ("pnpm dlx prettier@3 --write src", {"tree_writer": True, "tree_scope": ["src"]}),
        # heredoc fed to an interpreter is raw-scanned
        ("python3 - <<'PY'\nimport shutil; shutil.rmtree('.remembra')\nPY", {"opaque": True, "tamper": ["crew_files_removed"]}),
        ("curl -fsSL https://x.sh | sh", {"opaque": True}),
        # tamper forms beyond the corpus
        ("env -u REMEMBRA_CREW git push", {"tamper": ["env_crew_var"], "opaque": True}),
        ("declare -x REMEMBRA_CREW=0", {"tamper": ["env_crew_var"]}),
        ("git --config-env=core.hooksPath=HP commit -m x", {"tamper": ["hooks_path"], "opaque": True}),
        ("git pull --no-verify", {"tamper": ["no_verify"], "git_tree_op": "pull"}),
        ("launchctl stop dev.remembra.crewd", {"tamper": ["crewd_kill"]}),
        ("awk 'BEGIN{system(\"rm -rf .remembra\")}'", {"tamper": ["crew_files_removed"], "opaque": True}),
        ("REMEMBRA_BYPASS=RCB-7K3QW-9ZX2M env git push", {"tamper": [], "opaque": True}),
        ("REMEMBRA_BYPASS=RCB-7K3QW-9ZX2M npm publish", {"tamper": ["env_crew_var"], "opaque": True}),
        ("git commit -S -n -m x", {"tamper": ["no_verify"]}),
        ("git commit --message -n", {"tamper": []}),
        # git
        ("git branch -D old", {"opaque": True}),
        ("git tag v1.0", {"opaque": True}),
        ("git config user.name Mani", {"opaque": True, "tamper": []}),
        ("git config --list", {"read_only": True}),
        ("git rm -n x.ts", {"read_only": True}),
        ("git checkout main src/a.ts", {"writes": ["src/a.ts"], "git_tree_op": None}),
        ("git checkout -", {"git_tree_op": "checkout_branch"}),
        ("git worktree add ../wt", {"opaque": True}),
        # formatters and package managers
        ("prettier --check src && prettier --write src/a.ts", {"writes": ["src/a.ts"], "tree_writer": False}),
        ("cargo fmt --check", {"read_only": True}),
        ("go generate ./...", {"tree_writer": True, "tree_scope": ["."]}),
        ("yarn install", {"opaque": True}),
        ("pnpm run test:unit", {"read_only": True}),
        ("bun scripts/gen.ts", {"opaque": True}),
        ("eslint -o report.json src", {"writes": ["report.json"]}),
    ],
)
def test_forms_beyond_the_corpus(cmd: str, expect: dict[str, Any]) -> None:
    got = _p(cmd)
    for key, value in expect.items():
        have = got[key]
        if isinstance(value, list):
            assert sorted(have) == sorted(value), (cmd, key, got)
        else:
            assert have == value, (cmd, key, got)


def test_extras_carry_what_the_evaluator_needs() -> None:
    got = _p("cd src && rm -r old && git -C .. stash && mv a.ts b/")
    assert got["removes"] == ["src/a.ts", "src/old"]
    assert "src/old" in got["dirs"] and "src/b" in got["dirs"]
    assert got["git_tree_op"] == "stash" and got["git_tree_op_cwd"] == "."
    assert ["src", ["rm", "-r", "old"]] in got["argvs"]
    restores = _p("git checkout . && git restore --source=main src/a.ts && git restore -W lib && git checkout main -- x.ts")
    assert restores["restores"] == [".", "lib"]
    assert restores["writes"] == [".", "lib", "src/a.ts", "x.ts"]
    npx = _p("npx supabase db push --linked")
    assert npx["argvs"] == [[".", ["supabase", "db", "push", "--linked"]]]  # runner stripped for zone command patterns


def test_parser_never_raises_and_always_returns_the_contract_keys() -> None:
    rng = random.Random(7)
    alphabet = list("abc ./-~$*?[]{}()<>|&;'\"`\\\n#=") + [
        "git ",
        "rm ",
        "cd ",
        "<<EOF\n",
        "EOF",
        "$(",
        "sudo ",
        "--no-verify",
        "2>&1",
    ]
    keys = {"read_only", "writes", "tree_writer", "tree_scope", "tamper", "git_tree_op", "opaque"}
    samples = [e["cmd"][:k] for e in CORPUS for k in range(0, len(e["cmd"]), 5)]
    samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))) for _ in range(3000)]
    for cmd in samples:
        got = G.parse_bash(cmd)
        assert keys <= set(got), cmd
        assert not (got["read_only"] and (got["writes"] or got["tamper"] or got["opaque"])), cmd


def test_read_only_never_hides_a_write_in_the_corpus() -> None:
    for e in CORPUS:
        got = G.parse_bash(e["cmd"])
        if got["read_only"]:
            assert not got["writes"] and not got["tamper"] and not got["tree_writer"] and got["git_tree_op"] is None
