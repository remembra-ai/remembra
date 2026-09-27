"""Write docs.remembra.dev's Content-Security-Policy from the built MkDocs site.

MkDocs Material puts small inline scripts in every page (the ``__md_scope``
bootstrap, whose text changes with the page's depth, and the palette, tab and
anchor restores), and a few docs pages carry their own (the redirect stubs).
Instead of allowing inline script wholesale, every inline script is allowed by
its SHA-256 hash, and nothing else inline runs.

The hashes come from the built HTML, so docs.Dockerfile runs this on the site it
has just built and serves that result; .github/workflows/docs.yml runs it with
--check so the committed docs-nginx/remembra-headers.conf stays in step.

    python scripts/docs_csp.py SITE_DIR [HEADERS]          # rewrite the CSP line in HEADERS
    python scripts/docs_csp.py SITE_DIR [HEADERS] --check  # exit 1 if it is out of date

HEADERS defaults to docs-nginx/remembra-headers.conf. Like remembra.dev and
app.remembra.dev, the policy ships as Content-Security-Policy-Report-Only
(CSP_HEADER): browsers block nothing and report each violation to the API's
/csp-report. frame-ancestors is ignored in report-only mode; the enforced
X-Frame-Options: DENY keeps the docs out of frames meanwhile.

A page breaks the policy when it has an inline event handler, a javascript:
URL, or a <script src> on another host: no hash can allow those.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

HEADERS = Path(__file__).resolve().parents[1] / "docs-nginx" / "remembra-headers.conf"
CSP_BLOCK = re.compile(r"(# @csp\n).*?(# /@csp)", re.S)

# Script types a browser executes. Anything else (MkDocs' application/json config) is data.
JS_TYPES = {"", "text/javascript", "application/javascript", "module"}

# The theme's repository widget reads the repo's stars and forks from the GitHub API.
GITHUB_API = "https://api.github.com"
CSP_HEADER = "Content-Security-Policy-Report-Only"
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


def script_hash(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def scan_site(site: Path) -> tuple[list[str], list[str]]:
    """(sorted distinct inline-script hashes, problems) for every page of a built site."""
    pages = sorted(site.rglob("*.html"))
    if not pages:
        raise SystemExit(f"{site}: no HTML pages; build the site first (mkdocs build)")
    found: set[str] = set()
    problems: list[str] = []
    for page in pages:
        parser = _Scripts()
        parser.feed(page.read_text(encoding="utf-8"))
        parser.close()
        rel = page.relative_to(site).as_posix()
        found |= {script_hash(body) for body in parser.inline}
        problems += [f"{rel}: {p}" for p in parser.problems]
        problems += [
            f"{rel}: <script src={src}> loads a script from another host"
            for src in parser.external
            if src.startswith(("http:", "https:", "//"))
        ]
    return sorted(found), problems


def policy(hashes: list[str]) -> str:
    directives = [
        "default-src 'self'",
        " ".join(["script-src 'self'", *hashes]),
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self' data:",
        "img-src 'self' data:",
        f"connect-src 'self' {GITHUB_API}",
        "worker-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
        "upgrade-insecure-requests",
        f"report-uri {REPORT_URI}",
    ]
    return "; ".join(directives)


def render(conf: str, hashes: list[str]) -> str:
    if not CSP_BLOCK.search(conf):
        raise SystemExit("headers file: missing the # @csp ... # /@csp block")
    line = f'add_header {CSP_HEADER} "{policy(hashes)}" always;\n'
    return CSP_BLOCK.sub(lambda m: m.group(1) + line + m.group(2), conf, count=1)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    paths = [a for a in argv if a != "--check"]
    if not paths or len(paths) > 2:
        print("usage: python scripts/docs_csp.py SITE_DIR [HEADERS] [--check]")
        return 2
    site = Path(paths[0])
    headers = Path(paths[1]) if len(paths) == 2 else HEADERS
    hashes, problems = scan_site(site)
    if problems:
        print("These cannot be allowed by the docs CSP; move the code into a .js file:")
        print("\n".join(f"  x {p}" for p in problems))
        return 1
    before = headers.read_text()
    after = render(before, hashes)
    if check:
        if after != before:
            print(f"{headers} is out of date: run python scripts/docs_csp.py <built site>")
            return 1
        print(f"docs CSP up to date ({len(hashes)} inline script hashes)")
        return 0
    headers.write_text(after)
    print(f"docs CSP {'updated' if after != before else 'already up to date'} ({len(hashes)} inline script hashes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
