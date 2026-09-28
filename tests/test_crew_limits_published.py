"""The Crew mode limits the docs and the pricing page publish are the ones cloud/plans.py enforces (decision 15).

docs/reference/plans-and-credits.md (and its copy in the Marshal pack) and landing/pricing.html each carry a
table of live sessions per crew and teammates per plan, and the sub-agent allowance; every number here is read
from ``PLANS``, so a change to plans.py without the pages (or the other way round) fails. The pricing page says
Crew mode is not available yet for as long as the site's CREW_LIVE switch is off.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from remembra.cloud.plans import PLANS, PlanTier

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "plans-and-credits.md"
PRICING = ROOT / "landing" / "pricing.html"
TIERS = (PlanTier.FREE, PlanTier.SOLO, PlanTier.PRO, PlanTier.TEAM, PlanTier.ENTERPRISE)


def _partials():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("site_partials_for_crew_limits", ROOT / "scripts" / "site_partials.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _expected() -> dict[str, list[str]]:
    return {
        "live sessions per crew": [f"{PLANS[t].max_crew_sessions_live:,}" for t in TIERS],
        "teammates in a crew": ["Yes" if PLANS[t].crew_teammates else "No" for t in TIERS],
    }


def _allowance_sentence() -> str:
    free = {PLANS[t].crew_free_sub_agents_per_parent for t in TIERS}
    assert len(free) == 1, "the pages state one sub-agent allowance for every plan"
    return f"Each session runs its first {free.pop()} live sub-agents on its own seat"


def test_the_plans_doc_publishes_the_crew_limits_plans_py_enforces() -> None:
    doc = DOC.read_text()
    section = doc.split("### Crew mode limits", 1)[1].split("\n## ", 1)[0]
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in section.splitlines() if line.startswith("|")]
    header, body = rows[0], rows[2:]
    assert header[1:] == ["Free", "Solo", "Pro", "Team", "Enterprise"]
    assert {r[0].lower(): r[1:] for r in body} == _expected()
    flat = " ".join(section.split())
    assert _allowance_sentence() in flat
    assert "The crew owner can add teammates through the API" in flat


def test_the_pricing_page_publishes_the_same_crew_limits() -> None:
    html = PRICING.read_text()
    table = re.search(r'<table class="crew-limits">(.*?)</table>', html, re.S)
    assert table, "pricing.html has no crew limits table"
    thead = re.search(r"<thead>(.*?)</thead>", table.group(1), re.S).group(1)
    heads = [_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", thead)]
    assert heads[1:] == ["Free", "Solo", "Pro", "Team", "Enterprise"]
    rows = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", re.search(r"<tbody>(.*?)</tbody>", table.group(1), re.S).group(1), re.S):
        cells = [_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        rows[cells[0].lower()] = cells[1:]
    assert rows == _expected()
    section = _text(re.search(r'<section class="sec" aria-labelledby="crew-limits-title">(.*?)</section>', html, re.S).group(1))
    assert _allowance_sentence() in section
    assert "The crew owner can add teammates through the API" in section


def test_the_pricing_page_says_crew_mode_is_not_available_while_the_site_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partials = _partials()
    html = PRICING.read_text()
    assert partials.CREW_LIVE is False
    assert partials.render(html) == html  # the page is up to date with the switch
    off = _text(html)
    assert "Crew mode is not available on Remembra Cloud yet; when it is, each crew gets the limits of its owner's plan." in off
    monkeypatch.setattr(partials, "CREW_LIVE", True)
    on = _text(partials.render(html))
    assert "not available on Remembra Cloud yet" not in on
    assert "Each crew gets the limits of its owner's plan." in on


def test_the_crew_guide_links_to_the_limits_it_mentions() -> None:
    guide = (ROOT / "docs" / "relay" / "crew.md").read_text()
    assert "(../reference/plans-and-credits.md#crew-mode-limits)" in guide
    assert "(../reference/plans-and-credits.md)" not in guide
    assert "\n### Crew mode limits\n" in DOC.read_text()  # mkdocs gives it the id crew-mode-limits
