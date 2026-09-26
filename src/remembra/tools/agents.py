"""Universal agent installer: points each detected agent's MCP config at remembra-mcp.

Most agents keep MCP servers in a JSON file under ``mcpServers``. Codex keeps
them in TOML (``~/.codex/config.toml``), so it goes through the Codex
installer in :mod:`remembra.tools.codex`. Claude Code reads its user-scope MCP
servers from ``~/.claude.json`` (what ``claude mcp add --scope user`` writes),
not from ``~/.claude/settings.json``; an entry an older installer put in
``settings.json`` is taken out again, since Claude Code never loaded it and it
held the key.

``remembra-install`` is a dry run by default: it shows each change as a diff
(the Remembra key masked, every other secret in the file hidden; see
:mod:`remembra.relay.config_view`) and writes only with ``--apply`` (or a "y"
on a terminal).
Every write keeps a backup of the old file, is atomic, and leaves the file
owner-only (0600) because it holds the key. The key is read from
``REMEMBRA_API_KEY``, a hidden prompt, ``--api-key-stdin`` or
``~/.remembra/credentials``; ``--api-key`` on the command line still works but
warns. Each agent's entry carries its own ``REMEMBRA_AGENT_ID``.
``--remove`` takes the Remembra entries out again, and lists the backups
that still hold the key (``--delete-backups`` deletes them too).

Without ``--url`` the server is the one already saved (``REMEMBRA_URL``,
``~/.remembra/credentials``, or an existing remembra entry), so re-running the
installer never moves a self-hosted setup to Remembra Cloud.

Windsurf is not verified: ``--all`` leaves it out, ``--agent windsurf``
writes it (see ``UNVERIFIED_AGENTS``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from remembra.relay.adapters.base import Change, backup_and_write
from remembra.tools.codex import (
    DEFAULT_CODEX_CONFIG,
    _is_remembra_table,
    install_codex_config,
    plan_codex_config,
    remove_codex_mcp_block,
)
from remembra.tools.keyinput import mask_key, resolve_api_key

# Default config paths for each agent
AGENT_CONFIGS = {
    "claude-desktop": Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
    # User scope: the file `claude mcp add --scope user` writes. settings.json is not read for MCP servers.
    "claude-code": Path.home() / ".claude.json",
    "gemini": Path.home() / ".gemini" / "settings.json",
    "cursor": Path.home() / ".cursor" / "mcp.json",
    # Windsurf's docs (docs.windsurf.com, now served from docs.devin.ai/windsurf/plugins/cascade/mcp) name
    # ~/.codeium/windsurf/mcp_config.json as the Windsurf Editor's file (read through its MCP discovery) and
    # ~/.config/devin/mcp_config.json as the file Cascade's "Open MCP config file" opens. Neither has been run
    # against Windsurf here: see UNVERIFIED_AGENTS.
    "windsurf": Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
    "codex": DEFAULT_CODEX_CONFIG,
}

# Agents whose config is not the JSON mcpServers shape.
TOML_AGENTS = {"codex"}

# Written only when named with --agent: the path follows the vendor's docs but has not been
# checked against the tool itself, so --all does not claim to set it up.
UNVERIFIED_AGENTS = {
    "windsurf": (
        "Windsurf is unverified: its docs name ~/.codeium/windsurf/mcp_config.json (the editor's MCP discovery; "
        "enable the 'windsurf' source in Settings > chat.mcp.discovery.enabled) and ~/.config/devin/mcp_config.json "
        "(what Cascade's 'Open MCP config file' opens). This writes the first; if Cascade does not list remembra, "
        "add the same block to the second."
    ),
}


def claude_code_old_config() -> Path:
    """Where remembra-install <= 0.16.0 put Claude Code's entry: ``~/.claude/settings.json``
    (next to the configured ``~/.claude.json``). Claude Code does not load MCP servers from
    it, so that entry, and the key in it, is taken out again."""
    return AGENT_CONFIGS["claude-code"].parent / ".claude" / "settings.json"


# Centralized credentials
REMEMBRA_HOME = Path.home() / ".remembra"
CREDENTIALS_FILE = REMEMBRA_HOME / "credentials"

DEFAULT_REMEMBRA_URL = "https://api.remembra.dev"
DEFAULT_REMEMBRA_COMMAND = "remembra-mcp"

# A Remembra key anywhere in a file (backups of agent configs and of the credentials file).
_KEY_IN_FILE_RE = re.compile(rb"\brem_[A-Za-z0-9_\-]{20,}")

# Exit code when there were changes to make but none was written (a dry run,
# or "no" at the prompt), so `remembra-install --all && remembra-relay connect
# --apply` stops there. 0 means everything is already in place or was written.
EXIT_NOT_WRITTEN = 3
MCP_BY_HAND_URL = "https://docs.remembra.dev/guides/relay/#mcp-by-hand"


def write_credentials(
    api_key: str,
    project: str = "default",
    user_id: str = "default",
    url: str = DEFAULT_REMEMBRA_URL,
) -> Path:
    """Write credentials to ~/.remembra/credentials with secure permissions.

    Returns the path to the credentials file.
    """
    REMEMBRA_HOME.mkdir(parents=True, exist_ok=True)

    credentials = {
        "api_key": api_key,
        "project": project,
        "user_id": user_id,
        "url": url,
    }

    change = Change(path=CREDENTIALS_FILE, before=None, after=json.dumps(credentials, indent=2), summary=[])
    backup_and_write(change, label="remembra", private=True)
    os.chmod(CREDENTIALS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return CREDENTIALS_FILE


def read_credentials() -> dict[str, str] | None:
    """Read credentials from ~/.remembra/credentials.

    Returns None if file doesn't exist or is invalid.
    """
    if not CREDENTIALS_FILE.exists():
        return None

    try:
        data: dict[str, str] = json.loads(CREDENTIALS_FILE.read_text())
        return data
    except (json.JSONDecodeError, OSError):
        return None


def get_api_key(cli_api_key: str | None = None) -> str | None:
    """Get API key from CLI argument, env var, or credentials file.

    Priority: CLI arg > env var > credentials file
    """
    if cli_api_key:
        return cli_api_key

    env_key = os.environ.get("REMEMBRA_API_KEY")
    if env_key:
        return env_key

    creds = read_credentials()
    if creds and creds.get("api_key"):
        return creds["api_key"]

    return None


@dataclass(slots=True)
class AgentInstallResult:
    """Result of installing or updating an agent's MCP configuration."""

    agent: str
    config_path: Path
    command: str
    url: str
    project: str
    user_id: str
    created: bool
    updated: bool


