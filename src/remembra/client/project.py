"""Project id normalization shared by the SDK and the MCP server (AGT-6).

Agents drifted into split namespaces ("clawdbot" vs "clawbot" vs "default")
because each client typed its own project id. ``normalize_project_id`` trims
whitespace and resolves configured aliases to one canonical id, so every
client that shares an alias map writes to and reads from the same namespace.

Case is preserved for ids that are not aliases: server-side project ids are
case-sensitive and lower-casing them would silently hide existing data.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_PROJECT = "default"
ALIASES_ENV = "REMEMBRA_PROJECT_ALIASES"


def parse_project_aliases(spec: str | None) -> dict[str, str]:
    """Parse ``"alias=canonical, other=canonical"`` into ``{alias: canonical}``.

    Alias keys are matched case-insensitively. Malformed entries (no ``=``,
    empty side) are ignored rather than raising, so a typo in an env var can't
    take a client down.
    """
    aliases: dict[str, str] = {}
    for part in (spec or "").split(","):
        if "=" not in part:
            continue
        alias, canonical = (x.strip() for x in part.split("=", 1))
        if alias and canonical:
            aliases[alias.lower()] = canonical
    return aliases


def aliases_from_env() -> dict[str, str]:
    return parse_project_aliases(os.environ.get(ALIASES_ENV))


def normalize_project_id(project: str | None, aliases: Mapping[str, str] | None = None) -> str:
    """Return the canonical project id for ``project``.

    - ``None`` / blank -> ``"default"``
    - surrounding whitespace removed, internal whitespace collapsed to ``-``
    - a configured alias (case-insensitive) maps to its canonical id
    """
    cleaned = "-".join((project or "").split())
    if not cleaned:
        return DEFAULT_PROJECT
    if aliases:
        return aliases.get(cleaned.lower(), cleaned)
    return cleaned
