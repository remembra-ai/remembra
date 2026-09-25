"""Pure validation rules for the remote MCP connector's OAuth server.

No I/O and no settings access here, so every rule is unit-testable and the
module can be imported from ``remembra.config`` without a cycle.

Sources for the rules (verified 2026-09-25):
- Claude connectors: https://claude.com/docs/connectors/building/authentication
  (S256 PKCE on every request, DCR fallback, redirect
  ``https://claude.ai/api/mcp/auth_callback``, Claude Code loopback on any port,
  refresh rotation for public clients, ``invalid_grant`` on dead refresh tokens).
- ChatGPT apps: https://developers.openai.com/apps-sdk/build/auth
  (``code_challenge_methods_supported`` must include S256, redirect
  ``https://chatgpt.com/connector_platform_oauth_redirect`` when the server
  returns ``iss``; ``https://chatgpt.com/connector/oauth/{callback_id}`` otherwise).
- MCP authorization spec 2025-11-25 (RFC 8707 resource, RFC 9728, RFC 8414).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

SCOPE_RECALL = "memory:recall"
SCOPE_STORE = "memory:store"
SCOPE_BRIEF = "session:brief"

SUPPORTED_SCOPES: tuple[str, ...] = (SCOPE_BRIEF, SCOPE_RECALL, SCOPE_STORE)

# Plain-language consent text for each scope (shown on the consent page).
SCOPE_DESCRIPTIONS: dict[str, str] = {
    SCOPE_BRIEF: "Read the pickup brief and the handoff/checkpoint trail your agents left",
    SCOPE_RECALL: "Search your memories",
    SCOPE_STORE: "Leave notes and send instructions to your agents' inboxes (no edits, no deletes)",
}

# Clients may ask for these; they are accepted and ignored because a refresh
# token is always issued (Claude appends offline_access when an AS lists it).
IGNORED_SCOPES = frozenset({"offline_access"})


class ScopeError(ValueError):
    """The requested scope string names an unsupported scope."""


def parse_scope(raw: str | None) -> list[str]:
    """Parse a space-delimited OAuth scope string.

    Missing/empty means every supported scope (MCP clients request what the
    protected-resource metadata advertises). Unknown scopes raise ScopeError.
    Returned in canonical order without duplicates.
    """
    requested = {s for s in (raw or "").split() if s and s not in IGNORED_SCOPES}
    if not requested:
        return list(SUPPORTED_SCOPES)
    unknown = sorted(requested - set(SUPPORTED_SCOPES))
    if unknown:
        raise ScopeError(f"unsupported scope: {' '.join(unknown)}")
    return [s for s in SUPPORTED_SCOPES if s in requested]


def format_scope(scopes: Iterable[str]) -> str:
    return " ".join(scopes)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_public_url(url: str) -> str:
    """Validate and canonicalize the configured public origin.

    HTTPS is required (OAuth 2.1 / MCP: all authorization endpoints over
    HTTPS) except for loopback hosts, which are allowed over HTTP for local
    development. No query or fragment; trailing slash removed.
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("https", "http") or not parts.hostname:
        raise ValueError(f"public_url must be an absolute http(s) URL, got {raw!r}")
    if parts.query or parts.fragment:
        raise ValueError("public_url must not contain a query or fragment")
    if parts.path not in ("", "/"):
        # The issuer's well-known documents and /mcp are served at the origin root.
        raise ValueError("public_url must be an origin without a path, e.g. https://api.example.com")
    if parts.scheme.lower() == "http" and not _is_loopback_host(parts.hostname):
        raise ValueError("public_url must use https (http is only allowed for localhost)")
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, "", "", ""))


def canonical_resource(url: str) -> str:
    """RFC 8707 comparison form: lower-case scheme/host, no trailing slash, no fragment."""
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def resource_matches(requested: str | None, expected: str) -> bool:
    """True when a client-sent ``resource`` names this MCP server."""
    if requested is None:
        return True
    return canonical_resource(requested) == canonical_resource(expected)


# ---------------------------------------------------------------------------
# Redirect URIs
# ---------------------------------------------------------------------------

