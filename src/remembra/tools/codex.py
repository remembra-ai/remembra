"""Codex installer helpers that avoid importing server-only dependencies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from remembra.relay.adapters.base import Change, backup_and_write
from remembra.tools.bridge import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_BRIDGE_UPSTREAM,
    DEFAULT_PID_FILE,
    BridgePortInUseError,
    BridgeStartupError,
    check_port_available,
    is_process_running,
    parse_bridge_command,
    read_pid_file,
    stop_bridge,
    wait_for_healthy,
)
from remembra.tools.keyinput import mask_key, resolve_api_key

DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_REMEMBRA_URL = "https://api.remembra.dev"
DEFAULT_REMEMBRA_COMMAND = "remembra-mcp"
DEFAULT_BRIDGE_COMMAND = "remembra-bridge"
DEFAULT_BRIDGE_LOG_DIR = Path.home() / ".remembra"
DEFAULT_BRIDGE_STDOUT = DEFAULT_BRIDGE_LOG_DIR / "bridge.stdout.log"
DEFAULT_BRIDGE_STDERR = DEFAULT_BRIDGE_LOG_DIR / "bridge.stderr.log"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


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
    agent_id: str | None = "codex",
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
    if agent_id:
        lines.append(f"REMEMBRA_AGENT_ID = {json.dumps(agent_id)}")
    return "\n".join(lines)


def _is_table_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _is_remembra_table(line: str) -> bool:
    stripped = line.strip()
    return stripped == "[mcp_servers.remembra]" or stripped.startswith("[mcp_servers.remembra.")


def upsert_codex_mcp_block(content: str, block: str) -> str:
    """Insert or replace the Remembra MCP block in a Codex TOML config (idempotent)."""
    rest = remove_codex_mcp_block(content).rstrip()
    return (rest + "\n\n" if rest else "") + block.strip() + "\n"


CODEX_BLOCK_COMMENT = "# MCP Servers - Remembra Shared Memory"


def remove_codex_mcp_block(content: str) -> str:
    """``content`` without the ``[mcp_servers.remembra*]`` tables (and the comment this installer puts above them)."""
    lines = content.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        if _is_table_header(line):
            skipping = _is_remembra_table(line)
            if skipping:
                while kept and kept[-1].strip() in ("", CODEX_BLOCK_COMMENT):
                    if kept[-1].strip() == CODEX_BLOCK_COMMENT:
                        kept.pop()
                        break
                    kept.pop()
                continue
        if not skipping:
            kept.append(line)
    text = "\n".join(kept).rstrip()
    return text + "\n" if text else ""


def plan_codex_config(
    config_path: Path,
    *,
    api_key: str | None,
    project: str,
    user_id: str,
    url: str,
    command: str,
    agent_id: str | None = "codex",
) -> Change:
    """The Codex config with the Remembra block written (not yet saved)."""
    before = config_path.read_text() if config_path.exists() else None
    block = build_codex_mcp_block(command=command, url=url, api_key=api_key, project=project, user_id=user_id, agent_id=agent_id)
    after = upsert_codex_mcp_block(before or "", block)
    summary = (
        []
        if after == before
        else [f"{'update' if before and '[mcp_servers.remembra]' in before else 'add'} [mcp_servers.remembra]"]
    )
    return Change(path=config_path, before=before, after=after, summary=summary)


def install_codex_config(
    config_path: Path,
    *,
    api_key: str,
    project: str,
    user_id: str,
    url: str = DEFAULT_REMEMBRA_URL,
    command: str = DEFAULT_REMEMBRA_COMMAND,
    use_bridge: bool | None = None,
    bridge_host: str = DEFAULT_BRIDGE_HOST,
    bridge_port: int = DEFAULT_BRIDGE_PORT,
    environ: Mapping[str, str] | None = None,
) -> CodexInstallResult:
    """Create or update the Codex config file with Remembra MCP settings.

    The file is written atomically after a backup of the old one, and owner-only
    (0600) because it holds the API key.
    """
    created = not config_path.exists()
    bridge_enabled = use_bridge if use_bridge is not None else is_sandboxed_codex(environ)
    target_url = build_bridge_url(bridge_host, bridge_port) if bridge_enabled else url
    mcp_api_key = None if bridge_enabled else api_key
    change = plan_codex_config(
        config_path, api_key=mcp_api_key, project=project, user_id=user_id, url=target_url, command=command
    )
    if change.changed:
        backup_and_write(change, label="remembra", private=True)

    return CodexInstallResult(
        config_path=config_path,
        command=command,
        url=target_url,
        project=project,
        user_id=user_id,
        created=created,
        bridge_url=build_bridge_url(bridge_host, bridge_port),
        bridge_enabled=bridge_enabled,
    )


def build_bridge_url(host: str, port: int) -> str:
    """Build the local bridge URL advertised to Codex."""
    return f"http://{host}:{port}"


def is_sandboxed_codex(environ: Mapping[str, str] | None = None) -> bool:
    """Detect whether the current Codex session requires the local bridge."""
    env = os.environ if environ is None else environ
    sandbox_mode = env.get("CODEX_SANDBOX", "").strip().lower()
    if sandbox_mode and sandbox_mode not in {"0", "false", "off", "none"}:
        return True

    network_disabled = env.get("CODEX_SANDBOX_NETWORK_DISABLED", "").strip().lower()
    return network_disabled in TRUTHY_ENV_VALUES


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
    pid_file: Path = DEFAULT_PID_FILE,
    health_timeout: float = 5.0,
) -> int:
    """Start the local bridge as a detached background process.

    Raises:
        BridgePortInUseError: If the port is already in use.
        BridgeStartupError: If the bridge fails to start or become healthy.
    """
    # Check if bridge is already running
    existing_pid = read_pid_file(pid_file)
    if existing_pid and is_process_running(existing_pid):
        # Verify it's actually responding
        if wait_for_healthy(host, port, timeout=1.0):
            return existing_pid  # Already running and healthy
        # Process exists but not responding, stop it
        stop_bridge(pid_file)

    # Check port availability
    check_port_available(host, port)

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
            "--pid-file",
            str(pid_file),
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

    # Wait for bridge to become healthy
    if not wait_for_healthy(host, port, timeout=health_timeout):
        # Check if process died
        if process.poll() is not None:
            raise BridgeStartupError(f"Bridge process exited immediately. Check logs at {stderr_path}")
        raise BridgeStartupError(
            f"Bridge started (PID {process.pid}) but failed health check after {health_timeout}s. Check logs at {stderr_path}"
        )

    return process.pid


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for Codex installation."""
    parser = argparse.ArgumentParser(
        description="Install or update the Remembra MCP server for Codex.",
    )
    parser.add_argument("--api-key", help=argparse.SUPPRESS)  # deprecated: visible in shell history
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read the API key from stdin (default: REMEMBRA_API_KEY, a prompt, or ~/.remembra/credentials)",
    )
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
    api_key, source = resolve_api_key(
        cli_key=args.api_key,
        from_stdin=args.api_key_stdin,
        credentials=Path.home() / ".remembra" / "credentials",
        environ=os.environ,
    )
    if not api_key:
        parser.error(
            f"no API key ({source}). Set REMEMBRA_API_KEY, pipe it with --api-key-stdin, or run in a terminal to be asked."
        )
    print(f"Key: {mask_key(api_key)} (from {source})")

    result = install_codex_config(
        args.config_path,
        api_key=api_key,
        project=args.project,
        user_id=args.user_id,
        url=args.upstream_url,
        command=args.command,
        use_bridge=False if args.no_bridge else None,
        bridge_host=args.bridge_host,
        bridge_port=args.bridge_port,
    )

    if args.start_bridge and result.bridge_enabled:
        try:
            result.bridge_pid = start_bridge_background(
                upstream=args.upstream_url,
                port=args.bridge_port,
                api_key=api_key,
                host=args.bridge_host,
                command=args.bridge_command,
            )
            result.bridge_started = True
        except BridgePortInUseError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            print("Config installed but bridge not started.", file=sys.stderr)
        except BridgeStartupError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            print("Config installed but bridge may not be healthy.", file=sys.stderr)

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
        print("Bridge health: OK")


if __name__ == "__main__":
    main()
