"""The dashboard's credit copy states the Free plan's daily cap on stores without enrichment.

When credits run out, or the free-tier breaker is open, a store is saved without enrichment,
but on Free only up to ``max_unenriched_writes_per_day``: past it the server answers 429
(cloud/limits.py). The dashboard used to say only that new memories "still save". It now
names the cap, from ``FREE_UNENRICHED_PER_DAY`` in dashboard/src/lib/credits.ts, which must
be the plan's own number.
"""

from __future__ import annotations

import re
from pathlib import Path

from remembra.cloud.plans import PLANS, PlanTier

ROOT = Path(__file__).resolve().parent.parent
CREDITS_TS = ROOT / "dashboard" / "src" / "lib" / "credits.ts"
CREDITS_CARD = ROOT / "dashboard" / "src" / "components" / "credits" / "Credits.tsx"


def test_the_dashboard_free_cap_is_the_free_plans_cap() -> None:
    match = re.search(r"export const FREE_UNENRICHED_PER_DAY = (\d+);", CREDITS_TS.read_text())
    assert match, "FREE_UNENRICHED_PER_DAY not found in dashboard/src/lib/credits.ts"
    assert int(match.group(1)) == PLANS[PlanTier.FREE].max_unenriched_writes_per_day


def test_the_running_low_line_names_the_free_cap() -> None:
    card = CREDITS_CARD.read_text()
    assert "just without enrichment" not in card  # "still save, just without enrichment" left the cap out
    assert "(on Free, up to ${FREE_UNENRICHED_PER_DAY} a day)" in card
