"""R-29: the transactional emails of Remembra Relay.

Two layers:

* Rendering: every template, rendered with sample data, has an HTML and a
  plain-text part, carries no API key, no retired price ($49 / $199 outside a
  legacy subscriber's own email), no ``pip install remembra`` SDK snippet and
  no link to a dashboard route that does not exist (checked against
  dashboard/src/App.tsx and dashboard/src/lib/nav.ts). Prices come from the
  plan catalog.
* Delivery: the real routes send them. A recording backend stands in for
  Resend at the ``EmailBackend`` edge, so the real ``EmailService``,
  ``EmailMessage`` (From / Reply-To) and templates run: signup, password
  reset, key creation and the signed Paddle webhook (plan change, renewal,
  past due with a redelivery, cancel) each produce exactly the email they
  should, or none.
"""

from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from remembra.api.v1 import auth, keys
from remembra.cloud import email as email_module
from remembra.cloud import email_templates as tpl
from remembra.cloud.email import EmailBackend, EmailMessage, EmailResult, EmailService, ResendBackend
from remembra.cloud.plans import FOUNDING_ANNUAL_PRICE_CENTS, BillingInterval, PlanTier, get_plan
from tests._cost_harness import cost_app
from tests.security_harness import make_settings, secure_app
from tests.test_paddle_subscription_ownership import _bound, _hook, _purchase
from tests.test_plans_billing_signup import PRICES, _paddle

ROOT = Path(__file__).resolve().parents[1]
DASH = "https://app.remembra.dev"
RESEND_TEST_KEY = "re_test_placeholder_not_a_key"


# ---------------------------------------------------------------------------
# Dashboard routes, read from the dashboard source
# ---------------------------------------------------------------------------


def _dashboard_routes() -> tuple[set[str], tuple[str, ...], set[str]]:
    app = (ROOT / "dashboard" / "src" / "App.tsx").read_text()
    nav = (ROOT / "dashboard" / "src" / "lib" / "nav.ts").read_text()
    exact = set(re.findall(r"path === '(/[a-z/-]*)'", app)) | {"/"}
    prefixes = tuple(re.findall(r"path\.startsWith\('(/[a-z-]+/)'\)", app))
    tab_block = nav.split("export type TabType =", 1)[1].split(";", 1)[0]
    tabs = set(re.findall(r"'([a-z]+)'", tab_block))
    return exact, prefixes, tabs


EXACT_ROUTES, PREFIX_ROUTES, TABS = _dashboard_routes()


def test_route_table_was_read() -> None:
    assert {"/signup", "/forgot-password", "/reset-password", "/verify-email", "/oauth/callback"} <= EXACT_ROUTES
    assert "/invite/" in PREFIX_ROUTES
    assert {"home", "keys", "billing", "settings", "memories"} <= TABS


def _check_dashboard_link(url: str) -> None:
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" != DASH:
        return
    if parts.fragment:
        assert parts.path in ("", "/"), url
        tab = parts.fragment.lstrip("/").split("?", 1)[0]
        assert tab in TABS, f"unknown dashboard tab in {url}"
        return
    path = parts.path or "/"
    assert path in EXACT_ROUTES or path.startswith(PREFIX_ROUTES), f"unknown dashboard route in {url}"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs += [v for k, v in attrs if k == "href" and v]


def _hrefs(html: str) -> list[str]:
    parser = _Links()
    parser.feed(html)
    return parser.hrefs


def _text_links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s)]+", text)


RETIRED = re.compile(r"\$49\b|\$199\b|pip install remembra\b|\{api_key\}|/unsubscribe")
KEY_SHAPED = re.compile(r"\brem_[A-Za-z0-9_-]{8,}")


def _assert_clean(email: tpl.RenderedEmail, *, legacy_ok: bool = False) -> None:
    for part in (email.subject, email.html, email.text):
        found = RETIRED.findall(part)
        if legacy_ok:
            found = [f for f in found if f not in ("$49", "$199")]
        assert not found, (email.template, found)
        assert not KEY_SHAPED.search(part), email.template
    assert email.text.strip() and email.html.lstrip().startswith("<!DOCTYPE html>")
    hrefs = _hrefs(email.html)
    assert hrefs, email.template
    for href in hrefs:
        assert href.startswith("https://"), (email.template, href)
        _check_dashboard_link(href)
        assert href in email.text, f"{email.template}: {href} missing from the plain-text part"
    for link in _text_links(email.text):
        _check_dashboard_link(link)
    assert f"{DASH}/#/settings" in email.text  # the footer's Preferences link