def build_mcp_server_config(
    command: str,
    url: str,
    api_key: str,
    project: str,
    user_id: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build the MCP server config block for Remembra (``agent_id`` sets REMEMBRA_AGENT_ID)."""
    if not api_key:
        raise ValueError("api_key is required")
    if not project:
        raise ValueError("project is required")
    if not user_id:
        raise ValueError("user_id is required")

    env = {
        "REMEMBRA_URL": url,
        "REMEMBRA_API_KEY": api_key,
        "REMEMBRA_PROJECT": project,
        "REMEMBRA_USER_ID": user_id,
    }
    if agent_id:
        env["REMEMBRA_AGENT_ID"] = agent_id
    if agent_id == "claude-code":
        # The shape `claude mcp add --scope user` writes.
        return {"type": "stdio", "command": command, "args": [], "env": env}
    return {"command": command, "env": env}


def _json_object(text: str | None, path: Path) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc.msg} at line {exc.lineno}); fix or move it, then re-run") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def plan_json_config(config_path: Path, server_config: dict[str, Any], *, first: bool = False) -> Change:
    """The JSON config with ``mcpServers.remembra`` set (not yet saved); an invalid file raises.

    With ``first``, a missing ``mcpServers`` goes at the top of the file, so the
    printed diff's context is the file's first lines rather than its last ones
    (``~/.claude.json`` also holds account details).
    """
    before = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    data = _json_object(before, config_path)
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ValueError(f"{config_path}: 'mcpServers' is not an object")
    existing = (servers or {}).get("remembra")
    if existing == server_config:
        return Change(path=config_path, before=before, after=before or "", summary=[])
    merged = {**(servers or {}), "remembra": server_config}
    new = {"mcpServers": merged, **data} if first and servers is None else {**data, "mcpServers": merged}
    after = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    summary = ["update the remembra MCP server" if existing is not None else "add the remembra MCP server"]
    return Change(path=config_path, before=before, after=after, summary=summary)


def plan_json_removal(config_path: Path, *, keep_file: bool = False) -> Change:
    """The JSON config without ``mcpServers.remembra`` (unchanged when it has none).

    The file is deleted when nothing else is left in it, unless ``keep_file``.
    """
    before = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    if before is None:
        return Change(path=config_path, before=None, after="", summary=[], delete=True)
    data = _json_object(before, config_path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "remembra" not in servers:
        return Change(path=config_path, before=before, after=before, summary=[])
    new = dict(data)
    rest = {k: v for k, v in servers.items() if k != "remembra"}
    if rest:
        new["mcpServers"] = rest
    else:
        del new["mcpServers"]
    after = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    return Change(
        path=config_path, before=before, after=after, summary=["remove the remembra MCP server"], delete=not new and not keep_file
    )


def plan_claude_code_old_entry() -> Change | None:
    """Taking out the entry an older installer wrote to ~/.claude/settings.json (None when there is none)."""
    path = claude_code_old_config()
    if not path.is_file():
        return None
    change = plan_json_removal(path, keep_file=True)
    if not change.changed:
        return None
    change.summary = ["remove the old remembra entry (Claude Code never loaded it from settings.json; it held the key)"]
    return change


def agent_detected(agent: str) -> bool:
    """True when ``agent`` looks installed: its config file's directory exists (Claude Code: ``~/.claude``)."""
    config_path = AGENT_CONFIGS[agent]
    if config_path.name == ".claude.json":  # it sits in HOME, which always exists: look for ~/.claude too
        return config_path.exists() or (config_path.parent / ".claude").is_dir()
    return config_path.parent.exists()


def upsert_mcp_config(
    config_path: Path,
    server_config: dict[str, Any],
) -> tuple[dict[str, Any], bool, bool]:
    """
    Insert or replace the Remembra MCP block in a JSON config.

    Returns: (updated_config, was_created, was_updated)
    """
    created = False
    updated = False

    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except json.JSONDecodeError:
            # Backup corrupted file
            backup = config_path.with_suffix(".json.bak")
            config_path.rename(backup)
            config = {}
            created = True
    else:
        config = {}
        created = True

    # Ensure mcpServers exists
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    # Check if we're updating existing config
    if "remembra" in config["mcpServers"]:
        updated = True

    # Set the remembra config
    config["mcpServers"]["remembra"] = server_config

    return config, created, updated


def install_agent_config(
    agent: str,
    config_path: Path | None = None,
    *,
    api_key: str,
    project: str,
    user_id: str,
    url: str = DEFAULT_REMEMBRA_URL,
    command: str = DEFAULT_REMEMBRA_COMMAND,
) -> AgentInstallResult:
    """Install or update the Remembra MCP config for one agent."""
    if config_path is None:
        if agent not in AGENT_CONFIGS:
            raise ValueError(f"Unknown agent: {agent}")
        config_path = AGENT_CONFIGS[agent]

    if agent in TOML_AGENTS:
        if not api_key:
            raise ValueError("api_key is required")
        before = config_path.read_text() if config_path.exists() else ""
        had_remembra = any(_is_remembra_table(line) for line in before.splitlines())
        codex = install_codex_config(
            config_path,
            api_key=api_key,
            project=project,
            user_id=user_id,
            url=url,
            command=command,
            # Direct to the URL like every other agent. A Codex sandbox with no
            # network needs the local bridge: remembra-install-codex sets that up.
            use_bridge=False,
        )
        return AgentInstallResult(
            agent=agent,
            config_path=codex.config_path,
            command=codex.command,
            url=codex.url,
            project=codex.project,
            user_id=codex.user_id,
            created=codex.created,
            updated=had_remembra,
        )

    server_config = build_mcp_server_config(
        command=command,
        url=url,
        api_key=api_key,
        project=project,
        user_id=user_id,
        agent_id=agent,
    )
    change = plan_json_config(config_path, server_config, first=agent == "claude-code")
    created = change.before is None
    had = (_json_object(change.before, config_path).get("mcpServers") or {}) if change.before else {}
    updated = isinstance(had, dict) and "remembra" in had
    if change.changed:
        backup_and_write(change, label="remembra", private=True)

    return AgentInstallResult(
        agent=agent,
        config_path=config_path,
        command=command,
        url=url,
        project=project,
        user_id=user_id,
        created=created,
        updated=updated,
    )


def install_all_agents(
    *,
    api_key: str,
    project: str,
    user_id: str,
    url: str = DEFAULT_REMEMBRA_URL,
    command: str = DEFAULT_REMEMBRA_COMMAND,
) -> list[AgentInstallResult]:
    """
    Install MCP config for all detected agents.

    Only installs to agents whose config directories exist.
    """
    results: list[AgentInstallResult] = []

    for agent, config_path in AGENT_CONFIGS.items():
        # Only install if the agent looks installed (see agent_detected).
        if agent_detected(agent):
            try:
                result = install_agent_config(
                    agent,
                    config_path,
                    api_key=api_key,
                    project=project,
                    user_id=user_id,
                    url=url,
                    command=command,
                )
                results.append(result)
            except Exception as e:
                print(f"[!] Warning: Failed to configure {agent}: {e}")

    return results


def detect_agents(include_unverified: bool = True) -> list[str]:
    """Detect which agents are installed based on config directory existence."""
    return [agent for agent in AGENT_CONFIGS if agent_detected(agent) and (include_unverified or agent not in UNVERIFIED_AGENTS)]


def _entry_url(agent: str, config_path: Path) -> str | None:
    """``REMEMBRA_URL`` of the remembra entry already in ``agent``'s config, if any."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        if agent in TOML_AGENTS:
            server = (tomllib.loads(text).get("mcp_servers") or {}).get("remembra") or {}
        else:
            data = json.loads(text)
            server = (data.get("mcpServers") or {}).get("remembra") or {} if isinstance(data, dict) else {}
    except (ValueError, AttributeError):
        return None
    env = server.get("env") if isinstance(server, dict) else None
    url = env.get("REMEMBRA_URL") if isinstance(env, dict) else None
    return url.strip() if isinstance(url, str) and url.strip() else None


def resolve_url(cli_url: str | None, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The server to write, and where it came from.

    ``--url`` > ``REMEMBRA_URL`` > the saved ``~/.remembra/credentials`` > an
    existing remembra entry > Remembra Cloud. A re-run without ``--url`` keeps
    the server already set up (a self-hosted URL is never swapped for the cloud
    while the key, which belongs to that server, is kept).
    """
    env = os.environ if environ is None else environ
    if cli_url and cli_url.strip():
        return cli_url.strip(), "--url"
    if (env.get("REMEMBRA_URL") or "").strip():
        return str(env["REMEMBRA_URL"]).strip(), "REMEMBRA_URL"
    saved = read_credentials() or {}
    if isinstance(saved.get("url"), str) and saved["url"].strip():
        return saved["url"].strip(), str(CREDENTIALS_FILE)
    for agent, path in AGENT_CONFIGS.items():
        url = _entry_url(agent, path)
        if url:
            return url, f"the remembra entry in {path}"
    return DEFAULT_REMEMBRA_URL, "default (Remembra Cloud)"


def backups_holding_a_key(home: Path | None = None) -> list[Path]:
    """Backups (``*.bak-*``) the installer and the relay kept next to agent configs that still hold a Remembra key."""
    from remembra.relay.adapters import REGISTRY

    home = home or Path.home()
    files = {*AGENT_CONFIGS.values(), claude_code_old_config(), CREDENTIALS_FILE}
    files.update(adapter.spec.config_path(home) for adapter in REGISTRY.values())
    found: set[Path] = set()
    for path in files:
        try:
            candidates = list(path.parent.glob(f"{path.name}.bak-*"))
        except OSError:
            continue
        for backup in candidates:
            try:
                if backup.is_file() and _KEY_IN_FILE_RE.search(backup.read_bytes()):
                    found.add(backup)
            except OSError:
                continue
    return sorted(found)


def plan_agent(
    agent: str,
    config_path: Path,
    *,
    api_key: str,
    project: str,
    user_id: str,
    url: str,
    command: str,
) -> Change:
    """What writing ``agent``'s Remembra entry would change (nothing is written)."""
    if agent in TOML_AGENTS:
        return plan_codex_config(
            config_path, api_key=api_key, project=project, user_id=user_id, url=url, command=command, agent_id=agent
        )
    server = build_mcp_server_config(command=command, url=url, api_key=api_key, project=project, user_id=user_id, agent_id=agent)
    return plan_json_config(config_path, server, first=agent == "claude-code")


def plan_agent_removal(agent: str, config_path: Path) -> Change:
    """What removing ``agent``'s Remembra entry would change."""
    if agent in TOML_AGENTS:
        before = config_path.read_text() if config_path.exists() else None
        if before is None:
            return Change(path=config_path, before=None, after="", summary=[], delete=True)
        if not any(_is_remembra_table(line) for line in before.splitlines()):
            return Change(path=config_path, before=before, after=before, summary=[])
        after = remove_codex_mcp_block(before)
        return Change(
            path=config_path, before=before, after=after, summary=["remove [mcp_servers.remembra]"], delete=not after.strip()
        )
    # ~/.claude.json is Claude Code's own state file: never deleted, even when nothing else is in it.
    return plan_json_removal(config_path, keep_file=agent == "claude-code")


def plan_credentials(api_key: str, project: str, user_id: str, url: str) -> Change:
    before = CREDENTIALS_FILE.read_text() if CREDENTIALS_FILE.exists() else None
    after = json.dumps({"api_key": api_key, "project": project, "user_id": user_id, "url": url}, indent=2)
    try:
        same = before is not None and json.loads(before) == json.loads(after)
    except ValueError:
        same = False
    if same:
        return Change(path=CREDENTIALS_FILE, before=before, after=before or "", summary=[])
    return Change(path=CREDENTIALS_FILE, before=before, after=after, summary=["save the key where remembra-relay reads it"])


def _loose(path: Path) -> bool:
    """True when group or others can read ``path``."""
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for agent installation."""
    parser = argparse.ArgumentParser(
        prog="remembra-install",
        description="Add (or --remove) the Remembra MCP server in your AI agents' configs. A dry run unless --apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Agents: Claude Desktop, Claude Code (user scope, ~/.claude.json), Codex,
Cursor and Gemini CLI. --all configures each one whose config directory
exists. Windsurf is unverified: only --agent windsurf writes it
(~/.codeium/windsurf/mcp_config.json). Qwen Code and Kimi are not written
here yet; add remembra-mcp to them by hand:
https://docs.remembra.dev/guides/relay/#mcp-by-hand

Without --url the server already set up is kept (REMEMBRA_URL, then
~/.remembra/credentials, then an existing remembra entry); a first install
defaults to https://api.remembra.dev.

The API key is read from REMEMBRA_API_KEY, a hidden prompt (on a terminal),
--api-key-stdin, or ~/.remembra/credentials. Never type it on the command
line: shell history keeps it. The key is saved to ~/.remembra/credentials
(where remembra-relay reads it) even when no agent is detected.

Exit codes: 0 written, or nothing to change; 1 a config could not be read or
written; 2 bad arguments or no key; 3 changes shown but not written (no
--apply, or "no" at the prompt), so a command chained after && does not run.

Examples:
  remembra-install --all                          show the changes, then ask
  remembra-install --all --apply                  write without asking
  pbpaste | remembra-install --all --api-key-stdin --apply
  remembra-install --agent cursor --project myproject
  remembra-install --remove --all --apply         take Remembra out again
  remembra-install --remove --all --apply --delete-backups
                                                  ...and delete the backups that hold the key
        """,
    )
    parser.add_argument("--version", action="version", version=f"remembra-install {_version()}")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="All detected agents")
    group.add_argument("--agent", choices=list(AGENT_CONFIGS.keys()), help="One agent")
    group.add_argument("--detect", action="store_true", help="Only list the detected agents")

    parser.add_argument("--apply", "--yes", dest="apply", action="store_true", help="Write the changes (backups are kept)")
    parser.add_argument("--remove", action="store_true", help="Remove the Remembra MCP entries instead of adding them")
    parser.add_argument(
        "--delete-backups",
        action="store_true",
        help="With --remove --apply: also delete the *.bak-* backups that still hold a Remembra key",
    )
    parser.add_argument(
        "--api-key-stdin", action="store_true", help="Read the API key from stdin (e.g. piped from a password manager)"
    )
    # Deprecated: a key on the command line lands in shell history and `ps`. Still accepted, with a warning.
    parser.add_argument("--api-key", help=argparse.SUPPRESS)
    parser.add_argument("--project", default="default", help="Project namespace for shared memories")
    parser.add_argument("--user-id", default="default", help="User identifier for shared memories")
    parser.add_argument(
        "--url",
        default=None,
        help=f"Remembra base URL or local bridge URL (default: the one already set up, else {DEFAULT_REMEMBRA_URL})",
    )
    parser.add_argument("--command", default=DEFAULT_REMEMBRA_COMMAND, help="MCP command for the Remembra server")
    return parser


