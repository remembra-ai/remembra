# ruff: noqa: E501  (inline SVG and HTML templates read better unwrapped)
"""Keep the shared head, header and footer identical across the site's pages.

The marketing site (landing/) is plain static HTML with no build step, so
each page carries its own copy of the shared blocks. This script rewrites
the regions between these markers in every page listed in PAGES:

    <!-- @head -->   ... <!-- /@head -->     favicons, fonts, theme bootstrap, site.css
    <!-- @header --> ... <!-- /@header -->   skip link, lockup, primary nav, mobile menu
    <!-- @footer --> ... <!-- /@footer -->   terrain strip, lockup, footer links

and fills brand snippets wherever a page asks for them (any number of times):

    <!-- @lockup --><!-- /@lockup -->       horizontal lockup, decorative
    <!-- @mark --><!-- /@mark -->           vector brain mark, decorative
    <!-- @pixbrain --><!-- /@pixbrain -->   the 32 px pixel brain, decorative

The current page is marked from the body's data-page attribute.

    python scripts/site_partials.py          # rewrite in place
    python scripts/site_partials.py --check  # exit 1 if any page is out of date
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

LANDING = Path(__file__).resolve().parents[1] / "landing"
PAGES = ["index.html", "pricing.html", "crew.html", "about.html", "contact.html", "security.html", "privacy.html", "terms.html"]

DOCS = "https://docs.remembra.dev"
GITHUB = "https://github.com/remembra-ai/remembra"
SIGNUP = "https://app.remembra.dev/signup"

HEAD = """<script>
  /* Dark for every visitor unless they chose light. Storage may be unavailable; the page works without it. */
  (function () { var t = "dark"; try { if (localStorage.getItem("remembra-theme") === "light") t = "light"; } catch (e) {} document.documentElement.setAttribute("data-theme", t); })();
</script>
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<meta name="theme-color" content="#131416">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Hanken+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="site.css">"""

SUN = (
    '<svg class="sun" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
    '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6'
    'M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6" stroke-linecap="round"/></svg>'
)
MOON = (
    '<svg class="moon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
    '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" stroke-linejoin="round"/></svg>'
)
BARS = (
    '<svg class="bars" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
    '<path d="M4 7h16M4 12h16M4 17h16" stroke-linecap="square"/></svg>'
)
CROSS = (
    '<svg class="x" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
    '<path d="M6 6l12 12M18 6L6 18" stroke-linecap="square"/></svg>'
)

# (key, label, href, extra classes)
NAV = [
    ("how", "How it works", "/#how", ""),
    ("crew", "Crew mode", "/crew", ""),
    ("pricing", "Pricing", "/pricing", ""),
    ("docs", "Docs", DOCS, " nav-hide-lg"),
    ("github", "GitHub", GITHUB, " nav-hide-lg"),
]
MENU = [
    ("how", "How it works", "/#how"),
    ("crew", "Crew mode", "/crew"),
    ("pricing", "Pricing", "/pricing"),
    ("docs", "Docs", DOCS),
    ("github", "GitHub", GITHUB),
    ("security", "Security", "/security"),
    ("contact", "Contact", "/contact"),
]
FOOT = [
    ("docs", "Docs", DOCS),
    ("github", "GitHub", GITHUB),
    ("pricing", "Pricing", "/pricing"),
    ("crew", "Crew mode", "/crew"),
    ("security", "Security", "/security"),
    ("privacy", "Privacy", "/privacy"),
    ("terms", "Terms", "/terms"),
    ("contact", "Contact", "/contact"),
]


def lockup() -> str:
    svg = (LANDING / "brand" / "partials" / "lockup-inline.svg").read_text().strip()
    return svg.replace('role="img" aria-label="Remembra"', 'aria-hidden="true" focusable="false"')


def cur(key: str, page: str) -> str:
    return ' aria-current="page"' if key == page else ""


def header(page: str) -> str:
    links = "\n      ".join(
        f'<a class="nav-link{cls}" href="{href}"{cur(key, page)}>{label}</a>' for key, label, href, cls in NAV
    )
    menu = "\n    ".join(f'<a href="{href}"{cur(key, page)}>{label}</a>' for key, label, href in MENU)
    return f"""<a class="skip" href="#main">Skip to content</a>
<header class="site-header">
  <div class="wrap bar">
    <a class="brand" href="/" aria-label="Remembra home">{lockup()}</a>
    <nav class="nav-links" aria-label="Primary">
      {links}
      <button class="icon-btn theme-btn" type="button" data-theme-toggle aria-label="Switch to light theme">{SUN}{MOON}</button>
      <button class="icon-btn menu-btn" type="button" data-menu-toggle aria-expanded="false" aria-controls="site-menu" aria-label="Open menu">{BARS}{CROSS}</button>
      <a class="btn-nav" href="{SIGNUP}">Start free</a>
    </nav>
  </div>
  <nav class="menu-panel" id="site-menu" aria-label="Menu">
    {menu}
  </nav>
</header>"""


def footer(page: str) -> str:
    links = "\n        ".join(f'<li><a href="{href}"{cur(key, page)}>{label}</a></li>' for key, label, href in FOOT)
    return f"""<footer class="site-footer">
  <div class="terrain" aria-hidden="true"></div>
  <div class="ground"><div class="wrap foot">
    <div class="foot-brand">
      <a class="brand" href="/" aria-label="Remembra home">{lockup()}</a>
      <p>One cloud memory for all your AI agents. The core is open source under the MIT license.</p>
      <p>&copy; 2026 DolphyTech</p>
    </div>
    <nav aria-label="Footer">
      <ul class="foot-links">
        {links}
      </ul>
    </nav>
  </div></div>
</footer>"""


def snippet(name: str) -> str:
    return (LANDING / "brand" / "partials" / f"{name}.svg").read_text().strip()


def _mark() -> str:
    return snippet("mark-inline")


def _pixbrain() -> str:
    return snippet("brain-pixel-inline")


SNIPPETS: dict[str, Callable[[], str]] = {"lockup": lockup, "mark": _mark, "pixbrain": _pixbrain}


def fill_snippets(html: str) -> str:
    for name, make in SNIPPETS.items():
        pattern = re.compile(rf"<!-- @{name} -->.*?<!-- /@{name} -->", re.S)
        if pattern.search(html):
            filled = f"<!-- @{name} -->{make()}<!-- /@{name} -->"
            html = pattern.sub(filled.replace("\\", "\\\\"), html)
    return html


def replace_block(html: str, name: str, body: str) -> str:
    pattern = re.compile(rf"<!-- @{name} -->.*?<!-- /@{name} -->", re.S)
    if not pattern.search(html):
        raise SystemExit(f"missing <!-- @{name} --> block")
    return pattern.sub(lambda _: f"<!-- @{name} -->\n{body}\n<!-- /@{name} -->", html, count=1)


def render(html: str) -> str:
    m = re.search(r'<body[^>]*data-page="([a-z-]+)"', html)
    page = m.group(1) if m else ""
    html = replace_block(html, "head", HEAD)
    html = replace_block(html, "header", header(page))
    html = replace_block(html, "footer", footer(page))
    return fill_snippets(html)


def main() -> int:
    check = "--check" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    stale = []
    for name in only or PAGES:
        path = LANDING / name
        before = path.read_text()
        after = render(before)
        if after != before:
            stale.append(name)
            if not check:
                path.write_text(after)
    if check and stale:
        print("out of date:", ", ".join(stale))
        return 1
    print("up to date" if check else "updated: " + (", ".join(stale) or "nothing"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