# ---------------------------------------------------------------------------
# Rendering: one test per template
# ---------------------------------------------------------------------------


SAMPLES = tpl.sample_renders(DASH)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_template_renders_clean(name: str) -> None:
    _assert_clean(SAMPLES[name])


def test_welcome_teaches_the_relay_install_not_the_sdk_and_holds_no_key() -> None:
    email = SAMPLES["welcome"]
    landing = (ROOT / "landing" / "index.html").read_text()
    for line in tpl.INSTALL_LINES:
        assert line in email.text
        assert line.replace("<", "&lt;").replace(">", "&gt;").replace("'", "&#x27;") in email.html
        assert line.replace("<", "&lt;").replace(">", "&gt;") in landing  # the same lines as the landing page
    assert f"{DASH}/#/home" in email.text and f"{DASH}/#/keys" in email.text
    assert "never sent by email" in email.text
    api = SAMPLES["welcome_api_account"]
    assert "#/keys" not in api.text and "shown once, where you signed up" in api.text


def test_email_verification_and_reset_links() -> None:
    assert f"{DASH}/verify-email?token=t" in SAMPLES["email_verification"].text
    reset = SAMPLES["password_reset"]
    assert f"{DASH}/reset-password?token=t&email=a%40example.com" in reset.text
    assert "24 hours" in reset.text and "never verified" in reset.text
    verified = tpl.password_reset(
        dashboard=DASH, reset_url=f"{DASH}/reset-password?token=x", expires_hours=24, email_verified=True
    )
    assert "never verified" not in verified.text


def test_key_created_names_the_key_but_never_contains_it() -> None:
    email = SAMPLES["key_created"]
    assert "laptop" in email.text and "codex" in email.text and "widget" in email.text
    assert f"{DASH}/#/keys" in email.text


@pytest.mark.parametrize(
    ("tier", "interval", "seats", "founding", "expected"),
    [
        (PlanTier.SOLO, BillingInterval.MONTH, None, False, "$12/month"),
        (PlanTier.SOLO, BillingInterval.YEAR, None, False, "$120/year"),
        (PlanTier.SOLO, BillingInterval.YEAR, None, True, "$108/year (Founding 100"),
        (PlanTier.PRO, BillingInterval.MONTH, None, False, "$29/month"),
        (PlanTier.PRO, BillingInterval.YEAR, None, False, "$290/year"),
        (PlanTier.TEAM, BillingInterval.MONTH, 3, False, "$15 per seat/month x 3 seats = $45/month"),
        (PlanTier.TEAM, BillingInterval.YEAR, 5, False, "$150 per seat/year x 5 seats = $750/year"),
    ],
)
def test_plan_changed_prices_come_from_the_catalog(tier, interval, seats, founding, expected) -> None:
    limits = get_plan(tier)
    email = tpl.plan_changed(
        dashboard=DASH,
        old_tier=PlanTier.FREE,
        new_tier=tier,
        interval=interval,
        seats=seats,
        founding=founding,
        memory_cap=limits.scaled(seats).max_memories,
    )
    _assert_clean(email)
    assert expected in email.text
    assert f"{limits.scaled(seats).max_smart_credits_per_month:,} a month" in email.text  # pooled for Team seats
    assert email.subject == f"Remembra: you're on {limits.display_name} now"


def test_prices_follow_the_catalog_not_copy(monkeypatch) -> None:
    """Change a catalog price: the email changes with it."""
    from dataclasses import replace

    from remembra.cloud import plans

    monkeypatch.setitem(plans.PLANS, PlanTier.SOLO, replace(plans.PLANS[PlanTier.SOLO], price_monthly_cents=1_450))
    assert tpl.price_line(PlanTier.SOLO, BillingInterval.MONTH) == "$14.50/month"
    assert "$14.50/month" in tpl.plan_offer(PlanTier.SOLO)
    assert tpl.money(FOUNDING_ANNUAL_PRICE_CENTS) == "$108"


def test_legacy_subscribers_see_their_own_price_and_no_upsell_prices() -> None:
    failed = tpl.payment_failed(
        dashboard=DASH, tier=PlanTier.LEGACY_PRO, interval=BillingInterval.MONTH, seats=None, founding=False
    )
    _assert_clean(failed, legacy_ok=True)
    assert "$49/month (your original price" in failed.text
    warning = tpl.usage_warning(dashboard=DASH, tier=PlanTier.LEGACY_TEAM, usage_percent=90, current_usage=540_000, limit=600_000)
    _assert_clean(warning, legacy_ok=True)
    body = warning.text.split("--")[0]
    assert "Reply to this email" in body
    assert not re.search(r"\$(12|29|15|120|290|150)\b", body)  # no new-catalog upsell for a grandfathered plan


