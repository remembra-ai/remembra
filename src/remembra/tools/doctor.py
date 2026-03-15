"""Lightweight diagnostics for agent setup flows."""

from __future__ import annotations

import argparse
import json
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# Default config paths for each agent
DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_CLAUDE_DESKTOP_CONFIG = (
    Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
)
DEFAULT_CLAUDE_CODE_CONFIG = Path.home() / ".claude" / "settings.json"
DEFAULT_GEMINI_CONFIG = Path.home() / ".gemini" / "settings.json"
DEFAULT_CURSOR_CONFIG = Path.home() / ".cursor" / "mcp.json"
DEFAULT_WINDSURF_CONFIG = Path.home() / ".windsurf" / "mcp_config.json"


@dataclass(slots=True)
class CheckResult:
    """A single diagnostic check."""

    name: str
    status: str
    message: str


@dataclass(slots=True)
class DoctorTarget:
    """Resolved Remembra configuration for any agent."""

    agent: str
    config_path: Path
    command: str
    url: str | None
    api_key: str | None
    project: str | None
    user_id: str | None


# Keep alias for backward compatibility
CodexDoctorTarget = DoctorTarget


class DoctorError(RuntimeError):
    """Raised when a configuration cannot be resolved."""


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _extract_mcp_env(server: dict[str, Any]) -> dict[str, Any]:
    """Extract environment variables from an MCP server config."""
    env = server.get("env", {})
    if env is not None and not isinstance(env, dict):
        raise DoctorError("invalid_env:mcpServers.remembra.env")
    return env if isinstance(env, dict) else {}


def load_codex_target(config_path: Path) -> DoctorTarget:
    """Parse a Codex config file (TOML) and extract Remembra configuration."""
    if not config_path.exists():
        raise DoctorError(f"config_missing:{config_path}")

    try:
        data = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise DoctorError(f"invalid_config:{exc}") from exc

    try:
        server = data["mcp_servers"]["remembra"]
    except KeyError as exc:
        raise DoctorError("missing_server:mcp_servers.remembra") from exc

    command = server.get("command")
    if not isinstance(command, str) or not command.strip():
        raise DoctorError("missing_command:mcp_servers.remembra.command")

    env = server.get("env", {})
    if env is not None and not isinstance(env, dict):
        raise DoctorError("invalid_env:mcp_servers.remembra.env")
    env = env if isinstance(env, dict) else {}

    return DoctorTarget(
        agent="codex",
        config_path=config_path,
        command=command,
        url=_optional_str(env.get("REMEMBRA_URL")),
        api_key=_optional_str(env.get("REMEMBRA_API_KEY")),
        project=_optional_str(env.get("REMEMBRA_PROJECT")),
        user_id=_optional_str(env.get("REMEMBRA_USER_ID")),
    )


def load_json_agent_target(agent: str, config_path: Path) -> DoctorTarget:
    """Parse a JSON MCP config file and extract Remembra configuration."""
    if not config_path.exists():
        raise DoctorError(f"config_missing:{config_path}")

    try:
        data = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise DoctorError(f"invalid_config:{exc}") from exc

    # JSON agents use "mcpServers" (camelCase)
    try:
        server = data["mcpServers"]["remembra"]
    except KeyError as exc:
        raise DoctorError("missing_server:mcpServers.remembra") from exc

    command = server.get("command")
    if not isinstance(command, str) or not command.strip():
        raise DoctorError("missing_command:mcpServers.remembra.command")

    env = _extract_mcp_env(server)

    return DoctorTarget(
        agent=agent,
        config_path=config_path,
        command=command,
        url=_optional_str(env.get("REMEMBRA_URL")),
        api_key=_optional_str(env.get("REMEMBRA_API_KEY")),
        project=_optional_str(env.get("REMEMBRA_PROJECT")),
        user_id=_optional_str(env.get("REMEMBRA_USER_ID")),
    )


def load_claude_desktop_target(config_path: Path) -> DoctorTarget:
    """Parse Claude Desktop config and extract Remembra configuration."""
    return load_json_agent_target("claude-desktop", config_path)


def load_claude_code_target(config_path: Path) -> DoctorTarget:
    """Parse Claude Code config and extract Remembra configuration."""
    return load_json_agent_target("claude-code", config_path)


def load_gemini_target(config_path: Path) -> DoctorTarget:
    """Parse Gemini config and extract Remembra configuration."""
    return load_json_agent_target("gemini", config_path)


def load_cursor_target(config_path: Path) -> DoctorTarget:
    """Parse Cursor config and extract Remembra configuration."""
    return load_json_agent_target("cursor", config_path)


