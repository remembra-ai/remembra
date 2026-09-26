#!/usr/bin/env python3
"""Accounts whose recorded Paddle customer has another email than the account (read-only).

Since BILL-1/BILL-6 the dashboard's "Manage subscription" opens a Paddle
customer only when that customer's email is the account's verified sign-in
email. An owner who paid under another email therefore gets "No billing
account found" and must use the link in the Paddle receipt. Run this once
before shipping the fix, then handle each ``mismatch`` row by hand (support,
or update the Paddle customer's email after checking who pays).

    python scripts/maintenance/paddle_customer_email_report.py --db /data/remembra.db

Reads cloud_tenants and users (SQLite, opened read-only) and asks the Paddle
API (PADDLE_API_KEY, PADDLE_SANDBOX from the server's environment) for each
recorded customer. Nothing is written anywhere. Emails are masked unless
--show-emails is given. Output: one JSON object per account, then a summary
on stderr.

Status per account
  match            the customer's email is the account's email (the portal opens)
  mismatch         another email: the owner cannot open the portal from the dashboard
  login_unverified same email, but the account never verified it (portal asks to verify)
  no_login         the account row is gone or deactivated (erased / deleted account)
  customer_missing Paddle has no such customer
  paddle_error     Paddle could not be asked for this customer (re-run later)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from typing import Any

import httpx


def mask_email(email: str | None) -> str | None:
    """``owner@example.com`` -> ``o***@example.com`` (the domain is kept so a company address stays recognisable)."""
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and str(a).strip().casefold() == str(b).strip().casefold()


def load_accounts(db_path: str) -> list[dict[str, Any]]:
    """Every tenant with a recorded Paddle customer id, with its login email (read-only connection)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT t.user_id, t.plan, t.stripe_customer_id AS customer_id,
                   u.email AS login_email, u.email_verified, u.is_active, u.id AS login_id
            FROM cloud_tenants t LEFT JOIN users u ON u.id = t.user_id
            WHERE t.stripe_customer_id IS NOT NULL AND t.stripe_customer_id != ''
            ORDER BY t.user_id
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


async def build_report(accounts: list[dict[str, Any]], billing: Any, *, show_emails: bool = False) -> list[dict[str, Any]]:
    """Compare each account's email with its Paddle customer's email (``billing`` is a PaddleBillingManager)."""
    report: list[dict[str, Any]] = []
    for account in accounts:
        customer_id = str(account["customer_id"])
        row: dict[str, Any] = {"user_id": account["user_id"], "plan": account["plan"], "customer_id": customer_id}
        paddle_email: str | None = None
        try:
            paddle_email = (await billing.get_customer(customer_id)).get("email")
        except httpx.HTTPStatusError as e:
            row["status"] = "customer_missing" if e.response.status_code == 404 else "paddle_error"
        except httpx.HTTPError:
            row["status"] = "paddle_error"
        login_email = account.get("login_email")
        if "status" not in row:
            if account.get("login_id") is None or not account.get("is_active"):
                row["status"] = "no_login"
            elif not _same(paddle_email, login_email):
                row["status"] = "mismatch"
            elif not account.get("email_verified"):
                row["status"] = "login_unverified"
            else:
                row["status"] = "match"
        shown = (lambda e: e) if show_emails else mask_email
        row["login_email"] = shown(login_email)
        row["paddle_email"] = shown(paddle_email)
        report.append(row)
    return report


async def _main(args: argparse.Namespace) -> int:
    from remembra.cloud.billing_paddle import PaddleBillingManager
    from remembra.cloud.paddle_config import get_paddle_settings

    paddle = get_paddle_settings()
    billing = PaddleBillingManager(api_key=paddle.api_key, webhook_secret=paddle.webhook_secret or "", sandbox=paddle.sandbox)
    report = await build_report(load_accounts(args.db), billing, show_emails=args.show_emails)
    counts: dict[str, int] = {}
    for row in report:
        print(json.dumps(row, sort_keys=True))
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(json.dumps({"accounts": len(report), "by_status": counts}, sort_keys=True), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="path to the Remembra SQLite database")
    parser.add_argument("--show-emails", action="store_true", help="print full email addresses instead of masked ones")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
