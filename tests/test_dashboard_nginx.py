"""app.remembra.dev's nginx (dashboard/nginx.conf, dashboard/security-headers.conf).

The dashboard keeps its session token in localStorage, so a Content-Security-
Policy is its main defense against injected script, and framing it must be
refused. The policy has to allow exactly what the dashboard loads: its inline
theme bootstrap (by hash), Paddle.js and the Paddle checkout frame (live and
sandbox), Cloudflare Turnstile, Google Fonts, and the API over HTTPS and
WebSocket. These tests read those from the dashboard's own files.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
CONF = DASH / "nginx.conf"
HEADERS = DASH / "security-headers.conf"
INDEX = DASH / "index.html"


def _nginx():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("site_nginx", ROOT / "scripts" / "site_nginx.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_nginx"] = module
    spec.loader.exec_module(module)
    return module


def _headers() -> dict[str, str]:
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'^add_header\s+(\S+)\s+"((?:[^"\\]|\\.)*)"\s+always;$', HEADERS.read_text(), re.M)
    }


def _csp() -> dict[str, list[str]]:
    out = {}
    for part in _headers()["Content-Security-Policy"].split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def _inline_scripts(html: str) -> list[str]:
    return [
        m.group(2)
        for m in re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", html, re.S)
        if "ld+json" not in m.group(1)
    ]


def _hash(body: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


def test_security_headers_are_set_on_every_response() -> None:
    headers = _headers()
    assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in headers["Permissions-Policy"]
    csp = _csp()
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert "'unsafe-inline'" not in csp["script-src"] and "'unsafe-eval'" not in csp["script-src"]


def test_every_location_that_adds_a_header_includes_the_shared_set() -> None:
    nginx = _nginx()
    text = nginx._strip_comments(CONF.read_text())
    assert "include /etc/nginx/remembra-headers.conf;" in text.split("location")[0]
    for loc in nginx.parse(text):
        if loc.directive("add_header") is not None:
            assert ["include", "/etc/nginx/remembra-headers.conf"] in loc.body, loc.pattern
    docker = (DASH / "Dockerfile").read_text()
    assert "COPY security-headers.conf /etc/nginx/remembra-headers.conf" in docker
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" in docker


def test_csp_allows_the_inline_theme_script_by_its_hash_and_nothing_inline_else() -> None:
    html = INDEX.read_text()
    scripts = _inline_scripts(html)
    assert scripts, "index.html lost its theme bootstrap"
    hashes = [w for w in _csp()["script-src"] if w.startswith("'sha256-")]
    assert sorted(hashes) == sorted({_hash(s) for s in scripts}), f"expected {[_hash(s) for s in scripts]}"
    assert not re.search(r"\son[a-z]+=", html), "inline event handlers cannot run under this CSP"


def test_csp_allows_every_script_host_the_dashboard_loads() -> None:
    script_src = _csp()["script-src"]
    hosts = {
        urlsplit(src).scheme + "://" + urlsplit(src).netloc
        for src in re.findall(r'<script[^>]+src="(https://[^"]+)"', INDEX.read_text())
    }
    turnstile = re.search(r"TURNSTILE_SCRIPT_URL = '([^']+)'", (DASH / "src" / "lib" / "turnstile.ts").read_text())
    assert turnstile is not None
    hosts.add("https://" + urlsplit(turnstile.group(1)).netloc)
    assert hosts == {"https://cdn.paddle.com", "https://challenges.cloudflare.com"}
    for host in hosts:
        assert host in script_src, host


def test_csp_allows_the_paddle_checkout_and_turnstile_frames_live_and_sandbox() -> None:
    csp = _csp()
    for host in ("https://buy.paddle.com", "https://sandbox-buy.paddle.com", "https://challenges.cloudflare.com"):
        assert host in csp["frame-src"], host
    assert "https://*.paddle.com" in csp["connect-src"]
    assert "https://cdn.paddle.com" in csp["style-src"]


def test_csp_lets_the_dashboard_reach_the_api_it_is_built_for() -> None:
    api = re.search(r"ARG VITE_API_URL=(\S+)", (DASH / "Dockerfile").read_text())
    assert api is not None
    host = urlsplit(api.group(1)).netloc
    connect = _csp()["connect-src"]
    assert f"https://{host}" in connect and f"wss://{host}" in connect


def test_csp_allows_the_fonts_index_html_loads() -> None:
    csp = _csp()
    html = INDEX.read_text()
    assert "https://fonts.googleapis.com/css2" in html
    assert "https://fonts.googleapis.com" in csp["style-src"] and "https://fonts.gstatic.com" in csp["font-src"]


def test_spa_routes_like_pay_fall_back_to_index_html(tmp_path: Path) -> None:
    nginx = _nginx()
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc.js").write_text("x")
    locs = nginx.parse(CONF.read_text())
    for path in ("/pay?_ptxn=txn_01abcdefghij", "/verify-email", "/signup", "/"):
        res = nginx.follow(path, locs, tmp_path)
        assert (res.status, res.file) == (200, tmp_path / "index.html"), path