def load_windsurf_target(config_path: Path) -> DoctorTarget:
    """Parse Windsurf config and extract Remembra configuration."""
    return load_json_agent_target("windsurf", config_path)


def resolve_command(command: str) -> str | None:
    """Resolve a configured command to an executable path."""
    path = Path(command).expanduser()
    if path.is_absolute() or "/" in command:
        return str(path) if path.exists() else None
    return shutil.which(command)


def classify_http_error(exc: Exception) -> str:
    """Map transport failures to user-facing failure reasons."""
    message = str(exc).lower()
    if "nodename nor servname provided" in message:
        return "dns_failure"
    if "name or service not known" in message:
        return "dns_failure"
    if "temporary failure in name resolution" in message:
        return "dns_failure"
    if "operation not permitted" in message:
        return "sandbox_blocked"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "upstream_unreachable"
    return "unknown_error"


def run_remote_checks(
    target: DoctorTarget,
    *,
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> list[CheckResult]:
    """Run health and recall probes against a resolved Remembra target."""
    if not target.url:
        return [CheckResult("remote", "warn", "REMEMBRA_URL missing; skipped network probes")]

    headers: dict[str, str] = {}
    if target.api_key:
        headers["X-API-Key"] = target.api_key

    checks: list[CheckResult] = []

    try:
        with httpx.Client(
            base_url=target.url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        ) as client:
            health_response = client.get("/health")
            health_response.raise_for_status()
            checks.append(
                CheckResult(
                    "health",
                    "pass",
                    f"health check ok ({health_response.status_code})",
                )
            )

            if not target.project or not target.user_id:
                checks.append(
                    CheckResult(
                        "recall",
                        "warn",
                        "project/user missing; skipped recall probe",
                    )
                )
                return checks

            recall_response = client.post(
                "/api/v1/memories/recall",
                json={
                    "user_id": target.user_id,
                    "project_id": target.project,
                    "query": "remembra doctor probe",
                    "limit": 1,
                    "threshold": 0.0,
                },
            )
            recall_response.raise_for_status()
            checks.append(
                CheckResult(
                    "recall",
                    "pass",
                    f"recall probe ok ({recall_response.status_code})",
                )
            )
            return checks
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return [CheckResult("remote", "fail", f"auth_failure ({exc.response.status_code})")]
        return [CheckResult("remote", "fail", f"http_error ({exc.response.status_code})")]
    except Exception as exc:  # pragma: no cover - exercised via classification tests
        return [CheckResult("remote", "fail", classify_http_error(exc))]


def _run_doctor(
    agent: str,
    config_path: Path,
    loader: callable,
    timeout: float = 5.0,
) -> list[CheckResult]:
    """Generic doctor runner for any agent."""
    results: list[CheckResult] = []

    try:
        target = loader(config_path)
        results.append(CheckResult("config", "pass", f"loaded {config_path}"))
    except DoctorError as exc:
        results.append(CheckResult("config", "fail", str(exc)))
        return results

    command_path = resolve_command(target.command)
    if command_path:
        results.append(CheckResult("command", "pass", f"resolved {command_path}"))
    else:
        results.append(CheckResult("command", "fail", f"command_missing:{target.command}"))
        return results

    results.extend(run_remote_checks(target, timeout=timeout))
    return results


def doctor_codex(config_path: Path, *, timeout: float = 5.0) -> list[CheckResult]:
    """Run Codex-specific diagnostics for Remembra MCP setup."""
    return _run_doctor("codex", config_path, load_codex_target, timeout)


def doctor_claude_desktop(config_path: Path, *, timeout: float = 5.0) -> list[CheckResult]:
    """Run Claude Desktop diagnostics for Remembra MCP setup."""
    return _run_doctor("claude-desktop", config_path, load_claude_desktop_target, timeout)


def doctor_claude_code(config_path: Path, *, timeout: float = 5.0) -> list[CheckResult]:
    """Run Claude Code diagnostics for Remembra MCP setup."""
    return _run_doctor("claude-code", config_path, load_claude_code_target, timeout)


def doctor_gemini(config_path: Path, *, timeout: float = 5.0) -> list[CheckResult]:
    """Run Gemini diagnostics for Remembra MCP setup."""
    return _run_doctor("gemini", config_path, load_gemini_target, timeout)


def doctor_cursor(config_path: Path, *, timeout: float = 5.0) -> list[CheckResult]:
    """Run Cursor diagnostics for Remembra MCP setup."""
    return _run_doctor("cursor", config_path, load_cursor_target, timeout)


def doctor_windsurf(config_path: Path, *, timeout: float = 5.0) -> list[CheckResult]:
    """Run Windsurf diagnostics for Remembra MCP setup."""
    return _run_doctor("windsurf", config_path, load_windsurf_target, timeout)


def doctor_all(*, timeout: float = 5.0) -> dict[str, list[CheckResult]]:
    """Run diagnostics for all known agents."""
    agents = {
        "codex": (DEFAULT_CODEX_CONFIG, doctor_codex),
        "claude-desktop": (DEFAULT_CLAUDE_DESKTOP_CONFIG, doctor_claude_desktop),
        "claude-code": (DEFAULT_CLAUDE_CODE_CONFIG, doctor_claude_code),
        "gemini": (DEFAULT_GEMINI_CONFIG, doctor_gemini),
        "cursor": (DEFAULT_CURSOR_CONFIG, doctor_cursor),
        "windsurf": (DEFAULT_WINDSURF_CONFIG, doctor_windsurf),
    }
    
    results: dict[str, list[CheckResult]] = {}
    for agent, (config_path, doctor_fn) in agents.items():
        if config_path.exists():
            results[agent] = doctor_fn(config_path, timeout=timeout)
    
    return results


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for diagnostics."""
    parser = argparse.ArgumentParser(description="Run Remembra setup diagnostics.")
    subparsers = parser.add_subparsers(dest="agent", required=True)

    # Codex
    codex = subparsers.add_parser("codex", help="Diagnose the Codex MCP setup")
    codex.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CODEX_CONFIG,
        help="Codex TOML config path",
    )
    codex.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    # Claude Desktop
    claude_desktop = subparsers.add_parser(
        "claude-desktop", help="Diagnose Claude Desktop MCP setup"
    )
    claude_desktop.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CLAUDE_DESKTOP_CONFIG,
        help="Claude Desktop JSON config path",
    )
    claude_desktop.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    # Claude Code
    claude_code = subparsers.add_parser("claude-code", help="Diagnose Claude Code MCP setup")
    claude_code.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CLAUDE_CODE_CONFIG,
        help="Claude Code JSON config path",
    )
    claude_code.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    # Gemini
    gemini = subparsers.add_parser("gemini", help="Diagnose Gemini MCP setup")
    gemini.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_GEMINI_CONFIG,
        help="Gemini JSON config path",
    )
    gemini.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    # Cursor
    cursor = subparsers.add_parser("cursor", help="Diagnose Cursor MCP setup")
    cursor.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CURSOR_CONFIG,
        help="Cursor JSON config path",
    )
    cursor.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    # Windsurf
    windsurf = subparsers.add_parser("windsurf", help="Diagnose Windsurf MCP setup")
    windsurf.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_WINDSURF_CONFIG,
        help="Windsurf JSON config path",
    )
    windsurf.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    # All agents
    all_agents = subparsers.add_parser("all", help="Diagnose all detected agents")
    all_agents.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP probe timeout in seconds",
    )

    return parser


def main() -> None:
    """CLI entry point for diagnostics."""
    parser = build_parser()
    args = parser.parse_args()

    agent_doctors = {
        "codex": (lambda: doctor_codex(args.config_path, timeout=args.timeout)),
        "claude-desktop": (lambda: doctor_claude_desktop(args.config_path, timeout=args.timeout)),
        "claude-code": (lambda: doctor_claude_code(args.config_path, timeout=args.timeout)),
        "gemini": (lambda: doctor_gemini(args.config_path, timeout=args.timeout)),
        "cursor": (lambda: doctor_cursor(args.config_path, timeout=args.timeout)),
        "windsurf": (lambda: doctor_windsurf(args.config_path, timeout=args.timeout)),
    }

    if args.agent == "all":
        all_results = doctor_all(timeout=args.timeout)
        if not all_results:
            print("No agents detected. Install an agent first.")
            raise SystemExit(1)
        
        failed = False
        for agent, results in all_results.items():
            print(f"\n=== {agent} ===")
            for result in results:
                print(f"[{result.status}] {result.name}: {result.message}")
                if result.status == "fail":
                    failed = True
        
        raise SystemExit(1 if failed else 0)

    if args.agent not in agent_doctors:
        parser.error(f"unsupported agent: {args.agent}")

    results = agent_doctors[args.agent]()
    failed = False
    for result in results:
        print(f"[{result.status}] {result.name}: {result.message}")
        if result.status == "fail":
            failed = True

    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
