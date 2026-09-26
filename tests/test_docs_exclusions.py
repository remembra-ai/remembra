"""Repository notes under docs/ that must never be published on docs.remembra.dev.

docs.Dockerfile builds every file in docs/, so a note that is not documentation
(the cloud operator's runbook, bug write-ups, feedback transcripts, the
competitor scan) became a public page as soon as it was committed. mkdocs.yml's
exclude_docs keeps them off the site; `mkdocs build --strict` then fails if a
published page links into an excluded one, and this test catches it earlier.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
EXCLUDED = ["bugs/", "feedback/", "competitive-analysis-2026.md", "DEPLOYING.md", "DEPLOYMENT.md"]


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
