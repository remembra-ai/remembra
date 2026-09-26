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
- the value after a ``--token``-style flag in an argument list;
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


def scrub(text: str, keys: tuple[str, ...] = ()) -> str:
    """Credential-looking substrings of free text hidden (the given keys and rem_ keys masked)."""
    return redact_secrets(mask_text(text, keys)).text


def _view_args(items: list[Any], keys: tuple[str, ...]) -> list[Any]:
    out: list[Any] = []
    after_flag = False
    for item in items:
        if isinstance(item, str):
            assigned = _SECRET_FLAG_ASSIGN_RE.match(item)
            if after_flag:
                item = hide(item)
            elif assigned:
                item = assigned.group(1) + hide(assigned.group(2))
            else:
                item = scrub(item, keys)
            after_flag = bool(_SECRET_FLAG_RE.match(item)) if isinstance(item, str) else False
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


def _toml_array_view(value: str) -> str:
    """Hide the string after a ``--token``-style flag (and ``--token=x``) in a TOML array."""
    after_flag = False

    def one(match: re.Match[str]) -> str:
        nonlocal after_flag
        literal = match.group(0)
        inner = literal[1:-1]
        if after_flag:
            after_flag = False
            return _hide_literal(literal)
        assigned = _SECRET_FLAG_ASSIGN_RE.match(inner)
        after_flag = bool(_SECRET_FLAG_RE.match(inner))
        if assigned:
            return literal[0] + assigned.group(1) + hide(assigned.group(2)) + literal[0]
        return literal

    return _STRING_RE.sub(one, value)


def _inline_pair_view(match: re.Match[str]) -> str:
    """One ``name = "value"`` inside an inline table: the value hidden when the name says it is secret."""
    name, eq, literal = match.groups()
    return name + eq + (_hide_literal(literal) if secret_name(name.strip("\"'")) else literal)


def _toml_view(text: str, keys: tuple[str, ...]) -> str:
    out: list[str] = []
    table: list[str] = []
    for line in text.splitlines(keepends=True):
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
            if name in SHOWN_SETTINGS:
                pass
            elif in_block or secret_name(name) or name.lower() in SECRET_BLOCKS:
                value = _hide_strings(value)
            elif value.lstrip().startswith("{"):
                value = _INLINE_PAIR_RE.sub(_inline_pair_view, value)
            elif value.lstrip().startswith("["):
                value = _toml_array_view(value)
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
