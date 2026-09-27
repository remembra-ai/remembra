"""Repository notes under docs/ that must never be published on docs.remembra.dev.

docs.Dockerfile builds every file in docs/, so a note that is not documentation
(the cloud operator's runbook, bug write-ups, feedback transcripts, the
competitor scan) became a public page as soon as it was committed. mkdocs.yml's
exclude_docs keeps them off the site; `mkdocs build --strict` then fails if a
published page links into an excluded one, and this test catches it earlier.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
EXCLUDED = [
    "bugs/",
    "feedback/",
    "ENTITY-RESOLUTION.md",
    "guides/field-notes.md",
    "competitive-analysis-2026.md",
    "DEPLOYING.md",
    "DEPLOYMENT.md",
]


def _exclude_patterns() -> list[str]:
    text = (ROOT / "mkdocs.yml").read_text()
    block = re.search(r"^exclude_docs: \|\n((?:  .*\n)+)", text, re.M)
    assert block, "mkdocs.yml has no exclude_docs block"
    return [line.strip() for line in block.group(1).splitlines() if line.strip()]


def _is_excluded(rel: str) -> bool:
    return any(rel.startswith(p) if p.endswith("/") else rel == p for p in EXCLUDED)


def test_internal_notes_are_excluded_from_the_docs_site() -> None:
    assert _exclude_patterns() == EXCLUDED


def test_no_nav_entry_points_at_an_excluded_page() -> None:
    nav = (ROOT / "mkdocs.yml").read_text().split("\nnav:", 1)[1]
    targets = re.findall(r":\s*([\w./-]+\.md)\s*$", nav, re.M) + re.findall(r"^\s*-\s*([\w./-]+\.md)\s*$", nav, re.M)
    assert targets
    assert not [t for t in targets if _is_excluded(t)]


def test_no_published_page_links_into_an_excluded_one() -> None:
    offenders = []
    for page in DOCS.rglob("*.md"):
        rel = page.relative_to(DOCS).as_posix()
        if _is_excluded(rel):
            continue
        for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", page.read_text()):
            if target.startswith(("http://", "https://")):
                continue
            resolved = (page.parent / target).resolve()
            try:
                linked = resolved.relative_to(DOCS.resolve()).as_posix()
            except ValueError:
                continue
            if _is_excluded(linked):
                offenders.append(f"{rel} -> {target}")
    assert not offenders, offenders


def test_the_feedback_transcript_is_not_in_the_public_repository() -> None:
    # It quoted the owner's personal details and user counts; it lives in the private audits folder now.
    assert not list((DOCS / "feedback").glob("*.md")) if (DOCS / "feedback").exists() else True


def _tracked(*pathspec: str) -> list[str]:
    if not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout of the repository")
    out = subprocess.run(["git", "ls-files", "-z", *pathspec], cwd=ROOT, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p]


def test_no_built_site_is_tracked() -> None:
    # site/ is MkDocs' build output. docs.Dockerfile and the Pages workflow build it from docs/ on every
    # deploy; a committed copy went stale and kept publishing pages that were later unpublished.
    assert _tracked("site") == []


def test_the_feedback_transcript_is_nowhere_in_the_tree() -> None:
    # Not in docs/, and no rendered or copied version of it anywhere else (the old site/ build held one).
    tracked = _tracked()
    spec = importlib.util.spec_from_file_location("repo_hygiene", ROOT / "scripts" / "ci" / "repo_hygiene.py")
    assert spec is not None and spec.loader is not None
    hygiene = importlib.util.module_from_spec(spec)
    sys.modules["repo_hygiene"] = hygiene
    spec.loader.exec_module(hygiene)
    copies = []
    for rel in tracked:
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > hygiene.MAX_TEXT_BYTES:
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        copies += [
            str(f) for f in hygiene.content_findings(rel, data.decode("utf-8", "replace")) if f.rule == "feedback-transcript"
        ]
    assert not copies, copies
    assert not [p for p in tracked if p.startswith(("docs/feedback/", "site/"))]
