"""remembra.dev is served by nginx from landing/ (landing/Dockerfile, landing/nginx.conf).

No nginx runs in CI, so ``scripts/site_nginx.py`` applies nginx's location
rules to the config. These tests first hold that model to nginx's documented
matching rules, then hold the real config to what the site needs:

* old and outside paths redirect somewhere that exists (/signup and
  /dashboard to the dashboard with the query string kept, /docs to the docs
  site, retired release pages to their anchor on /changelog);
* every page answers on its clean URL, and the .html form redirects to it;
* every response carries the security headers, and the CSP allows exactly the
  inline scripts the pages contain (``scripts/site_csp.py``);
* /.well-known/security.txt is valid under RFC 9116;
* the Dockerfile installs the config where nginx.conf looks for it and runs
  as the unprivileged nginx user.

When an nginx binary is on PATH, ``nginx -t`` also checks the syntax for real.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from urllib.parse import urljoin, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing"
SCRIPTS = ROOT / "scripts"
CONF = LANDING / "nginx.conf"
HEADERS = LANDING / "remembra-headers.conf"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


nginx = _load("site_nginx")
csp = _load("site_csp")
predeploy = _load("site_predeploy")
partials = _load("site_partials")


# ---------------------------------------------------------------------------
# The model follows nginx's own matching rules
# ---------------------------------------------------------------------------

MODEL_CONF = """
server {
    location / { try_files $uri $uri.html =404; }
    location /docs/ { return 302 /prefix; }
    location ^~ /static/ { return 302 /caret; }
    location ~ ^/static/x\\.css$ { return 302 /regex-static; }
    location ~ ^/docs/(.*)$ { return 301 "https://docs.example/$1#top"; }
    location ~ ^/docs/a$ { return 302 /second-regex; }
    location = /docs/exact { return 302 /exact; }
    location /docs/deeper/ { return 302 /longer-prefix; }
    location = /gone { internal; }
    location ^~ /old/ { rewrite ^/old/(.*)$ https://new.example/$1 permanent; }
}
"""


@pytest.fixture
def site(tmp_path: Path) -> Path:
    (tmp_path / "page.html").write_text("<p>page</p>")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "index.html").write_text("<p>dir</p>")
    return tmp_path


def test_model_exact_match_wins_over_everything(site: Path) -> None:
    locs = nginx.parse(MODEL_CONF)
    assert nginx.resolve("/docs/exact", locs, site).location == "/exact"


def test_model_caret_prefix_stops_the_regex_search(site: Path) -> None:
    locs = nginx.parse(MODEL_CONF)
    assert nginx.resolve("/static/x.css", locs, site).location == "/caret"


def test_model_first_matching_regex_beats_any_plain_prefix(site: Path) -> None:
    locs = nginx.parse(MODEL_CONF)
    # /docs/deeper/ is the longest plain prefix, but a regex matches first and wins.
    assert nginx.resolve("/docs/deeper/x", locs, site).location == "https://docs.example/deeper/x#top"
    # Two regexes match /docs/a: the first one in the file wins.
    assert nginx.resolve("/docs/a", locs, site).location == "https://docs.example/a#top"


def test_model_longest_plain_prefix_when_no_regex_matches(site: Path) -> None:
    locs = nginx.parse(
        "server { location / { return 302 /root; } location /a/ { return 302 /a; } location /a/b/ { return 302 /ab; } }"
    )
    assert nginx.resolve("/a/b/c", locs, site).location == "/ab"
    assert nginx.resolve("/a/x", locs, site).location == "/a"
    assert nginx.resolve("/z", locs, site).location == "/root"


def test_model_reads_hash_inside_a_token_as_text_not_a_comment(site: Path) -> None:
    locs = nginx.parse("server { location = /c { return 301 /privacy#cookies; } # a real comment { }\n}")
    assert nginx.resolve("/c", locs, site).location == "/privacy#cookies"


def test_model_internal_rewrite_and_try_files(site: Path) -> None:
    locs = nginx.parse(MODEL_CONF)
    assert nginx.resolve("/gone", locs, site).status == 404
    moved = nginx.resolve("/old/a/b?x=1", locs, site)
    assert (moved.status, moved.location) == (301, "https://new.example/a/b?x=1")
    page = nginx.resolve("/page", locs, site)
    assert page.status == 200 and page.file == site / "page.html"
    assert nginx.resolve("/missing", locs, site).status == 404


def test_model_return_keeps_the_query_string_only_when_asked(site: Path) -> None:
    locs = nginx.parse(
        "server { location = /a { return 301 https://x.example/$is_args$args; } location = /b { return 301 /c; } }"
    )
    assert nginx.resolve("/a?checkout=success", locs, site).location == "https://x.example/?checkout=success"
    assert nginx.resolve("/a", locs, site).location == "https://x.example/"
    assert nginx.resolve("/b?q=1", locs, site).location == "/c"


# ---------------------------------------------------------------------------
# What remembra.dev sends
# ---------------------------------------------------------------------------

EXPECTED = [
    # path, status, redirect target or served file
    ("/", 200, "index.html"),
    ("/pricing", 200, "pricing.html"),
    ("/refunds", 200, "refunds.html"),
    ("/subprocessors", 200, "subprocessors.html"),
    ("/dpa", 200, "dpa.html"),
    ("/changelog", 200, "changelog.html"),
    ("/blog", 200, "blog/index.html"),
    ("/blog/remembra-vs-mem0-vs-zep", 200, "blog/remembra-vs-mem0-vs-zep.html"),
    ("/.well-known/security.txt", 200, ".well-known/security.txt"),
    ("/site.css", 200, "site.css"),
    ("/pricing.html", 301, "/pricing"),
    ("/pricing/", 301, "/pricing"),
    ("/index.html", 301, "/"),
    ("/blog/index.html", 301, "/blog"),
    ("/blog/", 301, "/blog"),
    ("/signup", 301, "https://app.remembra.dev/signup"),
    ("/signup.html?ref=hn", 301, "https://app.remembra.dev/signup?ref=hn"),
    ("/dashboard?checkout=success", 301, "https://app.remembra.dev/?checkout=success"),
    ("/dashboard.html", 301, "https://app.remembra.dev/"),
    ("/docs", 301, "https://docs.remembra.dev/"),
    ("/docs/guides/relay/", 301, "https://docs.remembra.dev/guides/relay/"),
    ("/refund", 301, "/refunds"),
    ("/cookies", 301, "/privacy#cookies"),
    ("/relay", 302, "/"),
    ("/changelog/v0.13.2", 301, "/changelog#v0.13.2"),
    ("/changelog/v0.7.2.html", 301, "/changelog#v0.7.2"),
    ("/changelog/mcp-server", 301, "https://docs.remembra.dev/integrations/mcp-server/"),
    ("/blog/getting-started-mcp-memory", 301, "https://docs.remembra.dev/getting-started/agent-setup/"),
    ("/blog/self-hosting-ai-memory.html", 301, "https://docs.remembra.dev/getting-started/docker/"),
    ("/no-such-page", 404, None),
    ("/404", 404, None),
    ("/404.html", 404, None),
    ("/.git/config", 404, None),
    ("/.env", 404, None),
    ("/nginx.conf", 404, None),
    ("/Dockerfile", 404, None),
]


@pytest.mark.parametrize(("path", "status", "target"), EXPECTED)
def test_remembra_dev_answers(path: str, status: int, target: str | None) -> None:
    res = nginx.resolve(path, nginx.load(CONF), _web_root())
    assert res.status == status, (path, res)
    if status == 200:
        assert res.file == _web_root() / str(target)
    elif target is not None:
        assert res.location == target


def _web_root() -> Path:
    """landing/ as the image serves it: the Dockerfile deletes its build files from the web root."""
    return _IMAGE_ROOT


def _image_root(tmp: Path) -> Path:
    root = tmp / "html"
    shutil.copytree(LANDING, root, ignore=shutil.ignore_patterns("node_modules"))
    removed = re.search(r"rm -f ([^\\\n&]*?)\s*\\", (LANDING / "Dockerfile").read_text())
    assert removed is not None, "the Dockerfile no longer removes the build files from the web root"
    for name in removed.group(1).split():
        (root / name).unlink(missing_ok=True)
    return root


_IMAGE_ROOT: Path = LANDING


@pytest.fixture(autouse=True, scope="module")
def _image(tmp_path_factory: pytest.TempPathFactory) -> None:
    global _IMAGE_ROOT
    _IMAGE_ROOT = _image_root(tmp_path_factory.mktemp("image"))


def test_the_image_web_root_holds_no_build_files() -> None:
    for name in ("nginx.conf", "remembra-headers.conf", "Dockerfile", "package.json"):
        assert not (_web_root() / name).exists(), name


def test_every_page_answers_on_its_clean_url_and_its_html_url_redirects_there() -> None:
    locs = nginx.load(CONF)
    for page in sorted(_web_root().rglob("*.html")):
        rel = page.relative_to(_web_root()).as_posix()
        if rel == "404.html":
            continue
        clean = "/" + re.sub(r"(^|/)index\.html$", "", rel).removesuffix(".html")
        clean = clean.rstrip("/") or "/"
        res = nginx.resolve(clean, locs, _web_root())
        assert (res.status, res.file) == (200, page), rel
        moved = nginx.resolve("/" + rel, locs, _web_root())
        assert (moved.status, moved.location) == (301, clean), rel


def _redirects() -> list[tuple[str, str]]:
    """(location pattern, target) for every return/rewrite in nginx.conf."""
    out = []
    for loc in nginx.load(CONF):
        ret = loc.directive("return")
        if ret and ret[0].startswith("30") and len(ret) > 1:
            out.append((loc.pattern, ret[1]))
        rw = loc.directive("rewrite")
        if rw:
            out.append((loc.pattern, rw[1]))
    return out


def test_every_redirect_lands_on_a_page_that_exists() -> None:
    locs = nginx.load(CONF)
    redirects = _redirects()
    assert len(redirects) >= 20
    for pattern, target in redirects:
        target = re.sub(r"\$(is_args|args|\d)", "", target)
        parts = urlsplit(target)
        if parts.scheme:
            assert parts.hostname in ("docs.remembra.dev", "app.remembra.dev"), (pattern, target)
            if parts.hostname == "docs.remembra.dev" and parts.path.strip("/"):
                assert predeploy.source_for(f"https://docs.remembra.dev{parts.path}") is not None, (pattern, target)
            continue
        if not target:  # "/changelog#$1": checked per release below
            continue
        res = nginx.follow(parts.path or "/", locs, _web_root())
        assert res.status == 200, (pattern, target)
        if parts.fragment:
            assert f'id="{parts.fragment}"' in Path(res.file).read_text(), (pattern, target)


# Hosts a redirect from remembra.dev may send a visitor to.
OUR_HOSTS = {"remembra.dev", "app.remembra.dev", "docs.remembra.dev"}


def _browser_host(location: str, base: str = "https://remembra.dev/some/page") -> str | None:
    """The host a browser goes to for ``Location: <location>`` on ``base``.

    Browsers parse Location with the WHATWG URL rules, which differ from
    RFC 3986 in the two ways that matter here: tabs and newlines anywhere in
    the value are dropped, and for http(s) a backslash is a slash. So
    ``/\\evil.com`` and ``/<tab>/evil.com`` both mean ``//evil.com``.
    """
    control_or_space = "".join(chr(c) for c in range(0x21))
    cleaned = re.sub(r"[\t\n\r]", "", location).strip(control_or_space).replace("\\", "/")
    return urlsplit(urljoin(base, cleaned)).hostname


def _hostile_paths() -> list[str]:
    """Encoded paths that try to turn a clean-URL redirect into a link to another site."""
    tricks = ["%5C", "%5C%5C", "%09/", "%09%09", "%0A/", "%0D/", "%20/", "/", "%2F", "%2F%5C", "%5C%2F", "%09%5C"]
    parents = ["", "/blog", "/changelog", "/docs", "/pricing"]
    endings = ["/", ".html", "/index.html", ""]
    return [f"{parent}/{trick}evil.com{end}" for parent in parents for trick in tricks for end in endings]


def test_browser_host_reads_location_the_way_browsers_do() -> None:
    assert _browser_host("/pricing") == "remembra.dev"
    assert _browser_host("https://docs.remembra.dev/x") == "docs.remembra.dev"
    assert _browser_host("/\\evil.com") == "evil.com"
    assert _browser_host("/\t/evil.com") == "evil.com"
    assert _browser_host("//evil.com") == "evil.com"
    assert _browser_host("/a//evil.com") == "remembra.dev"
    assert _browser_host(" \x01/\\evil.com") == "evil.com"
    assert _browser_host("/-x-") == "remembra.dev"


@pytest.mark.parametrize("path", _hostile_paths())
def test_no_path_redirects_off_the_site(path: str) -> None:
    res = nginx.resolve(path, nginx.load(CONF), _web_root())
    if res.status in (301, 302, 307, 308):
        assert res.location is not None
        assert _browser_host(res.location) in OUR_HOSTS, (path, res.location)


def test_the_encoded_backslash_and_tab_redirects_are_closed() -> None:
    # The report: each of these used to answer 301 with a Location a browser reads as evil.com.
    locs = nginx.load(CONF)
    for path in ("/%5Cevil.com/", "/%09/evil.com/", "/%5Cevil.com.html", "/%5Cevil.com/index.html", "/%09/evil.com.html"):
        res = nginx.resolve(path, locs, _web_root())
        assert res.status == 404, (path, res)
    # The old patterns, to show the check above would have caught them.
    old = nginx.parse("server { location ~ ^(/.+)/$ { return 301 $1$is_args$args; } }")
    moved = nginx.resolve("/%5Cevil.com/", old, _web_root())
    assert moved.status == 301 and _browser_host(str(moved.location)) == "evil.com"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed; the Python reading above still ran")
def test_browser_host_agrees_with_a_real_whatwg_url_parser() -> None:
    samples = ["/pricing", "/\\evil.com", "/\t/evil.com", "/\t\\evil.com", "//evil.com", "/a//evil.com", "/changelog#v0.16.0"]
    script = "for (const l of JSON.parse(process.argv[1])) console.log(new URL(l, 'https://remembra.dev/some/page').hostname)"
    run = subprocess.run(["node", "-e", script, json.dumps(samples)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout.split() == [_browser_host(s) for s in samples]


def test_every_retired_release_page_has_its_anchor_on_the_changelog() -> None:
    changelog = (LANDING / "changelog.html").read_text()
    for version in (
        "v0.7.0",
        "v0.7.1",
        "v0.7.2",
        "v0.8.2",
        "v0.9.0",
        "v0.10.0",
        "v0.10.1",
        "v0.12.0",
        "v0.13.0",
        "v0.13.1",
        "v0.13.2",
    ):
        res = nginx.resolve(f"/changelog/{version}.html", nginx.load(CONF), _web_root())
        assert res.location == f"/changelog#{version}"
        assert f'id="{version}"' in changelog, version


def test_changelog_leads_with_v0_16_0() -> None:
    changelog = (LANDING / "changelog.html").read_text()
    first = re.search(r'<article class="rel[^"]*" id="([^"]+)"', changelog)
    assert first is not None and first.group(1) == "v0.16.0"
    assert "Remembra Relay" in changelog[first.start() : first.start() + 600]
    assert 'href="/site.css"' in changelog and "tailwindcss" not in changelog


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

REQUIRED_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _headers() -> dict[str, str]:
    out = {}
    for m in re.finditer(r'^add_header\s+(\S+)\s+"([^"]*)"\s+always;$', HEADERS.read_text(), re.M):
        out[m.group(1)] = m.group(2)
    return out


def test_every_security_header_is_set_on_every_response() -> None:
    headers = _headers()
    for name, value in REQUIRED_HEADERS.items():
        assert headers.get(name) == value, name
    assert "camera=()" in headers["Permissions-Policy"] and "microphone=()" in headers["Permissions-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    # Every add_header line uses "always", so 3xx and 4xx responses carry them too.
    assert all(line.rstrip().endswith("always;") for line in HEADERS.read_text().splitlines() if line.startswith("add_header"))


def test_every_location_that_adds_a_header_includes_the_shared_set() -> None:
    conf = nginx._strip_comments(CONF.read_text())
    server = conf[conf.index("server {") :]
    assert "include /etc/nginx/remembra-headers.conf;" in server.split("location")[0]
    for loc in nginx.load(CONF):
        if loc.directive("add_header") is not None:
            assert ["include", "/etc/nginx/remembra-headers.conf"] in loc.body, loc.pattern


def test_csp_is_up_to_date_and_the_pages_need_nothing_it_forbids() -> None:
    assert csp.policy_problems() == []
    assert csp.render(HEADERS.read_text()) == HEADERS.read_text(), "run python scripts/site_csp.py"
    policy = _headers()["Content-Security-Policy"]
    script_src = re.search(r"script-src ([^;]+)", policy)
    assert script_src is not None
    assert "'unsafe-inline'" not in script_src.group(1) and "'unsafe-eval'" not in script_src.group(1)
    # Every inline script on every page is allowed by its hash, and nothing else is.
    wanted = {csp.script_hash(body) for page in csp.served_pages() for body in csp.scan(page).inline}
    assert set(re.findall(r"'sha256-[^']+'", script_src.group(1))) == wanted
    assert len(wanted) >= 4  # theme bootstrap, pricing toggle, contact, crew, home


def test_csp_hash_is_the_browser_hash_of_the_exact_script_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # sha256 of "alert(1)" in base64, as a browser computes it for <script>alert(1)</script>.
    assert csp.script_hash("alert(1)") == "'sha256-bhHHL3z2vDgxUt0W3dWQOrprscmda2Y5pLsLg4GF+pI='"
    (tmp_path / "a.html").write_text(
        '<script>alert(1)</script><script type="application/ld+json">{"a": 1}</script>'
        '<script type="module">x()</script><script src="/site.js"></script>'
    )
    monkeypatch.setattr(csp, "LANDING", tmp_path)
    assert csp.hashes() == sorted({csp.script_hash("alert(1)"), csp.script_hash("x()")})
    assert csp.policy_problems() == []


def test_csp_check_rejects_what_a_hash_cannot_allow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.html").write_text(
        '<button onclick="go()">x</button><a href="javascript:void(0)">y</a><script src="https://cdn.example/x.js"></script>'
    )
    monkeypatch.setattr(csp, "LANDING", tmp_path)
    assert csp.policy_problems() == [
        "a.html: inline event handler onclick= on <button>",
        "a.html: javascript: URL in href= on <a>",
        "a.html: <script src=https://cdn.example/x.js> loads a script from another host",
    ]


def test_every_outside_host_in_the_csp_is_a_named_subprocessor() -> None:
    policy = _headers()["Content-Security-Policy"]
    subs = (LANDING / "subprocessors.html").read_text()
    hosts = set(re.findall(r"https://([a-z0-9.-]+)", policy))
    assert hosts == {"fonts.googleapis.com", "fonts.gstatic.com", "formsubmit.co"}
    assert "Google Fonts" in subs and "formsubmit.co" in subs
    form = re.search(r'<form[^>]+action="https://([^/"]+)/', (LANDING / "contact.html").read_text())
    assert form is not None and form.group(1) in hosts


# ---------------------------------------------------------------------------
# security.txt (RFC 9116)
# ---------------------------------------------------------------------------


def _security_txt() -> dict[str, str]:
    fields = {}
    for line in (LANDING / ".well-known" / "security.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition(": ")
            fields[key] = value
    return fields


def test_security_txt_has_the_required_fields_and_a_valid_expiry() -> None:
    fields = _security_txt()
    assert fields["Contact"].startswith("mailto:")
    expires = datetime.fromisoformat(fields["Expires"].replace("Z", "+00:00"))
    now = datetime.now(UTC)
    # RFC 9116 2.5.5: in the future, and less than a year out is recommended. Renew before it lapses.
    assert now < expires <= now + timedelta(days=366), "renew landing/.well-known/security.txt"
    assert fields["Canonical"] == "https://remembra.dev/.well-known/security.txt"
    assert fields["Policy"] == "https://remembra.dev/security#disclosure"
    assert 'id="disclosure"' in (LANDING / "security.html").read_text()
    assert fields["Contact"].removeprefix("mailto:") in (LANDING / "security.html").read_text()


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------


def test_dockerfile_installs_the_config_where_nginx_reads_it_and_runs_unprivileged() -> None:
    docker = (LANDING / "Dockerfile").read_text()
    conf = CONF.read_text()
    assert "COPY nginx.conf /etc/nginx/nginx.conf" in docker
    for include in re.findall(r"include (/etc/nginx/remembra-[\w.-]+);", conf):
        assert f"COPY {Path(include).name} {include}" in docker, include
    assert re.search(r"^USER nginx$", docker, re.M)
    port = re.search(r"listen (\d+) default_server;", conf)
    assert port is not None and f"EXPOSE {port.group(1)}" in docker
    assert int(port.group(1)) >= 1024  # an unprivileged user cannot bind below 1024
    assert "pid /tmp/nginx.pid;" in conf
    assert "rm -f /etc/nginx/conf.d/default.conf" in docker


def _syntax_problems(text: str) -> list[str]:
    """Brace balance and statement termination, the way nginx's tokenizer sees them."""
    problems, depth = [], 0
    for n, raw in enumerate(nginx._strip_comments(text).splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        depth += line.count("{") - line.count("}")
        if depth < 0:
            problems.append(f"line {n}: unbalanced }}")
        if not line.endswith(("{", "}", ";")):
            problems.append(f"line {n}: statement does not end with ;")
    if depth != 0:
        problems.append("unbalanced braces")
    return problems


def test_config_files_are_well_formed() -> None:
    assert _syntax_problems(CONF.read_text()) == []
    assert _syntax_problems(HEADERS.read_text()) == []
    assert _syntax_problems("server {\n  listen 80\n}\n") == ["line 2: statement does not end with ;"]
    assert _syntax_problems("server {\n") == ["unbalanced braces"]


@pytest.mark.skipif(shutil.which("nginx") is None, reason="no nginx binary on PATH; the static checks above still ran")
def test_nginx_accepts_the_config(tmp_path: Path) -> None:
    binary = shutil.which("nginx")
    assert binary is not None
    mime = next(
        (
            p
            for p in (
                Path("/etc/nginx/mime.types"),
                Path("/opt/homebrew/etc/nginx/mime.types"),
                Path("/usr/local/etc/nginx/mime.types"),
            )
            if p.is_file()
        ),
        None,
    )
    if mime is None:
        pytest.skip("nginx is installed but its mime.types was not found")
    conf = CONF.read_text().replace("/etc/nginx/mime.types", str(mime)).replace("/etc/nginx/remembra-headers.conf", str(HEADERS))
    conf = conf.replace("/usr/share/nginx/html", str(LANDING)).replace("/tmp/", f"{tmp_path}/")
    (tmp_path / "nginx.conf").write_text(conf)
    run = subprocess.run(
        [binary, "-t", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr


# ---------------------------------------------------------------------------
# The shell every page shares
# ---------------------------------------------------------------------------


def test_every_page_footer_links_the_legal_pages() -> None:
    for name in partials.PAGES:
        page = (LANDING / name).read_text()
        footer = page[page.index("<!-- @footer -->") :]
        for href in ("/privacy", "/terms", "/refunds", "/subprocessors", "/dpa", "/security"):
            assert f'href="{href}"' in footer, (name, href)


def test_the_404_page_is_not_indexed_and_links_home() -> None:
    page = (LANDING / "404.html").read_text()
    assert '<meta name="robots" content="noindex">' in page
    assert 'href="/"' in page and 'href="/site.css"' in page  # absolute: it is served at any path
