"""Signup hardening: rate limits per network and email domain, Cloudflare Turnstile.

* **Rate limits** — ``signup_ip_rate_limit`` (default 3/hour) per client /24
  (IPv6 /56) and ``signup_domain_rate_limit`` (default 20/day) per email
  domain, with large mailbox providers exempt from the domain limit (the
  network limit still applies to them). Stored on the shared rate-limit
  backend (memory or Redis). Active whenever rate limiting is enabled.
* **Turnstile** — when ``REMEMBRA_TURNSTILE_SECRET`` is set, the signup must
  carry a Turnstile token, verified server-side against Cloudflare's
  ``siteverify`` endpoint. Inert when the secret is unset. Fails closed: an
  unreachable siteverify rejects the signup (503) rather than letting it in.

Order: a loose per-network attempt cap (only with Turnstile on, bounding
siteverify calls), then Turnstile, then the strict signup limits, so
requests without a valid token cannot use up a network's or a domain's quota.

New Free accounts can additionally be held at 25 smart credits until the
email is verified, once ``unverified_credit_cap_effective_at`` is set (see
:meth:`remembra.cloud.metering.UsageMeter.get_account`).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import structlog
from fastapi import HTTPException, status

from remembra.cloud.ratelimit import get_cloud_rate_limiter, network_key, rate_limits_enabled
from remembra.config import get_settings

log = structlog.get_logger(__name__)

TURNSTILE_HEADER = "CF-Turnstile-Response"


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


# Factory for the siteverify HTTP client (swapped in tests for a mock transport).
http_client_factory: Callable[[], httpx.AsyncClient] = _default_http_client


def turnstile_enabled() -> bool:
    return bool(get_settings().turnstile_secret)


async def verify_turnstile(token: str | None, remote_ip: str | None) -> None:
    """Verify a Turnstile token server-side. No-op when no secret is configured.

    Raises:
        HTTPException 400 when the token is missing or rejected, 503 when
        siteverify cannot be reached (fail closed).
    """
    settings = get_settings()
    secret = settings.turnstile_secret
    if not secret:
        return
    if not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Human verification is required (missing Turnstile token).",
        )
    form = {"secret": secret, "response": token.strip()}
    if remote_ip:
        form["remoteip"] = remote_ip
    try:
        async with http_client_factory() as client:
            response = await client.post(settings.turnstile_verify_url, data=form)
            payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        log.error("turnstile_siteverify_unreachable", error_type=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Human verification is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "5"},
        ) from e
    if not isinstance(payload, dict) or payload.get("success") is not True:
        codes = payload.get("error-codes") if isinstance(payload, dict) else None
        log.warning("turnstile_rejected", error_codes=codes)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Human verification failed.")


def enforce_signup_rate_limits(client_ip: str, email: str) -> None:
    """429 when the client network or the email domain exceeded its signup limit."""
    if not rate_limits_enabled():
        return
    settings = get_settings()
    limiter = get_cloud_rate_limiter()
    if not limiter.hit("signup_ip", network_key(client_ip), settings.signup_ip_rate_limit):
        log.warning("signup_rate_limited", scope="network")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many signups from your network. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    exempt = {d.strip().lower() for d in settings.signup_domain_limit_exempt}
    if domain and domain not in exempt and not limiter.hit("signup_domain", domain, settings.signup_domain_rate_limit):
        log.warning("signup_rate_limited", scope="domain", domain=domain)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many signups for this email domain today. Please try again tomorrow.",
            headers={"Retry-After": "86400"},
        )


def enforce_signup_attempt_limit(client_ip: str) -> None:
    """Loose per-network cap on signup ATTEMPTS, checked before Turnstile (bounds siteverify calls)."""
    if not rate_limits_enabled() or not turnstile_enabled():
        return
    limit = get_settings().signup_attempt_ip_rate_limit
    if not get_cloud_rate_limiter().hit("signup_attempt_ip", network_key(client_ip), limit):
        log.warning("signup_rate_limited", scope="attempts")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many signup attempts from your network. Please try again later.",
            headers={"Retry-After": "3600"},
        )


async def guard_signup(*, client_ip: str, email: str, turnstile_token: str | None) -> None:
    """Run every signup check in order: attempt cap, Turnstile, then the signup limits.

    The strict per-network and per-domain signup limits are only charged once
    the human check has passed, so requests without a valid token (or with
    random addresses at someone else's domain) cannot use them up and lock
    real users out.
    """
    enforce_signup_attempt_limit(client_ip)
    await verify_turnstile(turnstile_token, client_ip)
    enforce_signup_rate_limits(client_ip, email)
