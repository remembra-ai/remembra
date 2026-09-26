"""Security sweep 2026-09-26, billing: who may open a Paddle portal or a checkout.

* BILL-1: an account never reaches a Paddle customer it does not own. The
  portal opens only a customer whose email is this account's VERIFIED email
  (the recorded customer id, else a lookup by that email whose subscription
  names this account); an unverified account gets 404 with no Paddle lookup.
* BILL-3: ``/billing/portal`` and ``/billing/checkout`` take a dashboard
  session only (no API key of any role or scope), and not an untrusted session
  while an account review is pending.

Every test drives the real routes (signup, login, billing) over SQLite; the
Paddle API is the in-process mock (``tests/_paddle_mock.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from remembra.auth import account_review
from remembra.cloud.plans import PlanTier
from tests import _paddle_mock
from tests._cost_harness import cost_app
from tests.test_plans_billing_signup import PRICES, _paddle

PASSWORD = "Str0ng!Passw0rd"


async def _signup_and_login(c: Any, email: str) -> tuple[str, dict[str, str]]:
    """A fresh account made through the real routes (email NOT verified); returns (user_id, session headers)."""
    r = await c.h.client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    uid = str(r.json()["id"])
    r = await c.h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------------------
# BILL-1: no portal for a Paddle customer the account does not own
# ---------------------------------------------------------------------------


async def test_unverified_signup_with_a_customers_email_gets_404_and_no_paddle_lookup(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    # A Paddle customer with no Remembra login at this address (paid with it, or deleted the account).
    paddle.customers_by_email["victim@example.com"] = "ctm_victim"
    paddle.add_subscription("sub_victim", "ctm_victim", custom_data={"remembra_user_id": "deleted-user"})
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid, session = await _signup_and_login(c, "victim@example.com")
        assert not (await c.h.db.get_user_by_id(uid))["email_verified"]

        r = await c.h.client.post("/api/v1/billing/portal", headers=session)
        assert r.status_code == 404, r.text
        assert "portal_url" not in r.json()
        # No lookup by email (no 200/404 oracle on who is a customer) and no portal session.
        assert paddle.requests("GET", "/customers") == []
        assert paddle.requests("POST", "/customers") == []


async def test_verified_account_with_a_recorded_customer_of_its_own_email_opens_its_portal(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers["ctm_own"] = "owner-of-sub@example.com"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("owner-of-sub@example.com", verified=True)
        await c.meter.apply_subscription(uid, PlanTier.SOLO, customer_id="ctm_own", subscription_id="sub_own")
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(uid, "owner-of-sub@example.com"))
        assert r.status_code == 200, r.text
        assert r.json()["portal_url"] == "https://portal.example/ctm_own"
        # The recorded id is used directly: no lookup by email.
        assert paddle.requests("GET", "/customers") == [("GET", "/customers/ctm_own", {}, None)]


async def test_a_recorded_customer_of_another_email_never_opens(tmp_path, monkeypatch) -> None:
    """The customer id came from a checkout where someone typed another person's email (BILL-6's path)."""
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers["ctm_someone_else"] = "someone-else@example.com"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("buyer@example.com", verified=True)
        await c.meter.apply_subscription(uid, PlanTier.SOLO, customer_id="ctm_someone_else", subscription_id="sub_b")
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(uid, "buyer@example.com"))
        assert r.status_code == 404, r.text
        assert "Paddle receipt" in r.json()["detail"]
        assert paddle.requests("POST", "/customers/ctm_someone_else/portal-sessions") == []


async def test_unverified_account_with_a_recorded_customer_is_asked_to_verify(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers["ctm_u"] = "unverified-payer@example.com"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("unverified-payer@example.com", verified=False)
        await c.meter.apply_subscription(uid, PlanTier.SOLO, customer_id="ctm_u", subscription_id="sub_u")
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(uid, "unverified-payer@example.com"))
        assert r.status_code == 403, r.text
        assert "Verify your email" in r.json()["detail"]
        assert paddle.calls == []
        await c.h.db.update_user_email_verified(uid, True)
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(uid, "unverified-payer@example.com"))
        assert r.status_code == 200 and r.json()["portal_url"] == "https://portal.example/ctm_u"


async def test_email_lookup_needs_a_verified_email_and_a_subscription_naming_the_account(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers_by_email["lookup@example.com"] = "ctm_lookup"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("lookup@example.com", verified=True)
        hdr = c.h.jwt(uid, "lookup@example.com")
        # A customer with this email but no subscription for this account (another purchase on the seller).
        paddle.add_subscription("sub_other", "ctm_lookup", custom_data={"remembra_user_id": "another-account"})
        r = await c.h.client.post("/api/v1/billing/portal", headers=hdr)
        assert r.status_code == 404, r.text
        assert paddle.requests("POST", "/customers/ctm_lookup/portal-sessions") == []
        # Its own (pre-customer-id) subscription: the portal opens.
        paddle.add_subscription("sub_mine", "ctm_lookup", custom_data={"remembra_user_id": uid, "plan": "pro"})
        r = await c.h.client.post("/api/v1/billing/portal", headers=hdr)
        assert r.status_code == 200 and r.json()["portal_url"] == "https://portal.example/ctm_lookup"


def test_the_by_email_portal_helper_is_gone() -> None:
    from remembra.cloud.billing_paddle import PaddleBillingManager

    assert not hasattr(PaddleBillingManager, "create_portal_session_by_email")


# ---------------------------------------------------------------------------
# BILL-3: dashboard sessions only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "projects"),
    [("viewer", None), ("editor", None), ("admin", None), ("viewer", ["p1"]), ("editor", ["p1"])],
)
async def test_api_keys_cannot_open_the_portal_or_a_checkout(tmp_path, monkeypatch, role, projects) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers["ctm_k"] = "keys@example.com"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("keys@example.com", verified=True)
        await c.meter.register_tenant(uid, PlanTier.FREE, stripe_customer_id="ctm_k")
        key, _ = await c.h.api_key(uid, role, project_ids=projects)
        for headers in ({"X-API-Key": key}, {"Authorization": f"Bearer {key}"}):
            r = await c.h.client.post("/api/v1/billing/portal", headers=headers)
            assert r.status_code == 403, r.text
            # The dashboard also offers "Use API Key instead": tell that user what to do there.
            assert r.json()["detail"].startswith("Sign in with your email (not an API key) to manage billing")
            r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=headers)
            assert r.status_code == 403, r.text
            assert "Sign in with your email" in r.json()["detail"]
        assert paddle.calls == []
        # The owner's dashboard session still gets both.
        hdr = c.h.jwt(uid, "keys@example.com")
        assert (await c.h.client.post("/api/v1/billing/portal", headers=hdr)).status_code == 200
        assert (await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=hdr)).status_code == 200


async def test_an_untrusted_session_during_an_account_review_gets_403(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers["ctm_r"] = "review@example.com"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("review@example.com", verified=True)
        await c.meter.register_tenant(uid, PlanTier.FREE, stripe_customer_id="ctm_r")
        await account_review.open_review(
            c.h.db,
            uid,
            origin=account_review.ORIGIN_GOOGLE,
            verified_at="2026-09-26T00:00:00+00:00",
            totp_enabled=False,
            proof_identity=("google", "google-sub-1"),
        )
        untrusted = c.h.jwt(uid, "review@example.com")  # e.g. the password set before the mailbox was proven
        for path, body in (("/api/v1/billing/portal", None), ("/api/v1/billing/checkout", {"plan": "solo"})):
            r = await c.h.client.post(path, json=body, headers=untrusted)
            assert r.status_code == 403, r.text
            assert "Google" in r.json()["detail"]
        assert paddle.calls == []

        # The session that proved the mailbox (carries the review claim) is let through.
        review = await account_review.pending_review(c.h.db, uid)
        assert review is not None
        claims = {account_review.REVIEW_CLAIM: review.review_id}
        token = c.h.users.create_jwt_token(uid, "review@example.com", extra_claims=claims)
        trusted = {"Authorization": f"Bearer {token}"}
        assert (await c.h.client.post("/api/v1/billing/portal", headers=trusted)).status_code == 200


# ---------------------------------------------------------------------------
# BILL-6: a purchase never records a Paddle customer that another account holds
# ---------------------------------------------------------------------------


async def test_a_purchase_carrying_another_accounts_customer_is_not_recorded(tmp_path, monkeypatch) -> None:
    from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase

    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers["ctm_victim"] = "victim@example.com"
    alerts = RecordingAlerts()
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        c.h.app.state.alerts = alerts
        c.h.app.state.tasks = None
        victim = await c.h.create_user("victim@example.com", verified=True)
        await _hook(c, "transaction.completed", _purchase("txn_v", "sub_v", "pri_pro_m", _bound(victim), customer="ctm_victim"))
        assert (await c.meter.get_tenant(victim))["stripe_customer_id"] == "ctm_victim"

        # The attacker checks out with its own binding but types the victim's email, so Paddle
        # attaches the payment to the victim's customer.
        attacker = await c.h.create_user("attacker@example.com", verified=True)
        activated = {**_purchase("", "", "pri_solo_m", _bound(attacker), customer="ctm_victim"), "id": "sub_a"}
        bought = await _hook(c, "subscription.activated", activated)
        assert bought["applied"] == "applied"  # a real payment: the plan is applied...
        tenant = await c.meter.get_tenant(attacker)
        assert (tenant["plan"], tenant["stripe_subscription_id"]) == ("solo", "sub_a")
        assert tenant["stripe_customer_id"] is None  # ...but the victim's customer is not recorded
        assert tenant["billing_flag"] == "paddle_customer_conflict"
        event, _message, details = alerts.sent[-1]
        assert event == f"paddle_customer_conflict:{attacker}:ctm_victim"
        assert details["other_accounts"] == [victim]
        # The victim's own record is untouched.
        assert (await c.meter.get_tenant(victim))["stripe_customer_id"] == "ctm_victim"

        # The attacker's portal never opens the victim's customer.
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(attacker, "attacker@example.com"))
        assert r.status_code == 404, r.text
        assert paddle.requests("POST", "/customers/ctm_victim/portal-sessions") == []

        # A renewal of the attacker's subscription keeps it that way, without a second alert.
        sent = len(alerts.sent)
        renew = _purchase("txn_a2", "sub_a", "pri_solo_m", _bound(attacker), customer="ctm_victim")
        assert (await _hook(c, "transaction.completed", renew))["applied"] == "applied"
        assert (await c.meter.get_tenant(attacker))["stripe_customer_id"] is None
        assert len(alerts.sent) == sent


async def test_an_account_keeps_its_own_customer_when_an_event_carries_anothers(tmp_path) -> None:
    from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase

    alerts = RecordingAlerts()
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        c.h.app.state.alerts = alerts
        c.h.app.state.tasks = None
        victim = await c.h.create_user("v2@example.com")
        await c.meter.register_tenant(victim, PlanTier.FREE, stripe_customer_id="ctm_v2")
        buyer = await c.h.create_user("b2@example.com")
        await c.meter.register_tenant(buyer, PlanTier.FREE, stripe_customer_id="ctm_b2")
        await _hook(c, "transaction.completed", _purchase("txn_b2", "sub_b2", "pri_solo_m", _bound(buyer), customer="ctm_v2"))
        assert (await c.meter.get_tenant(buyer))["stripe_customer_id"] == "ctm_b2"
        # An event with the account's own customer, or a customer nobody holds yet, is recorded as before.
        other = await c.h.create_user("fresh-buyer@example.com")
        await _hook(c, "transaction.completed", _purchase("txn_f", "sub_f", "pri_solo_m", _bound(other), customer="ctm_new"))
        assert (await c.meter.get_tenant(other))["stripe_customer_id"] == "ctm_new"
        assert (await c.meter.get_tenant(other))["billing_flag"] is None


def _renewal(txn: str, sub: str, price: str, customer: str) -> dict[str, Any]:
    """A later payment of a subscription, as Paddle sends it: no custom_data (the account is found by the subscription)."""
    from tests.test_paddle_subscription_ownership import _purchase

    return {**_purchase(txn, sub, price, {}, customer=customer), "custom_data": None}


def _updated(sub: str, price: str, customer: str) -> dict[str, Any]:
    return {"id": sub, "status": "active", "customer_id": customer, "items": [{"price": {"id": price}, "quantity": 1}]}


async def test_renewals_of_accounts_one_payer_pays_for_are_not_flagged(tmp_path) -> None:
    """Review regression: one Paddle customer paying for several accounts is supported, and 0.16.0 recorded the
    shared customer on each. After the upgrade every routine renewal flagged both accounts and sent the owner a
    critical alert, again after each clear. A renewal that attaches nothing new is not a conflict."""
    from tests.test_paddle_subscription_ownership import RecordingAlerts, _hook

    alerts = RecordingAlerts()
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        c.h.app.state.alerts = alerts
        c.h.app.state.tasks = None
        # The state 0.16.0 left: both accounts record the shared customer, each its own subscription.
        payer = await c.h.create_user("payer@example.com", verified=True)
        second = await c.h.create_user("second@example.com", verified=True)
        await c.meter.apply_subscription(payer, PlanTier.SOLO, customer_id="ctm_shared", subscription_id="sub_payer")
        await c.meter.apply_subscription(second, PlanTier.PRO, customer_id="ctm_shared", subscription_id="sub_second")

        for cycle in range(2):
            for sub, price in (("sub_payer", "pri_solo_m"), ("sub_second", "pri_pro_m")):
                paid = await _hook(c, "transaction.completed", _renewal(f"txn_{cycle}_{sub}", sub, price, "ctm_shared"))
                assert paid["applied"] == "applied", paid
                assert (await _hook(c, "subscription.updated", _updated(sub, price, "ctm_shared")))["applied"] in (
                    "applied",
                    "no_change",
                )
            for uid, plan in ((payer, "solo"), (second, "pro")):
                tenant = await c.meter.get_tenant(uid)
                assert (tenant["plan"], tenant["stripe_customer_id"], tenant["billing_flag"]) == (plan, "ctm_shared", None)
        assert [e for e, *_ in alerts.sent if e.startswith("paddle_customer_conflict")] == []


async def test_a_conflict_is_flagged_once_per_subscription_and_never_replaces_another_flag(tmp_path) -> None:
    """A NEW subscription paid as another account's customer is still flagged (BILL-6). Its renewals are not flagged
    again after the owner clears the flag, and a flag already waiting on the account is kept."""
    from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase

    alerts = RecordingAlerts()
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        c.h.app.state.alerts = alerts
        c.h.app.state.tasks = None
        holder = await c.h.create_user("holder@example.com", verified=True)
        await c.meter.apply_subscription(holder, PlanTier.SOLO, customer_id="ctm_holder", subscription_id="sub_holder")

        buyer = await c.h.create_user("buyer@example.com", verified=True)
        await _hook(c, "transaction.completed", _purchase("txn_b1", "sub_b", "pri_solo_m", _bound(buyer), customer="ctm_holder"))
        tenant = await c.meter.get_tenant(buyer)
        assert (tenant["billing_flag"], tenant["stripe_customer_id"]) == ("paddle_customer_conflict", None)
        conflicts = [e for e, *_ in alerts.sent if e.startswith("paddle_customer_conflict")]
        assert conflicts == [f"paddle_customer_conflict:{buyer}:ctm_holder"]

        # The owner reviews it and clears the flag (DELETE /admin/billing-flags/{id}); the next renewals stay quiet.
        await c.meter.set_billing_flag(buyer, None)
        for n in range(2):
            renewed = await _hook(c, "transaction.completed", _renewal(f"txn_b_r{n}", "sub_b", "pri_solo_m", "ctm_holder"))
            assert renewed["applied"] == "applied"
        tenant = await c.meter.get_tenant(buyer)
        assert (tenant["plan"], tenant["billing_flag"], tenant["stripe_customer_id"]) == ("solo", None, None)
        assert len([e for e, *_ in alerts.sent if e.startswith("paddle_customer_conflict")]) == 1

        # Another account with a flag waiting for review buys as the holder's customer: its flag is kept, the
        # owner is still told, and the id is not recorded.
        flagged = await c.h.create_user("flagged@example.com", verified=True)
        await c.meter.register_tenant(flagged, PlanTier.FREE)
        await c.meter.set_billing_flag(flagged, "founding_over_cap")
        await _hook(
            c, "transaction.completed", _purchase("txn_f1", "sub_f", "pri_solo_m", _bound(flagged), customer="ctm_holder")
        )
        tenant = await c.meter.get_tenant(flagged)
        assert (tenant["plan"], tenant["billing_flag"], tenant["stripe_customer_id"]) == ("solo", "founding_over_cap", None)
        event, message, details = alerts.sent[-1]
        assert event == f"paddle_customer_conflict:{flagged}:ctm_holder"
        assert details["kept_flag"] == "founding_over_cap" and "founding_over_cap" in message


# ---------------------------------------------------------------------------
# BILL-1 review regression: a server checkout is paid under the account's own
# verified email, so the owner's portal (which requires that email) opens.
# ---------------------------------------------------------------------------


def _transactions(paddle: Any) -> list[dict[str, Any]]:
    return [body for _m, _p, _q, body in paddle.requests("POST", "/transactions")]


async def test_a_server_checkout_is_bound_to_the_verified_emails_paddle_customer(tmp_path, monkeypatch) -> None:
    from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase

    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers_by_email["has-customer@example.com"] = "ctm_existing"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        c.h.app.state.alerts = RecordingAlerts()
        c.h.app.state.tasks = None
        # An existing Paddle customer for the verified email is attached to the transaction.
        uid = await c.h.create_user("has-customer@example.com", verified=True)
        r = await c.h.client.post(
            "/api/v1/billing/checkout", json={"plan": "solo"}, headers=c.h.jwt(uid, "has-customer@example.com")
        )
        assert r.status_code == 200, r.text
        assert _transactions(paddle)[-1]["customer_id"] == "ctm_existing"

        # No customer yet: one is created with the verified email and attached.
        new = await c.h.create_user("first-purchase@example.com", verified=True)
        r = await c.h.client.post(
            "/api/v1/billing/checkout", json={"plan": "solo"}, headers=c.h.jwt(new, "first-purchase@example.com")
        )
        assert r.status_code == 200, r.text
        created = paddle.customers_by_email["first-purchase@example.com"]
        assert paddle.requests("POST", "/customers")[-1][3] == {
            "email": "first-purchase@example.com",
            "custom_data": {"remembra_user_id": new},
        }
        assert _transactions(paddle)[-1]["customer_id"] == created

        # The purchase records that customer, and the owner's portal opens it.
        paid = _purchase("txn_fp", "sub_fp", "pri_solo_m", _bound(new), customer=created)
        assert (await _hook(c, "transaction.completed", paid))["applied"] == "applied"
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(new, "first-purchase@example.com"))
        assert r.status_code == 200, r.text
        assert r.json()["portal_url"] == f"https://portal.example/{created}"


async def test_a_server_checkout_of_an_unverified_account_names_no_customer(tmp_path, monkeypatch) -> None:
    """An unproven email is never looked up or attached (it may be someone else's Paddle customer)."""
    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers_by_email["not-proven@example.com"] = "ctm_someone"
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("not-proven@example.com")
        r = await c.h.client.post(
            "/api/v1/billing/checkout", json={"plan": "solo"}, headers=c.h.jwt(uid, "not-proven@example.com")
        )
        assert r.status_code == 200, r.text
        assert "customer_id" not in _transactions(paddle)[-1]
        assert paddle.requests("GET", "/customers") == [] and paddle.requests("POST", "/customers") == []


@pytest.mark.parametrize("failing", ["GET /customers", "POST /customers"])
async def test_a_paddle_customer_failure_does_not_block_the_checkout(tmp_path, monkeypatch, failing) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.failures[failing] = 500
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("paddle-hiccup@example.com", verified=True)
        r = await c.h.client.post(
            "/api/v1/billing/checkout", json={"plan": "solo"}, headers=c.h.jwt(uid, "paddle-hiccup@example.com")
        )
        assert r.status_code == 200, r.text
        assert "customer_id" not in _transactions(paddle)[-1]


async def test_the_owner_report_lists_accounts_whose_paddle_customer_has_another_email(tmp_path, monkeypatch) -> None:
    """scripts/maintenance/paddle_customer_email_report.py: read-only, masked emails, one status per account."""
    import importlib.util
    import sys
    from pathlib import Path

    from remembra.cloud.billing_paddle import PaddleBillingManager

    script = Path(__file__).resolve().parents[1] / "scripts" / "maintenance" / "paddle_customer_email_report.py"
    spec = importlib.util.spec_from_file_location("paddle_customer_email_report", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["paddle_customer_email_report"] = module
    spec.loader.exec_module(module)

    paddle = _paddle_mock.install(monkeypatch)
    paddle.customers.update(
        {
            "ctm_same": "Same@Example.com",
            "ctm_other": "billing@owner-company.example",
            "ctm_unverified": "unverified@example.com",
            "ctm_erased": "erased@example.com",
        }
    )
    paddle.failures["GET /customers/ctm_flaky"] = 503
    async with cost_app(tmp_path) as c:
        rows = {
            await c.h.create_user("same@example.com", verified=True): "ctm_same",
            await c.h.create_user("owner@example.com", verified=True): "ctm_other",
            await c.h.create_user("unverified@example.com"): "ctm_unverified",
            await c.h.create_user("flaky@example.com", verified=True): "ctm_flaky",
            await c.h.create_user("gone@example.com", verified=True): "ctm_gone",
        }
        for uid, customer in rows.items():
            await c.meter.register_tenant(uid, PlanTier.SOLO, stripe_customer_id=customer)
        await c.meter.register_tenant("user_erased", PlanTier.FREE, stripe_customer_id="ctm_erased")
        await c.h.create_user("no-customer@example.com", verified=True)  # never listed
        before = await c.h.db.conn.execute("SELECT COUNT(*) FROM cloud_tenants")
        tenants_before = (await before.fetchone())[0]

        accounts = module.load_accounts(str(tmp_path / "security.db"))
        billing = PaddleBillingManager(api_key="pdl_test", webhook_secret="s", sandbox=True)
        report = {row["customer_id"]: row for row in await module.build_report(accounts, billing)}

        status = {cid: row["status"] for cid, row in report.items()}
        assert status == {
            "ctm_same": "match",
            "ctm_other": "mismatch",
            "ctm_unverified": "login_unverified",
            "ctm_flaky": "paddle_error",
            "ctm_gone": "customer_missing",
            "ctm_erased": "no_login",
        }
        assert report["ctm_other"]["login_email"] == "o***@example.com"
        assert report["ctm_other"]["paddle_email"] == "b***@owner-company.example"
        # Read-only: the database is unchanged and Paddle was only read.
        after = await c.h.db.conn.execute("SELECT COUNT(*) FROM cloud_tenants")
        assert (await after.fetchone())[0] == tenants_before
        assert {method for method, *_ in paddle.calls} == {"GET"}
        full = await module.build_report(accounts[:1], billing, show_emails=True)
        assert "***" not in str(full[0]["login_email"])
