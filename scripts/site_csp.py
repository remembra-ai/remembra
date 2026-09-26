"""Write the Content-Security-Policy for remembra.dev from the pages themselves.

The site's pages carry a few small inline scripts (the theme bootstrap in every
head, the pricing toggle, the contact form's sent note, ...). A CSP that allows
inline scripts wholesale would let any injected script run, so instead every
inline script is allowed by its SHA-256 hash, and nothing else inline runs.

    python scripts/site_csp.py          # rewrite the CSP line in landing/remembra-headers.conf
    python scripts/site_csp.py --check  # exit 1 if it is out of date, or a page breaks the policy

At launch the policy ships as Content-Security-Policy-Report-Only (CSP_HEADER):
browsers apply nothing, and send each violation to the API's /csp-report,
which logs it. docs/DEPLOYING.md says how to switch to enforcing (set
CSP_HEADER to "Content-Security-Policy" and rerun this script) after a clean
week of reports. frame-ancestors is ignored in report-only mode; the enforced
X-Frame-Options: DENY keeps the site out of frames meanwhile.

A page breaks the policy when it has an inline event handler (onclick=...), a
javascript: URL, or a <script src> on another host: none of those can be
allowed by a hash, and the site has no need for them.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

LANDING = Path(__file__).resolve().parents[1] / "landing"
HEADERS = LANDING / "remembra-headers.conf"
CSP_BLOCK = re.compile(r"(# @csp\n).*?(# /@csp)", re.S)

# Script types a browser executes. Anything else (application/ld+json) is data.
JS_TYPES = {"", "text/javascript", "application/javascript", "module"}

# Hosts the site loads from, per directive, besides itself.
FONT_CSS = "https://fonts.googleapis.com"
FONT_FILES = "https://fonts.gstatic.com"
FORM_POST = "https://formsubmit.co"  # contact.html posts its form here
API = "https://api.remembra.dev"  # pricing.html reads the Founding 100 seats left (GET /api/v1/billing/founding)

# Report-only for launch; "Content-Security-Policy" once a week of reports is clean.
CSP_HEADER = "Content-Security-Policy-Report-Only"
# Where browsers send violation reports (remembra.api.v1.csp_report logs them).
REPORT_URI = "https://api.remembra.dev/api/v1/csp-report"


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.inline: list[str] = []
        self.external: list[str] = []
        self.problems: list[str] = []
        self._open: dict[str, str] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        for key, value in a.items():
            if key.startswith("on"):
                self.problems.append(f"inline event handler {key}= on <{tag}>")
            if key in ("href", "src", "action") and value.strip().lower().startswith("javascript:"):
                self.problems.append(f"javascript: URL in {key}= on <{tag}>")
        if tag == "script":
            if "src" in a:
                self.external.append(a["src"])
            else:
                self._open = a
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._open is not None:
            if self._open.get("type", "").strip().lower() in JS_TYPES:
                self.inline.append("".join(self._buf))
            self._open = None


def served_pages() -> list[Path]:
    """Every HTML page nginx serves from landing/ (brand partials are SVG, not pages)."""
    return sorted(p for p in LANDING.rglob("*.html") if "node_modules" not in p.parts)


def scan(page: Path) -> _Scripts:
    parser = _Scripts()
    parser.feed(page.read_text(encoding="utf-8"))
    parser.close()
    return parser


def script_hash(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def policy_problems() -> list[str]:
    """Anything on a page that no hash-based policy can allow."""
    problems = []
    for page in served_pages():
        rel = page.relative_to(LANDING).as_posix()
        found = scan(page)
        problems += [f"{rel}: {p}" for p in found.problems]
        for src in found.external:
            if src.startswith(("http:", "https:", "//")):
                problems.append(f"{rel}: <script src={src}> loads a script from another host")
    return problems


def hashes() -> list[str]:
    """The sorted, distinct hashes of every inline script on the site."""
    return sorted({script_hash(body) for page in served_pages() for body in scan(page).inline})


def policy() -> str:
    directives = [
        "default-src 'self'",
        "script-src 'self' " + " ".join(hashes()),
        f"style-src 'self' 'unsafe-inline' {FONT_CSS}",
        f"font-src 'self' {FONT_FILES}",
        "img-src 'self' data:",
        f"connect-src 'self' {API}",
        f"form-action 'self' {FORM_POST}",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
        "upgrade-insecure-requests",
        f"report-uri {REPORT_URI}",
    ]
    return "; ".join(directives)


def render(conf: str) -> str:
    if not CSP_BLOCK.search(conf):
        raise SystemExit(f"{HEADERS.name}: missing the # @csp ... # /@csp block")
    line = f'add_header {CSP_HEADER} "{policy()}" always;\n'
    return CSP_BLOCK.sub(lambda m: m.group(1) + line + m.group(2), conf, count=1)


def main(argv: list[str]) -> int:
    problems = policy_problems()
    if problems:
        print("These cannot be allowed by the CSP; move the code into a .js file:")
        print("\n".join(f"  x {p}" for p in problems))
        return 1
    before = HEADERS.read_text()
    after = render(before)
    if "--check" in argv:
        if after != before:
            print(f"{HEADERS.relative_to(LANDING.parent)} is out of date: run python scripts/site_csp.py")
            return 1
        print("CSP up to date")
        return 0
    HEADERS.write_text(after)
    print("CSP updated" if after != before else "CSP already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
