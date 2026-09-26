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

The current page is marked from the body's data-page attribute. The
home page's section eyebrows (<p class="eyebrow"><span class="n">NN</span>)
are numbered in page order.

Crew mode is behind one switch, CREW_LIVE. While it is False (Crew mode is
not built yet), no page links to /crew, the home page leaves out its crew
section, sitemap.xml leaves out /crew, and crew.html itself is noindex and
says the feature is not available yet. When feat/crew is merged and live,
set CREW_LIVE = True and run this script; every crew link, the home section
(kept in scripts/site-crew-section.html), the sitemap entry and the page's
launch label come back in one step. The regions it owns:

    <!-- @crew-section --> ... <!-- /@crew-section -->   index.html: the section, or nothing
    <!-- @crew-url --><!-- /@crew-url -->                sitemap.xml: the /crew entry, or nothing
    <!-- @crew-robots --><!-- /@crew-robots -->          crew.html: noindex while off
    <!-- @crew-tag --><!-- /@crew-tag -->                crew.html: "Part of launch" / "Not available yet"
    <!-- @crew-note --><!-- /@crew-note -->              crew.html: the not-available notice while off
    <!-- @crew-meta --><!-- /@crew-meta -->              crew.html: description and og:description (link previews)
    <!-- @crew-plan-title --><!-- /@crew-plan-title -->  crew.html: "What ships at launch" / "What ships first"
    <!-- @crew-plan-first --><!-- /@crew-plan-first -->  crew.html: "At launch" / "First release"

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
SITEMAP = "sitemap.xml"
CREW_SECTION = Path(__file__).resolve().parent / "site-crew-section.html"

# Crew mode is not built yet: keep this False until feat/crew is merged and live.
CREW_LIVE = False
CREW_URL = "https://remembra.dev/crew"
# What a shared /crew link shows in a preview. While crew mode is off it must not say "launch".
CREW_META_LIVE = (
    '\n<meta name="description" content="Crew mode, part of launch: several AI agents on one project at once. Each holds the zones it is working in, checkpoints on its own and hands off the moment it stops. Four layers keep one agent out of another\'s zone.">'
    '\n<meta property="og:description" content="Zones, automatic checkpoints and batons, four enforcement layers and a live dashboard. Part of launch.">\n'
)
CREW_META_OFF = (
    '\n<meta name="description" content="Crew mode is planned and not available yet: several AI agents on one project at once. Each will hold the zones it is working in, checkpoint on its own and hand off the moment it stops. Four layers will keep one agent out of another\'s zone.">'
    '\n<meta property="og:description" content="Planned, not available yet: zones, automatic checkpoints and batons, four enforcement layers and a live dashboard.">\n'
)

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


def shown(key: str) -> bool:
    """Every link is shown, except Crew mode while it is not live."""
    return key != "crew" or CREW_LIVE


def header(page: str) -> str:
    links = "\n      ".join(
        f'<a class="nav-link{cls}" href="{href}"{cur(key, page)}>{label}</a>' for key, label, href, cls in NAV if shown(key)
    )
    menu = "\n    ".join(f'<a href="{href}"{cur(key, page)}>{label}</a>' for key, label, href in MENU if shown(key))
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
    links = "\n        ".join(f'<li><a href="{href}"{cur(key, page)}>{label}</a></li>' for key, label, href in FOOT if shown(key))
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


def fill_inline(html: str, name: str, body: str) -> str:
    """Fill an inline <!-- @name --><!-- /@name --> region, if the page has one."""
    pattern = re.compile(rf"<!-- @{name} -->.*?<!-- /@{name} -->", re.S)
    return pattern.sub(lambda _: f"<!-- @{name} -->{body}<!-- /@{name} -->", html)


def crew_regions(html: str) -> str:
    """Everything Crew mode adds to a page, on or off with CREW_LIVE."""
    if "<!-- @crew-section -->" in html:
        if CREW_LIVE:
            section = CREW_SECTION.read_text()
            section = section[section.index("-->") + 3 :].strip("\n")  # drop the file's own note
            html = replace_block(html, "crew-section", section)
        else:
            note = "  <!-- Crew mode is off: CREW_LIVE in scripts/site_partials.py; the section is scripts/site-crew-section.html -->"
            html = replace_block(html, "crew-section", note)
    url = f"<url><loc>{CREW_URL}</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>"
    html = fill_inline(html, "crew-url", url if CREW_LIVE else "")
    html = fill_inline(html, "crew-robots", "" if CREW_LIVE else '<meta name="robots" content="noindex">')
    tag = '<span class="tag signal">Part of launch</span>' if CREW_LIVE else '<span class="tag">Not available yet</span>'
    html = fill_inline(html, "crew-tag", tag)
    note = (
        ""
        if CREW_LIVE
        else '<p class="crew-status" role="note"><b>Crew mode is still being built and is not available yet.</b> '
        "This page describes how it will work when it ships.</p>"
    )
    html = fill_inline(html, "crew-note", note)
    html = fill_inline(html, "crew-meta", CREW_META_LIVE if CREW_LIVE else CREW_META_OFF)
    title = "What ships at launch, and what follows." if CREW_LIVE else "What ships first, and what follows."
    html = fill_inline(html, "crew-plan-title", title)
    return fill_inline(html, "crew-plan-first", "At launch" if CREW_LIVE else "First release")


def number_sections(html: str) -> str:
    """Number the section eyebrows 01, 02, ... in page order."""
    count = iter(range(1, 100))
    return re.sub(
        r'(<p class="eyebrow"><span class="n">)\d\d(</span>)', lambda m: f"{m.group(1)}{next(count):02d}{m.group(2)}", html
    )


def render(html: str) -> str:
    m = re.search(r'<body[^>]*data-page="([a-z-]+)"', html)
    page = m.group(1) if m else ""
    html = replace_block(html, "head", HEAD)
    html = replace_block(html, "header", header(page))
    html = replace_block(html, "footer", footer(page))
    html = crew_regions(html)
    return number_sections(fill_snippets(html))


def render_sitemap(xml: str) -> str:
    return crew_regions(xml)


def main() -> int:
    check = "--check" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    stale = []
    for name in only or [*PAGES, SITEMAP]:
        path = LANDING / name
        before = path.read_text()
        after = render_sitemap(before) if name == SITEMAP else render(before)
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
