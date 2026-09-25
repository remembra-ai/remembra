"""Email verification for tenants created by the master-key POST /api/v1/cloud/signup.

Those tenants have no dashboard ``users`` row. They now get a verification
link at signup (and on request with their API key), confirm it with the
token alone at /cloud/verify-email/confirm, and are held at the
unverified-email credit cap until they do (once the cap is switched on).
One free account per verified email: a second tenant cannot verify an
address another account already verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from remembra.cloud import email as email_module
from remembra.cloud.metering import TENANT_SIGNUP_SOURCE
from tests._cost_harness import cost_app
from tests.security_harness import MASTER_KEY

CAP_ON = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _Result:
    success: bool = True
    error: str | None = None


@dataclass
class FakeEmail:
    verification_urls: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def send_welcome_email(self, **_: Any) -> _Result:
        return _Result()

    async def send_email_verification_email(self, to: str, verify_url: str) -> _Result:
        self.verification_urls.append((to, verify_url))
        return _Result(success=not self.fail)


@pytest.fixture()
def outbox(monkeypatch: pytest.MonkeyPatch) -> FakeEmail:
    fake = FakeEmail()
    monkeypatch.setattr(email_module.EmailService, "create", classmethod(lambda cls, **_: fake))
    return fake


def token_from(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


async def cloud_signup(c: Any, email: str, ip: str = "198.51.100.7") -> dict[str, Any]:
    r = await c.h.client.post("/api/v1/cloud/signup", json={"email": email, "client_ip": ip}, headers={"X-API-Key": MASTER_KEY})
    assert r.status_code == 201, r.text
    return r.json()


async def test_api_signup_tenant_verifies_by_emailed_token_and_leaves_the_hold(tmp_path, outbox) -> None:
    async with cost_app(
        tmp_path, unverified_credit_cap_effective_at=CAP_ON, public_dashboard_url="https://app.example.test"
    ) as c:
        body = await cloud_signup(c, "Builder@Example.com")
        uid = body["user_id"]
        assert body["email_verification_sent"] is True
        tenant = await c.meter.get_tenant(uid)
        assert tenant["signup_source"] == TENANT_SIGNUP_SOURCE and not tenant["email_verified"]

        # Held at 25 credits until verified (it used to be exempt with no way to verify).
        assert (await c.meter.get_account(uid)).credit_limit == 25
        hdr = {"X-API-Key": body["api_key"]}
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert summary["email_verified"] is False and summary["credits"]["unverified_cap_applied"] is True

        to, url = outbox.verification_urls[-1]
        assert to == "Builder@Example.com"
        assert url.startswith("https://app.example.test/verify-email?")
        params = token_from(url)
        assert params["account"] == "api" and len(params["token"]) >= 40

        bad = await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": "x" * 43})
        assert bad.status_code == 400
        r = await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": params["token"]})
        assert r.status_code == 200 and r.json() == {"message": "Email verified", "email_verified": True}
        assert (await c.meter.get_account(uid)).credit_limit == 500
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert summary["email_verified"] is True and summary["credits"]["unverified_cap_applied"] is False

        # Single use.
        again = await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": params["token"]})
        assert again.status_code == 400
        # Asking again once verified is a no-op.
        r = await c.h.client.post("/api/v1/cloud/verify-email/request", headers=hdr)
        assert r.status_code == 200 and r.json()["email_verified"] is True


async def test_tenant_can_request_a_new_link_with_its_api_key(tmp_path, outbox) -> None:
    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        body = await cloud_signup(c, "later@example.com")
        first = token_from(outbox.verification_urls[-1][1])["token"]
        r = await c.h.client.post("/api/v1/cloud/verify-email/request", headers={"X-API-Key": body["api_key"]})
        assert r.status_code == 200 and r.json() == {"message": "Verification email sent", "email_verified": False}
        _, url = outbox.verification_urls[-1]
        assert url.startswith("https://app.remembra.dev/verify-email?")  # default dashboard origin
        second = token_from(url)["token"]
        # The newer link replaces the older one.
        assert (await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": first})).status_code == 400
        assert (await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": second})).status_code == 200

        outbox.fail = True
        other = await cloud_signup(c, "other@example.com", ip="198.51.101.7")
        r = await c.h.client.post("/api/v1/cloud/verify-email/request", headers={"X-API-Key": other["api_key"]})
        assert r.status_code == 503


async def test_dashboard_accounts_keep_their_own_verification_path(tmp_path, outbox) -> None:
    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        uid, hdr = await c.account("dash@example.com", verified=False)
        r = await c.h.client.post("/api/v1/cloud/verify-email/request", headers=hdr)
        assert r.status_code == 409 and "/auth/verify-email/request" in r.json()["detail"]
        # A dashboard user's token cannot be spent on the token-only tenant endpoint.
        r = await c.h.client.post("/api/v1/auth/verify-email/request", headers=c.h.jwt(uid, "dash@example.com"))
        assert r.status_code == 200, r.text
        token = token_from(outbox.verification_urls[-1][1])["token"]
        assert (await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": token})).status_code == 400
        r = await c.h.client.post(
            "/api/v1/auth/verify-email/confirm", json={"token": token}, headers=c.h.jwt(uid, "dash@example.com")
        )
        assert r.status_code == 200 and r.json()["email_verified"] is True


async def test_one_free_account_per_verified_email(tmp_path, outbox) -> None:
    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        first = await cloud_signup(c, "farm@example.com", ip="198.51.100.7")
        first_token = token_from(outbox.verification_urls[-1][1])["token"]
        second = await cloud_signup(c, "FARM@example.com", ip="198.51.101.7")
        second_token = token_from(outbox.verification_urls[-1][1])["token"]
        assert (await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": first_token})).status_code == 200
        r = await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": second_token})
        assert r.status_code == 409 and "already verified on another" in r.json()["detail"]
        assert (await c.meter.get_account(second["user_id"])).credit_limit == 25
        assert (await c.meter.get_account(first["user_id"])).credit_limit == 500

        # A verified dashboard account holds its address too.
        await c.account("taken@example.com", verified=True)
        third = await cloud_signup(c, "taken@example.com", ip="198.51.102.7")
        token = token_from(outbox.verification_urls[-1][1])["token"]
        r = await c.h.client.post("/api/v1/cloud/verify-email/confirm", json={"token": token})
        assert r.status_code == 409
        assert (await c.meter.get_account(third["user_id"])).credit_limit == 25


async def test_other_tenant_only_records_stay_exempt(tmp_path, outbox) -> None:
    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        # Admin-assigned plans / legacy rows / no email: nothing to verify, so no hold.
        await c.meter.register_tenant("tenant_only_user", email="api-signup@example.com")
        assert (await c.meter.get_account("tenant_only_user")).credit_limit == 500
        await c.meter.register_tenant("no_email_tenant")
        await c.meter.mark_tenant_signup("no_email_tenant")
        assert (await c.meter.get_account("no_email_tenant")).credit_limit == 500


async def test_signup_without_email_delivery_still_succeeds(tmp_path, monkeypatch) -> None:
    def no_email(cls: Any, **_: Any) -> Any:
        raise ValueError("RESEND_API_KEY environment variable is required for Resend backend")

    monkeypatch.setattr(email_module.EmailService, "create", classmethod(no_email))
    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        body = await cloud_signup(c, "quiet@example.com")
        assert body["email_verification_sent"] is False
        r = await c.h.client.post("/api/v1/cloud/verify-email/request", headers={"X-API-Key": body["api_key"]})
        assert r.status_code == 503 and r.json()["detail"] == "Email delivery is not configured"