@pytest.mark.parametrize("tier", [PlanTier.FREE, PlanTier.SOLO, PlanTier.PRO, PlanTier.TEAM, PlanTier.ENTERPRISE])
def test_usage_and_limit_emails_use_the_credit_model_for_every_plan(tier: PlanTier) -> None:
    cap = get_plan(tier).max_memories
    warning = tpl.usage_warning(dashboard=DASH, tier=tier, usage_percent=80, current_usage=cap * 4 // 5, limit=cap)
    limit = tpl.limit_exceeded(dashboard=DASH, tier=tier, current_usage=cap, limit=cap)
    for email in (warning, limit):
        _assert_clean(email)
    assert "never blocks a store" in warning.text
    upsell = {PlanTier.FREE: PlanTier.SOLO, PlanTier.SOLO: PlanTier.PRO, PlanTier.PRO: PlanTier.TEAM}.get(tier)
    if upsell is not None:
        assert tpl.plan_offer(upsell) in warning.text and tpl.plan_offer(upsell) in limit.text


def test_values_are_html_escaped() -> None:
    email = tpl.team_invite(
        dashboard=DASH,
        team_name="<script>alert(1)</script>",
        inviter_email="a&b@example.com",
        role="member",
        invite_url=f"{DASH}/invite/tok",
        expires_at="soon",
    )
    assert "<script>" not in email.html and "&lt;script&gt;" in email.html and "a&amp;b@example.com" in email.html
    assert "<script>alert(1)</script>" in email.text  # plain text is not HTML
    with pytest.raises(ValueError):
        tpl.render("x", subject="s", heading="h", blocks=[tpl.Button("go", "javascript:alert(1)")], dashboard=DASH)


def test_self_hosted_dashboard_origin_is_used() -> None:
    email = tpl.welcome(dashboard="https://dash.example.org/", verify_url="https://dash.example.org/verify-email?token=t")
    assert "https://dash.example.org/#/keys" in email.text and DASH not in email.text


# ---------------------------------------------------------------------------
# Delivery through the real routes
# ---------------------------------------------------------------------------


class Outbox(EmailBackend):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> EmailResult:
        self.sent.append(message)
        return EmailResult(success=True, message_id=f"msg_{len(self.sent)}")

    def of(self, template: str) -> list[EmailMessage]:
        return [m for m in self.sent if (m.tags or {}).get("template") == template]

    async def wait(self, count: int, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.sent) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"expected {count} emails, got {[(m.tags or {}).get('template') for m in self.sent]}")
            await asyncio.sleep(0.01)

    async def settle(self) -> None:
        """Let background notifications run (for asserting that nothing more is sent)."""
        for _ in range(20):
            await asyncio.sleep(0.01)


