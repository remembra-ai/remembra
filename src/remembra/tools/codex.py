"""Codex installer helpers that avoid importing server-only dependencies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from remembra.tools.bridge import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_BRIDGE_UPSTREAM,
    parse_bridge_command,
)

DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_REMEMBRA_URL = "https://api.remembra.dev"
DEFAULT_REMEMBRA_COMMAND = "remembra-mcp"
DEFAULT_BRIDGE_COMMAND = "remembra-bridge"
DEFAULT_BRIDGE_LOG_DIR = Path.home() / ".remembra"
DEFAULT_BRIDGE_STDOUT = DEFAULT_BRIDGE_LOG_DIR / "bridge.stdout.log"
DEFAULT_BRIDGE_STDERR = DEFAULT_BRIDGE_LOG_DIR / "bridge.stderr.log"


@dataclass(slots=True)
class CodexInstallResult:
    """Result of installing or updating Codex MCP configuration."""

    config_path: Path
    command: str
    url: str
    project: str
    user_id: str
    created: bool
    bridge_url: str
    bridge_enabled: bool
    bridge_started: bool = False
    bridge_pid: int | None = None


def build_codex_mcp_block(
    command: str,
    url: str,
    api_key: str | None,
    project: str,
    user_id: str,
) -> str:
    """Build a TOML block for the Remembra Codex MCP server."""
    if not project:
        raise ValueError("project is required")
    if not user_id:
        raise ValueError("user_id is required")

    lines = [
        "# MCP Servers - Remembra Shared Memory",
        "[mcp_servers.remembra]",
        f"command = {json.dumps(command)}",
        "",
        "[mcp_servers.remembra.env]",
        f"REMEMBRA_URL = {json.dumps(url)}",
    ]
    if api_key:
        lines.append(f"REMEMBRA_API_KEY = {json.dumps(api_key)}")
    lines.extend(
        [
            f"REMEMBRA_PROJECT = {json.dumps(project)}",
            f"REMEMBRA_USER_ID = {json.dumps(user_id)}",
        ]
    )
    return "\n".join(lines)


def _is_table_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _is_remembra_table(line: str) -> bool:
    stripped = line.strip()
    return stripped == "[mcp_servers.remembra]" or stripped.startswith("[mcp_servers.remembra.")


def upsert_codex_mcp_block(content: str, block: str) -> str:
    """Insert or replace the Remembra MCP block in a Codex TOML config."""
    if not content.strip():
        return block.strip() + "\n"

    new_lines: list[str] = []
    skipping = False

    for line in content.splitlines():
        if _is_table_header(line):
            if _is_remembra_table(line):
                skipping = True
                continue
            if skipping:
                skipping = False

        if not skipping:
            new_lines.append(line)

    updated = "\n".join(new_lines).rstrip()
    if updated:
        updated += "\n\n"
    updated += block.strip() + "\n"
    return updated


def install_codex_config(
    config_path: Path,
    *,
    api_key: str,
    project: str,
    user_id: str,
    url: str = DEFAULT_REMEMBRA_URL,
    command: str = DEFAULT_REMEMBRA_COMMAND,
    use_bridge: bool = True,
    bridge_host: str = DEFAULT_BRIDGE_HOST,
    bridge_port: int = DEFAULT_BRIDGE_PORT,
) -> CodexInstallResult:
    """Create or update the Codex config file with Remembra MCP settings."""
    config_path.parent.mkdir(parents=True, exist_ok=True)

    created = not config_path.exists()
    existing = config_path.read_text() if config_path.exists() else ""
    target_url = build_bridge_url(bridge_host, bridge_port) if use_bridge else url
    mcp_api_key = None if use_bridge else api_key
    block = build_codex_mcp_block(
        command=command,
        url=target_url,
        api_key=mcp_api_key,
        project=project,
        user_id=user_id,
    )
    updated = upsert_codex_mcp_block(existing, block)
    config_path.write_text(updated)

    return CodexInstallResult(
        config_path=config_path,
        command=command,
        url=target_url,
        project=project,
        user_id=user_id,
        created=created,
        bridge_url=build_bridge_url(bridge_host, bridge_port),
        bridge_enabled=use_bridge,
    )


def build_bridge_url(host: str, port: int) -> str:
    """Build the local bridge URL advertised to Codex."""
    return f"http://{host}:{port}"


def resolve_bridge_launch_command(command: str | None) -> list[str]:
    """Resolve the bridge launch command for installed and repo-local runs."""
    argv = parse_bridge_command(command)
    if argv[0] == DEFAULT_BRIDGE_COMMAND and shutil.which(DEFAULT_BRIDGE_COMMAND) is None:
        repo_script = Path(__file__).resolve().parents[3] / "scripts" / "remembra_bridge.py"
        return [sys.executable, str(repo_script), *argv[1:]]
    return argv


def start_bridge_background(
    *,
    upstream: str,
    port: int,
    api_key: str,
    host: str = DEFAULT_BRIDGE_HOST,
    command: str | None = None,
    stdout_path: Path = DEFAULT_BRIDGE_STDOUT,
    stderr_path: Path = DEFAULT_BRIDGE_STDERR,
) -> int:
    """Start the local bridge as a detached background process."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    argv = resolve_bridge_launch_command(command)
    argv.extend(
        [
            "--upstream",
            upstream,
            "--host",
            host,
            "--port",
            str(port),
        ]
    )

    env = os.environ.copy()
    env["REMEMBRA_API_KEY"] = api_key

    with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
        process = subprocess.Popen(
            argv,
            stdout=stdout_file,
            stderr=stderr_file,
            env=env,
            start_new_session=True,
        )
    return process.pid


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for Codex installation."""
    parser = argparse.ArgumentParser(
        description="Install or update the Remembra MCP server for Codex.",
    )
    parser.add_argument("--api-key", required=True, help="Remembra API key")
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
        "--upstream-url",
        "--url",
        dest="upstream_url",
        default=DEFAULT_BRIDGE_UPSTREAM,
        help="Upstream Remembra URL used by direct mode or the local bridge",
    )
    parser.add_argument(
        "--command",
        default=DEFAULT_REMEMBRA_COMMAND,
        help="MCP command for the Remembra server",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CODEX_CONFIG,
        help="Codex TOML config path",
    )
    parser.add_argument(
        "--bridge-host",
        default=DEFAULT_BRIDGE_HOST,
        help="Local bridge host for sandboxed Codex sessions",
    )
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=DEFAULT_BRIDGE_PORT,
        help="Local bridge port for sandboxed Codex sessions",
    )
    parser.add_argument(
        "--no-bridge",
        action="store_true",
        help="Use the upstream URL directly instead of the local bridge",
    )
    parser.add_argument(
        "--start-bridge",
        action="store_true",
        help="Start the local bridge as a background process after installing",
    )
    parser.add_argument(
        "--bridge-command",
        default=DEFAULT_BRIDGE_COMMAND,
        help="Bridge launch command or executable",
    )
    return parser


def main() -> None:
    """CLI entry point for the Codex installer."""
    parser = build_parser()
    args = parser.parse_args()

    result = install_codex_config(
        args.config_path,
        api_key=args.api_key,
        project=args.project,
        user_id=args.user_id,
        url=args.upstream_url,
        command=args.command,
        use_bridge=not args.no_bridge,
        bridge_host=args.bridge_host,
        bridge_port=args.bridge_port,
    )

    if args.start_bridge and result.bridge_enabled:
        result.bridge_pid = start_bridge_background(
            upstream=args.upstream_url,
            port=args.bridge_port,
            api_key=args.api_key,
            host=args.bridge_host,
            command=args.bridge_command,
        )
        result.bridge_started = True

    print("Remembra Codex install complete")
    print(f"Config: {result.config_path}")
    print(f"Command: {result.command}")
    print(f"URL: {result.url}")
    print(f"Project: {result.project}")
    print(f"User: {result.user_id}")
    if result.bridge_enabled:
        print(f"Bridge URL: {result.bridge_url}")
    if result.bridge_started:
        print(f"Bridge PID: {result.bridge_pid}")


if __name__ == "__main__":
    main()