# Hosted Claude apps (web, desktop, mobile, Cowork) — one fixed callback.
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
# ChatGPT, when the AS returns ``iss`` (we do).
CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
# ChatGPT legacy / fallback callback-ID form.
_CHATGPT_CALLBACK_RE = re.compile(r"^https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}$")

BUILTIN_REDIRECT_URIS: tuple[str, ...] = (CLAUDE_REDIRECT_URI, CHATGPT_REDIRECT_URI)


def is_loopback_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and _is_loopback_host(parts.hostname)


def redirect_uri_allowed(uri: str, extra: Iterable[str] = (), allow_loopback: bool = True) -> bool:
    """Whether dynamic client registration may register ``uri``.

    Only known client callbacks are accepted, so a registered client can never
    send authorization codes to an arbitrary site (open-redirect / code theft).
    """
    if not uri or len(uri) > 2048 or any(c.isspace() for c in uri):
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.scheme or not parts.hostname:
        return False
    if uri in BUILTIN_REDIRECT_URIS or _CHATGPT_CALLBACK_RE.match(uri):
        return True
    if uri in set(extra):
        return True
    return allow_loopback and is_loopback_redirect(uri) and not parts.username and not parts.password


def _without_port(uri: str) -> str:
    parts = urlsplit(uri)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def redirect_uri_matches(requested: str, registered: Iterable[str]) -> bool:
    """Exact-match a requested redirect URI against a client's registered set.

    Loopback URIs match with the port ignored (RFC 8252 section 7.3; Claude
    Code uses an ephemeral port). Everything else must match exactly.
    """
    registered = list(registered)
    if requested in registered:
        return True
    if is_loopback_redirect(requested):
        wanted = _without_port(requested)
        return any(is_loopback_redirect(r) and _without_port(r) == wanted for r in registered)
    return False


def redirect_origin(uri: str) -> str:
    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def redirect_host(uri: str) -> str:
    return urlsplit(uri).hostname or ""


def default_agent_label(redirect_uri: str) -> str:
    """Suggested agent id for the consent form, from where the client lives."""
    host = redirect_host(redirect_uri).lower()
    if host in ("claude.ai", "claude.com"):
        return "claude-app"
    if host == "chatgpt.com":
        return "chatgpt"
    return "mcp-client"


# ---------------------------------------------------------------------------
# PKCE (RFC 7636, S256 only)
# ---------------------------------------------------------------------------

_PKCE_VALUE_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # base64url(sha256) without padding


def valid_code_challenge(challenge: str | None) -> bool:
    return bool(challenge) and bool(_CHALLENGE_RE.match(challenge or ""))


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(verifier: str | None, challenge: str) -> bool:
    if not verifier or not _PKCE_VALUE_RE.match(verifier):
        return False
    return hmac.compare_digest(s256(verifier), challenge)


# ---------------------------------------------------------------------------
# Consent inputs
# ---------------------------------------------------------------------------

AGENT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_GRANT_PROJECTS = 20


def normalize_agent_label(raw: str | None) -> str:
    label = (raw or "").strip().lower()
    if not AGENT_LABEL_RE.match(label):
        raise ValueError("Agent label must be 1-64 characters: lowercase letters, digits, '.', '_' or '-'.")
    return label


def normalize_project(raw: str | None) -> str:
    """Server-side project ids are free-form (case preserved, whitespace -> '-')."""
    cleaned = "-".join((raw or "").split())
    if not cleaned:
        raise ValueError("Project id must not be empty.")
    if len(cleaned) > 128:
        raise ValueError("Project id must be at most 128 characters.")
    if any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        raise ValueError("Project id contains control characters.")
    return cleaned


def normalize_projects(raw: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in raw:
        if not (value or "").strip():
            continue
        project = normalize_project(value)
        if project not in seen:
            seen.append(project)
    if not seen:
        raise ValueError("Choose at least one project.")
    if len(seen) > MAX_GRANT_PROJECTS:
        raise ValueError(f"Choose at most {MAX_GRANT_PROJECTS} projects.")
    return seen


def token_hash(token: str) -> str:
    """Storage form of a bearer secret. Tokens are 256-bit random, so an
    unsalted SHA-256 cannot be brute-forced; lookups stay O(1)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
