"""Sign in with GitHub / Google, and the public sign-in configuration for the dashboard.

* ``GET  /api/v1/auth/providers``                 enabled providers + Turnstile site key
* ``GET  /api/v1/auth/oauth/{provider}/start``    begin a sign-in (302 to the provider)
* ``GET  /api/v1/auth/oauth/{provider}/callback`` provider redirect target (303 to the dashboard)
* ``POST /api/v1/auth/oauth/exchange``            single-use login code (+ browser cookie) -> dashboard JWT
* ``POST /api/v1/auth/oauth/{provider}/link``     signed in: ticket to connect a provider from Settings
* ``GET  /api/v1/auth/identities``                signed in: connected providers
* ``DELETE /api/v1/auth/identities/{provider}``   signed in: disconnect a provider

A provider without credentials (or without ``REMEMBRA_PUBLIC_URL`` and
``REMEMBRA_PUBLIC_DASHBOARD_URL``) answers 404 on every route and is not
listed. See :mod:`remembra.auth.social` for the security model.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from remembra.api.v1.auth import CurrentUser, get_user_manager
from remembra.auth import social
from remembra.auth.middleware import get_client_ip
from remembra.auth.superadmin import account_is_owner
from remembra.config import get_settings
from remembra.core.limiter import limiter
from remembra.security import state as security_state

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


class ProviderInfo(BaseModel):
    id: str = Field(description="Provider id used in the /oauth/{provider}/* paths")
    name: str = Field(description="Display name (GitHub, Google)")
    start_path: str = Field(description="Path to navigate the browser to; append ?from=login|signup")


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]
    turnstile_site_key: str | None = Field(
        None, description="Cloudflare Turnstile site key; when set, password signup requires a Turnstile token"
    )


class OAuthExchangeRequest(BaseModel):
    code: str = Field(min_length=16, max_length=256, description="Single-use login code from the callback redirect")
    totp_code: str | None = Field(None, min_length=6, max_length=6, description="6-digit TOTP code when 2FA is on")


class OAuthExchangeResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    user: dict[str, Any] | None = None
    requires_2fa: bool = False
    new_account: bool = False
    provider: str | None = None
    message: str | None = None


class LinkStartResponse(BaseModel):
    start_path: str = Field(description="Navigate the browser here (on the API origin) within 2 minutes; single use")


class IdentityInfo(BaseModel):
    provider: str
    name: str
    email: str
    created_at: str | None = None
    last_login_at: str | None = None


class IdentitiesResponse(BaseModel):
    identities: list[IdentityInfo]
    available: list[ProviderInfo] = Field(description="Providers enabled on this server")


def _provider_infos() -> list[ProviderInfo]:
    return [
        ProviderInfo(id=spec.id, name=spec.name, start_path=f"/api/v1/auth/oauth/{spec.id}/start")
        for spec in social.enabled_providers()
    ]


def _require_provider(provider: str) -> None:
    if not social.provider_enabled(provider):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _db(request: Request) -> Any:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not initialized")
    return db


@router.get("/providers", response_model=ProvidersResponse, summary="Sign-in options for the dashboard")
@limiter.limit("60/minute")
async def list_providers(request: Request) -> ProvidersResponse:
    """Enabled social sign-in providers, and the Turnstile site key when signup requires it."""
    settings = get_settings()
    site_key = (settings.turnstile_site_key or "").strip() or None
    return ProvidersResponse(
        providers=_provider_infos(),
        # Only when the server will actually verify it; a widget without a
        # secret would be theatre, and a secret without a widget locks signup.
        turnstile_site_key=site_key if settings.turnstile_secret else None,
    )


@router.get("/oauth/{provider}/start", summary="Begin Sign in with GitHub / Google", include_in_schema=True)
@limiter.limit("20/minute")
async def oauth_start(request: Request, provider: str) -> RedirectResponse:
    """Create a single-use state (PKCE + nonce) bound to this browser and redirect to the provider.

    With ``?link=<ticket>`` (from ``POST /oauth/{provider}/link``) the flow
    connects the provider to the signed-in account that asked for the ticket
    instead of signing in.
    """
    _require_provider(provider)
    from_page = request.query_params.get("from", "login")
    if from_page not in social.FROM_PAGES or from_page == "settings":
        from_page = "login"
    db = _db(request)
    link_user_id: str | None = None
    if "link" in request.query_params:
        link_user_id = await social.consume_link_ticket(db, provider, request.query_params.get("link"))
        if link_user_id is None:
            target = social.dashboard_url("/oauth/callback", {"error": "invalid_state", "provider": provider, "from": "settings"})
            return RedirectResponse(target, status_code=303, headers=_NO_STORE)
        from_page = "settings"
    state, browser_secret, verifier, nonce = await social.create_login_state(db, provider, from_page, link_user_id=link_user_id)
    response = RedirectResponse(social.authorize_url(provider, state, verifier, nonce), status_code=302, headers=_NO_STORE)
    response.set_cookie(
        social.cookie_name(provider),
        browser_secret,
        max_age=social.STATE_TTL_SECONDS,
        path="/",
        secure=social.cookie_secure(),
        httponly=True,
        samesite="lax",  # sent on the provider's top-level redirect back to us
    )
    return response


@router.get("/oauth/{provider}/callback", summary="OAuth redirect target", include_in_schema=False)
@limiter.limit("30/minute")
async def oauth_callback(request: Request, provider: str) -> RedirectResponse:
    """Finish the sign-in and send the browser to the dashboard with a login code (or an error code)."""
    _require_provider(provider)
    db = _db(request)
    params = request.query_params
    state = params.get("state")
    from_page = "login"
    browser_binding: str | None = None
    try:
        if params.get("error"):
            # The user cancelled (access_denied) or the provider refused the request.
            from_page = await social.peek_login_state_page(db, state)
            await social.discard_login_state(db, state)
            raise social.SocialLoginError("access_denied")
        login_state = await social.consume_login_state(db, provider, state, request.cookies.get(social.cookie_name(provider)))
        from_page = login_state.from_page
        identity = await social.fetch_identity(provider, params.get("code") or "", login_state)
        if login_state.link_user_id:
            await social.link_identity(db, identity, login_state.link_user_id)
            target = social.dashboard_url("/oauth/callback", {"linked": "1", "provider": provider, "from": "settings"})
        else:
            user_id, created = await social.resolve_account(db, identity, get_client_ip(request))
            code, browser_binding = await social.issue_login_code(db, user_id, provider, new_account=created)
            target = social.dashboard_url("/oauth/callback", {"code": code, "provider": provider})
    except social.SocialLoginError as e:
        log.info("oauth_login_failed", provider=provider, reason=e.code)
        target = social.dashboard_url("/oauth/callback", {"error": e.code, "provider": provider, "from": from_page})
    except Exception as e:  # never leave the user on a JSON 500 mid-flow
        log.error("oauth_callback_error", provider=provider, error_type=type(e).__name__)
        target = social.dashboard_url("/oauth/callback", {"error": "provider_error", "provider": provider, "from": from_page})
    response = RedirectResponse(target, status_code=303, headers=_NO_STORE)
    response.delete_cookie(social.cookie_name(provider), path="/", secure=social.cookie_secure(), httponly=True, samesite="lax")
    if browser_binding:
        # The login code only works together with this cookie (see /oauth/exchange).
        response.set_cookie(
            social.login_cookie_name(),
            browser_binding,
            max_age=social.LOGIN_CODE_TTL_SECONDS,
            path="/",
            secure=social.cookie_secure(),
            httponly=True,
            samesite="lax",  # sent on the dashboard's same-site fetch to the API
        )
    return response


@router.post("/oauth/exchange", response_model=OAuthExchangeResponse, summary="Trade a login code for a session")
@limiter.limit("10/minute")
async def oauth_exchange(
    request: Request,
    response: Response,
    body: Annotated[OAuthExchangeRequest, Body(...)],
) -> OAuthExchangeResponse:
    """Single use, 5 minutes. With 2FA on, answers ``requires_2fa`` until a valid ``totp_code`` is sent.

    The browser must send the HttpOnly cookie the callback set with the code
    (``fetch(..., {credentials: 'include'})``). A code presented without it
    was carried to another browser, e.g. an attacker's code planted in a
    victim's dashboard link; it is refused and burned.
    """
    user_manager = await get_user_manager(request)
    db = user_manager.db
    expired = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This sign-in link has expired or was already used. Please sign in again.",
    )
    info = await social.peek_login_code(db, body.code)
    if info is None:
        raise expired
    if not social.login_code_bound_to_browser(info, request.cookies.get(social.login_cookie_name())):
        await social.consume_login_code(db, body.code)
        log.warning("oauth_exchange_refused_other_browser", provider=info["provider"])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This sign-in did not finish in the browser that started it. Please sign in again.",
        )
    user_row = await db.get_user_by_id(info["user_id"])
    if not user_row or not user_row.get("is_active", True):
        await social.consume_login_code(db, body.code)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is deactivated")

    # Same per-account lockout as password login: 2FA guesses count toward it.
    lock_key = security_state.account_key("login", user_row["email"])
    remaining = await security_state.lockout_remaining_seconds(db, lock_key)
    if remaining:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(remaining)},
        )
    if await user_manager.is_totp_enabled(user_row["id"]):
        if not body.totp_code:
            return OAuthExchangeResponse(requires_2fa=True, provider=info["provider"], message="2FA code required")
        if not await user_manager.verify_totp(user_row["id"], body.totp_code):
            await security_state.record_failure(db, lock_key)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA code")

    if not await social.consume_login_code(db, body.code):
        raise expired
    await security_state.clear_failures(db, lock_key)
    await db.update_user_last_login(user_row["id"])
    token = user_manager.create_jwt_token(user_row["id"], user_row["email"])
    response.delete_cookie(social.login_cookie_name(), path="/", secure=social.cookie_secure(), httponly=True, samesite="lax")
    log.info("oauth_session_issued", provider=info["provider"], user_id=user_row["id"], new_account=info["new_account"])
    return OAuthExchangeResponse(
        access_token=token,
        user={
            "id": user_row["id"],
            "email": user_row["email"],
            "name": user_row.get("name"),
            "email_verified": bool(user_row.get("email_verified")),
            "is_admin": account_is_owner(user_row),
        },
        new_account=info["new_account"],
        provider=info["provider"],
    )


# ---------------------------------------------------------------------------
# Connected sign-in methods (signed-in dashboard session)
# ---------------------------------------------------------------------------


@router.post(
    "/oauth/{provider}/link",
    response_model=LinkStartResponse,
    summary="Start connecting GitHub / Google to the signed-in account",
)
@limiter.limit("10/minute")
async def oauth_link_start(request: Request, provider: str, current_user: CurrentUser) -> LinkStartResponse:
    """Returns a single-use ``start_path`` (2 minutes). Needs a session from the last 15 minutes.

    This is the only way to add GitHub to an account that already exists:
    GitHub sign-in never links by email (see :mod:`remembra.auth.social`).
    """
    _require_provider(provider)
    user_manager = await get_user_manager(request)
    payload = user_manager.verify_jwt_token(current_user["token"]) or {}
    age_ms = time.time() * 1000 - security_state.token_issued_at_ms(payload)
    if age_ms > social.LINK_REAUTH_SECONDS * 1000:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="For your security, sign in again to connect a sign-in method.",
        )
    ticket = await social.create_link_ticket(user_manager.db, current_user["id"], provider)
    return LinkStartResponse(start_path=f"/api/v1/auth/oauth/{provider}/start?link={ticket}")


@router.get("/identities", response_model=IdentitiesResponse, summary="Sign-in providers connected to this account")
@limiter.limit("30/minute")
async def list_connected_identities(request: Request, current_user: CurrentUser) -> IdentitiesResponse:
    db = _db(request)
    rows = await social.list_identities(db, current_user["id"])
    return IdentitiesResponse(
        identities=[
            IdentityInfo(
                provider=row["provider"],
                name=social.PROVIDERS[row["provider"]].name if row["provider"] in social.PROVIDERS else row["provider"],
                email=row["email"],
                created_at=row["created_at"],
                last_login_at=row["last_login_at"],
            )
            for row in rows
        ],
        available=_provider_infos(),
    )


@router.delete("/identities/{provider}", summary="Disconnect a sign-in provider from this account")
@limiter.limit("10/minute")
async def disconnect_identity(request: Request, provider: str, current_user: CurrentUser) -> dict[str, bool]:
    """The account keeps its email and password; "Forgot password" still works after this."""
    if provider not in social.PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not await social.unlink_identity(_db(request), current_user["id"], provider):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This sign-in method is not connected")
    return {"removed": True}
