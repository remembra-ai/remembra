"""OAuth 2.1 authorization server for the remote MCP connector.

Endpoints (all under the configured ``public_url`` origin):

- ``GET  /.well-known/oauth-authorization-server``   RFC 8414 metadata
- ``GET  /.well-known/oauth-protected-resource[/mcp]`` RFC 9728 metadata
- ``POST /oauth/register``            RFC 7591 dynamic client registration
- ``GET  /oauth/authorize``           authorization code + PKCE (S256 only)
- ``POST /oauth/authorize/login``     Remembra dashboard email/password (+TOTP)
- ``POST /oauth/authorize/consent``   choose projects + agent label, approve/deny
- ``POST /oauth/token``               code exchange and refresh (rotation)
- ``POST /oauth/revoke``              RFC 7009 revocation
- ``GET/DELETE /api/v1/connector/connections`` list / disconnect (dashboard JWT)

Authorization responses always carry ``iss`` (RFC 9207), which is what lets
ChatGPT use its stable redirect URI.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated, Any
from urllib.parse import unquote, urlencode, urlsplit

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import FormData

from remembra.api.v1.auth import CurrentUser as DashboardUser
from remembra.auth.users import UserManager
from remembra.config import Settings, get_settings
from remembra.connector import pages
from remembra.connector.policy import (
    SCOPE_DESCRIPTIONS,
    SUPPORTED_SCOPES,
    ScopeError,
    default_agent_label,
    format_scope,
    is_loopback_redirect,
    normalize_agent_label,
    normalize_projects,
    parse_scope,
    redirect_host,
    redirect_origin,
    redirect_uri_allowed,
    redirect_uri_matches,
    resource_matches,
    valid_code_challenge,
)
from remembra.connector.store import AuthRequest, ConnectorStore, Grant, OAuthError
from remembra.core.limiter import limiter
from remembra.security import state as security_state

log = structlog.get_logger(__name__)

MCP_PATH = "/mcp"
_MAX_STATE_LEN = 2048
_MAX_REDIRECT_URIS = 10
_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
_GRANT_TYPES = ["authorization_code", "refresh_token"]
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

router = APIRouter(include_in_schema=False)
connections_router = APIRouter(prefix="/connector", tags=["connector"])


# ---------------------------------------------------------------------------
# URLs and shared helpers
# ---------------------------------------------------------------------------


def issuer(settings: Settings) -> str:
    assert settings.public_url, "connector requires public_url"
    return settings.public_url


def resource_url(settings: Settings) -> str:
    return issuer(settings) + MCP_PATH


def protected_resource_metadata_url(settings: Settings) -> str:
    return issuer(settings) + "/.well-known/oauth-protected-resource" + MCP_PATH


def get_store(request: Request) -> ConnectorStore:
    store: ConnectorStore | None = getattr(request.app.state, "connector_store", None)
    if store is None or not get_settings().connector_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return store


StoreDep = Annotated[ConnectorStore, Depends(get_store)]


def _oauth_error(
    error: OAuthError | str, description: str = "", status_code: int = 400, headers: dict[str, str] | None = None
) -> JSONResponse:
    if isinstance(error, OAuthError):
        description, status_code, error = error.description, error.status_code, error.error
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={**_NO_STORE, **(headers or {})},
    )


def _redirect_to_client(redirect_uri: str, params: dict[str, str | None], settings: Settings) -> RedirectResponse:
    """303 to the client's registered redirect URI with ``iss`` (RFC 9207)."""
    query = {k: v for k, v in params.items() if v is not None}
    query["iss"] = issuer(settings)
    separator = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(redirect_uri + separator + urlencode(query), status_code=status.HTTP_303_SEE_OTHER, headers=_NO_STORE)


def _csrf_cookie_name(request_id: str) -> str:
    return "rmb_oauth_" + request_id


async def account_allows_grant(db: Any, grant: Grant) -> bool:
    """The owning account is active and has not invalidated sessions since approval.

    ``invalidate_user_sessions`` (password change or reset, deactivation) moves the
    user's cut-off forward; connections approved before it stop working, the
    same rule dashboard JWTs follow.
    """
    try:
        user_row = await db.get_user_by_id(grant.user_id)
        if not user_row or not user_row.get("is_active", True):
            return False
        valid_after = await security_state.get_tokens_valid_after_ms(db, grant.user_id)
    except Exception as e:  # fail closed
        log.error("connector_account_check_failed", error_type=type(e).__name__)
        return False
    return not (valid_after and grant.created_at_ms < valid_after)


# ---------------------------------------------------------------------------
# Discovery metadata
# ---------------------------------------------------------------------------


