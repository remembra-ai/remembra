"""Universal agent installer: points each detected agent's MCP config at remembra-mcp.

Most agents keep MCP servers in a JSON file under ``mcpServers``. Codex keeps
them in TOML (``~/.codex/config.toml``), so it goes through the Codex
installer in :mod:`remembra.tools.codex`.

``remembra-install`` is a dry run by default: it shows each change as a diff
(API keys masked) and writes only with ``--apply`` (or a "y" on a terminal).
Every write keeps a backup of the old file, is atomic, and leaves the file
owner-only (0600) because it holds the key. The key is read from
``REMEMBRA_API_KEY``, a hidden prompt, ``--api-key-stdin`` or
``~/.remembra/credentials``; ``--api-key`` on the command line still works but
warns. Each agent's entry carries its own ``REMEMBRA_AGENT_ID``.
``--remove`` takes the Remembra entries out again.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
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
from remembra.tools.keyinput import mask_key, mask_text, resolve_api_key

# Default config paths for each agent
AGENT_CONFIGS = {
    "claude-desktop": Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
    "claude-code": Path.home() / ".claude" / "settings.json",
    "gemini": Path.home() / ".gemini" / "settings.json",
    "cursor": Path.home() / ".cursor" / "mcp.json",
    "windsurf": Path.home() / ".windsurf" / "mcp_config.json",
    "codex": DEFAULT_CODEX_CONFIG,
}

# Agents whose config is not the JSON mcpServers shape.
TOML_AGENTS = {"codex"}

# Centralized credentials
REMEMBRA_HOME = Path.home() / ".remembra"
CREDENTIALS_FILE = REMEMBRA_HOME / "credentials"

DEFAULT_REMEMBRA_URL = "https://api.remembra.dev"
DEFAULT_REMEMBRA_COMMAND = "remembra-mcp"


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


def plan_json_config(config_path: Path, server_config: dict[str, Any]) -> Change:
    """The JSON config with ``mcpServers.remembra`` set (not yet saved); an invalid file raises."""
    before = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    data = _json_object(before, config_path)
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ValueError(f"{config_path}: 'mcpServers' is not an object")
    existing = (servers or {}).get("remembra")
    if existing == server_config:
        return Change(path=config_path, before=before, after=before or "", summary=[])
    new = dict(data)
    new["mcpServers"] = {**(servers or {}), "remembra": server_config}
    after = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    summary = ["update the remembra MCP server" if existing is not None else "add the remembra MCP server"]
    return Change(path=config_path, before=before, after=after, summary=summary)


def plan_json_removal(config_path: Path) -> Change:
    """The JSON config without ``mcpServers.remembra`` (unchanged when it has none)."""
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
    return Change(path=config_path, before=before, after=after, summary=["remove the remembra MCP server"], delete=not new)


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
    change = plan_json_config(config_path, server_config)
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
        # Only install if the agent's config directory exists
        # (indicates the agent is installed)
        if config_path.parent.exists():
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


def detect_agents() -> list[str]:
    """Detect which agents are installed based on config directory existence."""
    detected = []
    for agent, config_path in AGENT_CONFIGS.items():
        if config_path.parent.exists():
            detected.append(agent)
    return detected


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
    return plan_json_config(config_path, server)


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
    return plan_json_removal(config_path)


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
Agents: Claude Desktop, Claude Code, Codex, Cursor, Gemini CLI and Windsurf.
--all configures each one whose config directory exists. Qwen Code and Kimi
are not written here yet; add remembra-mcp to them by hand:
https://docs.remembra.dev/guides/relay/#mcp-by-hand

The API key is read from REMEMBRA_API_KEY, a hidden prompt (on a terminal),
--api-key-stdin, or ~/.remembra/credentials. Never type it on the command
line: shell history keeps it.

Examples:
  remembra-install --all                          show the changes, then ask
  remembra-install --all --apply                  write without asking
  pbpaste | remembra-install --all --api-key-stdin --apply
  remembra-install --agent cursor --project myproject
  remembra-install --remove --all --apply         take Remembra out again
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="All detected agents")
    group.add_argument("--agent", choices=list(AGENT_CONFIGS.keys()), help="One agent")
    group.add_argument("--detect", action="store_true", help="Only list the detected agents")

    parser.add_argument("--apply", "--yes", dest="apply", action="store_true", help="Write the changes (backups are kept)")
    parser.add_argument("--remove", action="store_true", help="Remove the Remembra MCP entries instead of adding them")
    parser.add_argument(
        "--api-key-stdin", action="store_true", help="Read the API key from stdin (e.g. piped from a password manager)"
    )
    # Deprecated: a key on the command line lands in shell history and `ps`. Still accepted, with a warning.
    parser.add_argument("--api-key", help=argparse.SUPPRESS)
    parser.add_argument("--project", default="default", help="Project namespace for shared memories")
    parser.add_argument("--user-id", default="default", help="User identifier for shared memories")
    parser.add_argument("--url", default=DEFAULT_REMEMBRA_URL, help="Remembra base URL or local bridge URL")
    parser.add_argument("--command", default=DEFAULT_REMEMBRA_COMMAND, help="MCP command for the Remembra server")
    return parser


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
    diff = change.diff(mask=lambda line: mask_text(line, keys))
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
    """CLI entry point for agent installation. Returns the exit code (0 also for a dry run)."""
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

    agents = detect_agents() if args.all else [args.agent]
    if not agents:
        print("No agents detected. Install an agent first, or name one with --agent.")
        return 0

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
        print(f"Remembra: key {mask_key(api_key)} (from {source}), server {args.url}, project {args.project}")
        planned.append(("credentials", plan_credentials(api_key, args.project, args.user_id, args.url)))
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

    for agent, change in planned:
        _show(agent, change, keys, removing=args.remove)
    todo = [(agent, change) for agent, change in planned if change.changed]
    # Files that already hold a key but are readable by others get tightened even without a content change.
    loose = [(a, c) for a, c in planned if not c.changed and c.before is not None and not args.remove and _loose(c.path)]
    for agent, change in loose:
        print(f"  [{agent}] {change.path} is readable by other users: it will be made owner-only (0600)")

    if not todo and not loose:
        print("\nNothing to change.")
        return 1 if problems else 0
    if not args.apply and not _confirm(f"\nWrite {len(todo) + len(loose)} file(s)? Backups are kept. [y/N] "):
        print("\nDry run: nothing was written. Re-run with --apply to write (a backup of each file is kept).")
        return 1 if problems else 0

    failed = _write_all(todo)
    for agent, change in loose:
        try:
            os.chmod(change.path, 0o600)
            print(f"[+] {agent}: {change.path} is now owner-only (0600)")
        except OSError as e:
            print(f"[!] {agent}: could not chmod {change.path}: {e}", file=sys.stderr)
            failed += 1
    if args.remove:
        print(
            "\nThe saved key is still in ~/.remembra/credentials (remembra-relay reads it). To remove everything:"
            " remembra-relay disconnect --apply, pipx uninstall remembra, delete ~/.remembra and revoke the key"
            " in the dashboard (API keys)."
        )
    else:
        print("\nRestart your agents to load the new MCP config. Next: remembra-relay connect")
    return 1 if (failed or problems) else 0


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
