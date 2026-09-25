"""Connection config for ``remembra-relay``, discovered from existing setup.

Sources, first one holding an API key wins (the URL is taken from the SAME
source, so a key is never sent to a server configured somewhere else):

1. environment: ``REMEMBRA_API_KEY`` (+ ``REMEMBRA_URL``);
2. ``~/.claude.json``: ``env`` of the ``remembra`` MCP server (top level or
   any project entry);
3. ``~/.codex/config.toml``: ``[mcp_servers.remembra.env]``;
4. ``~/.remembra/credentials`` (written by ``remembra-install``).

The agent id is ``--agent`` > ``REMEMBRA_AGENT_ID`` > the source's value.
Nothing here writes configuration: keys stay where the user put them.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://localhost:8787"


@dataclass(frozen=True)
class RelayConfig:
    url: str
    api_key: str | None
    agent_id: str | None
    source: str
    project: str | None = None
    project_aliases: str = ""

    def redacted(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "api_key": "set" if self.api_key else "missing",
            "agent_id": self.agent_id,
            "source": self.source,
            "project": self.project,
        }


def _from_claude_json(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    candidates: list[Any] = [(data.get("mcpServers") or {}).get("remembra")]
    for project in (data.get("projects") or {}).values():
        if isinstance(project, dict):
            candidates.append((project.get("mcpServers") or {}).get("remembra"))
    for server in candidates:
        env = server.get("env") if isinstance(server, dict) else None
        if isinstance(env, dict) and env.get("REMEMBRA_API_KEY"):
            return {str(k): str(v) for k, v in env.items()}
    return {}


def _from_codex_toml(path: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    env = ((data.get("mcp_servers") or {}).get("remembra") or {}).get("env")
    if isinstance(env, dict) and env.get("REMEMBRA_API_KEY"):
        return {str(k): str(v) for k, v in env.items()}
    return {}


def _from_credentials(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data.get("api_key"):
        return {}
    out = {"REMEMBRA_API_KEY": str(data["api_key"])}
    if data.get("url"):
        out["REMEMBRA_URL"] = str(data["url"])
    if data.get("project"):
        out["REMEMBRA_PROJECT"] = str(data["project"])
    return out


def load_config(
    agent: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    prefer: str | None = None,
) -> RelayConfig:
    """Discover the connection config. ``prefer`` ("claude" | "codex") tries that
    agent's config file first (a Codex hook should use Codex's key)."""
    env = dict(os.environ if environ is None else environ)
    home = home or Path(env.get("HOME") or Path.home())

    sources: list[tuple[str, dict[str, str]]] = []
    if env.get("REMEMBRA_API_KEY"):
        sources.append(("env", env))
    else:
        claude_path = Path(env.get("REMEMBRA_HOOK_CLAUDE_CONFIG") or home / ".claude.json")
        candidates = [
            ("claude", _from_claude_json, claude_path),
            ("codex", _from_codex_toml, home / ".codex" / "config.toml"),
            ("credentials", _from_credentials, home / ".remembra" / "credentials"),
        ]
        candidates.sort(key=lambda c: 0 if c[0] == prefer else 1)
        for name, loader, path in candidates:
            found = loader(path)
            if found:
                sources.append((f"{name}:{path}", found))
                break

    source_name, chosen = sources[0] if sources else ("none", {})
    url = (chosen.get("REMEMBRA_URL") or DEFAULT_URL).strip().rstrip("/")
    agent_id = (agent or env.get("REMEMBRA_AGENT_ID") or chosen.get("REMEMBRA_AGENT_ID") or "").strip() or None
    project = (env.get("REMEMBRA_PROJECT") or chosen.get("REMEMBRA_PROJECT") or "").strip() or None
    aliases = env.get("REMEMBRA_PROJECT_ALIASES") or chosen.get("REMEMBRA_PROJECT_ALIASES") or ""
    return RelayConfig(
        url=url,
        api_key=(chosen.get("REMEMBRA_API_KEY") or "").strip() or None,
        agent_id=agent_id,
        source=source_name,
        project=project,
        project_aliases=aliases,
    )
