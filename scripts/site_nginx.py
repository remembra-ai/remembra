"""Answer "what does remembra.dev send for this path?" from landing/nginx.conf.

There is no nginx on a dev machine or in CI, so this module reads the server's
location blocks and applies nginx's own matching rules to them:

  1. an exact match (location = /x) wins outright;
  2. otherwise the longest prefix match is remembered, and if it is a
     location ^~ /x, it wins;
  3. otherwise the regex locations (~, ~*) are tried in file order and the
     first match wins;
  4. otherwise the remembered prefix match is used.

Inside the chosen location it runs the directives the site uses: return,
rewrite ... permanent, internal, and try_files against the files in landing/.
Anything else in a location (add_header, include, default_type) does not
change which file or redirect is sent.

``content_type`` answers the Content-Type nginx sends with a file: the type
its extension has in the config's ``types`` blocks or in the stock
mime.types the config includes (STOCK_TYPES), else the location's (or the
server's) ``default_type``, with ``; charset=utf-8`` when that type is in
``charset_types`` (text/html always is).

    python scripts/site_nginx.py /pricing /dashboard?checkout=success

tests/test_landing_nginx.py checks this model against nginx's documented
behavior and holds the site to it; tests/test_landing_site.py's link checker
resolves every internal link through it.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

LANDING = Path(__file__).resolve().parents[1] / "landing"
CONF = LANDING / "nginx.conf"

_LOCATION = re.compile(r"location\s+(=|\^~|~\*|~)?\s*(\S+)\s*\{([^{}]*)\}", re.S)


@dataclass(frozen=True)
class Location:
    modifier: str  # "=", "^~", "~", "~*" or "" (plain prefix)
    pattern: str
    body: list[list[str]]  # directives, each split into words

    def directive(self, name: str) -> list[str] | None:
        return next((d[1:] for d in self.body if d and d[0] == name), None)


@dataclass(frozen=True)
class Response:
    status: int
    location: str | None = None  # the redirect target, for 3xx
    file: Path | None = None  # the file served, for 200
    matched: Location | None = None


def _strip_comments(text: str) -> str:
    """Drop comments. As in nginx, # starts one only at the start of a token, outside quotes."""
    out = []
    for line in text.splitlines():
        quoted = False
        for i, ch in enumerate(line):
            if ch == '"':
                quoted = not quoted
            elif ch == "#" and not quoted and (i == 0 or line[i - 1].isspace()):
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def _directives(body: str) -> list[list[str]]:
    return [[w.strip('"') for w in stmt.split()] for stmt in (s.strip() for s in body.split(";")) if stmt]


def parse(text: str) -> list[Location]:
    """The location blocks of the config, in file order."""
    return [Location(m.group(1) or "", m.group(2), _directives(m.group(3))) for m in _LOCATION.finditer(_strip_comments(text))]


def load(conf: Path | None = None) -> list[Location]:
    return parse((conf or CONF).read_text())


def match(locations: list[Location], path: str) -> tuple[Location | None, re.Match[str] | None]:
    """The location nginx picks for ``path``, and the regex match that picked it (if any)."""
    for loc in locations:
        if loc.modifier == "=" and loc.pattern == path:
            return loc, None
    prefixes = [loc for loc in locations if loc.modifier in ("", "^~") and path.startswith(loc.pattern)]
    best = max(prefixes, key=lambda loc: len(loc.pattern), default=None)
    if best is not None and best.modifier == "^~":
        return best, None
    for loc in locations:
        if loc.modifier in ("~", "~*"):
            m = re.search(loc.pattern, path, re.I if loc.modifier == "~*" else 0)
            if m:
                return loc, m
    return best, None


def _expand(template: str, groups: tuple[str | None, ...], args: str) -> str:
    out = template.replace("$is_args", "?" if args else "").replace("$args", args)
    for i in range(9, 0, -1):
        out = out.replace(f"${i}", (groups[i - 1] or "") if i <= len(groups) else "")
    return out


def _file(root: Path, uri: str) -> Path | None:
    rel = uri.lstrip("/")
    candidate = root / rel if rel else root
    if uri.endswith("/"):
        index = candidate / "index.html"
        return index if index.is_file() else None
    return candidate if candidate.is_file() else None


