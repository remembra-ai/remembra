"""Pre-deploy checklist for the marketing site (landing/).

The site ships as static files, so nothing stops it going live before what it
links to or promises exists. Run this before deploying it:

    python scripts/site_predeploy.py            # also fetches every docs link
    python scripts/site_predeploy.py --offline  # skip the network

It prints the owner actions the pages declare in their deploy-gate comments
(<!-- requires ... -->) and the Crew mode switch, then checks every
https://docs.remembra.dev/... link on the pages:

  * offline: the link maps to a page in docs/ that mkdocs.yml builds
  * online:  the live docs site answers it with HTTP 200

Online, it also asks PyPI for the latest remembra release and holds it
against the version the install gates name (remembra>=X).

It exits 1 when a docs link has no source page or is not live yet, or when
PyPI is behind the gate, so the site is not deployed ahead of the docs it
points to or the package its install lines pull.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
DOCS_DIR = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
DOCS_HOST = "docs.remembra.dev"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_partials  # noqa: E402  (the pages and the crew switch live there)

GATE = re.compile(r"<!--\s*(requires\b.*?)\s*-->", re.S)
DOCS_LINK = re.compile(r'href="(https://docs\.remembra\.dev[^"#]*)')
PYPI_JSON = "https://pypi.org/pypi/remembra/json"
MIN_RELEASE = re.compile(r"remembra>=(\d+(?:\.\d+)*)")


def pages() -> list[Path]:
    return [LANDING / name for name in site_partials.PAGES]


def gates() -> dict[str, list[str]]:
    """Each distinct deploy gate, with the pages that carry it."""
    out: dict[str, list[str]] = {}
    for page in pages():
        for m in GATE.finditer(page.read_text()):
            text = " ".join(m.group(1).split())
            if page.name not in out.setdefault(text, []):
                out[text].append(page.name)
    return out


def docs_links() -> dict[str, list[str]]:
    """Each docs.remembra.dev URL on the pages, with the pages that link it."""
    out: dict[str, list[str]] = {}
    for page in pages():
        for url in DOCS_LINK.findall(page.read_text()):
            if page.name not in out.setdefault(url, []):
                out[url].append(page.name)
    return out


def built_docs() -> set[str]:
    """The docs/ source files mkdocs.yml puts in its nav."""
    return set(re.findall(r":\s*([\w./-]+\.md)\s*$", MKDOCS.read_text(), re.M))


def source_for(url: str) -> str | None:
    """The docs/ page a docs URL is built from, or None if there is none."""
    path = urlsplit(url).path.strip("/")
    candidates = ["index.md"] if not path else [f"{path}.md", f"{path}/index.md"]
    for rel in candidates:
        if (DOCS_DIR / rel).is_file() and (rel == "index.md" or rel in built_docs()):
            return rel
    return None


def required_release() -> str | None:
    """The highest remembra>=X any install gate asks for."""
    found = [v for text in gates() for v in MIN_RELEASE.findall(text)]
    return max(found, key=_version_key) if found else None


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def pypi_latest() -> str | None:
    """The latest remembra version on PyPI, or None if PyPI can't be reached."""
    req = urllib.request.Request(PYPI_JSON, headers={"User-Agent": "remembra-site-predeploy"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.load(resp)["info"]["version"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None


def http_status(url: str) -> int:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "remembra-site-predeploy"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return int(err.code)
    except (urllib.error.URLError, TimeoutError):
        return 0


def check(
    online: bool,
    fetch: Callable[[str], int] = http_status,
    latest: Callable[[], str | None] = pypi_latest,
) -> tuple[list[str], list[str]]:
    """(report lines, problems)."""
    lines = ["Owner actions before this site is deployed:"]
    for text, where in gates().items():
        lines.append(f"  - {text}  [{', '.join(where)}]")
    crew = (
        "on: /crew is linked and listed" if site_partials.CREW_LIVE else "off: /crew is unlinked, noindex and out of the sitemap"
    )
    lines.append(f"  - Crew mode switch (CREW_LIVE in scripts/site_partials.py) is {crew}")
    lines.append("")
    lines.append("Docs links:")
    problems: list[str] = []
    for url, where in docs_links().items():
        src = source_for(url)
        state = f"source docs/{src}" if src else "NO SOURCE PAGE"
        if src is None:
            problems.append(f"{url} has no page in docs/ built by mkdocs.yml ({', '.join(where)})")
        if online:
            code = fetch(url)
            state += f", live {code or 'unreachable'}"
            if code != 200:
                status = f"HTTP {code}" if code else "unreachable"
                problems.append(f"{url} is not live yet ({status}): deploy the docs site first ({', '.join(where)})")
        lines.append(f"  - {url}  {state}")
    need = required_release()
    if online and need:
        have = latest()
        lines.append("")
        lines.append(f"PyPI: remembra {have or 'unreachable'} (the install lines need remembra>={need})")
        if have is None:
            problems.append(f"PyPI could not be reached to confirm remembra>={need} is released")
        elif _version_key(have) < _version_key(need):
            problems.append(f"PyPI has remembra {have}; the install lines need remembra>={need}: release it first")
    return lines, problems


def main(argv: list[str]) -> int:
    lines, problems = check(online="--offline" not in argv)
    print("\n".join(lines))
    if problems:
        print("\nNot ready to deploy:")
        print("\n".join(f"  x {p}" for p in problems))
        return 1
    if "--offline" in argv:
        print("\nEvery docs link has a source page. Not checked live (--offline): run without it before deploying.")
    else:
        print("\nEvery docs link has a source page and is live, and PyPI has the release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
