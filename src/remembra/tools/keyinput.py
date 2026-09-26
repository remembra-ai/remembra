"""Reading and showing a Remembra API key without putting it where others can see it.

The key is taken, in order, from ``--api-key-stdin`` (piped), ``--api-key``
(still accepted, with a warning: it lands in shell history and the process
list), ``REMEMBRA_API_KEY``, a prompt on a terminal (Enter keeps the saved
key), or ``~/.remembra/credentials``. Output only ever shows it masked.

A key typed at the prompt or piped on stdin must look like a Remembra key
(``rem_`` and at least 20 letters, digits, ``-`` or ``_``; the server issues
``rem_`` + 43): a hidden prompt shows nothing of what was pasted, so a wrong
clipboard (a password, a JWT, half a key) would otherwise be written to every
agent's config without anyone seeing it.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

ARGV_KEY_WARNING = (
    "warning: --api-key puts your key in your shell history and in the process list, where other programs can "
    "read it. It still works, but prefer REMEMBRA_API_KEY, --api-key-stdin, or run without it and paste the key "
    "at the prompt."
)

_REM_KEY_RE = re.compile(r"\brem_[A-Za-z0-9_\-]{4,}")
KEY_SHAPE_RE = re.compile(r"^rem_[A-Za-z0-9_\-]{20,}$")
PROMPT_ATTEMPTS = 3
KEY_SHAPE_HINT = (
    "that is not a Remembra API key: a key starts with rem_ followed by letters, digits, - and _ "
    "(copy it from the dashboard, Settings > API keys)"
)


def looks_like_key(value: str) -> bool:
    """True when ``value`` has the shape of a Remembra API key (``rem_`` + 20 or more URL-safe characters)."""
    return bool(KEY_SHAPE_RE.match(value))


def _clean_typed(value: str) -> str:
    """What was pasted, without surrounding whitespace or quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


_ASSIGNED_RE = re.compile(r"""((?:REMEMBRA_API_KEY|"api_key")["']?\s*[:=]\s*["']?)([^"'\s,]+)""")


def mask_key(key: str) -> str:
    """``rem_…wxyz``: enough to recognise a key, not to use it."""
    key = key.strip()
    if len(key) <= 12:
        return "…"
    return f"{key[:4]}…{key[-4:]}"


def mask_text(text: str, keys: tuple[str, ...] = ()) -> str:
    """``text`` with every API key in it masked (the given keys, rem_ keys and assigned key values)."""
    for key in keys:
        if key:
            text = text.replace(key, mask_key(key))
    text = _REM_KEY_RE.sub(lambda m: mask_key(m.group(0)), text)
    return _ASSIGNED_RE.sub(lambda m: m.group(1) + (m.group(2) if "…" in m.group(2) else mask_key(m.group(2))), text)


def saved_key(credentials: Path) -> str | None:
    try:
        data = json.loads(credentials.read_text())
    except (OSError, ValueError):
        return None
    key = data.get("api_key") if isinstance(data, dict) else None
    return key.strip() if isinstance(key, str) and key.strip() else None


def _is_tty(stream: object) -> bool:
    try:
        return bool(stream is not None and stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def resolve_api_key(
    *,
    cli_key: str | None,
    from_stdin: bool,
    credentials: Path,
    environ: Mapping[str, str],
    interactive: bool | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
    warn: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """``(key, where it came from)``; ``(None, reason)`` when there is none."""
    warn = warn or (lambda message: print(message, file=sys.stderr))
    if from_stdin:
        key = _clean_typed(sys.stdin.readline()) if sys.stdin is not None else ""
        if not key:
            return None, "nothing on stdin"
        if not looks_like_key(key):
            return None, f"stdin: {KEY_SHAPE_HINT}"
        return key, "stdin"
    if cli_key:
        warn(ARGV_KEY_WARNING)
        return cli_key.strip(), "--api-key"
    env_key = (environ.get("REMEMBRA_API_KEY") or "").strip()
    if env_key:
        return env_key, "REMEMBRA_API_KEY"
    saved = saved_key(credentials)
    if interactive is None:
        interactive = _is_tty(sys.stdin) and _is_tty(sys.stderr)
    if interactive:
        hint = f" [Enter keeps the saved key {mask_key(saved)}]" if saved else ""
        for attempt in range(PROMPT_ATTEMPTS):
            typed = _clean_typed(prompt(f"Remembra API key (input hidden){hint}: "))
            if not typed:
                break  # Enter: keep the saved key
            if looks_like_key(typed):
                return typed, "prompt"
            left = PROMPT_ATTEMPTS - attempt - 1
            warn(f"{KEY_SHAPE_HINT}." + (f" Try again ({left} left)." if left else ""))
        else:
            return None, f"the prompt: {KEY_SHAPE_HINT}"
    if saved:
        return saved, str(credentials)
    return None, "no key given"
