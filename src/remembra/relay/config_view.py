"""A config file as it may be printed: every secret in it hidden.

``remembra-install`` and ``remembra-relay connect`` / ``disconnect`` print each
change as a diff before they write. Agent config files hold other tools'
credentials as well (another MCP server's token, an API key in an ``env``
block), and printed text ends up in terminal scrollback, screen shares and CI
logs. So the printed view hides:

- every value inside an ``env`` / ``headers`` block, except Remembra's own
  non-secret settings (``REMEMBRA_URL``, ``REMEMBRA_PROJECT``,
  ``REMEMBRA_USER_ID``, ``REMEMBRA_AGENT_ID``);
- every value whose name says it is a secret (``*token*``, ``*secret*``,
  ``*password*``, ``api_key``, ``*_key``, ``auth*``, ...);
- the value after a ``--token``-style flag in an argument list, and in an
  argument the value of ``NAME=value`` (``docker -e DB_PASSWORD=...``) and of
  an HTTP header (``--header "Authorization: Bearer ..."``) whose name says it
  is a secret;
- a URL query parameter whose name says it is a secret (``?key=...``), however
  short the value;
- in TOML, the lines of a multi-line array and a multi-line string with the
  same rules as a one-line value (a multi-line string of a secret setting is
  hidden whole);
- anything that looks like a credential wherever it is (provider key formats,
  credentials in a URL, bearer tokens, long random strings), via
  :func:`remembra.security.secrets.redact_secrets`.

A Remembra key is shown as ``rem_…wxyz`` so it can be recognised; everything
else becomes ``[hidden]``. JSON is shown in one canonical layout (two-space
indent), so a diff of two views shows what changes, not the whole file
re-indented. Only the printed view changes: the file is written as planned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from remembra.security.secrets import redact_secrets
from remembra.tools.keyinput import mask_key, mask_text

HIDDEN = "[hidden]"

# Remembra's own settings that are not secret: shown so the user can check them.
SHOWN_SETTINGS = frozenset({"REMEMBRA_URL", "REMEMBRA_PROJECT", "REMEMBRA_USER_ID", "REMEMBRA_AGENT_ID"})
# Blocks whose every value is treated as secret (MCP server env, HTTP headers).
SECRET_BLOCKS = frozenset({"env", "environment", "headers", "http_headers", "env_http_headers"})

_SECRET_NAME_RE = re.compile(
    r"(?i)(token|secret|passw|passphrase|pwd|credential|cookie|bearer|auth|apikey|api[_\-.]key|private[_\-.]?key"
    r"|(?:^|[_\-.])key$)"
)
_SECRET_FLAG_RE = re.compile(r"(?i)^--?[\w.\-]*(?:token|secret|passw|pwd|auth|key|credential)[\w.\-]*$")
_SECRET_FLAG_ASSIGN_RE = re.compile(r"(?i)^(--?[\w.\-]*(?:token|secret|passw|pwd|auth|key|credential)[\w.\-]*=)(.+)$")
_REM_KEY_RE = re.compile(r"^rem_[A-Za-z0-9_\-]{8,}$")


def secret_name(name: str) -> bool:
    """True when a setting's name says its value is a credential."""
    return name not in SHOWN_SETTINGS and bool(_SECRET_NAME_RE.search(name))


def hide(value: str) -> str:
    """A secret value as printed: a Remembra key as ``rem_…wxyz``, anything else ``[hidden]``."""
    value = value.strip()
    return mask_key(value) if _REM_KEY_RE.match(value) else HIDDEN


_QUERY_PARAM_RE = re.compile(r"([?&;])([^=&#;\s\"'?]+)=([^&#;\s\"']*)")
_NAME_ASSIGN_RE = re.compile(r"^(\s*[A-Za-z_][\w.\-]*=)(.+)$", re.S)
_HEADER_RE = re.compile(r"^(\s*[A-Za-z][\w\-]*\s*:\s*)(\S.*)$", re.S)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s\"',]+")
_FLAG_ASSIGN_RE = re.compile(r"^(--?[\w.\-]+=)(.+)$", re.S)


def _hide_query(text: str) -> str:
    """``?key=abc`` -> ``?key=[hidden]`` for every query parameter whose name says it is a secret."""

    def one(match: re.Match[str]) -> str:
        sep, name, value = match.groups()
        return f"{sep}{name}={HIDDEN}" if value and secret_name(name) else match.group(0)

    return _QUERY_PARAM_RE.sub(one, text)