def authorization_server_metadata(settings: Settings) -> dict[str, Any]:
    base = issuer(settings)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "scopes_supported": list(SUPPORTED_SCOPES),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": list(_GRANT_TYPES),
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": list(_AUTH_METHODS),
        "revocation_endpoint_auth_methods_supported": list(_AUTH_METHODS),
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": "https://docs.remembra.dev/integrations/claude-and-chatgpt-apps/",
    }


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "resource": resource_url(settings),
        "authorization_servers": [issuer(settings)],
        "scopes_supported": list(SUPPORTED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Remembra",
        "resource_documentation": "https://docs.remembra.dev/integrations/claude-and-chatgpt-apps/",
    }


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(store: StoreDep) -> JSONResponse:
    return JSONResponse(authorization_server_metadata(get_settings()), headers={"Cache-Control": "public, max-age=3600"})


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def pr_metadata(store: StoreDep) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(get_settings()), headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------


def _client_name(raw: Any) -> str:
    name = raw if isinstance(raw, str) else ""
    name = "".join(ch for ch in name if ch.isprintable()).strip()[:100]
    return name or "MCP client"


@router.post("/oauth/register")
@limiter.limit("60/minute")
async def register_client(request: Request, store: StoreDep) -> JSONResponse:
    settings = get_settings()
    try:
        body = json.loads(await request.body() or b"null")
    except (ValueError, UnicodeDecodeError):
        return _oauth_error("invalid_client_metadata", "Body must be a JSON object.")
    if not isinstance(body, dict):
        return _oauth_error("invalid_client_metadata", "Body must be a JSON object.")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or not all(isinstance(u, str) for u in redirect_uris):
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty array of strings.")
    if len(redirect_uris) > _MAX_REDIRECT_URIS:
        return _oauth_error("invalid_redirect_uri", f"At most {_MAX_REDIRECT_URIS} redirect_uris.")
    rejected = [
        u
        for u in redirect_uris
        if not redirect_uri_allowed(u, settings.connector_redirect_uris, settings.connector_allow_loopback_redirects)
    ]
    if rejected:
        log.warning("oauth_register_redirect_rejected", count=len(rejected))
        return _oauth_error("invalid_redirect_uri", "redirect_uri is not an allowed client callback for this server.")

    method = body.get("token_endpoint_auth_method", "client_secret_basic")
    if method not in _AUTH_METHODS:
        return _oauth_error("invalid_client_metadata", f"token_endpoint_auth_method must be one of {list(_AUTH_METHODS)}.")
    grant_types = body.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types or not set(grant_types) <= set(_GRANT_TYPES):
        return _oauth_error(
            "invalid_client_metadata",
            "grant_types must include authorization_code and only use authorization_code/refresh_token.",
        )
    response_types = body.get("response_types", ["code"])
    if response_types != ["code"]:
        return _oauth_error("invalid_client_metadata", 'response_types must be ["code"].')
    if body.get("scope") is not None:
        try:
            parse_scope(str(body["scope"]))
        except ScopeError as e:
            return _oauth_error("invalid_client_metadata", str(e))

    client, secret = await store.register_client(
        client_name=_client_name(body.get("client_name")),
        redirect_uris=list(dict.fromkeys(redirect_uris)),
        grant_types=list(_GRANT_TYPES),  # refresh tokens are always issued
        token_endpoint_auth_method=method,
    )
    log.info("oauth_client_registered", client_id=client["client_id"], auth_method=method)
    payload: dict[str, Any] = {
        "client_id": client["client_id"],
        "client_id_issued_at": client["client_id_issued_at"],
        "client_name": client["client_name"],
        "redirect_uris": client["redirect_uris"],
        "grant_types": client["grant_types"],
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "scope": format_scope(SUPPORTED_SCOPES),
    }
    if secret:
        payload["client_secret"] = secret
        payload["client_secret_expires_at"] = 0
    return JSONResponse(payload, status_code=status.HTTP_201_CREATED, headers=_NO_STORE)


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize")
@limiter.limit("60/minute")
async def authorize(request: Request, store: StoreDep) -> Response:
    settings = get_settings()
    q = request.query_params
    client = await store.get_client(q.get("client_id"))
    if client is None:
        return pages.error_page("Unknown client. Remove the connector and add it again.")

    redirect_uri = q.get("redirect_uri")
    if redirect_uri is None:
        if len(client["redirect_uris"]) != 1:
            return pages.error_page("redirect_uri is required.")
        redirect_uri = client["redirect_uris"][0]
    elif not redirect_uri_matches(redirect_uri, client["redirect_uris"]):
        # Never redirect to an unregistered URI (open redirect / code theft).
        return pages.error_page("redirect_uri is not registered for this client.")

    state = q.get("state")
    if state is not None and len(state) > _MAX_STATE_LEN:
        return _redirect_to_client(redirect_uri, {"error": "invalid_request", "error_description": "state too long"}, settings)

    def fail(error: str, description: str) -> RedirectResponse:
        return _redirect_to_client(redirect_uri, {"error": error, "error_description": description, "state": state}, settings)

    if q.get("response_type") != "code":
        return fail("unsupported_response_type", "Only response_type=code is supported.")
    challenge = q.get("code_challenge")
    if not valid_code_challenge(challenge):
        return fail("invalid_request", "A PKCE code_challenge (S256) is required.")
    if q.get("code_challenge_method") != "S256":
        return fail("invalid_request", "code_challenge_method must be S256.")
    try:
        scopes = parse_scope(q.get("scope"))
    except ScopeError as e:
        return fail("invalid_scope", str(e))
    if not resource_matches(q.get("resource"), resource_url(settings)):
        return fail("invalid_target", "resource does not name this server's MCP endpoint.")

    assert challenge is not None
    request_id, csrf = await store.create_auth_request(
        client_id=client["client_id"],
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        scopes=scopes,
        resource=resource_url(settings),
    )
    response = pages.login_page(
        request_id=request_id,
        client_name=client["client_name"],
        redirect_host=redirect_host(redirect_uri),
        redirect_origin=redirect_origin(redirect_uri),
        loopback=is_loopback_redirect(redirect_uri),
        scopes=scopes,
    )
    response.set_cookie(
        _csrf_cookie_name(request_id),
        csrf,
        max_age=900,
        path="/oauth/authorize",
        secure=issuer(settings).startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    return response


async def _load_request(
    request: Request, store: ConnectorStore, form: FormData
) -> tuple[AuthRequest | None, dict[str, Any] | None, Response | None]:
    """The pending authorization for this form post, CSRF-checked against its cookie."""
    request_id = str(form.get("request_id") or "")
    auth_req = await store.get_auth_request(request_id)
    if auth_req is None:
        return None, None, pages.error_page("This sign-in page expired. Start connecting again from the app.")
    if not store.csrf_valid(auth_req, request.cookies.get(_csrf_cookie_name(auth_req.request_id))):
        log.warning("oauth_csrf_rejected")
        return (
            None,
            None,
            pages.error_page("This form could not be verified. Start connecting again from the app.", status_code=403),
        )
    client = await store.get_client(auth_req.client_id)
    if client is None:
        return None, None, pages.error_page("Unknown client. Remove the connector and add it again.")
    return auth_req, client, None


async def _deny(store: ConnectorStore, auth_req: AuthRequest, settings: Settings) -> RedirectResponse:
    await store.delete_auth_request(auth_req.request_id)
    return _redirect_to_client(
        auth_req.redirect_uri,
        {"error": "access_denied", "error_description": "The user denied the request.", "state": auth_req.state},
        settings,
    )


def _login_again(auth_req: AuthRequest, client: dict[str, Any], error: str, email: str, status_code: int) -> Response:
    return pages.login_page(
        request_id=auth_req.request_id,
        client_name=client["client_name"],
        redirect_host=redirect_host(auth_req.redirect_uri),
        redirect_origin=redirect_origin(auth_req.redirect_uri),
        loopback=is_loopback_redirect(auth_req.redirect_uri),
        scopes=auth_req.scopes,
        error=error,
        email=email,
        status_code=status_code,
    )


async def _consent(
    store: ConnectorStore,
    auth_req: AuthRequest,
    client: dict[str, Any],
    user_id: str,
    email: str,
    *,
    error: str | None = None,
    selected: list[str] | None = None,
    agent_label: str | None = None,
    status_code: int = 200,
) -> Response:
    projects = await store.user_projects(user_id)
    for extra in selected or []:
        if extra not in {p["project_id"] for p in projects}:
            projects.append({"project_id": extra, "memories": 0})
    return pages.consent_page(
        request_id=auth_req.request_id,
        client_name=client["client_name"],
        redirect_host=redirect_host(auth_req.redirect_uri),
        redirect_origin=redirect_origin(auth_req.redirect_uri),
        loopback=is_loopback_redirect(auth_req.redirect_uri),
        scopes=auth_req.scopes,
        email=email,
        projects=projects,
        agent_label=agent_label or default_agent_label(auth_req.redirect_uri),
        error=error,
        selected=selected,
        status_code=status_code,
    )


@router.post("/oauth/authorize/login")
@limiter.limit("10/minute")
async def authorize_login(request: Request, store: StoreDep) -> Response:
    settings = get_settings()
    form = await request.form()
    auth_req, client, failure = await _load_request(request, store, form)
    if failure is not None:
        return failure
    assert auth_req is not None and client is not None
    if form.get("decision") == "deny":
        return await _deny(store, auth_req, settings)

    email = str(form.get("email") or "").strip()[:320]
    password = str(form.get("password") or "")
    totp_code = str(form.get("totp_code") or "").strip()
    if not email or not password:
        return _login_again(auth_req, client, "Enter your email and password.", email, 400)

    db = request.app.state.db
    users = UserManager(db, settings.jwt_secret)
    # Same per-account lockout as dashboard login, so the two can't be combined
    # to get more guesses.
    lock_key = security_state.account_key("login", email)
    if await security_state.lockout_remaining_seconds(db, lock_key):
        return _login_again(auth_req, client, "Too many failed attempts. Try again later.", email, 429)

    user, _jwt, error = await users.authenticate(email=email, password=password)
    if error or user is None:
        await security_state.record_failure(db, lock_key)
        return _login_again(auth_req, client, "Invalid email or password.", email, 401)
    if await users.is_totp_enabled(user.id):
        if not totp_code:
            return _login_again(auth_req, client, "Enter the 6-digit code from your authenticator app.", email, 401)
        if not await users.verify_totp(user.id, totp_code):
            await security_state.record_failure(db, lock_key)
            return _login_again(auth_req, client, "Invalid two-factor code.", email, 401)
    await security_state.clear_failures(db, lock_key)

    await store.set_auth_request_user(auth_req.request_id, user.id)
    log.info("oauth_login_ok", user_id=user.id, client_id=client["client_id"])
    return await _consent(store, auth_req, client, user.id, user.email)


@router.post("/oauth/authorize/consent")
@limiter.limit("20/minute")
async def authorize_consent(request: Request, store: StoreDep) -> Response:
    settings = get_settings()
    form = await request.form()
    auth_req, client, failure = await _load_request(request, store, form)
    if failure is not None:
        return failure
    assert auth_req is not None and client is not None
    if form.get("decision") == "deny":
        return await _deny(store, auth_req, settings)
    if not auth_req.user_id:
        return pages.error_page("Sign in first. Start connecting again from the app.", status_code=403)

    db = request.app.state.db
    user_row = await db.get_user_by_id(auth_req.user_id)
    if not user_row or not user_row.get("is_active", True):
        await store.delete_auth_request(auth_req.request_id)
        return pages.error_page("This account can't be connected.", status_code=403)

    raw_projects = [str(v) for v in form.getlist("project")]
    new_project = str(form.get("new_project") or "")
    raw_agent = str(form.get("agent_id") or "")
    try:
        projects = normalize_projects([*raw_projects, new_project])
        agent_id = normalize_agent_label(raw_agent)
    except ValueError as e:
        return await _consent(
            store,
            auth_req,
            client,
            auth_req.user_id,
            user_row.get("email", ""),
            error=str(e),
            selected=[p for p in raw_projects if p.strip()],
            agent_label=raw_agent.strip()[:64],
            status_code=400,
        )

    if not await store.delete_auth_request(auth_req.request_id):
        return pages.error_page("This request was already answered.", status_code=409)
    code = await store.create_grant_with_code(
        user_id=auth_req.user_id,
        client_id=auth_req.client_id,
        scopes=auth_req.scopes,
        resource=auth_req.resource,
        project_ids=projects,
        agent_id=agent_id,
        redirect_uri=auth_req.redirect_uri,
        code_challenge=auth_req.code_challenge,
    )
    log.info("oauth_consent_granted", user_id=auth_req.user_id, client_id=auth_req.client_id, projects=len(projects))
    response = _redirect_to_client(auth_req.redirect_uri, {"code": code, "state": auth_req.state}, settings)
    response.delete_cookie(_csrf_cookie_name(auth_req.request_id), path="/oauth/authorize")
    return response


# ---------------------------------------------------------------------------
# Token and revocation endpoints
# ---------------------------------------------------------------------------


async def _read_form(request: Request) -> FormData | None:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return None
    return await request.form()


async def _authenticate_client(request: Request, store: ConnectorStore, form: FormData) -> dict[str, Any]:
    """client_secret_basic, client_secret_post, or a public client (none)."""
    body_id = str(form.get("client_id") or "") or None
    body_secret = str(form.get("client_secret") or "") or None
    basic_id = basic_secret = None
    auth = request.headers.get("authorization", "")
    if auth[:6].lower() == "basic ":
        try:
            decoded = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
            raw_id, _, raw_secret = decoded.partition(":")
            basic_id, basic_secret = unquote(raw_id), unquote(raw_secret)
        except (binascii.Error, UnicodeDecodeError) as e:
            raise OAuthError("invalid_client", "Malformed Basic credentials.", 401) from e
        if body_id and body_id != basic_id:
            raise OAuthError("invalid_client", "client_id mismatch.", 401)
    client_id = basic_id or body_id
    client = await store.get_client(client_id)
    if client is None:
        raise OAuthError("invalid_client", "Unknown client.", 401)
    if not store.client_secret_valid(client, basic_secret or body_secret):
        raise OAuthError("invalid_client", "Client authentication failed.", 401)
    return client


@router.post("/oauth/token")
@limiter.limit("120/minute")
async def token(request: Request, store: StoreDep) -> JSONResponse:
    settings = get_settings()
    form = await _read_form(request)
    if form is None:
        return _oauth_error("invalid_request", "Use Content-Type: application/x-www-form-urlencoded.")
    try:
        client = await _authenticate_client(request, store, form)
        grant_type = form.get("grant_type")
        resource = form.get("resource")
        if resource is not None and not resource_matches(str(resource), resource_url(settings)):
            raise OAuthError("invalid_target", "resource does not name this server's MCP endpoint.")
        if grant_type == "authorization_code":
            code = str(form.get("code") or "")
            if not code:
                raise OAuthError("invalid_request", "code is required.")
            redirect = form.get("redirect_uri")
            grant, pair = await store.exchange_code(
                code=code,
                client_id=client["client_id"],
                redirect_uri=str(redirect) if redirect is not None else None,
                code_verifier=str(form.get("code_verifier") or "") or None,
            )
        elif grant_type == "refresh_token":
            refresh_token = str(form.get("refresh_token") or "")
            if not refresh_token:
                raise OAuthError("invalid_request", "refresh_token is required.")
            scope_raw = form.get("scope")
            try:
                scope = parse_scope(str(scope_raw)) if scope_raw else None
            except ScopeError as e:
                raise OAuthError("invalid_scope", str(e)) from e
            grant, pair = await store.refresh(refresh_token=refresh_token, client_id=client["client_id"], scope=scope)
        else:
            raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token.")
        if not await account_allows_grant(request.app.state.db, grant):
            await store.revoke_grant(grant.user_id, grant.grant_id, reason="account_sessions_invalidated")
            raise OAuthError("invalid_grant", "The authorization is no longer valid. Connect again.")
    except OAuthError as e:
        headers = {"WWW-Authenticate": "Basic"} if e.status_code == 401 else None
        return _oauth_error(e, headers=headers)
    log.info("oauth_token_issued", grant_type=grant_type, grant_id=grant.grant_id)
    return JSONResponse(pair.response(), headers=_NO_STORE)


@router.post("/oauth/revoke")
@limiter.limit("60/minute")
async def revoke(request: Request, store: StoreDep) -> Response:
    form = await _read_form(request)
    if form is None:
        return _oauth_error("invalid_request", "Use Content-Type: application/x-www-form-urlencoded.")
    try:
        client = await _authenticate_client(request, store, form)
    except OAuthError as e:
        return _oauth_error(e, headers={"WWW-Authenticate": "Basic"})
    token_value = str(form.get("token") or "")
    if not token_value:
        return _oauth_error("invalid_request", "token is required.")
    await store.revoke_token(token_value, client["client_id"])
    # RFC 7009 2.2: 200 whether or not the token was valid.
    return Response(status_code=200, headers=_NO_STORE)


# ---------------------------------------------------------------------------
# Connection management for the signed-in user (dashboard JWT)
# ---------------------------------------------------------------------------


@connections_router.get("/connections", summary="Apps connected through the Claude/ChatGPT connector")
async def list_connections(request: Request, user: DashboardUser, store: StoreDep) -> dict[str, Any]:
    items = await store.list_grants(user["id"])
    return {"count": len(items), "connections": items, "scopes": SCOPE_DESCRIPTIONS}


@connections_router.delete("/connections/{connection_id}", summary="Disconnect an app (revokes all of its tokens)")
async def delete_connection(request: Request, connection_id: str, user: DashboardUser, store: StoreDep) -> dict[str, Any]:
    if not await store.revoke_grant(user["id"], connection_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    log.info("oauth_connection_revoked_by_user", user_id=user["id"], grant_id=connection_id)
    return {"connection_id": connection_id, "revoked": True}
