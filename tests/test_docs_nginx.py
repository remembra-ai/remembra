"""docs.remembra.dev's nginx (docs-nginx/nginx.conf, docs-nginx/remembra-headers.conf, docs.Dockerfile).

LIVE-4 / CLI-11: the docs image served the site with nginx's stock
default.conf: no HSTS, X-Frame-Options, nosniff, Referrer-Policy or CSP, the
nginx version in every error page, and dotfiles served if present. CTR-1: it
also ran as root on an unmaintained base (nginx 1.27).

Now the image carries its own config (unprivileged on 8080, like landing/),
the shared security headers, and a CSP that allows each inline script of the
built site by hash (scripts/docs_csp.py, run on every image build).
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "docs-nginx" / "nginx.conf"
HEADERS = ROOT / "docs-nginx" / "remembra-headers.conf"
DOCKERFILE = ROOT / "docs.Dockerfile"
DOCKERIGNORE = ROOT / "docs.Dockerfile.dockerignore"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


nginx = _load("site_nginx")
docs_csp = _load("docs_csp")


def _headers() -> dict[str, str]:
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'^add_header\s+(\S+)\s+"((?:[^"\\]|\\.)*)"\s+always;$', HEADERS.read_text(), re.M)
    }


def _csp() -> dict[str, list[str]]:
    out = {}
    for part in _headers()["Content-Security-Policy-Report-Only"].split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_the_security_headers_are_sent() -> None:
    headers = _headers()
    assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in headers["Permissions-Policy"] and "payment=()" in headers["Permissions-Policy"]


def test_csp_is_report_only_like_the_other_sites_and_allows_inline_script_only_by_hash() -> None:
    headers = _headers()
    assert "Content-Security-Policy" not in headers  # LIVE-3: enforce after a clean week of reports
    csp = _csp()
    assert csp["report-uri"] == ["https://api.remembra.dev/api/v1/csp-report"]
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert csp["default-src"] == ["'self'"]
    script_src = csp["script-src"]
    assert script_src[0] == "'self'"
    assert script_src[1:] and all(re.fullmatch(r"'sha256-[A-Za-z0-9+/]{43}='", w) for w in script_src[1:])
    assert csp["connect-src"] == ["'self'", "https://api.github.com"]


def test_every_location_that_adds_a_header_includes_the_shared_set() -> None:
    text = nginx._strip_comments(CONF.read_text())
    assert "include /etc/nginx/remembra-headers.conf;" in text.split("location")[0]
    for loc in nginx.parse(text):
        if loc.directive("add_header") is not None:
            assert ["include", "/etc/nginx/remembra-headers.conf"] in loc.body, loc.pattern


# ---------------------------------------------------------------------------
# nginx.conf
# ---------------------------------------------------------------------------


def test_the_config_hides_the_version_serves_the_mkdocs_404_and_no_dotfiles(tmp_path: Path) -> None:
    text = nginx._strip_comments(CONF.read_text())
    assert re.search(r"^\s*server_tokens off;$", text, re.M)
    assert re.search(r"^\s*error_page 404 /404\.html;$", text, re.M)
    assert re.search(r"^\s*absolute_redirect off;$", text, re.M)
    assert re.search(r"^\s*port_in_redirect off;$", text, re.M)

    site = tmp_path / "site"
    (site / "guides" / "python-sdk").mkdir(parents=True)
    (site / "guides" / "python-sdk" / "index.html").write_text("<p>sdk</p>")
    (site / "404.html").write_text("<p>not found</p>")
    (site / ".env").write_text("SECRET=1")
    (site / "assets").mkdir()
    (site / "assets" / "app.js").write_text("")
    locations = nginx.load(CONF)
    assert nginx.resolve("/guides/python-sdk/", locations, site).status == 200
    assert nginx.resolve("/.env", locations, site).status == 404
    assert nginx.resolve("/.git/config", locations, site).status == 404
    assert nginx.resolve("/404.html", locations, site).status == 404  # internal: only as the error page
    assert nginx.resolve("/missing/", locations, site).status == 404
    asset = nginx.resolve("/assets/app.js", locations, site)
    assert asset.status == 200 and asset.matched is not None and asset.matched.directive("add_header") is not None


def test_config_files_are_well_formed() -> None:
    from tests.test_landing_nginx import _syntax_problems

    assert _syntax_problems(CONF.read_text()) == []
    assert _syntax_problems(HEADERS.read_text()) == []


@pytest.mark.skipif(shutil.which("nginx") is None, reason="no nginx binary on PATH; the static checks above still ran")
def test_nginx_accepts_the_config(tmp_path: Path) -> None:
    binary = shutil.which("nginx")
    assert binary is not None
    mime = next(
        (p for p in (Path("/etc/nginx/mime.types"), Path("/opt/homebrew/etc/nginx/mime.types")) if p.is_file()),
        None,
    )
    if mime is None:
        pytest.skip("nginx is installed but its mime.types was not found")
    conf = CONF.read_text().replace("/etc/nginx/mime.types", str(mime)).replace("/etc/nginx/remembra-headers.conf", str(HEADERS))
    conf = conf.replace("/usr/share/nginx/html", str(tmp_path)).replace("/tmp/", f"{tmp_path}/")
    (tmp_path / "nginx.conf").write_text(conf)
    run = subprocess.run(
        [binary, "-t", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------


def test_the_image_installs_its_config_and_runs_unprivileged() -> None:
    docker = DOCKERFILE.read_text()
    conf = CONF.read_text()
    final = docker[docker.rindex("\nFROM ") :]
    assert "COPY docs-nginx/nginx.conf /etc/nginx/nginx.conf" in final
    assert "COPY --from=build /src/remembra-headers.conf /etc/nginx/remembra-headers.conf" in final
    assert "rm -f /etc/nginx/conf.d/default.conf" in final and "nginx -t" in final and "rm -rf /tmp/*" in final
    assert re.search(r"^USER nginx$", final, re.M)
    port = re.search(r"listen (\d+) default_server;", conf)
    assert port is not None and f"EXPOSE {port.group(1)}" in final and int(port.group(1)) >= 1024
    assert "pid /tmp/nginx.pid;" in conf
    assert "Ports Exposes 8080" in docker  # the Coolify setting the header comment documents


def test_the_build_installs_hash_pinned_tools_and_hashes_the_built_pages() -> None:
    docker = DOCKERFILE.read_text()
    build = docker[: docker.rindex("\nFROM ")]
    assert "pip install --no-cache-dir --require-hashes --no-deps -r docs-requirements.txt" in build
    assert "COPY .github/docs-requirements.txt ./docs-requirements.txt" in build
    assert re.search(r"mkdocs build --strict --site-dir /out \\\n\s+&& python docs_csp.py /out remembra-headers.conf", build)
    # Every file the build copies from the context is let through by its .dockerignore.
    allowed = [line[1:].rstrip("/") for line in DOCKERIGNORE.read_text().splitlines() if line.startswith("!")]
    for sources in re.findall(r"^COPY (?!--from)(.+) \S+$", docker, re.M):
        for source in sources.split():
            assert any(source == a or source.startswith(a + "/") for a in allowed), source


# ---------------------------------------------------------------------------
# scripts/docs_csp.py
# ---------------------------------------------------------------------------


def _site(tmp_path: Path, pages: dict[str, str]) -> Path:
    site = tmp_path / "site"
    for rel, html in pages.items():
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text(html)
    return site


def test_csp_script_hashes_every_executable_inline_script_and_nothing_else(tmp_path: Path) -> None:
    site = _site(
        tmp_path,
        {
            "index.html": (
                '<script>__md_scope=new URL(".",location)</script><script id="__config" type="application/json">{}</script>'
            ),
            "a/b/index.html": '<script>__md_scope=new URL("../..",location)</script><script src="../../assets/x.js"></script>',
        },
    )
    hashes, problems = docs_csp.scan_site(site)
    assert problems == []
    assert hashes == sorted(
        {docs_csp.script_hash('__md_scope=new URL(".",location)'), docs_csp.script_hash('__md_scope=new URL("../..",location)')}
    )
    # The browser hashes the exact UTF-8 text between the tags.
    assert docs_csp.script_hash("alert(1)") == "'sha256-bhHHL3z2vDgxUt0W3dWQOrprscmda2Y5pLsLg4GF+pI='"


def test_csp_script_rewrites_the_block_and_check_mode_fails_when_stale(tmp_path: Path) -> None:
    site = _site(tmp_path, {"index.html": "<script>var a=1</script>"})
    headers = tmp_path / "remembra-headers.conf"
    headers.write_text('add_header X-Frame-Options "DENY" always;\n# @csp\n# /@csp\n')
    assert docs_csp.main([str(site), str(headers), "--check"]) == 1
    assert docs_csp.main([str(site), str(headers)]) == 0
    assert docs_csp.script_hash("var a=1") in headers.read_text()
    assert docs_csp.main([str(site), str(headers), "--check"]) == 0
    (site / "index.html").write_text("<script>var a=2</script>")
    assert docs_csp.main([str(site), str(headers), "--check"]) == 1


def test_csp_script_refuses_what_no_hash_can_allow(tmp_path: Path) -> None:
    site = _site(
        tmp_path,
        {
            "index.html": '<a href="javascript:x()">x</a><button onclick="y()">y</button><script src="https://unpkg.com/m.js"></script>'
        },
    )
    _, problems = docs_csp.scan_site(site)
    assert len(problems) == 3
    headers = tmp_path / "h.conf"
    headers.write_text("# @csp\n# /@csp\n")
    assert docs_csp.main([str(site), str(headers)]) == 1


@pytest.mark.skipif(importlib.util.find_spec("mkdocs") is None, reason="MkDocs is not installed here; docs.yml runs this check")
def test_the_committed_csp_matches_a_fresh_build(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(tmp_path / "site")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert run.returncode == 0, run.stderr
    assert docs_csp.main([str(tmp_path / "site"), str(HEADERS), "--check"]) == 0