def _version() -> str:
    try:
        from remembra import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _confirm(question: str) -> bool:
    """y/N on a terminal; False when there is none."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        return input(question).strip().lower() in ("y", "yes")
    except (EOFError, OSError, ValueError):
        return False


def _show(agent: str, change: Change, keys: tuple[str, ...], removing: bool = False) -> None:
    print(f"\n[{agent}] {change.path}")
    if not change.changed:
        print("  no Remembra entry, nothing to remove" if removing else "  already up to date, no change")
        return
    for line in change.summary:
        print(f"  - {line}")
    diff = change.diff(keys=keys)
    if diff:
        print("  " + diff.replace("\n", "\n  ").rstrip())


def _write_all(changes: list[tuple[str, Change]]) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = 0
    for agent, change in changes:
        try:
            backup = backup_and_write(change, stamp, label="remembra", private=True)
        except OSError as e:
            print(f"[!] {agent}: could not write {change.path}: {e}", file=sys.stderr)
            failed += 1
            continue
        verb = "removed" if change.delete else "written (0600)"
        print(f"[+] {agent}: {verb} {change.path}{f' (backup: {backup})' if backup else ''}")
    return failed


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for agent installation. Returns the exit code (``EXIT_NOT_WRITTEN`` for a dry run)."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.detect:
        detected = detect_agents()
        if detected:
            print("Detected agents:")
            for agent in detected:
                print(f"  ✓ {agent}")
        else:
            print("No agents detected.")
        return 0

    if args.delete_backups and not args.remove:
        parser.error("--delete-backups goes with --remove")
    agents = detect_agents(include_unverified=args.remove) if args.all else [args.agent]
    skipped = [a for a in detect_agents() if a in UNVERIFIED_AGENTS and a not in agents] if args.all else []
    for agent in skipped:
        print(f"[{agent}] detected, not set up by --all. {UNVERIFIED_AGENTS[agent]} Run: remembra-install --agent {agent}")
    for agent in agents:
        if agent in UNVERIFIED_AGENTS and not args.remove:
            print(f"[{agent}] {UNVERIFIED_AGENTS[agent]}")
    if not agents and args.remove:
        print("No agents detected: no MCP entry to remove.")
        return _report_backups(args)
    if not agents:
        # Still save the key: remembra-relay's hooks (Qwen Code, Kimi, ...) read it from ~/.remembra/credentials.
        print(
            "No agent MCP config found (Claude Desktop, Claude Code, Codex, Cursor, Gemini CLI)."
            f" This saves only the key (~/.remembra/credentials). For other agents add remembra-mcp by hand: {MCP_BY_HAND_URL}"
        )

    planned: list[tuple[str, Change]] = []
    problems = 0
    keys: tuple[str, ...] = ()
    if args.remove:
        print("Remembra: removing the remembra MCP server from your agents")
        for agent in agents:
            try:
                planned.append((agent, plan_agent_removal(agent, AGENT_CONFIGS[agent])))
            except (OSError, ValueError) as e:
                print(f"\n[{agent}] cannot read its config: {e}")
                problems += 1
    else:
        api_key, source = resolve_api_key(
            cli_key=args.api_key, from_stdin=args.api_key_stdin, credentials=CREDENTIALS_FILE, environ=os.environ
        )
        if not api_key:
            parser.error(
                f"no API key ({source}). Create one in the Remembra dashboard (API keys), then set REMEMBRA_API_KEY,"
                " pipe it with --api-key-stdin, or run this in a terminal to be asked for it."
            )
        keys = (api_key,)
        url, url_source = resolve_url(args.url)
        args.url = url
        print(f"Remembra: key {mask_key(api_key)} (from {source}), server {url} (from {url_source}), project {args.project}")
        planned.append(("credentials", plan_credentials(api_key, args.project, args.user_id, url)))
        for agent in agents:
            try:
                planned.append(
                    (
                        agent,
                        plan_agent(
                            agent,
                            AGENT_CONFIGS[agent],
                            api_key=api_key,
                            project=args.project,
                            user_id=args.user_id,
                            url=args.url,
                            command=args.command,
                        ),
                    )
                )
            except (OSError, ValueError) as e:
                print(f"\n[{agent}] cannot read its config: {e}")
                problems += 1

    if "claude-code" in agents:
        try:
            old = plan_claude_code_old_entry()
        except (OSError, ValueError) as e:
            print(f"\n[claude-code] cannot read {claude_code_old_config()}: {e}")
            old = None
            problems += 1
        if old is not None:
            planned.append(("claude-code (old entry)", old))

    for agent, change in planned:
        _show(agent, change, keys, removing=args.remove)
    todo = [(agent, change) for agent, change in planned if change.changed]
    # Files that already hold a key but are readable by others get tightened even without a content change.
    loose = [(a, c) for a, c in planned if not c.changed and c.before is not None and not args.remove and _loose(c.path)]
    for agent, change in loose:
        print(f"  [{agent}] {change.path} is readable by other users: it will be made owner-only (0600)")

    if not todo and not loose:
        print("\nNothing to change.")
        if args.remove:
            return _report_backups(args) or (1 if problems else 0)
        return 1 if problems else 0
    if not args.apply and not _confirm(f"\nWrite {len(todo) + len(loose)} file(s)? Backups are kept. [y/N] "):
        print("\nDry run: nothing was written. Re-run with --apply to write (a backup of each file is kept).")
        if args.remove:
            _report_backups(args, dry_run=True)
        return 1 if problems else EXIT_NOT_WRITTEN

    failed = _write_all(todo)
    for agent, change in loose:
        try:
            os.chmod(change.path, 0o600)
            print(f"[+] {agent}: {change.path} is now owner-only (0600)")
        except OSError as e:
            print(f"[!] {agent}: could not chmod {change.path}: {e}", file=sys.stderr)
            failed += 1
    if args.remove:
        failed += _report_backups(args)
        print(
            "\nThe saved key is still in ~/.remembra/credentials (remembra-relay reads it). To remove everything:"
            " remembra-relay disconnect --apply, pipx uninstall remembra, delete ~/.remembra and revoke the key"
            " in the dashboard (API keys)."
        )
    else:
        restart = "Restart your agents to load the new MCP config. " if agents else ""
        print(f"\n{restart}Next: remembra-relay connect")
    return 1 if (failed or problems) else 0


def _report_backups(args: argparse.Namespace, dry_run: bool = False) -> int:
    """List (or, with ``--delete-backups --apply``, delete) the backups that still hold a Remembra key.

    Returns the number of backups that could not be deleted.
    """
    backups = backups_holding_a_key()
    if not backups:
        return 0
    if args.delete_backups and args.apply and not dry_run:
        failed = 0
        for backup in backups:
            try:
                backup.unlink()
                print(f"[+] deleted backup {backup}")
            except OSError as e:
                print(f"[!] could not delete {backup}: {e}", file=sys.stderr)
                failed += 1
        return failed
    verb = "would be deleted with --apply" if args.delete_backups else "are kept"
    print(f"\n{len(backups)} backup file(s) still hold a Remembra API key and {verb}:")
    for backup in backups:
        print(f"  {backup}")
    if not args.delete_backups:
        print(
            "Delete them with: remembra-install --remove --all --apply --delete-backups"
            " (or by hand), and revoke the key in the dashboard (API keys)."
        )
    return 0


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
