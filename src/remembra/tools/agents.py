"""Universal agent installer for JSON-based MCP configs."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Default config paths for each agent
AGENT_CONFIGS = {
    "claude-desktop": Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
    "claude-code": Path.home() / ".claude" / "settings.json",
    "gemini": Path.home() / ".gemini" / "settings.json",
    "cursor": Path.home() / ".cursor" / "mcp.json",
    "windsurf": Path.home() / ".windsurf" / "mcp_config.json",
}

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
    
    # Write credentials
    CREDENTIALS_FILE.write_text(json.dumps(credentials, indent=2))
    
    # Secure with chmod 600 (owner read/write only)
    os.chmod(CREDENTIALS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    
    return CREDENTIALS_FILE


def read_credentials() -> dict[str, str] | None:
    """Read credentials from ~/.remembra/credentials.
    
    Returns None if file doesn't exist or is invalid.
    """
    if not CREDENTIALS_FILE.exists():
        return None
    
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
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
) -> dict[str, Any]:
    """Build the MCP server config block for Remembra."""
    if not api_key:
        raise ValueError("api_key is required")
    if not project:
        raise ValueError("project is required")
    if not user_id:
        raise ValueError("user_id is required")

    return {
        "command": command,
        "env": {
            "REMEMBRA_URL": url,
            "REMEMBRA_API_KEY": api_key,
            "REMEMBRA_PROJECT": project,
            "REMEMBRA_USER_ID": user_id,
        },
    }


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
    """Install or update MCP config for a JSON-based agent."""
    if config_path is None:
        if agent not in AGENT_CONFIGS:
            raise ValueError(f"Unknown agent: {agent}")
        config_path = AGENT_CONFIGS[agent]

    # Ensure parent directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Build the server config
    server_config = build_mcp_server_config(
        command=command,
        url=url,
        api_key=api_key,
        project=project,
        user_id=user_id,
    )

    # Upsert into config file
    config, created, updated = upsert_mcp_config(config_path, server_config)

    # Write the updated config
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

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


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for agent installation."""
    parser = argparse.ArgumentParser(
        description="Install Remembra MCP for AI agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  remembra-install --all --api-key rem_xxx
  remembra-install --agent claude-desktop --api-key rem_xxx
  remembra-install --agent cursor --api-key rem_xxx --project myproject
        """,
    )
    
    # Agent selection
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Install for all detected agents",
    )
    group.add_argument(
        "--agent",
        choices=list(AGENT_CONFIGS.keys()),
        help="Install for a specific agent",
    )
    group.add_argument(
        "--detect",
        action="store_true",
        help="Only detect installed agents, don't install",
    )

    # Required args
    parser.add_argument(
        "--api-key",
        help="Remembra API key (required for install)",
    )

    # Optional args
    parser.add_argument(
        "--project",
        default="default",
        help="Project namespace for shared memories",
    )
    parser.add_argument(
        "--user-id",
        default="default",
        help="User identifier for shared memories",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_REMEMBRA_URL,
        help="Remembra base URL or local bridge URL",
    )
    parser.add_argument(
        "--command",
        default=DEFAULT_REMEMBRA_COMMAND,
        help="MCP command for the Remembra server",
    )

    return parser


def main() -> None:
    """CLI entry point for agent installation."""
    parser = build_parser()
    args = parser.parse_args()

    # Detection mode
    if args.detect:
        detected = detect_agents()
        if detected:
            print("Detected agents:")
            for agent in detected:
                print(f"  ✓ {agent}")
        else:
            print("No agents detected.")
        return

    # Get API key from CLI, env var, or credentials file
    api_key = get_api_key(args.api_key)
    
    if not api_key:
        # Check if credentials file exists for better error message
        if CREDENTIALS_FILE.exists():
            parser.error(
                f"Credentials file exists at {CREDENTIALS_FILE} but contains no API key. "
                "Pass --api-key or set REMEMBRA_API_KEY environment variable."
            )
        else:
            parser.error(
                "--api-key is required for installation. "
                "You can also set REMEMBRA_API_KEY environment variable."
            )
    
    # Save credentials for future use
    creds_path = write_credentials(
        api_key=api_key,
        project=args.project,
        user_id=args.user_id,
        url=args.url,
    )
    print(f"📁 Credentials saved to {creds_path}")

    if args.all:
        print("🧠 Remembra Universal Agent Installer")
        print("=" * 50)
        
        results = install_all_agents(
            api_key=api_key,
            project=args.project,
            user_id=args.user_id,
            url=args.url,
            command=args.command,
        )

        if not results:
            print("No agents detected. Install an agent first.")
            return

        for result in results:
            status = "created" if result.created else "updated"
            print(f"[+] ✅ {result.agent} ({status})")

        print("=" * 50)
        print(f"🎉 Done! {len(results)} agents now share a single brain.")

    else:
        result = install_agent_config(
            args.agent,
            api_key=api_key,
            project=args.project,
            user_id=args.user_id,
            url=args.url,
            command=args.command,
        )

        print(f"Remembra {args.agent} install complete")
        print(f"Config: {result.config_path}")
        print(f"Command: {result.command}")
        print(f"URL: {result.url}")
        print(f"Project: {result.project}")
        print(f"User: {result.user_id}")


if __name__ == "__main__":
    main()
