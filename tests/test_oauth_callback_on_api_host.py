"""Social sign-in completes when the dashboard is served by the API host itself.

The production image serves the dashboard from the API process (Dockerfile.cloud
copies dashboard/dist to /app/static). Social sign-in ends with a redirect to
``<public_dashboard_url>/oauth/callback#code=…``, but the SPA catch-all
reserved every path starting with ``oauth`` for the API and answered that page
with a JSON 404. It now serves the SPA for exactly that page, while the remote
MCP connector's OAuth routes (/oauth/authorize, /oauth/token, ...) keep working
and any other unknown /oauth/* path still 404s as JSON.

Built with the production app factory (tests.connector_harness: real
middleware, routers and connector).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.connector_harness import PUBLIC, connector_app, pkce

INDEX = "<!doctype html><title>Remembra</title><div id=root></div>"
GH_ID, GH_SECRET = "gh-client-id", "gh-client-secret-value"
GO_ID, GO_SECRET = "go-client-id.apps.googleusercontent.com", "go-client-secret-value"


def _static(tmp_path: Path) -> str:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "app.js").write_text("console.log(1)")
    return str(root)


def _single_host(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "static_dir": _static(tmp_path),
        "public_dashboard_url": PUBLIC,  # the API host serves the dashboard
        "github_client_id": GH_ID,
        "github_client_secret": GH_SECRET,
        "google_client_id": GO_ID,
        "google_client_secret": GO_SECRET,
    }
    options.update(overrides)
    return options


@pytest.mark.parametrize("connector_enabled", [True, False])
async def test_dashboard_oauth_callback_page_is_the_spa(tmp_path, connector_enabled: bool) -> None:
    overrides = _single_host(tmp_path, connector_enabled=connector_enabled)
    async with connector_app(tmp_path, **overrides) as h:
        for path in ("/oauth/callback", "/oauth/callback/", "/oauth/callback?from=login"):
            r = await h.http.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text)
            assert r.text == INDEX, path
        # Everything else under the reserved prefixes stays a JSON 404.
        for path in ("/oauth/callbackx", "/oauth/nope", "/api/v1/nope", "/mcpx", "/.well-known/nope"):
            r = await h.http.get(path)
            assert r.status_code == 404, path
            assert r.headers["content-type"].startswith("application/json"), path
        # Ordinary SPA routes and assets are unchanged.
        assert (await h.http.get("/verify-email")).text == INDEX
        assert (await h.http.get("/assets/app.js")).text == "console.log(1)"


async def test_connector_oauth_routes_are_not_shadowed(tmp_path) -> None:
    async with connector_app(tmp_path, **_single_host(tmp_path)) as h:
        meta = await h.http.get("/.well-known/oauth-authorization-server")
        assert meta.status_code == 200 and meta.json()["authorization_endpoint"] == PUBLIC + "/oauth/authorize"
        reg = await h.register()
        assert reg.status_code == 201, reg.text
        _, challenge = pkce()
        page = await h.authorize(reg.json()["client_id"], challenge)
        assert page.status_code == 200 and page.text != INDEX and "request_id" in page.text
        token = await h.token({"grant_type": "authorization_code", "code": "bogus"})
        assert token.status_code in (400, 401) and token.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("provider", ["github", "google"])
async def test_provider_round_trip_lands_on_the_spa(tmp_path, provider: str) -> None:
    """start -> provider (redirect_uri registered with the provider) -> callback -> dashboard page."""
    async with connector_app(tmp_path, **_single_host(tmp_path)) as h:
        providers = (await h.http.get("/api/v1/auth/providers")).json()["providers"]
        assert provider in {p["id"] for p in providers}

        start = await h.http.get(f"/api/v1/auth/oauth/{provider}/start")
        assert start.status_code == 302, start.text
        query = dict(parse_qsl(urlsplit(start.headers["location"]).query))
        # The exact redirect URI the owner registers with GitHub / Google (docs/DEPLOYING.md).
        assert query["redirect_uri"] == f"{PUBLIC}/api/v1/auth/oauth/{provider}/callback"

        # The user cancels at the provider: it redirects back to that URI.
        back = await h.http.get(
            f"/api/v1/auth/oauth/{provider}/callback", params={"error": "access_denied", "state": query["state"]}
        )
        assert back.status_code == 303, back.text
        target = urlsplit(back.headers["location"])
        assert f"{target.scheme}://{target.netloc}" == PUBLIC and target.path == "/oauth/callback"
        assert dict(parse_qsl(target.fragment))["error"] == "access_denied"

        # The browser follows it on the same host: the dashboard page loads.
        landing = await h.http.get(target.path)
        assert landing.status_code == 200 and landing.text == INDEX


def test_deploying_doc_lists_the_redirect_uris_the_code_sends(monkeypatch) -> None:
    """docs/DEPLOYING.md tells the owner what to register; it must match callback_url()."""
    import remembra.config as config_module
    from remembra.auth import social
    from tests.security_harness import make_settings

    doc = (Path(__file__).resolve().parents[1] / "docs" / "DEPLOYING.md").read_text()
    monkeypatch.setattr(config_module, "_settings", make_settings(public_url="https://api.remembra.dev"))
    for provider in ("github", "google"):
        uri = social.callback_url(provider)
        assert uri == f"https://api.remembra.dev/api/v1/auth/oauth/{provider}/callback"
        assert f"`{uri}`" in doc, provider
    assert "`user:email`" in doc and social.PROVIDERS["github"].scope == "user:email"
    assert "`openid email profile`" in doc and social.PROVIDERS["google"].scope == "openid email profile"


REAL_INDEX = """<!doctype html>
<html><head>
<script>
  (function () { document.documentElement.classList.add('dark'); })();
