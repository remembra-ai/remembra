"""The fidelity benchmark corpus is synthetic (LEAK-4).

benchmarks/fidelity/corpus.json once held what read like real private
memories: trading settings, a customer's sign-up date, habits and server
details. It is public, so it must stay made up: no real names, products,
customers, infrastructure or addresses. The words it must not contain are
checked by scripts/ci/repo_hygiene.py's hashed "owner-private" rule, so this
file does not repeat them either.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIDELITY = ROOT / "benchmarks" / "fidelity"
CORPUS = json.loads((FIDELITY / "corpus.json").read_text())


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_corpus_items_have_the_benchmark_shape() -> None:
    runner = _load("run_fidelity", FIDELITY / "run_fidelity.py")
    items = CORPUS["items"]
    assert len(items) == 25
    assert len({it["id"] for it in items}) == len(items)
    for it in items:
        assert set(it) == {"id", "content", "probe", "numbers"}, it["id"]
        assert it["content"].strip() and it["probe"].strip(), it["id"]
        # Every figure the scorer looks for is one it can find in the source (numbers_in is the scorer's parser).
        expected = {n.replace(",", "") for n in it["numbers"]}
        assert expected <= runner.numbers_in(it["content"]), (it["id"], expected - runner.numbers_in(it["content"]))


def test_corpus_names_no_real_people_products_or_infrastructure() -> None:
    hygiene = _load("repo_hygiene", ROOT / "scripts" / "ci" / "repo_hygiene.py")
    text = (FIDELITY / "corpus.json").read_text()
    found = [str(f) for f in hygiene.content_findings("benchmarks/fidelity/corpus.json", text)]
    assert not found, found
    # No address of any kind, public or not.
    assert not re.search(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text)


def test_no_results_are_kept_from_another_corpus() -> None:
    # Per-item results are only meaningful for the corpus they were measured on.
    for path in (FIDELITY / "results").glob("*.json") if (FIDELITY / "results").exists() else []:
        assert json.loads(path.read_text()).get("corpus_version") == CORPUS["version"], path.name


def test_the_owner_private_rule_fires_in_benchmarks_only() -> None:
    hygiene = _load("repo_hygiene", ROOT / "scripts" / "ci" / "repo_hygiene.py")
    product = "Yaad" + "Books"  # split so this file does not match the rule itself
    line = f'{{"content": "{product} filed the quarterly return on day 12."}}'
    assert [f.rule for f in hygiene.content_findings("benchmarks/fidelity/corpus.json", line)] == ["owner-private"]
    assert hygiene.content_findings("benchmarks/other/data.json", line)[0].line == 1
    assert hygiene.content_findings("docs/guides/example.md", line) == []
