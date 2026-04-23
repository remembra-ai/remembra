"""Local bridge for sandboxed agents that cannot reach the cloud directly."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx

try:
    from remembra import __version__ as REMEMBRA_VERSION
except Exception:  # pragma: no cover - fallback for partial installs
    REMEMBRA_VERSION = "dev"

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 9819
DEFAULT_BRIDGE_UPSTREAM = "https://api.remembra.dev"
DEFAULT_PID_FILE = Path.home() / ".remembra" / "bridge.pid"
DEFAULT_BRIDGE_AGENT_NAME = "codex"
FORWARDED_AGENT_HEADER = "X-Remembra-Agent"
FORWARDED_BRIDGE_HEADER = "X-Remembra-Bridge"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
RESPONSE_HEADERS_TO_STRIP = {
    "content-encoding",
}


class BridgePortInUseError(Exception):
    """Raised when the bridge port is already in use."""


class BridgeStartupError(Exception):
    """Raised when the bridge fails to start or become healthy."""


def check_port_available(host: str, port: int) -> None:
    """Check if a port is available for binding.

    Raises:
        BridgePortInUseError: If the port is already in use. The exception
            message includes the offending PID when it can be resolved via
            ``lsof``/``ss``/``netstat`` — this turns the old generic
            "port in use" error into actionable diagnostic output and
            unblocks the zombie-state recovery flow.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            holder = find_pid_on_port(port)
            holder_note = (
                f" Process {holder} is currently holding it. "
                f"Run 'remembra-bridge --stop --force' to clean it up."
                if holder is not None
                else " Run 'remembra-bridge --stop --force' to clean it up."
            )
            raise BridgePortInUseError(
                f"Port {port} is already in use on {host}.{holder_note}"
            ) from exc


