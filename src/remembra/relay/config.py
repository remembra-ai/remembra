"""Connection config for ``remembra-relay``, discovered from existing setup.

Sources, first one holding an API key wins (the URL is taken from the SAME
source, so a key is never sent to a server configured somewhere else):

1. environment: ``REMEMBRA_API_KEY`` (+ ``REMEMBRA_URL``);
2. ``~/.claude.json``: ``env`` of the ``remembra`` MCP server (top level or
   any project entry);
3. ``~/.codex/config.toml``: ``[mcp_servers.remembra.env]``;
4. ``~/.remembra/credentials`` (written by ``remembra-install``).

A hook of an agent with its own MCP config (Gemini CLI ``~/.gemini/settings.json``,
Qwen Code ``~/.qwen/settings.json``, Cursor ``~/.cursor/mcp.json``) reads that
file first, so each agent sends with its own key; other agents never read it.

The agent id is ``--agent`` > ``REMEMBRA_AGENT_ID`` > the source's value.
With no key anywhere, the URL is ``REMEMBRA_URL`` (else the local default):
that is the server a handoff queued without a key is kept for.
Nothing here writes configuration: keys stay where the user put them.

:func:`load_config_from_source` reads ONE named source (``"env"``,
``"claude:<path>"``, ...): the outbox sends a queued handoff only with the
key of the source that queued it.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping, MutableMapping
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


def _from_mcp_json(path: Path) -> dict[str, str]:
    """``mcpServers.remembra.env`` of an agent's JSON MCP config (Gemini CLI, Qwen Code, Cursor)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    server = (data.get("mcpServers") or {}).get("remembra") if isinstance(data, dict) else None
    env = server.get("env") if isinstance(server, dict) else None
    if isinstance(env, dict) and env.get("REMEMBRA_API_KEY"):
        return {str(k): str(v) for k, v in env.items()}
    return {}


# agent-owned config files, read only by that agent's hooks (``prefer``)
_OWN_CONFIG = {
    "gemini": (".gemini", "settings.json"),
    "qwen": (".qwen", "settings.json"),
    "cursor": (".cursor", "mcp.json"),
}


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
    """Discover the connection config. ``prefer`` ("claude" | "codex" | "gemini" | "qwen" | "cursor")
    tries that agent's config file first (a Codex hook should use Codex's key)."""
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
        if prefer in _OWN_CONFIG:
            candidates.append((prefer, _from_mcp_json, home.joinpath(*_OWN_CONFIG[prefer])))
        candidates.sort(key=lambda c: 0 if c[0] == prefer else 1)
        for name, loader, path in candidates:
            found = loader(path)
            if found:
                sources.append((f"{name}:{path}", found))
                break

    if sources:
        return _build(sources[0][0], sources[0][1], agent, env)
    return _build("none", {"REMEMBRA_URL": env.get("REMEMBRA_URL") or ""}, agent, env)


_LOADERS = {
    "claude": _from_claude_json,
    "codex": _from_codex_toml,
    "credentials": _from_credentials,
    **{name: _from_mcp_json for name in _OWN_CONFIG},
}


def load_config_from_source(
    source: str | None,
    agent: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RelayConfig | None:
    """The config from exactly ``source`` (a ``RelayConfig.source`` value), or None when it holds no key now.

    Unlike :func:`load_config` nothing falls through to another source: a key
    found elsewhere may belong to another account or server.
    """
    env = dict(os.environ if environ is None else environ)
    if source == "env":
        return _build("env", env, agent, env) if (env.get("REMEMBRA_API_KEY") or "").strip() else None
    kind, _, path = (source or "").partition(":")
    loader = _LOADERS.get(kind)
    if loader is None or not path:
        return None
    found = loader(Path(path))
    return _build(source or kind, found, agent, env) if found else None


# each client's own MCP config file, for ``remembra-mcp --client``
CLIENT_CONFIG = {
    "claude": (".claude.json",),
    "codex": (".codex", "config.toml"),
    **_OWN_CONFIG,
}


def client_mcp_env(client: str, home: Path, codex_home: str | None = None) -> dict[str, str]:
    """The ``env`` of the ``remembra`` MCP server in ``client``'s own config file ({} when there is none).

    Unlike the loaders above, a block without a key is returned too: a sandboxed Codex
    config has only the local bridge URL.
    """
    parts = CLIENT_CONFIG.get(client)
    if parts is None:
        return {}
    path = home.joinpath(*parts)
    if client == "codex" and codex_home:  # Codex reads $CODEX_HOME/config.toml when it is set
        path = Path(codex_home).expanduser() / "config.toml"
    try:
        text = path.read_text()
        data = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
    except (OSError, ValueError):
        return {}
    servers = data.get("mcp_servers" if path.suffix == ".toml" else "mcpServers") if isinstance(data, dict) else None
    server = servers.get("remembra") if isinstance(servers, dict) else None
    env = server.get("env") if isinstance(server, dict) else None
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def fill_env_from_client(client: str, environ: MutableMapping[str, str], home: Path | None = None) -> list[str]:
    """Add the ``REMEMBRA_*`` settings missing from ``environ`` from ``client``'s own config file.

    The Codex app's remote exec-server starts MCP servers without the env configured for them,
    so the server fell back to localhost. Values already in ``environ`` win, except that a key
    taken from the file always brings the file's URL with it, and a key from the environment never
    takes the file's URL: a key is never sent to a server configured somewhere else. Codex's file is
    ``$CODEX_HOME/config.toml`` when ``CODEX_HOME`` is set. Returns the names set (never their values).
    """
    found = client_mcp_env(client, home or Path(environ.get("HOME") or Path.home()), environ.get("CODEX_HOME"))
    filled: list[str] = []
    env_key = bool((environ.get("REMEMBRA_API_KEY") or "").strip())
    if found.get("REMEMBRA_API_KEY") and not env_key:
        environ["REMEMBRA_API_KEY"] = found["REMEMBRA_API_KEY"]
        environ["REMEMBRA_URL"] = found.get("REMEMBRA_URL") or DEFAULT_URL
        filled += ["REMEMBRA_API_KEY", "REMEMBRA_URL"]
    for name, value in found.items():
        if not name.startswith("REMEMBRA_") or name in environ:
            continue
        if name == "REMEMBRA_API_KEY" or (name == "REMEMBRA_URL" and env_key):
            # A key that arrived in the environment keeps the URL that came with it (or the default):
            # the file's URL belongs to the file's key. A keyless block (a sandboxed Codex's local
            # bridge) still gives its URL.
            continue
        environ[name] = value
        filled.append(name)
    return filled


def _build(source_name: str, chosen: Mapping[str, str], agent: str | None, env: Mapping[str, str]) -> RelayConfig:
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
