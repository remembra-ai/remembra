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
    keep = _default("pre_migration_backup_keep")
    assert _default("pre_migration_backup") is True
    assert f"Each copy is deleted once {keep} newer ones exist" in DELETION
    assert f"until {keep} more deploys have happened" in DELETION
    # P-075: production runs no litestream replica, so no page promises a continuous backup.
    assert "There is no continuous backup yet." in DELETION
    for page in ("terms.html", "privacy.html", "security.html"):
        assert "continuous backup keeps" not in _text(LANDING / page)
    # The operator doc keeps the litestream figure for when it is switched on (P-300): 24h of history,
    # checked hourly, so an erased copy can outlive the window by up to that hour.
    entrypoint = (ROOT / "scripts" / "cloud-entrypoint.sh").read_text()
    operations = " ".join((ROOT / "docs" / "OPERATIONS.md").read_text().split())
    assert 'RETENTION="${LITESTREAM_RETENTION:-24h}"' in entrypoint and "retention-check-interval: 1h" in entrypoint
    assert "leaves the replica within about 25 hours" in operations and "48 hours" not in operations
    # The undo line says what an undo does not bring back.
    assert "a cancelled subscription and revoked API keys do not" in DELETION
    # The old promise the code never kept is gone.
    for page in ("terms.html", "privacy.html"):
        assert "delete your data within 30 days" not in _text(LANDING / page)
    settings_page = (DASHBOARD / "pages" / "Settings.tsx").read_text()
    assert "{DELETION_COPY}" in settings_page and "permanently delete your account and all memories" not in settings_page


def test_retention_promises_hold_with_the_server_defaults() -> None:
    """P-074 / P-096: what the pages say we keep, and what the security log holds, matches the code.

    Handoffs and checkpoints stay until the user deletes them, and notes are kept while the account
    is open, because the sleep-time decay cleanup is off unless an operator turns it on (and never
    deletes relay records even then: tests/test_sleep_time_retention.py). Pickup events go with the
    handoff, its project or the account (Database.delete_memory, delete_project_memories,
    delete_user_memories). Ordinary sign-ins write no security-log row.
    """
    assert _default("sleep_time_decay_cleanup_enabled") is False
    security, privacy = _text(LANDING / "security.html"), _text(LANDING / "privacy.html")
    assert "Handoffs and structured checkpoints never expire." in security
    assert "Notes and memories are kept while your account is open, unless you set an expiry on them." in security
    assert "Handoffs and structured checkpoints do not expire" in privacy
    assert "They are deleted when the handoff, its project or your account is deleted." in privacy
    assert "Our security log records sign-ins" not in privacy
    assert "Ordinary password and Google or GitHub sign-ins are not recorded there." in privacy


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