def find_pid_on_port(port: int) -> int | None:
    """Return the PID of a process listening on ``port``, or None.

    Uses whichever OS tool is available. Returns None if the port is
    free, no tool is available, or the lookup fails. This is a
    best-effort diagnostic helper — callers MUST treat ``None`` as
    "don't know" rather than "nothing listening".
    """
    # macOS / Linux — prefer lsof (consistent output across both).
    lsof = shutil.which("lsof")
    if lsof:
        try:
            result = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Take the first PID (there should normally be one)
                first = result.stdout.strip().splitlines()[0].strip()
                return int(first) if first.isdigit() else None
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

    # Linux fallback — `ss -ltnp` in case lsof is missing
    ss = shutil.which("ss")
    if ss:
        try:
            result = subprocess.run(
                [ss, "-ltnp"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if f":{port} " in line and "LISTEN" in line:
                        # ss output: users:(("py",pid=1234,fd=3))
                        marker = "pid="
                        idx = line.find(marker)
                        if idx >= 0:
                            tail = line[idx + len(marker) :]
                            num = tail.split(",", 1)[0].rstrip(")")
                            if num.isdigit():
                                return int(num)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

    # Windows fallback — `netstat -ano`
    if sys.platform == "win32":  # pragma: no cover
        netstat = shutil.which("netstat")
        if netstat:
            try:
                result = subprocess.run(
                    [netstat, "-ano"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if f":{port} " in line and "LISTENING" in line:
                            parts = line.split()
                            if parts and parts[-1].isdigit():
                                return int(parts[-1])
            except (subprocess.SubprocessError, OSError, ValueError):
                pass

    return None


def is_port_listening(host: str, port: int) -> bool:
    """Cheap probe — returns True if ``port`` on ``host`` accepts a TCP connect.

    Different from ``find_pid_on_port`` which identifies *who* is listening;
    this just answers *is anyone there*. Used by --status to detect orphans
    even when we can't name them.
    """
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (OSError, socket.timeout):
        return False


def write_pid_file(pid: int, pid_file: Path = DEFAULT_PID_FILE) -> None:
    """Write the bridge PID to a file for later cleanup."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid))


def read_pid_file(pid_file: Path = DEFAULT_PID_FILE) -> int | None:
    """Read the bridge PID from the file, or None if not found."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid_file(pid_file: Path = DEFAULT_PID_FILE) -> None:
    """Remove the PID file."""
    import contextlib

    with contextlib.suppress(OSError):
        pid_file.unlink(missing_ok=True)


def is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _terminate_pid(pid: int) -> bool:
    """SIGTERM a PID, SIGKILL if it's still alive after 3 seconds.

    Returns True if the process is gone after this call.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return not is_process_running(pid)

    for _ in range(30):
        if not is_process_running(pid):
            return True
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    # One last check after KILL
    time.sleep(0.1)
    return not is_process_running(pid)


def stop_bridge(
    pid_file: Path = DEFAULT_PID_FILE,
    *,
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    force: bool = False,
) -> bool:
    """Stop a running bridge.

    Default mode (``force=False``) uses the PID file.

    ``force=True`` additionally reconciles against the live listener: if
    the PID file is missing/stale but a process still holds the port,
    it kills that orphan too. This is the recovery path for the zombie
    state documented in issue #10.

    Returns:
        True if anything was stopped, False if the bridge was already down.
    """
    stopped_something = False

    pid = read_pid_file(pid_file)
    if pid is not None and is_process_running(pid):
        _terminate_pid(pid)
        stopped_something = True
    remove_pid_file(pid_file)

    if force:
        # Port may still be held by an orphan whose pidfile is gone.
        orphan = find_pid_on_port(port)
        if orphan is not None and (pid is None or orphan != pid):
            if _terminate_pid(orphan):
                stopped_something = True

    return stopped_something


def wait_for_healthy(
    host: str,
    port: int,
    timeout: float = 5.0,
    interval: float = 0.2,
) -> bool:
    """Wait for the bridge to become healthy.

    Returns:
        True if healthy, False if timeout reached.
    """
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=1.0) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    return True
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(interval)

    return False


@dataclass(slots=True)
class BridgeConfig:
    """Runtime configuration for the local bridge."""

    upstream: str
    host: str = DEFAULT_BRIDGE_HOST
    port: int = DEFAULT_BRIDGE_PORT
    api_key: str | None = None
    timeout: float = 30.0
    agent_name: str = DEFAULT_BRIDGE_AGENT_NAME

    @property
    def base_url(self) -> str:
        """Return the bridge URL advertised to sandboxed agents."""
        return f"http://{self.host}:{self.port}"


class RemembraBridgeServer(ThreadingHTTPServer):
    """HTTP bridge that forwards requests to the upstream Remembra API."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        config: BridgeConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.client = httpx.Client(
            base_url=config.upstream.rstrip("/"),
            timeout=config.timeout,
            transport=transport,
        )
        super().__init__((config.host, config.port), BridgeRequestHandler)

    def server_close(self) -> None:
        """Close the upstream client before closing the socket."""
        self.client.close()
        super().server_close()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    """Proxy requests to the configured upstream Remembra API."""

    protocol_version = "HTTP/1.1"

    def _server(self) -> RemembraBridgeServer:
        return cast(RemembraBridgeServer, self.server)

    def _forward(self) -> None:
        server = self._server()
        body = self._read_body()
        path = self.path if self.path.startswith("/") else f"/{self.path}"

        try:
            response = forward_upstream_request(
                client=server.client,
                method=self.command,
                path=path,
                headers=self.headers,
                body=body,
                api_key=server.config.api_key,
                agent_name=server.config.agent_name,
            )
        except httpx.HTTPError as exc:
            self._write_json_error(502, str(exc))
            return

        payload = response.content
        self.send_response(response.status_code)
        for header, value in response.headers.items():
            if header.lower() in HOP_BY_HOP_HEADERS | RESPONSE_HEADERS_TO_STRIP:
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _write_json_error(self, status_code: int, message: str) -> None:
        payload = json.dumps({"status": "error", "error": message}).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        """Keep the bridge quiet by default."""


def build_bridge_user_agent(agent_name: str) -> str:
    """Build a deterministic user agent for Cloudflare allow rules."""
    return f"Remembra-Bridge/{REMEMBRA_VERSION} ({agent_name}; +https://remembra.dev)"


def build_forward_headers(
    headers: HTTPMessage,
    *,
    api_key: str | None,
    agent_name: str,
) -> dict[str, str]:
    """Prepare upstream headers while stripping hop-by-hop ones."""
    forwarded: dict[str, str] = {}
    for header, value in headers.items():
        if header.lower() in HOP_BY_HOP_HEADERS:
            continue
        forwarded[header] = value

    if api_key:
        forwarded["X-API-Key"] = api_key
    forwarded["User-Agent"] = build_bridge_user_agent(agent_name)
    forwarded[FORWARDED_AGENT_HEADER] = agent_name
    forwarded[FORWARDED_BRIDGE_HEADER] = "true"
    return forwarded


def forward_upstream_request(
    *,
    client: httpx.Client,
    method: str,
    path: str,
    headers: HTTPMessage,
    body: bytes | None,
    api_key: str | None,
    agent_name: str = DEFAULT_BRIDGE_AGENT_NAME,
) -> httpx.Response:
    """Forward a request to the upstream Remembra API."""
    forwarded_headers = build_forward_headers(
        headers,
        api_key=api_key,
        agent_name=agent_name,
    )
    return client.request(
        method=method,
        url=path,
        content=body,
        headers=forwarded_headers,
    )


@dataclass(slots=True)
class DoctorReport:
    """Diagnostic snapshot of a remembra-bridge install."""

    pidfile_pid: int | None
    pidfile_process_alive: bool
    port_pid: int | None
    port_listening: bool
    health_ok: bool
    health_error: str | None

    @property
    def healthy(self) -> bool:
        """A healthy bridge has matching pidfile+port+/health."""
        if not self.port_listening or not self.health_ok:
            return False
        if self.pidfile_pid is None:
            # No pidfile but a port holder → orphan
            return False
        if self.port_pid is not None and self.port_pid != self.pidfile_pid:
            return False
        return self.pidfile_process_alive

    @property
    def has_orphan(self) -> bool:
        """Port is held but the pidfile doesn't know about it."""
        if not self.port_listening:
            return False
        if self.pidfile_pid is None:
            return True
        if not self.pidfile_process_alive:
            return True
        if self.port_pid is not None and self.port_pid != self.pidfile_pid:
            return True
        return False


def run_doctor(
    host: str,
    port: int,
    pid_file: Path = DEFAULT_PID_FILE,
) -> DoctorReport:
    """Collect diagnostic info about the bridge state without fixing anything."""
    pidfile_pid = read_pid_file(pid_file)
    pidfile_alive = pidfile_pid is not None and is_process_running(pidfile_pid)
    port_pid = find_pid_on_port(port)
    listening = is_port_listening(host, port)

    health_ok = False
    health_error: str | None = None
    if listening:
        try:
            with httpx.Client(timeout=1.0) as client:
                resp = client.get(f"http://{host}:{port}/health")
            health_ok = resp.status_code == 200
            if not health_ok:
                health_error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            health_error = str(exc)

    return DoctorReport(
        pidfile_pid=pidfile_pid,
        pidfile_process_alive=pidfile_alive,
        port_pid=port_pid,
        port_listening=listening,
        health_ok=health_ok,
        health_error=health_error,
    )


def format_doctor_report(
    report: DoctorReport,
    host: str,
    port: int,
    pid_file: Path,
) -> str:
    """Format a DoctorReport for human-readable CLI output."""
    lines: list[str] = ["remembra-bridge doctor"]
    lines.append("-" * 40)
    lines.append(f"  pid file:   {pid_file}")

    if report.pidfile_pid is None:
        lines.append("    pid:      (missing)")
    else:
        alive = "alive" if report.pidfile_process_alive else "DEAD (stale pidfile)"
        lines.append(f"    pid:      {report.pidfile_pid} ({alive})")

    lines.append(f"  port:       {host}:{port}")
    lines.append(f"    listening: {'yes' if report.port_listening else 'no'}")
    if report.port_pid is not None:
        lines.append(f"    holder:    PID {report.port_pid}")

    lines.append("  /health:")
    if report.health_ok:
        lines.append("    status:    ok")
    elif report.port_listening:
        lines.append(f"    status:    FAIL ({report.health_error or 'unknown'})")
    else:
        lines.append("    status:    n/a (nothing listening)")

    lines.append("-" * 40)
    if report.healthy:
        lines.append("verdict: HEALTHY")
    elif report.has_orphan:
        holder = report.port_pid if report.port_pid is not None else "unknown"
        lines.append(f"verdict: ORPHAN — port {port} held by PID {holder}")
        lines.append("         run: remembra-bridge --stop --force")
    elif not report.port_listening and report.pidfile_pid is None:
        lines.append("verdict: NOT RUNNING")
    else:
        lines.append("verdict: UNHEALTHY — see details above")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the bridge."""
    parser = argparse.ArgumentParser(description="Start the local Remembra bridge.")
    parser.add_argument(
        "--upstream",
        default=DEFAULT_BRIDGE_UPSTREAM,
        help="Upstream Remembra API URL",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_BRIDGE_HOST,
        help="Local host interface to bind",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_BRIDGE_PORT,
        help="Local port to bind",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("REMEMBRA_API_KEY"),
        help="API key to inject into upstream requests",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Upstream request timeout in seconds",
    )
    parser.add_argument(
        "--agent-name",
        default=os.environ.get("REMEMBRA_AGENT_NAME", DEFAULT_BRIDGE_AGENT_NAME),
        help="Agent name to stamp on forwarded requests",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop a running bridge (using PID file)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "With --stop, also kill any orphan process holding the port "
            "even if the PID file is missing/stale."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check if a bridge is running (probes both pidfile and port)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print a full diagnostic report (pidfile, port holder, /health).",
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_PID_FILE,
        help="Path to the PID file",
    )
    return parser


def parse_bridge_command(command: str | None) -> list[str]:
    """Resolve a bridge command string into argv form."""
    if not command:
        return ["remembra-bridge"]
    return shlex.split(command)


def main() -> None:
    """CLI entry point for the local bridge."""
    parser = build_parser()
    args = parser.parse_args()

    # Handle --stop
    if args.stop:
        if stop_bridge(
            args.pid_file,
            host=args.host,
            port=args.port,
            force=args.force,
        ):
            print("Bridge stopped.")
        else:
            # Even without force, a live port means there's still an orphan.
            orphan = find_pid_on_port(args.port) if not args.force else None
            if orphan is not None:
                print(
                    f"No managed bridge found, but port {args.port} is held by "
                    f"PID {orphan}. Re-run with --force to clean it up."
                )
            else:
                print("No running bridge found.")
        return

    # Handle --doctor
    if args.doctor:
        report = run_doctor(args.host, args.port, args.pid_file)
        print(format_doctor_report(report, args.host, args.port, args.pid_file))
        sys.exit(0 if report.healthy else 1)

    # Handle --status
    if args.status:
        pid = read_pid_file(args.pid_file)

        # Happy path: pidfile matches a live, responding process
        if pid and is_process_running(pid):
            print(f"Bridge is running (PID {pid})")
            if wait_for_healthy(args.host, args.port, timeout=1.0):
                print(f"  Listening on http://{args.host}:{args.port}")
                print("  Status: healthy")
            else:
                print("  Status: not responding")
            return

        # Stale pidfile pointing at a dead PID — clean it up.
        if pid:
            remove_pid_file(args.pid_file)

        # Reconcile against reality: is anyone actually holding the port?
        if is_port_listening(args.host, args.port):
            holder = find_pid_on_port(args.port)
            holder_str = f"PID {holder}" if holder is not None else "an unknown process"
            print(
                f"Bridge is NOT managed by this pidfile, but port {args.port} "
                f"is held by {holder_str}."
            )
            print("  Run 'remembra-bridge --stop --force' to clean up the orphan.")
            return

        print("Bridge is not running.")
        return

    # Check if port is available before starting
    try:
        check_port_available(args.host, args.port)
    except BridgePortInUseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    config = BridgeConfig(
        upstream=args.upstream,
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        timeout=args.timeout,
        agent_name=args.agent_name,
    )

    # Write PID file
    write_pid_file(os.getpid(), args.pid_file)

    server = RemembraBridgeServer(config)

    print(f"Remembra bridge listening on {config.base_url}")
    print(f"Forwarding to {config.upstream}")
    print(f"PID file: {args.pid_file}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        remove_pid_file(args.pid_file)


if __name__ == "__main__":
    main()