def scrub(text: str, keys: tuple[str, ...] = ()) -> str:
    """Credential-looking substrings of free text hidden (the given keys and rem_ keys masked)."""
    return redact_secrets(_hide_query(mask_text(text, keys))).text


def _arg_value(item: str) -> str | None:
    """One argument with its secret part hidden, or None when nothing in it is a secret by name.

    ``DB_PASSWORD=x`` (a ``docker -e`` / ``env`` assignment), ``Authorization: Bearer x``
    (an HTTP header), ``Bearer x`` and ``--flag=<one of those>``.
    """
    flagged = _FLAG_ASSIGN_RE.match(item)
    if flagged:
        inner = _arg_value(flagged.group(2))
        return None if inner is None else flagged.group(1) + inner
    assigned = _NAME_ASSIGN_RE.match(item)
    if assigned and secret_name(assigned.group(1).strip()[:-1]):
        return assigned.group(1) + hide(assigned.group(2))
    header = _HEADER_RE.match(item)
    if header and secret_name(header.group(1).strip()[:-1].strip()):
        return header.group(1) + HIDDEN
    if _AUTH_SCHEME_RE.search(item):
        return _AUTH_SCHEME_RE.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, item)
    return None


def _view_arg(item: str, after_flag: bool, keys: tuple[str, ...]) -> tuple[str, bool]:
    """One string of an argument list as printed, and whether the next one follows a secret flag."""
    if after_flag:
        return hide(item), False
    assigned = _SECRET_FLAG_ASSIGN_RE.match(item)
    if assigned:
        return assigned.group(1) + hide(assigned.group(2)), False
    if _SECRET_FLAG_RE.match(item):
        return item, True
    by_name = _arg_value(item)
    return (scrub(by_name, keys) if by_name is not None else scrub(item, keys)), False


def _view_args(items: list[Any], keys: tuple[str, ...]) -> list[Any]:
    out: list[Any] = []
    after_flag = False
    for item in items:
        if isinstance(item, str):
            item, after_flag = _view_arg(item, after_flag, keys)
        else:
            item = _view(item, keys, in_block=False)
            after_flag = False
        out.append(item)
    return out


def _view(value: Any, keys: tuple[str, ...], in_block: bool) -> Any:
    """``value`` with its secrets hidden; ``in_block`` is True inside an env/headers block."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for name, item in value.items():
            key = str(name)
            secret = in_block or key.lower() in SECRET_BLOCKS or secret_name(key)
            if key in SHOWN_SETTINGS and isinstance(item, str):
                out[name] = scrub(item, keys)
            elif isinstance(item, (dict, list)):
                out[name] = _view(item, keys, secret)
            elif isinstance(item, str):
                out[name] = hide(item) if secret else scrub(item, keys)
            else:
                out[name] = item  # numbers, booleans, null
        return out
    if isinstance(value, list):
        if in_block:
            return [hide(v) if isinstance(v, str) else _view(v, keys, True) for v in value]
        return _view_args(value, keys)
    if isinstance(value, str):
        return hide(value) if in_block else scrub(value, keys)
    return value


def canonical_json(text: str) -> str | None:
    """``text`` in the layout the installers write (None when it is not a JSON object or array)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, (dict, list)):
        return None
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _json_view(text: str, keys: tuple[str, ...]) -> str | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, (dict, list)):
        return None
    return json.dumps(_view(data, keys, in_block=False), indent=2, ensure_ascii=False) + "\n"


# --- TOML (Codex, Kimi): line by line, knowing which table each line is in ---

_TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_TOML_KEY = r"(?:[A-Za-z0-9_\-]+|\"[^\"]*\"|'[^']*')"
_KEY_VALUE_RE = re.compile(rf"^(\s*)({_TOML_KEY}(?:\s*\.\s*{_TOML_KEY})*)(\s*=\s*)(.*?)(\r?\n)?$")
_STRING_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
_INLINE_PAIR_RE = re.compile(rf"({_TOML_KEY})(\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*')")


def _segments(dotted: str) -> list[str]:
    return [part.strip().strip("\"'") for part in re.split(r"\.(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", dotted)]


def _hide_literal(literal: str) -> str:
    quote = literal[0]
    return quote + hide(literal[1:-1]) + quote


def _hide_strings(value: str) -> str:
    return _STRING_RE.sub(lambda m: _hide_literal(m.group(0)), value)


