"""Static checks for the marketing site in ``landing/`` (deployed by Vercel).

The site is plain HTML, so these tests guard what can silently break it:
internal links and #anchors must resolve under the ``vercel.json`` rules
(cleanUrls plus redirects), and the rebuilt home and pricing pages may only
load external assets from Google Fonts.
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

LANDING = Path(__file__).resolve().parent.parent / "landing"


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []  # (tag, attr, url)
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs if v is not None}
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "a" and "name" in a:
            self.ids.add(a["name"])
        for key in ("href", "src"):
            if key in a and not (tag == "link" and a.get("rel") in ("preconnect", "canonical")):
                self.refs.append((tag, key, a[key]))


def _parse(path: Path) -> _Collector:
    c = _Collector()
    c.feed(path.read_text(encoding="utf-8", errors="replace"))
    return c


def _redirect_patterns() -> list[re.Pattern[str]]:
    cfg = json.loads((LANDING / "vercel.json").read_text())
    out = []
    for r in cfg.get("redirects", []):
        src = re.sub(r":[a-z]+\*", ".*", r["source"])
        src = re.sub(r":[a-z]+", "[^/]+", src)
        out.append(re.compile("^" + src + "$"))
    return out


def _resolve(url_path: str, redirects: list[re.Pattern[str]]) -> Path | str | None:
    """Map a site path to the file Vercel serves (cleanUrls), 'redirect', or None."""
    if any(p.match(url_path) for p in redirects):
        return "redirect"
    rel = unquote(url_path).lstrip("/")
    candidates = [LANDING / "index.html"] if rel == "" else [LANDING / rel, LANDING / f"{rel}.html", LANDING / rel / "index.html"]
    return next((c for c in candidates if c.is_file()), None)


def _internal_link_problems() -> list[str]:
    redirects = _redirect_patterns()
    ids_cache: dict[Path, set[str]] = {}
    problems: list[str] = []
    for page in sorted(LANDING.rglob("*.html")):
        if "node_modules" in page.parts:
            continue
        rel_page = page.relative_to(LANDING).as_posix()
        for _tag, attr, url in _parse(page).refs:
            parts = urlsplit(url)
            if parts.scheme or url.startswith(("//", "mailto:", "tel:", "javascript:", "data:")):
                continue
            target: Path | str | None
            if url.startswith("#"):
                target, frag = page, url[1:]
            else:
                if parts.path.startswith("/"):
                    path = parts.path
                else:
                    base = "/" if page.parent == LANDING else "/" + page.parent.relative_to(LANDING).as_posix() + "/"
                    path = base + parts.path
                target = _resolve(path, redirects) if parts.path else page
                frag = parts.fragment
                if target is None:
                    problems.append(f"{rel_page}: {attr}={url} does not resolve")
                    continue
            if frag and isinstance(target, Path) and target.suffix == ".html":
                if target not in ids_cache:
                    ids_cache[target] = _parse(target).ids
                if frag not in ids_cache[target]:
                    problems.append(f"{rel_page}: {attr}={url} has no #{frag} target")
    return problems


def test_every_internal_link_resolves() -> None:
    assert _internal_link_problems() == []


def test_link_checker_catches_a_broken_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checker itself must fail on a missing page and a missing anchor."""
    (tmp_path / "vercel.json").write_text(json.dumps({"cleanUrls": True, "redirects": []}))
    (tmp_path / "index.html").write_text('<a href="/pricing">p</a><a href="/#nope">x</a><a href="/ok#here">y</a>')
    (tmp_path / "ok.html").write_text('<h2 id="here">ok</h2>')
    monkeypatch.setattr(sys.modules[__name__], "LANDING", tmp_path)
    problems = _internal_link_problems()
    assert problems == [
        "index.html: href=/pricing does not resolve",
        "index.html: href=/#nope has no #nope target",
    ]


@pytest.mark.parametrize("page", ["index.html", "pricing.html"])
def test_new_pages_load_external_assets_only_from_google_fonts(page: str) -> None:
    allowed = ("https://fonts.googleapis.com/", "https://fonts.gstatic.com/")
    external_assets = [
        url
        for tag, attr, url in _parse(LANDING / page).refs
        if (tag in ("script", "img", "link", "iframe") and url.startswith(("http:", "https:", "//")))
        and not (tag == "link" and attr == "href" and url.startswith(allowed))
    ]
    assert external_assets == []


def test_install_line_is_flagged_until_relay_ships_on_pypi() -> None:
    html = (LANDING / "index.html").read_text()
    installs = html.count("pipx install remembra</span>")
    assert installs == 2
    assert html.count("<!-- requires PyPI release with remembra-relay -->") == installs


def test_signup_links_point_at_the_dashboard_signup_route() -> None:
    for page in ("index.html", "pricing.html"):
        hrefs = [url for tag, _a, url in _parse(LANDING / page).refs if tag == "a"]
        assert "https://app.remembra.dev/signup" in hrefs, page
