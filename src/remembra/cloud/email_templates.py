"""Transactional email content for Remembra Relay: one renderer, HTML and plain text.

Every email is built from the same few blocks (paragraphs, a button, a list of
facts, a code block, small print) and rendered twice: an HTML part with inline
styles for mail clients, and a plain-text part with every link written out.
All interpolated values are HTML-escaped. Plan names, prices and limits come
from :mod:`remembra.cloud.plans`, never from copy in this file, and every
dashboard link is built from :func:`dashboard_url` with a route the dashboard
has (``/``, ``/verify-email``, ``/reset-password``, ``/invite/<token>`` or a
``/#/<tab>`` page).

No email ever contains an API key or any part of one.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from remembra.cloud.plans import (
    FOUNDING_ANNUAL_PRICE_CENTS,
    BillingInterval,
    PlanLimits,
    PlanTier,
    get_plan,
)

DEFAULT_DASHBOARD_URL = "https://app.remembra.dev"
RELAY_GUIDE_URL = "https://docs.remembra.dev/guides/relay/"
SUPPORT_EMAIL = "support@remembra.dev"

# The same three lines as the landing page's install block (landing/index.html).
INSTALL_LINES: tuple[str, ...] = (
    "pipx install --force 'remembra[mcp]>=0.16'",
    "remembra-install --all",
    "remembra-relay connect",
)

# Dashboard tabs linked from email (dashboard/src/lib/nav.ts TabType).
TAB_HOME = "home"
TAB_KEYS = "keys"
TAB_BILLING = "billing"
TAB_SETTINGS = "settings"

# Upsell path for the memory cap and usage emails: the next plan up.
_NEXT_TIER: dict[PlanTier, PlanTier] = {
    PlanTier.FREE: PlanTier.SOLO,
    PlanTier.SOLO: PlanTier.PRO,
    PlanTier.PRO: PlanTier.TEAM,
}

_INK = "#15171a"
_INK_2 = "#44484a"
_INK_3 = "#62676a"
_PAPER = "#e9e8e1"
_CARD = "#f7f6f1"
_RULE = "#d6d4ca"
_SIGNAL = "#ff5b14"
_SIGNAL_INK = "#b33900"
_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
_MONO = "'JetBrains Mono', SFMono-Regular, Menlo, Consolas, monospace"
_CARD_STYLE = f"max-width:560px;border-collapse:collapse;background:{_CARD};border:1px solid {_RULE};border-radius:10px;"
_BRAND_STYLE = f"margin:0;font-size:15px;font-weight:800;letter-spacing:0.02em;color:{_INK};"


@dataclass(frozen=True)
class RenderedEmail:
    """A ready-to-send email: subject, HTML part and plain-text part."""

    template: str
    subject: str
    html: str
    text: str


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class P:
    """A paragraph of plain text."""

    text: str


@dataclass(frozen=True)
class Button:
    label: str
    url: str


@dataclass(frozen=True)
class Link:
    """A secondary link on its own line."""

    label: str
    url: str


@dataclass(frozen=True)
class Code:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Small:
    """Small print."""

    text: str


Block = P | Button | Link | Code | Facts | Small


def _safe_url(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"email links must be absolute http(s) URLs: {url!r}")
    return url


def _html_block(block: Block) -> str:
    e = html.escape
    if isinstance(block, P):
        return f'<p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:{_INK};">{e(block.text)}</p>'
    if isinstance(block, Small):
        return f'<p style="margin:0 0 12px;font-size:13px;line-height:1.55;color:{_INK_3};">{e(block.text)}</p>'
    if isinstance(block, Button):
        url = e(_safe_url(block.url), quote=True)
        return (
            '<p style="margin:24px 0;">'
            f'<a href="{url}" style="display:inline-block;background:{_SIGNAL};color:{_INK};text-decoration:none;'
            f'font-weight:700;font-size:15px;padding:12px 22px;border-radius:6px;">{e(block.label)}</a></p>'
        )
    if isinstance(block, Link):
        url = e(_safe_url(block.url), quote=True)
        return f'<p style="margin:0 0 12px;font-size:15px;"><a href="{url}" style="color:{_SIGNAL_INK};">{e(block.label)}</a></p>'
    if isinstance(block, Code):
        body = "\n".join(e(line) for line in block.lines)
        return (
            f'<pre style="margin:0 0 16px;padding:14px 16px;background:{_INK};color:{_PAPER};border-radius:6px;'
            f'font-family:{_MONO};font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;">{body}</pre>'
        )
    rows = "".join(
        f'<tr><td style="padding:6px 16px 6px 0;font-size:14px;color:{_INK_2};vertical-align:top;white-space:nowrap;">{e(k)}</td>'
        f'<td style="padding:6px 0;font-size:14px;color:{_INK};vertical-align:top;">{e(v)}</td></tr>'
        for k, v in block.rows
    )
    return f'<table role="presentation" style="border-collapse:collapse;margin:0 0 16px;">{rows}</table>'


def _text_block(block: Block) -> str:
    if isinstance(block, (P, Small)):
        return block.text
    if isinstance(block, Button):
        return f"{block.label}:\n{_safe_url(block.url)}"
    if isinstance(block, Link):
        return f"{block.label}: {_safe_url(block.url)}"
    if isinstance(block, Code):
        return "\n".join(f"    {line}" for line in block.lines)
    width = max(len(k) for k, _ in block.rows)
    return "\n".join(f"{k + ':':<{width + 1}} {v}" for k, v in block.rows)


def render(
    template: str,
    *,
    subject: str,
    heading: str,
    blocks: Sequence[Block],
    dashboard: str,
    reason: str = "You are receiving this because it is about your Remembra account.",
) -> RenderedEmail:
    """Lay out one email (HTML and plain text) with the shared header and footer."""
    settings_url = dashboard_url(dashboard, tab=TAB_SETTINGS)
    footer = [
        f"Questions? Reply to this email or write to {SUPPORT_EMAIL}.",
        reason,
    ]
    e = html.escape
    body = "".join(_html_block(b) for b in blocks)
    footer_html = "".join(
        f'<p style="margin:0 0 8px;font-size:12px;line-height:1.5;color:{_INK_3};">{e(line)}</p>' for line in footer
    )
    html_part = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{e(subject)}</title>
</head>
<body style="margin:0;padding:0;background:{_PAPER};font-family:{_FONT};">
<table role="presentation" width="100%" style="border-collapse:collapse;background:{_PAPER};">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" style="{_CARD_STYLE}">
<tr><td style="padding:24px 32px 0;">
<p style="{_BRAND_STYLE}">Remembra <span style="color:{_SIGNAL_INK};">Relay</span></p>
</td></tr>
<tr><td style="padding:20px 32px 8px;">
<h1 style="margin:0 0 20px;font-size:22px;line-height:1.3;color:{_INK};">{e(heading)}</h1>
{body}
</td></tr>
<tr><td style="padding:16px 32px 24px;border-top:1px solid {_RULE};">
{footer_html}
<p style="margin:0;font-size:12px;"><a href="{e(settings_url, quote=True)}" style="color:{_INK_3};">Account settings</a></p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
    text_part = "\n\n".join(
        [
            heading,
            *(_text_block(b) for b in blocks),
            "--",
            *footer,
            f"Account settings: {settings_url}",
        ]
    )
    return RenderedEmail(template=template, subject=subject, html=html_part, text=text_part + "\n")


# ---------------------------------------------------------------------------
# Links and plan facts
# ---------------------------------------------------------------------------


def dashboard_url(base: str | None, path: str = "/", *, tab: str | None = None, query: str = "") -> str:
    """An absolute dashboard link: a path route (``/verify-email``) or a tab (``/#/billing``)."""
    origin = (base or DEFAULT_DASHBOARD_URL).rstrip("/")
    if tab:
        return f"{origin}/#/{tab}"
    return f"{origin}{path}{('?' + query) if query else ''}"


def money(cents: int) -> str:
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def plan_name(tier: PlanTier | str) -> str:
    return get_plan(tier).display_name


def price_line(
    tier: PlanTier | str,
    interval: BillingInterval | str | None = None,
    *,
    seats: int | None = None,
    founding: bool = False,
    founding_held: bool = True,
) -> str:
    """What the plan costs, from the catalog (per seat for Team).

    ``founding`` means the subscription is billed at the Founding 100 price
    (Solo, annual only); it is ignored for any other plan or interval.
    ``founding_held`` is whether the account holds one of the 100 Founding
    seats: a Founding charge that arrived after the offer was full is quoted
    at what was charged, without the lifetime lock.
    """
    limits = get_plan(tier)
    yearly = BillingInterval.parse(str(interval)) == BillingInterval.YEAR if interval else False
    if limits.tier == PlanTier.FREE:
        return "Free"
    if founding and limits.tier == PlanTier.SOLO and yearly:
        price = f"{money(FOUNDING_ANNUAL_PRICE_CENTS)}/year"
        if founding_held:
            return f"{price} (Founding 100, price locked for life)"
        return f"{price}, the Founding 100 price (the offer was full when this payment arrived; we will contact you about it)"
    cents = limits.price_annual_cents if yearly and limits.price_annual_cents is not None else limits.price_monthly_cents
    if cents is None:
        return "Custom contract"
    period = "year" if yearly and limits.price_annual_cents is not None else "month"
    if limits.per_seat:
        count = max(seats or limits.min_seats, 1)
        return f"{money(cents)} per seat/{period} x {count} seats = {money(cents * count)}/{period}"
    line = f"{money(cents)}/{period}"
    if limits.is_legacy:
        line += " (your original price, kept while this subscription stays active)"
    return line


def plan_offer(tier: PlanTier) -> str:
    """One line about a plan from the catalog, e.g. for an upgrade suggestion."""
    limits = get_plan(tier)
    parts = [f"{limits.max_memories:,} memories", f"{limits.max_smart_credits_per_month:,} smart credits a month"]
    if limits.per_seat:
        parts = [p + " per seat" for p in parts]
    prices = [f"{money(limits.price_monthly_cents or 0)}/month"]
    if limits.price_annual_cents:
        prices.append(f"{money(limits.price_annual_cents)}/year")
    per_seat = f" per seat, {limits.min_seats} seats minimum" if limits.per_seat else ""
    return f"{limits.display_name}: {', '.join(parts)}, {' or '.join(prices)}{per_seat}."


def _next_tier(tier: PlanTier) -> PlanTier | None:
    return _NEXT_TIER.get(tier)


def _plan_facts(limits: PlanLimits, memory_cap: int, credit_line: str) -> list[tuple[str, str]]:
    projects = "Unlimited" if limits.max_projects >= 1_000 else f"{limits.max_projects:,}"
    return [
        ("Relay handoffs", "Free on every plan"),
        ("Smart credits", credit_line),
        ("Memory cap", f"{memory_cap:,} memories"),
        ("Projects", projects),
    ]


def _credit_line(limits: PlanLimits, interval: BillingInterval | None) -> str:
    monthly = f"{limits.max_smart_credits_per_month:,} a month"
    if interval == BillingInterval.YEAR and limits.tier != PlanTier.FREE:
        return f"{monthly}, released as a yearly bank"
    return monthly


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def welcome(*, dashboard: str | None, verify_url: str | None = None, dashboard_account: bool = True) -> RenderedEmail:
    """After signup. No API key: keys are created (and shown once) in the dashboard."""
    free = get_plan(PlanTier.FREE)
    blocks: list[Block] = [
        P(
            "Your Remembra account is ready. Remembra Relay keeps one trail of handoffs across your coding agents: "
            "when one stops, the next one starts from where it left off."
        ),
    ]
    if verify_url:
        blocks += [
            P("First, confirm this email address:"),
            Button("Verify email address", verify_url),
            Small("The link works once and expires in 24 hours."),
        ]
    if dashboard_account:
        blocks += [
            P("Then create an API key in the dashboard. It is shown once there and never sent by email."),
            # One primary button per email: the verify link when there is one.
            (Link if verify_url else Button)("Open the dashboard", dashboard_url(dashboard, tab=TAB_HOME)),
            Link("Create an API key", dashboard_url(dashboard, tab=TAB_KEYS)),
        ]
    else:
        blocks.append(P("Your API key was shown once, where you signed up. It is never sent by email."))
    blocks += [
        P("On each machine, run these three lines:"),
        Code(INSTALL_LINES),
        P(
            "remembra-install asks for your key without showing it, saves it and adds Remembra to the agents it finds. "
            "remembra-relay connect shows "
            "what it will change; add --apply to wire it in."
        ),
        Link("Relay setup guide", RELAY_GUIDE_URL),
        Small(
            f"You are on {free.display_name}: relay handoffs are free, with "
            f"{free.max_smart_credits_per_month:,} smart credits a month for enrichment. When credits run out, "
            "stores still save, without enrichment."
        ),
    ]
    return render(
        "welcome",
        subject="Welcome to Remembra Relay",
        heading="Your agents can hand off to each other now",
        blocks=blocks,
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def email_verification(*, dashboard: str | None, verify_url: str) -> RenderedEmail:
    return render(
        "email_verification",
        subject="Remembra: confirm your email address",
        heading="Confirm your email address",
        blocks=[
            P("Confirm this address for your Remembra account."),
            Button("Verify email address", verify_url),
            Small("The link works once and expires in 24 hours."),
            Small("If you did not create a Remembra account, ignore this email and nothing happens."),
        ],
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def password_reset(*, dashboard: str | None, reset_url: str, expires_hours: int, email_verified: bool) -> RenderedEmail:
    blocks: list[Block] = [
        P("Someone asked to reset the password of your Remembra account. If it was you, choose a new password:"),
        Button("Choose a new password", reset_url),
        Small(f"The link works once and expires in {expires_hours} hours. Resetting signs out every session."),
    ]
    if not email_verified:
        blocks.append(
            Small(
                "This address was never verified, so a reset also treats you as the account's new owner: it revokes "
                "the API keys, turns off two-factor sign-in and removes the app connections and sign-in links made "
                "before, and pauses the webhooks until you turn them back on."
            )
        )
    blocks.append(Small("If you did not ask for this, ignore this email. Your password stays the same."))
    return render(
        "password_reset",
        subject="Remembra: reset your password",
        heading="Reset your password",
        blocks=blocks,
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def key_created(
    *,
    dashboard: str | None,
    key_name: str | None,
    role: str,
    project_ids: Sequence[str] = (),
    agent_id: str | None = None,
    created_at: datetime,
) -> RenderedEmail:
    rows = [
        ("Name", key_name or "(unnamed)"),
        ("Access", role),
        ("Projects", ", ".join(project_ids) if project_ids else "All projects"),
        ("Agent", agent_id or "Any agent"),
        ("Created", created_at.strftime("%Y-%m-%d %H:%M UTC")),
    ]
    return render(
        "key_created",
        subject="Remembra: a new API key was created",
        heading="A new API key was created",
        blocks=[
            P("An API key was just created on your Remembra account. The key itself is never sent by email."),
            Facts(tuple(rows)),
            Button("Review your API keys", dashboard_url(dashboard, tab=TAB_KEYS)),
            Small("If you did not create it, revoke it on that page and change your password."),
        ],
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def plan_changed(
    *,
    dashboard: str | None,
    old_tier: PlanTier | None,
    new_tier: PlanTier,
    interval: BillingInterval | None,
    seats: int | None,
    founding: bool,
    memory_cap: int,
    founding_held: bool = True,
) -> RenderedEmail:
    limits = get_plan(new_tier).scaled(seats)
    name = get_plan(new_tier).display_name
    if old_tier is not None and old_tier != new_tier:
        intro = f"Your plan changed from {plan_name(old_tier)} to {name}."
    else:
        intro = f"Your {name} plan was updated."
    price = price_line(new_tier, interval, seats=seats, founding=founding, founding_held=founding_held)
    facts = [("Plan", name), ("Price", price)]
    if get_plan(new_tier).per_seat:
        facts.append(("Seats", str(limits.max_users)))
    facts += _plan_facts(limits, memory_cap, _credit_line(limits, interval))
    blocks: list[Block] = [P(intro), Facts(tuple(facts))]
    if new_tier == PlanTier.FREE:
        blocks.append(P(_free_note(memory_cap)))
    blocks += [
        Button("View billing", dashboard_url(dashboard, tab=TAB_BILLING)),
        Small("Paddle, our reseller, sends the receipt and invoices for every payment."),
    ]
    return render(
        "plan_changed",
        subject=f"Remembra: you're on {name} now",
        heading=f"You're on {name} now",
        blocks=blocks,
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def payment_failed(
    *,
    dashboard: str | None,
    tier: PlanTier,
    interval: BillingInterval | None,
    seats: int | None,
    founding: bool | None,
    founding_held: bool = True,
) -> RenderedEmail:
    """``founding`` None: not known whether the Founding or the regular price is due, so no price is quoted."""
    name = plan_name(tier)
    price = (
        ""
        if founding is None
        else f" ({price_line(tier, interval, seats=seats, founding=founding, founding_held=founding_held)})"
    )
    return render(
        "payment_failed",
        subject=f"Remembra: the payment for your {name} plan failed",
        heading="Your payment didn't go through",
        blocks=[
            P(
                f"We couldn't collect the payment for your {name} plan{price}. Your plan keeps working for now, "
                "and Paddle, our reseller, will try the payment again."
            ),
            P("Please check or update your payment method:"),
            Button("Update payment method", dashboard_url(dashboard, tab=TAB_BILLING)),
            Small(
                "If the payment can't be collected, the subscription ends and the account moves to "
                f"{plan_name(PlanTier.FREE)}. Nothing is deleted."
            ),
        ],
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def subscription_cancelled(*, dashboard: str | None, old_tier: PlanTier, memory_cap: int) -> RenderedEmail:
    old = plan_name(old_tier)
    free = get_plan(PlanTier.FREE)
    return render(
        "subscription_cancelled",
        subject=f"Remembra: your {old} subscription has ended",
        heading=f"Your {old} subscription has ended",
        blocks=[
            P(f"Your account is on {free.display_name} now. Nothing was deleted: your memories, trail and keys stay."),
            Facts(tuple(_plan_facts(free, memory_cap, _credit_line(free, None)))),
            P(_free_note(memory_cap)),
            Button("Choose a plan", dashboard_url(dashboard, tab=TAB_BILLING)),
        ],
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def _free_note(memory_cap: int) -> str:
    return (
        f"If the account holds more than {memory_cap:,} memories, new stores are refused until it is under the cap "
        "again or on a bigger plan. Existing memories stay readable."
    )


def _upgrade_blocks(dashboard: str | None, tier: PlanTier) -> list[Block]:
    nxt = _next_tier(tier)
    if nxt is None:
        return [P("Reply to this email and we'll look at a bigger allowance with you.")]
    return [P(f"More room: {plan_offer(nxt)}"), Button("Compare plans", dashboard_url(dashboard, tab=TAB_BILLING))]


def usage_warning(*, dashboard: str | None, tier: PlanTier, usage_percent: int, current_usage: int, limit: int) -> RenderedEmail:
    name = plan_name(tier)
    blocks: list[Block] = [
        P(f"Your account holds {current_usage:,} of the {limit:,} memories {name} includes ({usage_percent}%)."),
        P(
            "At the cap, new memories are refused until you delete some or move to a bigger plan. Running out of "
            "smart credits never blocks a store: stores then save without enrichment."
        ),
        *_upgrade_blocks(dashboard, tier),
    ]
    return render(
        "usage_warning",
        subject=f"Remembra: {usage_percent}% of your memory cap used",
        heading=f"{usage_percent}% of your memory cap is used",
        blocks=blocks,
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def limit_exceeded(*, dashboard: str | None, tier: PlanTier, current_usage: int, limit: int) -> RenderedEmail:
    name = plan_name(tier)
    blocks: list[Block] = [
        P(
            f"Your account holds {current_usage:,} memories, the most {name} includes ({limit:,}). New memories are "
            "refused until you delete some or move to a bigger plan. Everything already stored stays readable."
        ),
        Link("Manage memories", dashboard_url(dashboard, tab="memories")),
        *_upgrade_blocks(dashboard, tier),
    ]
    return render(
        "limit_exceeded",
        subject="Remembra: memory cap reached",
        heading="Your memory cap is reached",
        blocks=blocks,
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def team_invite(
    *, dashboard: str | None, team_name: str, inviter_email: str, role: str, invite_url: str, expires_at: str
) -> RenderedEmail:
    return render(
        "team_invite",
        subject=f"You're invited to {team_name} on Remembra",
        heading=f"Join {team_name} on Remembra",
        blocks=[
            P(f"{inviter_email} invited you to the team {team_name} as {role}."),
            Button("Accept the invite", invite_url),
            Small(f"The invite expires {expires_at}. If you don't know this team, ignore this email."),
        ],
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
        reason="You are receiving this because a Remembra user invited this address.",
    )


def identity_linked(*, dashboard: str | None, provider_name: str, provider_email: str) -> RenderedEmail:
    return render(
        "identity_linked",
        subject=f"Remembra: {provider_name} sign-in added to your account",
        heading=f"{provider_name} sign-in was added",
        blocks=[
            P(f"{provider_name} sign-in ({provider_email}) was just added to your Remembra account."),
            Button("Review sign-in methods", dashboard_url(dashboard, tab=TAB_SETTINGS)),
            Small("If this was not you, sign in, remove it under Settings, and change your password."),
        ],
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


PRIVACY_RETENTION_URL = "https://remembra.dev/privacy#retention"


def account_deleted(*, dashboard: str | None, deleted_on: str, erase_after: str, subscriptions_cancelled: int) -> RenderedEmail:
    """Sent when an account is deleted (self-serve): what stopped, when the data goes, and how to undo."""
    blocks: list[Block] = [
        P(f"Your Remembra account was deleted on {deleted_on}. You are signed out everywhere and its API keys no longer work."),
    ]
    if subscriptions_cancelled:
        blocks.append(P("Your subscription is cancelled and will not charge again."))
    blocks += [
        P(
            f"After {erase_after} everything the account holds is erased for good: memories, handoffs, inbox, "
            "API keys, connections and settings. Until then this address cannot sign up again."
        ),
        P(
            "Changed your mind? Before that date, reply to this email or write to support@remembra.dev. Sign-in "
            "comes back; a cancelled subscription and revoked API keys do not."
        ),
        Button("What we erase and what we keep", PRIVACY_RETENTION_URL),
        Small("If you did not delete your account, reply to this email right away."),
    ]
    return render(
        "account_deleted",
        subject="Remembra: your account is deleted",
        heading="Your account is deleted",
        blocks=blocks,
        dashboard=dashboard or DEFAULT_DASHBOARD_URL,
    )


def sample_renders(dashboard: str = DEFAULT_DASHBOARD_URL) -> dict[str, RenderedEmail]:
    """Every template rendered with sample data (previews and tests)."""
    now = datetime(2026, 9, 26, 14, 30)
    samples: dict[str, Any] = {
        "welcome": welcome(dashboard=dashboard, verify_url=dashboard_url(dashboard, "/verify-email", query="token=t")),
        "welcome_api_account": welcome(dashboard=dashboard, dashboard_account=False),
        "email_verification": email_verification(
            dashboard=dashboard, verify_url=dashboard_url(dashboard, "/verify-email", query="token=t")
        ),
        "password_reset": password_reset(
            dashboard=dashboard,
            reset_url=dashboard_url(dashboard, "/reset-password", query="token=t&email=a%40example.com"),
            expires_hours=24,
            email_verified=False,
        ),
        "key_created": key_created(
            dashboard=dashboard, key_name="laptop", role="editor", project_ids=["widget"], agent_id="codex", created_at=now
        ),
        "plan_changed": plan_changed(
            dashboard=dashboard,
            old_tier=PlanTier.FREE,
            new_tier=PlanTier.SOLO,
            interval=BillingInterval.YEAR,
            seats=None,
            founding=True,
            memory_cap=get_plan(PlanTier.SOLO).max_memories,
        ),
        "payment_failed": payment_failed(
            dashboard=dashboard, tier=PlanTier.TEAM, interval=BillingInterval.MONTH, seats=4, founding=False
        ),
        "subscription_cancelled": subscription_cancelled(dashboard=dashboard, old_tier=PlanTier.PRO, memory_cap=25_000),
        "usage_warning": usage_warning(
            dashboard=dashboard, tier=PlanTier.FREE, usage_percent=85, current_usage=21_250, limit=25_000
        ),
        "limit_exceeded": limit_exceeded(dashboard=dashboard, tier=PlanTier.FREE, current_usage=25_000, limit=25_000),
        "team_invite": team_invite(
            dashboard=dashboard,
            team_name="Acme",
            inviter_email="owner@example.com",
            role="member",
            invite_url=dashboard_url(dashboard, "/invite/tok123"),
            expires_at="2026-10-03 14:30 UTC",
        ),
        "identity_linked": identity_linked(dashboard=dashboard, provider_name="GitHub", provider_email="me@example.com"),
        "account_deleted": account_deleted(
            dashboard=dashboard, deleted_on="2026-09-26", erase_after="2026-10-03", subscriptions_cancelled=1
        ),
    }
    return samples