class _ArrayState:
    """Where a TOML array is while it is read line by line: open brackets, and whether the next string follows a secret flag."""

    def __init__(self, secret: bool) -> None:
        self.secret = secret
        self.depth = 0
        self.after_flag = False


def _bracket_depth(fragment: str) -> int:
    """Open ``[`` minus closed ``]`` in a TOML value fragment, outside strings and comments."""
    bare = _STRING_RE.sub("", fragment).split("#", 1)[0]
    return bare.count("[") - bare.count("]")


def _toml_array_view(value: str, state: _ArrayState, keys: tuple[str, ...]) -> str:
    """The strings of (one line of) a TOML array as printed, with the argument-list rules."""

    def one(match: re.Match[str]) -> str:
        literal = match.group(0)
        if state.secret:
            return _hide_literal(literal)
        shown, state.after_flag = _view_arg(literal[1:-1], state.after_flag, keys)
        return literal[0] + shown + literal[0]

    state.depth += _bracket_depth(value)
    return _STRING_RE.sub(one, value)


def _multiline_opener(value: str) -> str | None:
    """The delimiter of a multi-line string that ``value`` opens and does not close on this line."""
    stripped = value.lstrip()
    for delim in ('"""', "'''"):
        if stripped.startswith(delim) and delim not in stripped[3:]:
            return delim
    return None


def _inline_pair_view(match: re.Match[str]) -> str:
    """One ``name = "value"`` inside an inline table: the value hidden when the name says it is secret."""
    name, eq, literal = match.groups()
    return name + eq + (_hide_literal(literal) if secret_name(name.strip("\"'")) else literal)


def _toml_view(text: str, keys: tuple[str, ...]) -> str:
    out: list[str] = []
    table: list[str] = []
    array: _ArrayState | None = None  # inside a multi-line array
    string: tuple[str, bool] | None = None  # inside a multi-line string: (delimiter, secret)
    for line in text.splitlines(keepends=True):
        if string is not None:
            delim, secret = string
            body, newline = (line[:-1], "\n") if line.endswith("\n") else (line, "")
            end = body.find(delim)
            content, rest = (body, "") if end < 0 else (body[:end], body[end:])
            if end >= 0:
                string = None
            if secret:
                out.append((HIDDEN if content.strip() else content) + rest + newline)
            else:
                out.append(scrub(line, keys))
            continue
        if array is not None:
            if line.lstrip().startswith("#"):
                out.append(scrub(line, keys))
                continue
            out.append(scrub(_toml_array_view(line, array, keys), keys))
            if array.depth <= 0:
                array = None
            continue
        header = _TABLE_RE.match(line)
        if header:
            table = _segments(header.group(1))
            out.append(scrub(line, keys))
            continue
        pair = _KEY_VALUE_RE.match(line)
        if pair and not line.lstrip().startswith("#"):
            indent, dotted, eq, value, newline = pair.groups()
            path = table + _segments(dotted)
            name = path[-1]
            in_block = any(seg.lower() in SECRET_BLOCKS for seg in path[:-1])
            secret = name not in SHOWN_SETTINGS and (in_block or secret_name(name) or name.lower() in SECRET_BLOCKS)
            opener = _multiline_opener(value)
            if opener is not None:
                string = (opener, secret)
                start = value.index(opener) + 3
                tail = value[start:]
                if secret and tail.strip():
                    tail = HIDDEN
                elif not secret:
                    tail = scrub(tail, keys)
                out.append(indent + dotted + eq + value[:start] + tail + (newline or ""))
                continue
            if name in SHOWN_SETTINGS:
                pass
            elif value.lstrip().startswith("["):
                state = _ArrayState(secret)
                value = _toml_array_view(value, state, keys)
                if state.depth > 0:
                    array = state
            elif secret:
                value = _hide_strings(value)
            elif value.lstrip().startswith("{"):
                value = _INLINE_PAIR_RE.sub(_inline_pair_view, value)
            line = indent + dotted + eq + value + (newline or "")
        out.append(scrub(line, keys))
    return "".join(out)


def config_view(text: str | None, path: Path, keys: tuple[str, ...] = ()) -> str | None:
    """How ``text`` (the content of ``path``) is printed: secrets hidden, JSON in canonical layout."""
    if text is None:
        return None
    if path.suffix.lower() == ".toml":
        return _toml_view(text, keys)
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        view = _json_view(text, keys)
        if view is not None:
            return view
    return "".join(scrub(line, keys) for line in text.splitlines(keepends=True))
