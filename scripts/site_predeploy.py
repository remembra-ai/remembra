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

Online, it also checks every package an install command in docs/ or the
README names (pip, pipx, uv, npm, yarn, pnpm) against PyPI or npm, so the
docs never tell anyone to install a package that does not exist:

    python scripts/site_predeploy.py --packages   # only the package check (the docs CI job runs this)

The package check also holds every minimum version an install command pins
(``'remembra[mcp]>=0.16'``) against the latest release on PyPI, so pushing
docs that install a release PyPI does not have yet fails the docs job instead
of publishing install lines that cannot resolve.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

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
NGINX_CONF = LANDING / "nginx.conf"
NGINX_DOCS_TARGET = re.compile(r"(https://docs\.remembra\.dev/[^\s;\"$]*)")

# `pip install x`, `npm i -g y`, ... up to the end of the command.
INSTALL_CMD = re.compile(
    r"\b(uv pip install|uv add|pipx install|pip3? install|npm (?:install|i)|yarn add|pnpm add)\b([^\n`|;&#)]*)"
)
TAKES_VALUE = {"-r", "-e", "-c", "--requirement", "--editable", "--constraint", "--index-url", "-i", "--python"}
PACKAGE_NAME = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*")
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
    """Each docs.remembra.dev URL on the pages or in nginx.conf's redirects, with where it appears."""
    out: dict[str, list[str]] = {url: list(where) for url, where in nginx_docs_targets().items()}
    for page in pages():
        for url in DOCS_LINK.findall(page.read_text()):
            if page.name not in out.setdefault(url, []):
                out[url].append(page.name)
    return out


def nginx_docs_targets() -> dict[str, list[str]]:
    """docs.remembra.dev pages that landing/nginx.conf redirects to (not the /docs/* passthrough)."""
    if not NGINX_CONF.is_file():
        return {}
    return {url: ["nginx.conf"] for url in NGINX_DOCS_TARGET.findall(NGINX_CONF.read_text()) if urlsplit(url).path.strip("/")}


def install_sources() -> list[Path]:
    return sorted([*DOCS_DIR.rglob("*.md"), ROOT / "README.md"])


def package_names(sources: list[Path] | None = None) -> dict[tuple[str, str], list[str]]:
    """(registry, package) for every package an install command names, with the files that name it."""
    out: dict[tuple[str, str], list[str]] = {}
    for path in install_sources() if sources is None else sources:
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
        for m in INSTALL_CMD.finditer(path.read_text(errors="replace")):
            registry = "npm" if m.group(1).split()[0] in ("npm", "yarn", "pnpm") else "pypi"
            skip = False
            for raw in m.group(2).split():
                token = raw.strip("'\"")
                if skip:
                    skip = False
                    continue
                if token in TAKES_VALUE:
                    skip = True
                    continue
                if (
                    not token
                    or token.startswith(("-", ".", "/", "~", "<", "$", "{", "git+", "http://", "https://"))
                    or token.endswith(".txt")
                ):
                    continue
                name = _package_of(token, registry)
                if not PACKAGE_NAME.fullmatch(name):
                    continue
                files = out.setdefault((registry, name), [])
                if rel not in files:
                    files.append(rel)
    return out


PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?>=(\d+(?:\.\d+)*)")


def version_pins(sources: list[Path] | None = None) -> dict[tuple[str, str], list[str]]:
    """(PyPI package, minimum version) for every ``name>=X`` an install command pins, with the files."""
    out: dict[tuple[str, str], list[str]] = {}
    for path in install_sources() if sources is None else sources:
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
        for m in INSTALL_CMD.finditer(path.read_text(errors="replace")):
            if m.group(1).split()[0] in ("npm", "yarn", "pnpm"):
                continue
            for raw in m.group(2).split():
                pin = PIN.match(raw.strip("'\""))
                if pin:
                    files = out.setdefault((pin.group(1), pin.group(2)), [])
                    if rel not in files:
                        files.append(rel)
    return out


def pypi_version(name: str) -> str | None:
    """The latest release of ``name`` on PyPI, or None if PyPI can't be reached (or has no such package)."""
    req = urllib.request.Request(f"https://pypi.org/pypi/{name}/json", headers={"User-Agent": "remembra-site-predeploy"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.load(resp)["info"]["version"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None


def release_problems(latest: Callable[[str], str | None] | None = None, sources: list[Path] | None = None) -> list[str]:
    """One line per pinned minimum version PyPI does not have yet (or could not confirm)."""
    latest = latest or pypi_version
    problems = []
    for (name, need), where in sorted(version_pins(sources).items()):
        have = latest(name)
        if have is None:
            problems.append(f"PyPI could not confirm {name}>={need} is released ({', '.join(where)})")
        elif _version_key(have) < _version_key(need):
            files = ", ".join(where)
            problems.append(f"PyPI has {name} {have}; the install lines need {name}>={need}: release it first ({files})")
    return problems


def _package_of(token: str, registry: str) -> str:
    """The bare package name in an install argument: remembra[mcp]>=0.16 -> remembra, @a/b@1.2 -> @a/b."""
    if registry == "npm":
        scope, _, rest = token.partition("/") if token.startswith("@") else ("", "", token)
        base = rest.split("@", 1)[0]
        return f"{scope}/{base}" if scope else base
    return re.split(r"[\[<>=!~;@ ]", token, maxsplit=1)[0]


def registry_url(registry: str, name: str) -> str:
    if registry == "npm":
        return "https://registry.npmjs.org/" + quote(name, safe="@")
    return f"https://pypi.org/pypi/{name}/json"


def package_problems(fetch: Callable[[str], int] | None = None, sources: list[Path] | None = None) -> list[str]:
    """One line per package the registry does not have, or could not confirm."""
    fetch = fetch or http_status
    problems = []
    for (registry, name), where in sorted(package_names(sources).items()):
        code = fetch(registry_url(registry, name))
        if code == 404:
            problems.append(f"{registry} has no package named {name} ({', '.join(where)})")
        elif code != 200:
            state = f"HTTP {code}" if code else "unreachable"
            problems.append(f"{registry} could not confirm {name} ({state}; {', '.join(where)})")
    return problems


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
    if "--packages" in argv:
        found = package_names()
        pins = version_pins()
        problems = package_problems() + release_problems()
        print(f"Checked {len(found)} packages and {len(pins)} minimum versions named by install commands in docs/ and README.md.")
        if problems:
            print("\n".join(f"  x {p}" for p in problems))
            return 1
        print("Every one exists on its registry, and PyPI has every pinned release.")
        return 0
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
