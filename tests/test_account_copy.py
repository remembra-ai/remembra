"""The words customers read say exactly what the code does (R-11, R-23, R-26, R-27).

One sentence per rule, kept identical across the dashboard, Terms, Privacy and
pricing pages, and checked against the server defaults it describes.
"""

from __future__ import annotations

import html
import re
from datetime import timedelta
from pathlib import Path

from remembra.cloud.metering import FOUNDING_CHECKOUT_HOLD, FOUNDING_LAPSE_GRACE
from remembra.config import Settings

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
DASHBOARD = ROOT / "dashboard" / "src"


def _text(path: Path) -> str:
    raw = re.sub(r"<[^>]+>", " ", path.read_text())
    return re.sub(r"\s+", " ", html.unescape(raw))


def _ts_string(path: Path, name: str) -> str:
    """A string constant from a TypeScript file (plain or '+'-joined literals)."""
    body = re.search(rf"export const {name} =(.*?);\n", path.read_text(), re.S)
    assert body, name
    parts = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", body.group(1))
    return "".join(a or b for a, b in parts).replace("\\'", "'")


DELETION = _ts_string(DASHBOARD / "lib" / "accountDeletion.ts", "DELETION_COPY")
UNLOCK = _ts_string(DASHBOARD / "lib" / "credits.ts", "BANK_UNLOCK_RULE")


def _default(name: str) -> object:
    return Settings.model_fields[name].default


def test_deletion_copy_is_the_same_everywhere_and_matches_the_code() -> None:
    assert DELETION in _text(LANDING / "terms.html")
    assert DELETION in _text(LANDING / "privacy.html")
    grace = _default("account_erasure_grace_days")
    assert f"{grace} days later everything the account holds is erased" in DELETION
    assert f"erased {grace} days later" in _text(LANDING / "about.html")
    assert f"only until {_default('pre_migration_backup_keep')} newer deploys replace it" in DELETION
    entrypoint = (ROOT / "scripts" / "cloud-entrypoint.sh").read_text()
    assert 'RETENTION="${LITESTREAM_RETENTION:-24h}"' in entrypoint and "keeps 24 hours of history" in DELETION
    # Retention is enforced by an hourly check: a copy can outlive the window by up to that hour.
    assert "retention-check-interval: 1h" in entrypoint and "within about 25 hours" in DELETION
    # The undo line says what an undo does not bring back.
    assert "a cancelled subscription and revoked API keys do not" in DELETION
    # The old promise the code never kept is gone.
    for page in ("terms.html", "privacy.html"):
        assert "delete your data within 30 days" not in _text(LANDING / page)
    settings_page = (DASHBOARD / "pages" / "Settings.tsx").read_text()
    assert "{DELETION_COPY}" in settings_page and "permanently delete your account and all memories" not in settings_page


def test_yearly_bank_rule_is_the_same_on_pricing_terms_and_dashboard() -> None:
    days, months = _default("annual_credit_unlock_days"), _default("annual_credit_initial_months")
    assert (days, months) == (14, 1)
    expected = f"A new yearly plan unlocks its full credit bank {days} days after purchase; "
    assert f"{expected}until then one month's credits are available." == UNLOCK
    pricing = _text(LANDING / "pricing.html")
    faq = re.search(r"What changes on a yearly plan\?(.*?)A refund or chargeback", pricing)
    assert faq and UNLOCK in faq.group(1)
    assert UNLOCK in _text(LANDING / "terms.html")
    assert "{BANK_UNLOCK_RULE}" in (DASHBOARD / "components" / "Billing.tsx").read_text()


def test_founding_lapse_rule_matches_the_seat_logic() -> None:
    assert timedelta(days=14) == FOUNDING_LAPSE_GRACE
    assert timedelta(hours=2) == FOUNDING_CHECKOUT_HOLD
    rule = "If it ends, the price and the seat are kept for 14 days; after that the seat goes to the next customer"
    assert rule in _text(LANDING / "terms.html")
    assert rule in _text(LANDING / "pricing.html")
    # The seats-left line reads the public endpoint the API serves.
    assert "https://api.remembra.dev/api/v1/billing/founding" in (LANDING / "pricing.html").read_text()
    assert "refund the difference" not in (ROOT / "src" / "remembra" / "api" / "v1" / "billing.py").read_text()