</script>
<script type="module" crossorigin src="/assets/index-abc.js"></script>
</head><body><div id="root"></div>
<script src="https://cdn.paddle.com/paddle/v2/paddle.js" onerror="console.warn('Paddle.js failed')"></script>
</body></html>"""


def _sha(source: str) -> str:
    import base64
    import hashlib

    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode() + "'"


def test_dashboard_csp_allows_exactly_this_index_inline_code() -> None:
    from remembra.main import dashboard_csp

    policy = dict(part.split(" ", 1) for part in dashboard_csp(REAL_INDEX).split("; "))
    script_src = policy["script-src"].split()
    inline = "\n  (function () { document.documentElement.classList.add('dark'); })();\n"
    assert script_src[0] == "'self'"
    assert _sha(inline) in script_src
    assert "'unsafe-hashes'" in script_src and _sha("console.warn('Paddle.js failed')") in script_src
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src and "*" not in script_src
    assert "https://cdn.paddle.com" in script_src
    assert policy["frame-ancestors"] == "'none'" and policy["object-src"] == "'none'"
    assert policy["connect-src"] == "'self' https://*.paddle.com"
    # Another inline script (e.g. injected into the file later) is not covered.
    assert _sha("alert(1)") not in script_src


async def test_dashboard_pages_carry_the_dashboard_csp_and_the_api_keeps_none(tmp_path) -> None:
    options = _single_host(tmp_path)
    (Path(options["static_dir"]) / "index.html").write_text(REAL_INDEX)
    async with connector_app(tmp_path, **options) as h:
        for path in ("/oauth/callback", "/verify-email", "/index.html", "/some/spa/route"):
            r = await h.http.get(path)
            assert r.status_code == 200 and r.text == REAL_INDEX, path
            csp = r.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "default-src 'self'" in csp, path
            assert r.headers["x-frame-options"] == "DENY"
        for path in ("/api/v1/auth/providers", "/.well-known/oauth-authorization-server", "/oauth/nope"):
            r = await h.http.get(path)
            assert r.headers["content-security-policy"].startswith("default-src 'none'"), path
        asset = await h.http.get("/assets/app.js")
        assert asset.status_code == 200 and "script-src" not in asset.headers["content-security-policy"]