def resolve(url: str, locations: list[Location] | None = None, root: Path | None = None) -> Response:
    """What nginx sends for ``url`` (a path with an optional query string)."""
    locations = load() if locations is None else locations
    root = root or LANDING
    parts = urlsplit(url)
    path, args = unquote(parts.path or "/"), parts.query
    loc, m = match(locations, path)
    if loc is None:
        found = _file(root, path)
        return Response(200, file=found) if found else Response(404)
    groups = m.groups() if m else ()
    if loc.directive("internal") is not None:
        return Response(404, matched=loc)
    ret = loc.directive("return")
    if ret is not None:
        code = int(ret[0])
        target = _expand(ret[1], groups, args) if len(ret) > 1 else None
        return Response(code, location=target, matched=loc)
    rw = loc.directive("rewrite")
    if rw is not None and rw[-1] in ("permanent", "redirect"):
        rm = re.search(rw[0], path)
        if rm:
            target = _expand(rw[1], rm.groups(), "")
            if args:
                target += "?" + args
            return Response(301 if rw[-1] == "permanent" else 302, location=target, matched=loc)
    tries = loc.directive("try_files")
    if tries is not None:
        *candidates, last = tries
        for cand in candidates:
            found = _file(root, cand.replace("$uri", path))
            if found:
                return Response(200, file=found, matched=loc)
        if last.startswith("="):
            return Response(int(last[1:]), matched=loc)
        return resolve(last, locations, root)
    found = _file(root, path)
    return Response(200, file=found, matched=loc) if found else Response(404, matched=loc)


# The entries of nginx's stock conf/mime.types (included by nginx.conf) that remembra.dev's files use.
# It has no entry for .md: a markdown file needs the config's own types block, or a default_type.
STOCK_TYPES = {
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "xml": "text/xml",
    "js": "application/javascript",
    "json": "application/json",
    "txt": "text/plain",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "ico": "image/x-icon",
    "webp": "image/webp",
}


def _http_level(text: str) -> str:
    """The config with every location block cut out: what is left is set for the whole server."""
    return _LOCATION.sub("", _strip_comments(text))


def config_types(text: str) -> dict[str, str]:
    """extension -> MIME type from the config's own ``types { ... }`` blocks."""
    out: dict[str, str] = {}
    for block in re.findall(r"\btypes\s*\{([^{}]*)\}", _strip_comments(text)):
        for words in _directives(block):
            for ext in words[1:]:
                out[ext] = words[0]
    return out


def http_directive(text: str, name: str) -> list[str] | None:
    """A directive set outside every location (``charset_types``, ``gzip_types``, ``default_type``)."""
    outside = re.sub(r"\btypes\s*\{[^{}]*\}", "", _http_level(text))
    m = re.search(rf"(?m)^\s*{re.escape(name)}\s+([^;{{}}]+);", outside)
    return m.group(1).split() if m else None


def content_type(url: str, conf: str | None = None, root: Path | None = None) -> str | None:
    """The Content-Type nginx sends for ``url``, or None when it sends no file (a redirect or a 404)."""
    text = conf if conf is not None else CONF.read_text()
    locations = parse(text)
    res = resolve(url, locations, root)
    if res.status != 200 or res.file is None:
        return None
    ext = res.file.suffix.lstrip(".").lower()
    mime = {**STOCK_TYPES, **config_types(text)}.get(ext)
    if mime is None:
        local = res.matched.directive("default_type") if res.matched else None
        mime = (local or http_directive(text, "default_type") or ["text/plain"])[0]
    charset = http_directive(text, "charset")
    charset_types = set(http_directive(text, "charset_types") or []) | {"text/html"}
    return f"{mime}; charset={charset[0]}" if charset and mime in charset_types else mime


def follow(url: str, locations: list[Location] | None = None, root: Path | None = None, hops: int = 5) -> Response:
    """Follow redirects that stay on this site; stop at one that leaves it."""
    locations = load() if locations is None else locations
    res = resolve(url, locations, root)
    while hops and res.status in (301, 302, 307, 308) and res.location and res.location.startswith("/"):
        res = resolve(res.location, locations, root)
        hops -= 1
    return res


def main(argv: list[str]) -> int:
    for url in argv or ["/"]:
        res = resolve(url)
        where = res.location or (res.file.relative_to(LANDING).as_posix() if res.file else "")
        print(f"{url} -> {res.status} {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