@pytest.fixture()
def outbox(monkeypatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr(EmailService, "create", classmethod(lambda cls, **_: cls(backend=box)))
    return box


def _message_ok(message: EmailMessage) -> None:
    assert message.from_email == "Remembra <noreply@remembra.dev>"
    assert message.reply_to == "support@remembra.dev"  # a reply reaches the support inbox
    assert message.text and message.html
    rendered = tpl.RenderedEmail(
        template=(message.tags or {})["template"], subject=message.subject, html=message.html, text=message.text
    )
    _assert_clean(rendered)


async def test_signup_sends_one_welcome_with_no_key_and_a_working_verify_link(tmp_path, outbox) -> None:
    settings = make_settings(resend_api_key=RESEND_TEST_KEY, public_dashboard_url=DASH)
    async with secure_app(tmp_path, [auth.router], settings=settings) as h:
        r = await h.client.post("/api/v1/auth/signup", json={"email": "new@example.com", "password": "Str0ng!Passw0rd"})
        assert r.status_code == 201, r.text
        uid = r.json()["id"]
        assert [(m.tags or {})["template"] for m in outbox.sent] == ["welcome"]
        [welcome] = outbox.sent
        _message_ok(welcome)
        assert welcome.to == "new@example.com" and welcome.subject == "Welcome to Remembra Relay"
        for line in tpl.INSTALL_LINES:
            assert line in welcome.text
        # No key was minted to be emailed: the account has none until the user creates one.
        cursor = await h.db.conn.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = ?", (uid,))
        assert (await cursor.fetchone())[0] == 0

        verify = next(u for u in _text_links(welcome.text) if "/verify-email?" in u)
        token = dict(parse_qsl(urlsplit(verify).query))["token"]
        ok = await h.client.post(
            "/api/v1/auth/verify-email/confirm", json={"token": token}, headers=h.jwt(uid, "new@example.com")
        )
        assert ok.status_code == 200 and ok.json()["email_verified"] is True, ok.text


async def test_signup_without_an_email_provider_sends_nothing(tmp_path, outbox) -> None:
    async with secure_app(tmp_path, [auth.router], settings=make_settings(resend_api_key=None)) as h:
        r = await h.client.post("/api/v1/auth/signup", json={"email": "quiet@example.com", "password": "Str0ng!Passw0rd"})
        assert r.status_code == 201, r.text
    assert outbox.sent == []


async def test_forgot_password_link_resets_the_password(tmp_path, outbox) -> None:
    settings = make_settings(resend_api_key=RESEND_TEST_KEY, public_dashboard_url=DASH)
    async with secure_app(tmp_path, [auth.router], settings=settings) as h:
        await h.create_user("Reset.Me+tag@example.com", verified=True)
        r = await h.client.post("/api/v1/auth/forgot-password", json={"email": "Reset.Me+tag@example.com"})
        assert r.status_code == 200, r.text
        [reset] = outbox.of("password_reset")
        _message_ok(reset)
        assert "24 hours" in reset.text and "never verified" not in reset.text
        link = next(u for u in _text_links(reset.text) if "/reset-password?" in u)
        assert link.startswith(f"{DASH}/reset-password?")
        params = dict(parse_qsl(urlsplit(link).query))
        assert params["email"] == "reset.me+tag@example.com"  # encoded, so "+" survives
        done = await h.client.post(
            "/api/v1/auth/reset-password",
            json={"email": params["email"], "token": params["token"], "new_password": "N3w!Passw0rdX"},
        )
        assert done.status_code == 200, done.text
        login = await h.client.post("/api/v1/auth/login", json={"email": params["email"], "password": "N3w!Passw0rdX"})
        assert login.status_code == 200, login.text


async def test_creating_a_key_sends_a_notice_without_the_key(tmp_path, outbox) -> None:
    settings = make_settings(resend_api_key=RESEND_TEST_KEY, public_dashboard_url=DASH)
    async with secure_app(tmp_path, [auth.router, keys.router], settings=settings) as h:
        uid = await h.create_user("keys@example.com", verified=True)
        r = await h.client.post(
            "/api/v1/keys",
            json={"name": "ci-runner", "role": "viewer", "project_ids": ["widget"]},
            headers=h.jwt(uid, "keys@example.com"),
        )
        assert r.status_code == 201, r.text
        raw_key = r.json()["key"]
        await outbox.wait(1)
        [notice] = outbox.of("key_created")
        _message_ok(notice)
        assert notice.to == "keys@example.com"
        assert "ci-runner" in notice.text and "viewer" in notice.text and "widget" in notice.text
        assert raw_key not in notice.text and raw_key not in notice.html
        assert raw_key[:12] not in notice.text


async def test_paddle_events_send_plan_change_payment_failed_and_cancel(tmp_path, outbox) -> None:
    async with cost_app(tmp_path, resend_api_key=RESEND_TEST_KEY, public_dashboard_url=DASH) as c:
        _paddle(c, **PRICES)
        c.h.app.state.tasks = None
        uid = await c.h.create_user("buyer@example.com", verified=True)

        # First purchase: Free -> Solo monthly.
        await _hook(c, "transaction.completed", _purchase("txn_1", "sub_1", "pri_solo_m", _bound(uid), customer="ctm_1"))
        await outbox.wait(1)
        [changed] = outbox.of("plan_changed")
        _message_ok(changed)
        assert changed.to == "buyer@example.com" and "from Relay Free to Solo" in changed.text and "$12/month" in changed.text

        # A renewal of the same plan sends nothing.
        await _hook(c, "transaction.completed", _purchase("txn_2", "sub_1", "pri_solo_m", _bound(uid), customer="ctm_1"))
        await outbox.settle()
        assert len(outbox.sent) == 1

        # Upgrade to Pro through the portal.
        await _hook(
            c,
            "subscription.updated",
            {"id": "sub_1", "status": "active", "customer_id": "ctm_1", "items": [{"price": {"id": "pri_pro_m"}, "quantity": 1}]},
        )
        await outbox.wait(2)
        upgrade = outbox.of("plan_changed")[-1]
        assert "from Solo to Pro" in upgrade.text and "$29/month" in upgrade.text

        # Past due, delivered twice by Paddle: one email.
        for _ in range(2):
            await _hook(c, "subscription.past_due", {"id": "sub_1", "customer_id": "ctm_1", "status": "past_due"})
        await outbox.wait(3)
        await outbox.settle()
        [failed] = outbox.of("payment_failed")
        _message_ok(failed)
        assert "Pro plan ($29/month)" in failed.text and f"{DASH}/#/billing" in failed.text
        # A past-due event for a subscription nobody holds is not emailed.
        await _hook(c, "subscription.past_due", {"id": "sub_unknown", "customer_id": "ctm_9", "status": "past_due"})
        await outbox.settle()
        assert len(outbox.of("payment_failed")) == 1

        # The subscription ends.
        await _hook(c, "subscription.canceled", {"id": "sub_1", "customer_id": "ctm_1", "status": "canceled"})
        await outbox.wait(4)
        [ended] = outbox.of("subscription_cancelled")
        _message_ok(ended)
        assert "Your Pro subscription has ended" in ended.text and "Relay Free now" in ended.text
        assert (await c.meter.get_tenant(uid))["plan"] == "free"
        assert [m.to for m in outbox.sent] == ["buyer@example.com"] * 4


async def test_api_signup_tenant_welcome_has_no_key(tmp_path, outbox) -> None:
    from tests.security_harness import MASTER_KEY

    async with cost_app(tmp_path, resend_api_key=RESEND_TEST_KEY, public_dashboard_url=DASH) as c:
        r = await c.h.client.post(
            "/api/v1/cloud/signup",
            json={"email": "api-only@example.com", "client_ip": "198.51.100.7"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert r.status_code == 201, r.text
        raw_key = r.json()["api_key"]
        [welcome] = outbox.of("welcome")
        _message_ok(welcome)
        assert raw_key not in welcome.text and raw_key not in welcome.html
        assert "shown once, where you signed up" in welcome.text
        assert len(outbox.of("email_verification")) == 1


async def test_notify_welcome_for_social_accounts_and_missing_addresses(tmp_path, outbox) -> None:
    from remembra.cloud import notify

    settings = make_settings(resend_api_key=RESEND_TEST_KEY)
    async with secure_app(tmp_path, [auth.router], settings=settings) as h:
        uid = await h.create_user("social@example.com", verified=True)
        notify.notify_welcome(h.app.state, uid)
        notify.notify_welcome(h.app.state, "user_that_does_not_exist")
        await outbox.wait(1)
        await outbox.settle()
        assert [m.to for m in outbox.sent] == ["social@example.com"]
        assert "/verify-email" not in outbox.sent[0].text


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


async def test_resend_backend_sends_text_reply_to_and_reads_the_prefixed_key(monkeypatch) -> None:
    import resend

    import remembra.config as config_module

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(config_module, "_settings", make_settings(resend_api_key=RESEND_TEST_KEY))
    captured: dict[str, Any] = {}

    def fake_send(params: dict[str, Any]) -> dict[str, str]:
        captured.update(params)
        captured["api_key"] = resend.api_key
        return {"id": "re_msg_1"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(fake_send))
    service = email_module.email_service_or_none()  # REMEMBRA_RESEND_API_KEY alone is enough
    assert service is not None and isinstance(service.backend, ResendBackend)
    result = await service.send_rendered("to@example.com", SAMPLES["key_created"])
    assert result.success and result.message_id == "re_msg_1"
    assert captured["api_key"] == RESEND_TEST_KEY
    assert captured["to"] == "to@example.com"
    assert captured["from"] == "Remembra <noreply@remembra.dev>" and captured["reply_to"] == "support@remembra.dev"
    assert captured["text"] == SAMPLES["key_created"].text and captured["html"] == SAMPLES["key_created"].html
    assert captured["tags"] == [{"name": "template", "value": "key_created"}]


def test_no_email_service_without_a_key(monkeypatch) -> None:
    import remembra.config as config_module

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(config_module, "_settings", make_settings(resend_api_key=None))
    assert email_module.email_service_or_none() is None


def test_sender_is_configurable(monkeypatch) -> None:
    import remembra.config as config_module

    monkeypatch.setattr(
        config_module, "_settings", make_settings(email_from="Staging <noreply@staging.example>", email_reply_to="")
    )
    message = EmailMessage(to="a@example.com", subject="s", html="<p>h</p>")
    assert message.from_email == "Staging <noreply@staging.example>" and message.reply_to is None


def test_no_template_files_or_key_parameters_remain() -> None:
    assert not (ROOT / "src" / "remembra" / "cloud" / "templates").exists()
    import inspect

    for name, method in inspect.getmembers(EmailService, inspect.isfunction):
        assert "api_key" not in inspect.signature(method).parameters, name
