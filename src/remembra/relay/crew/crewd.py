"""``remembra-crewd``: the supervised per-user crew daemon (spec D3, §8.1, §10).

One crewd runs per user, supervised by a launchd LaunchAgent (``KeepAlive``) on macOS or a
systemd ``--user`` unit on Linux (installed by ``remembra-crew connect`` after consent, WP-10;
:func:`launchd_plist` and :func:`systemd_unit` render the units). Any hook that finds the socket
dead respawns it (at most once per 30 s); a second instance exits at once (``crewd.lock``), and
the supervisor's instance (``--wait-lock``) waits for the lock instead, so a hook-spawned crewd and
the supervised one never run together.

What it does:

* **Host and sessions**: registers this machine once per API key (host token in ``keys/``), joins
  sessions for ``remembra-crew start`` (session tokens live only in memory and ``keys/``; the
  session file the gate reads never holds a token).
* **Heartbeat** every 60 s, batched per host, carrying activity ages (never absolute times),
  footprints, the git-hook state and detector limits; it renews leases and brings back deltas and
  urgent ``inject_text``.
* **Snapshot** writer (``snapshot/<crew>.json``, atomic, HMAC-sealed) with this host's checkouts.
* **Presence frames** over the crew WebSocket, at most one per session per 5 s.
* **Outbox** flush every 5 s (spooled events, unconfirmed claims, checkpoints, stall, leave).
* **Local arbiter** while the server is unreachable (D32), with replay on reconnect.
* **Fencing** (D31): the gate reads lease horizons from the snapshot; in server-outage mode
  (5xx and ``/health`` failing) crewd reports ``server_outage`` so own claims stay writable.
* **Baton refs** on stall, orphan, dirty SessionEnd and ``release --baton``; restore on adopt.
* **Read-only fence** for advisory sessions in their own worktree.
* **Transcript detector** (D14), **pid liveness** and the **orphan sweeper**.
* **Gate integrity** every 30 s (restores the vendored gate on mismatch) and the **git-hook check**.
* **zones.yml** compile and upload (default branch or a human TTY push only).

Socket peers are authenticated with ``getpeereid``-style credentials (``LOCAL_PEERCRED`` /
``LOCAL_PEERPID`` on macOS, ``SO_PEERCRED`` on Linux) and their process ancestry: crewd acts only
for the session whose recorded agent pid is an ancestor of the caller. Anything else may read
status but cannot claim, release, adopt or leave (§8.1; §11 states the limits).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import httpx

from remembra.crew import schemas as S
from remembra.crew.redact import outbound
from remembra.relay.config import RelayConfig, load_config
from remembra.relay.crew import baton as B
from remembra.relay.crew import detector as D
from remembra.relay.crew import outbox as O
from remembra.relay.crew import snapshot as SN
from remembra.relay.crew import zonescompile as ZC
from remembra.relay.crew.arbiter import Arbiter, LocalSessionRef
from remembra.relay.crew.fence import Fence, zones_to_fence
from remembra.relay.crew.gate import (
    Layout,
    pid_alive,
    read_json,
    session_key,
    socket_dir_trusted,
    vendor_gate,
    verify_gate,
    write_json,
)
from remembra.relay.handoff import police_item

CREWD_VERSION: Final = "1.0.0"
HEARTBEAT_S: Final = 60.0
OUTBOX_S: Final = 5.0
LIVENESS_S: Final = 30.0
INTEGRITY_S: Final = 30.0
DETECTOR_S: Final = 30.0
PRESENCE_S: Final = 5.0
# crew_exists (SessionStart in a repo with no .remembra/): bounded well inside the CLI's 5 s wait
CREW_EXISTS_REPO_S: Final = 1.5
CREW_EXISTS_REQUEST_S: Final = 3.0
CREW_EXISTS_NEGATIVE_TTL_S: Final = 300.0
CREW_EXISTS_UNREACHABLE_TTL_S: Final = 60.0
ZONES_S: Final = 30.0
TREE_EVERY_S: Final = 86400.0
BATON_SWEEP_S: Final = 3600.0
SETTINGS_EVERY_S: Final = 300.0
CHECKPOINT_INTERVAL_S: Final = 600.0
TEST_CHECKPOINT_MIN_S: Final = 120.0
END_GRACE_S: Final = 1.5  # SessionEnd waits this long so a StopFailure fired at the same time is handled first
AUTO_CLAIM_TIMEOUT_S: Final = 0.7
GIT_DELTA_TIMEOUT_S: Final = 0.8
MAX_NEWS: Final = 30
MAX_TOOL_WINDOWS: Final = 64
WINDOW_SLACK_S: Final = 0.25
OPEN_WINDOW_MAX_S: Final = 600.0  # mtime vs tool-window comparison slack (clock granularity, hook dispatch delay)
LAUNCHD_LABEL: Final = "dev.remembra.crewd"
SYSTEMD_UNIT: Final = "remembra-crewd.service"
SHELL_NAMES: Final = frozenset(
    {"sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh", "csh", "env", "sudo", "login", "nohup", "timeout"}
)
# D14 (S0): Claude usage-limit prefixes; a plain 429 is transient
USAGE_LIMIT_PREFIXES: Final = ("You've hit your", "You're out of usage credits", "Your org is out of usage", "You've used")
QUOTA_ERRORS: Final = ("billing_error",)
AUTH_ERRORS: Final = ("authentication_failed", "oauth_org_not_allowed")
ADAPTER_BINS: Final[Mapping[str, tuple[str, ...]]] = {
    "claude-code": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor", "cursor-agent", "Cursor"),
    "gemini": ("gemini",),
    "qwen": ("qwen",),
    "kimi": ("kimi",),
}
ADAPTER_CONFIG_SOURCE: Final[Mapping[str, str]] = {"claude-code": "claude", "codex": "codex"}

log = logging.getLogger("remembra.crewd")


def nothing_to_hand_off(body: Mapping[str, Any]) -> bool:
    """The relay CLI's empty-close rule (:func:`remembra.relay.cli.nothing_to_hand_off`) for a crewd close body."""
    from remembra.relay.handoff import build_sections, sections_have_substance

    facts = body.get("facts") or {}
    sections = build_sections(dict(facts), facts.get("next_step"))
    return not sections_have_substance(
        sections, summary=body.get("summary"), notes=facts.get("notes"), end_reason=body.get("end_reason")
    )


# ===========================================================================
# Peer credentials and process ancestry
# ===========================================================================


@dataclass(frozen=True)
class Peer:
    pid: int | None
    uid: int | None
    chain: tuple[int, ...] | None = None  # ancestry read while the peer was still connected


def peer_credentials(sock: socket.socket) -> Peer:
    """The connecting process (pid, uid) of a unix socket (``SO_PEERCRED`` / ``LOCAL_PEERCRED`` + ``LOCAL_PEERPID``)."""
    if sys.platform.startswith("linux"):
        so_peercred = getattr(socket, "SO_PEERCRED", 17)
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", raw)
        return Peer(pid, uid)
    if sys.platform == "darwin":
        sol_local, local_peercred, local_peerpid = 0, 0x001, 0x002
        pid = uid = None
        with contextlib.suppress(OSError):
            pid = struct.unpack("i", sock.getsockopt(sol_local, local_peerpid, 4))[0]
        with contextlib.suppress(OSError):
            raw = sock.getsockopt(sol_local, local_peercred, 4 + 4 + 2 + 16 * 4)
            _version, uid = struct.unpack("II", raw[:8])
        return Peer(pid, uid)
    return Peer(None, None)


def _ppid_map() -> dict[int, tuple[int, str]]:
    """pid → (ppid, command name) for every process (one ``ps`` call; /proc on Linux)."""
    out: dict[int, tuple[int, str]] = {}
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                    stat = fh.read()
            except OSError:
                continue
            lp, rp = stat.find("("), stat.rfind(")")
            if lp < 0 or rp < 0:
                continue
            name = stat[lp + 1 : rp]
            fields = stat[rp + 2 :].split()
            with contextlib.suppress(ValueError, IndexError):
                out[int(entry)] = (int(fields[1]), name)
        return out
    try:
        res = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, timeout=3.0, check=False)  # noqa: S603,S607
    except (OSError, subprocess.SubprocessError):
        return out
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2]) if len(parts) > 2 else "")
    return out


_PPID_CACHE: list[Any] = [0.0, {}]
PPID_CACHE_TTL_S: Final = 1.0


def cached_ppid_map(require: int | None = None) -> dict[int, tuple[int, str]]:
    """``_ppid_map`` cached for a second; refreshed at once when ``require`` (a new peer) is missing."""
    at, table = _PPID_CACHE
    if time.monotonic() - at > PPID_CACHE_TTL_S or (require is not None and require not in table):
        table = _ppid_map()
        _PPID_CACHE[0], _PPID_CACHE[1] = time.monotonic(), table
    return dict(table)


def ancestors(pid: int, table: Mapping[int, tuple[int, str]] | None = None) -> list[int]:
    """``pid`` and its ancestors, nearest first (stops at init or a loop)."""
    table = table if table is not None else _ppid_map()
    chain: list[int] = []
    cur: int | None = pid
    while cur and cur > 1 and cur not in chain and len(chain) < 64:
        chain.append(cur)
        cur = table.get(cur, (0, ""))[0]
    return chain


def process_name(pid: int, table: Mapping[int, tuple[int, str]] | None = None) -> str:
    table = table if table is not None else _ppid_map()
    return table.get(pid, (0, ""))[1]


def find_agent_pid(start_pid: int, adapter: str, table: Mapping[int, tuple[int, str]] | None = None) -> int | None:
    """The agent process that ran this hook: the nearest ancestor named like the adapter's binary,
    else the nearest ancestor that is not a shell or interpreter (a short-lived ``sh -c`` would be a
    false orphan)."""
    table = table if table is not None else _ppid_map()
    chain = ancestors(start_pid, table)[1:]
    bins = ADAPTER_BINS.get(adapter, ())
    for p in chain:
        name = table.get(p, (0, ""))[1]
        if name and any(name == b or name.startswith(b) for b in bins):
            return p
    for p in chain:
        name = table.get(p, (0, ""))[1].lstrip("-")
        if name and name not in SHELL_NAMES and not name.startswith(("python", "remembra-crew", "uv")):
            return p
    return chain[0] if chain else None


def has_controlling_tty(pid: int) -> bool:
    """Whether ``pid`` has a controlling terminal (the offline bypass is for a human at a TTY, D34)."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
            return int(stat[stat.rfind(")") + 2 :].split()[4]) != 0
        except (OSError, ValueError, IndexError):
            return False
    try:
        res = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, timeout=2.0, check=False)  # noqa: S603,S607
    except (OSError, subprocess.SubprocessError):
        return False
    tty = res.stdout.decode().strip()
    return bool(tty) and tty not in ("??", "?", "-")


# ===========================================================================
# Server API
# ===========================================================================


class Unreachable(Exception):
    """No HTTP response (connect error, timeout)."""


@dataclass
class Resp:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error(self) -> str:
        b = self.body
        if isinstance(b, dict):
            d = b.get("detail", b)
            if isinstance(d, dict):
                return str(d.get("error") or d.get("code") or d.get("message") or self.status)
            return str(b.get("error") or d)[:200]
        return str(self.status)

    def detail(self) -> dict[str, Any]:
        b = self.body
        if isinstance(b, dict):
            d = b.get("detail", b)
            return d if isinstance(d, dict) else {"message": str(d)}
        return {}


class Api:
    """One HTTP client per (server, key, agent id)."""

    def __init__(self, cfg: RelayConfig, *, agent_id: str | None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"User-Agent": f"remembra-crewd/{CREWD_VERSION}", "Accept": "application/json"}
        if cfg.api_key:
            headers["X-API-Key"] = cfg.api_key
        if agent_id:
            headers["X-Remembra-Agent-Id"] = agent_id
        self.cfg = cfg
        self.agent_id = agent_id
        self.client = httpx.AsyncClient(
            base_url=cfg.url, headers=headers, transport=transport, timeout=httpx.Timeout(10.0, connect=3.0)
        )

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        session_token: str | None = None,
        host_token: str | None = None,
        idem: str | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
        timeout: float = 10.0,
        root: bool = False,
    ) -> Resp:
        headers: dict[str, str] = {}
        if session_token:
            headers["X-Remembra-Crew-Session"] = session_token
        if host_token:
            headers["X-Remembra-Host-Token"] = host_token
        if idem:
            headers["Idempotency-Key"] = idem[:200]
        if if_match:
            headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        url = path if root else f"/api/v1{path}"
        try:
            r = await asyncio.wait_for(
                self.client.request(method, url, json=json_body, params=dict(params or {}) or None, headers=headers),
                timeout=timeout,
            )
        except (httpx.TransportError, TimeoutError, OSError) as e:
            raise Unreachable(f"{method} {path}: {e.__class__.__name__}") from e
        body: Any = None
        if r.content:
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:500]}
        return Resp(r.status_code, body, dict(r.headers))

    async def close(self) -> None:
        await self.client.aclose()


# ===========================================================================
# Supervision units (rendered here, installed by WP-10 after consent)
# ===========================================================================


def crewd_argv(python: str | None = None, *, supervised: bool = False) -> list[str]:
    argv = [python or sys.executable, "-m", "remembra.relay.crew.crewd"]
    return [*argv, "--wait-lock"] if supervised else argv


def unit_path(home: Path, system: str | None = None) -> Path:
    system = system or platform.system()
    if system == "Darwin":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def launchd_plist(home: Path, python: str | None = None) -> str:
    import plistlib

    layout = Layout(home)
    data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": crewd_argv(python, supervised=True),
        "KeepAlive": True,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "EnvironmentVariables": {"HOME": str(home)},
        "StandardOutPath": str(layout.log_dir / "launchd.out.log"),
        "StandardErrorPath": str(layout.log_dir / "launchd.err.log"),
    }
    return plistlib.dumps(data).decode()


def systemd_unit(home: Path, python: str | None = None) -> str:
    import shlex

    argv = " ".join(shlex.quote(a) for a in crewd_argv(python, supervised=True))
    return (
        "[Unit]\nDescription=Remembra Crew daemon (crewd)\nAfter=network-online.target\n\n"
        f"[Service]\nType=simple\nExecStart={argv}\nRestart=always\nRestartSec=2\nEnvironment=HOME={home}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


# ===========================================================================
# Helpers
# ===========================================================================


def _now() -> float:
    return time.time()


def _ts(epoch: float) -> str:
    return SN.format_ts(epoch)


def _sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def classify_stall(error: str, last_message: str | None) -> str:
    """D14 (S0): ``quota`` | ``auth`` | ``transient`` for a StopFailure error."""
    if error in QUOTA_ERRORS or error == "detected_limit":
        return "quota"
    if error in AUTH_ERRORS:
        return "auth"
    if error == "rate_limit" and (last_message or "").lstrip().startswith(USAGE_LIMIT_PREFIXES):
        return "quota"
    return "transient"


def build_tree(files: Iterable[str], *, max_depth: int = S.TREE_MAX_DEPTH, max_nodes: int = S.TREE_MAX_NODES) -> dict[str, Any]:
    """Folder names and file counts only, ≤3 levels, ≤400 nodes (§11 "Tree snapshot")."""
    root: dict[str, Any] = {"name": ".", "files": 0, "children": {}}
    for f in files:
        parts = f.split("/")
        node = root
        node["files"] += 1
        for depth, part in enumerate(parts[:-1]):
            if depth >= max_depth:
                break
            child = node["children"].get(part)
            if child is None:
                child = {"name": part, "files": 0, "children": {}}
                node["children"][part] = child
            child["files"] += 1
            node = child
    count = 0

    def freeze(node: dict[str, Any]) -> dict[str, Any]:
        nonlocal count
        count += 1
        kids: list[dict[str, Any]] = []
        for name in sorted(node["children"]):
            if count >= max_nodes:
                break
            kids.append(freeze(node["children"][name]))
        return {"name": node["name"][:128], "files": node["files"], "children": kids}

    return freeze(root)


def githook_state(toplevel: str) -> tuple[str, list[str]]:
    """``(state, missing hooks)``: the crew gate reachable from each active hook path (§8.4)."""
    hooks_dir = B.try_out(["rev-parse", "--path-format=absolute", "--git-path", "hooks"], toplevel)
    if not hooks_dir:
        return "unknown", []
    husky = os.path.join(toplevel, ".husky")
    lefthook_local = os.path.join(toplevel, "lefthook-local.yml")
    missing: list[str] = []
    chained = False
    for hook in ("pre-commit", "prepare-commit-msg", "pre-push"):
        candidates = [os.path.join(hooks_dir, hook)]
        if "/.husky" in hooks_dir.replace(os.sep, "/"):
            candidates.append(os.path.join(husky, hook))
        found = False
        for path in candidates:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if S.CREW_HOOK_MARKER in text or "crew-gate.py" in text:
                found = True
                chained = chained or path.startswith(husky)
        if not found and os.path.exists(lefthook_local):
            try:
                if S.CREW_HOOK_MARKER in Path(lefthook_local).read_text(encoding="utf-8", errors="replace"):
                    found = chained = True
            except OSError:
                pass
        if not found:
            missing.append(hook)
    if missing:
        return "missing", missing
    return ("chained" if chained else "ok"), []


def githook_state_repaired(home: Path, toplevel: str) -> tuple[str, list[str]]:
    """:func:`githook_state`, after reinstalling missing crew git hooks in a repo whose owner consented
    to them with ``remembra-crew connect --git-hooks`` (WP-10 ``install.ensure_git_hooks``). Without that
    consent nothing is written and the hooks stay reported as missing."""
    state, missing = githook_state(toplevel)
    if state != "missing":
        return state, missing
    try:
        from remembra.relay.crew.install import ensure_git_hooks

        ensure_git_hooks(home, Path(toplevel))
    except Exception as e:  # a failed repair is reported as githook.missing, never fatal
        log.warning("git hook repair failed in %s: %s", toplevel, e.__class__.__name__)
        return state, missing
    return githook_state(toplevel)


# ===========================================================================
# The daemon
# ===========================================================================

ConfigLoader = Callable[[str | None, str | None], RelayConfig]


def default_config_loader(home: Path) -> ConfigLoader:
    def load(agent: str | None, prefer: str | None) -> RelayConfig:
        return load_config(agent=agent, prefer=prefer, home=home)

    return load


class CrewdError(Exception):
    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.extra = extra


class Crewd:
    """All crewd state and behaviour; the socket server and the loops call into it."""

    def __init__(
        self,
        layout: Layout,
        *,
        config_loader: ConfigLoader | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = _now,
        is_alive: Callable[[int | None], bool] = pid_alive,
        ppid_table: Callable[[], Mapping[int, tuple[int, str]]] | None = None,
        ws_connect: Callable[[str, Mapping[str, str]], Awaitable[Any]] | None = None,
        hostname: str | None = None,
        restore_gate: bool = True,
    ) -> None:
        self.layout = layout
        self.config_loader = config_loader or default_config_loader(layout.home)
        self.transport = transport
        self.clock = clock
        self.is_alive = is_alive
        self.ppid_table_fn = ppid_table
        self.ws_connect = ws_connect
        self.hostname = hostname or socket.gethostname()
        self.restore_gate = restore_gate
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.hosts: dict[str, dict[str, Any]] = {}
        self.apis: dict[tuple[str, str, str], Api] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.etags: dict[str, str] = {}
        self.crew_settings: dict[str, tuple[float, int, dict[str, Any]]] = {}
        self.server_reachable = True
        self.server_outage = False
        self.unreachable_since: float | None = None
        self.crew_exists_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.detector_states: dict[str, D.TailState] = {}
        self.pending_footprints: dict[str, dict[str, dict[str, Any]]] = {}
        self.tool_windows: dict[str, list[list[float]]] = {}
        self.presence_dirty: set[str] = set()
        self.presence_sent: dict[str, float] = {}
        self.ws_links: dict[str, Any] = {}
        self.zones_uploaded: dict[str, str] = {}
        self.tree_sent: dict[str, float] = {}
        self.batons: dict[str, dict[str, Any]] = {}
        # last HEAD scanned for commits without a crew trailer, per checkout (persisted: a crewd that was
        # killed scans what was committed while it was down on its first heartbeat back)
        self.commits_seen: dict[str, str] = {}
        self.test_verdicts: dict[str, dict[str, dict[str, Any]]] = {}
        self.background: set[asyncio.Task[Any]] = set()
        self.server: asyncio.AbstractServer | None = None
        self.stopping = asyncio.Event()
        self.started_at = clock()
        self.salt = b""
        self.hmac_key = b""
        self.config: dict[str, Any] = {}
        self.arbiter = Arbiter(layout.arbiter, is_alive=is_alive, clock=clock)
        self.fence = Fence(layout.fence)
        self.events_log: list[dict[str, Any]] = []  # local audit of tamper/bypass for doctor

    # -- persistence ----------------------------------------------------------
    def load_state(self) -> None:
        self.layout.ensure()
        self.salt = self._secret("salt")
        self.hmac_key = self._secret("hmac")
        self.hosts = read_json(self.layout.keys / "hosts.json") or {}
        self.config = read_json(self.layout.config_file) or {}
        self.zones_uploaded = (read_json(self.layout.run / "zones.json") or {}).get("uploaded", {})
        self.tree_sent = (read_json(self.layout.run / "tree.json") or {}).get("sent", {})
        self.batons = (read_json(self.layout.run / "batons.json") or {}).get("refs", {})
        self.commits_seen = (read_json(self.layout.run / "commits.json") or {}).get("seen", {})
        for path in self.layout.sessions.glob("*.json"):
            if path.name.endswith(".turn.json"):
                continue
            data = read_json(path)
            if not data or not data.get("key"):
                continue
            if data.get("ended"):
                continue
            key = str(data["key"])
            token = self._read_token(key)
            if token is None:
                continue
            self.sessions[key] = data
            self.tokens[key] = token
            self.detector_states[key] = D.TailState.from_json(data.get("detector"))

    def _secret(self, name: str) -> bytes:
        path = self.layout.keys / name
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        data = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return data

    def _read_token(self, key: str) -> str | None:
        try:
            return (self.layout.keys / f"{key}.token").read_text().strip() or None
        except OSError:
            return None

    def _write_token(self, key: str, token: str) -> None:
        path = self.layout.keys / f"{key}.token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token)

    def _drop_token(self, key: str) -> None:
        self.tokens.pop(key, None)
        with contextlib.suppress(OSError):
            (self.layout.keys / f"{key}.token").unlink()

    def persist(self, key: str) -> None:
        sess = self.sessions.get(key)
        if sess is None:
            return
        sess["detector"] = self.detector_states.get(key, D.TailState()).to_json()
        write_json(self.layout.session_file(key), sess)

    def save_hosts(self) -> None:
        write_json(self.layout.keys / "hosts.json", self.hosts)

    def write_status(self) -> None:
        write_json(
            self.layout.status_file,
            {
                "pid": os.getpid(),
                "version": CREWD_VERSION,
                "server_reachable": self.server_reachable,
                "server_outage": self.server_outage,
                "unreachable_since": self.unreachable_since,
                "sessions": len(self.sessions),
                "updated_at": self.clock(),
                "started_at": self.started_at,
            },
        )

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for background work (snapshot syncs, outbox flushes, fire-and-forget requests)."""
        end = time.monotonic() + timeout
        while self.background and time.monotonic() < end:
            await asyncio.wait(list(self.background), timeout=max(0.01, end - time.monotonic()))

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    # -- API plumbing -----------------------------------------------------------
    def cfg_for(self, sess: Mapping[str, Any]) -> RelayConfig:
        return self.config_loader(sess.get("agent_id"), ADAPTER_CONFIG_SOURCE.get(str(sess.get("adapter") or "")))

    def api_for_cfg(self, cfg: RelayConfig, agent_id: str | None) -> Api:
        k = (cfg.url, cfg.api_key or "", agent_id or "")
        api = self.apis.get(k)
        if api is None:
            api = Api(cfg, agent_id=agent_id, transport=self.transport)
            self.apis[k] = api
        return api

    def api_for(self, sess: Mapping[str, Any]) -> Api:
        return self.api_for_cfg(self.cfg_for(sess), str(sess.get("agent_id") or "") or None)

    @staticmethod
    def host_fp(cfg: RelayConfig) -> str:
        return _sha(f"{cfg.url}\0{cfg.api_key or ''}", 16)

    def host_label(self) -> str:
        return "h" + hashlib.sha256(self.salt + self.hostname.encode()).hexdigest()[:11]

    async def note_result(self, api: Api, resp: Resp | None) -> None:
        """Track reachability; a 5xx with a failing ``/health`` is a server outage (§8.1 fencing)."""
        was = self.server_reachable
        if resp is None:
            self.server_reachable = False
            self.server_outage = False
            self.unreachable_since = self.unreachable_since or self.clock()
        elif resp.status >= 500:
            try:
                health = await api.call("GET", "/health", root=True, timeout=3.0)
                healthy = health.ok
            except Unreachable:
                healthy = False
            self.server_reachable = False
            self.server_outage = not healthy
            self.unreachable_since = self.unreachable_since or self.clock()
        else:
            self.server_reachable = True
            self.server_outage = False
            self.unreachable_since = None
            if not was:
                self.spawn(self.on_reconnect())
        self.write_status()

    async def request(self, api: Api, method: str, path: str, **kw: Any) -> Resp:
        try:
            resp = await api.call(method, path, **kw)
        except Unreachable:
            await self.note_result(api, None)
            raise
        await self.note_result(api, resp)
        return resp

    async def ensure_host(self, cfg: RelayConfig, api: Api) -> dict[str, Any]:
        fp = self.host_fp(cfg)
        host = self.hosts.get(fp)
        if host and host.get("host_id") and host.get("host_token"):
            return host
        resp = await self.request(
            api,
            "POST",
            "/crew/hosts/register",
            json_body={"host_label": self.host_label(), "platform": sys.platform[:32], "crewd_version": CREWD_VERSION},
        )
        if not resp.ok:
            raise CrewdError("host_register_failed", f"host registration failed: HTTP {resp.status} {resp.error()}")
        body = resp.body or {}
        host = {"host_id": body["host_id"], "host_token": body["host_token"], "url": cfg.url, "registered_at": self.clock()}
        self.hosts[fp] = host
        self.save_hosts()
        return host

    def ppid_table(self, require: int | None = None) -> Mapping[int, tuple[int, str]]:
        if self.ppid_table_fn is not None:
            return self.ppid_table_fn()
        return cached_ppid_map(require)

    # -- identity -------------------------------------------------------------------
    def resolve_peer(self, peer: Peer, key: str | None = None) -> dict[str, Any] | None:
        """The session this peer may act as: its agent pid must be an ancestor of the peer (or the peer)."""
        if peer.pid is None:
            return None
        chain = list(peer.chain) if peer.chain is not None else ancestors(peer.pid, self.ppid_table())
        if key is not None:
            sess = self.sessions.get(key)
            if sess is None or sess.get("ended"):
                return None
            return sess if sess.get("agent_pid") in chain else None
        best: tuple[int, dict[str, Any]] | None = None
        for sess in self.sessions.values():
            if sess.get("ended"):
                continue
            pid = sess.get("agent_pid")
            if pid in chain:
                depth = chain.index(pid)
                if best is None or depth < best[0]:
                    best = (depth, sess)
        return best[1] if best else None

    def require(self, peer: Peer, key: str | None) -> dict[str, Any]:
        sess = self.resolve_peer(peer, key)
        if sess is None:
            raise CrewdError("not_your_session", "this process is not inside a crew session's process tree")
        return sess

    def token_of(self, sess: Mapping[str, Any]) -> str:
        tok = self.tokens.get(str(sess["key"]))
        if not tok:
            raise CrewdError("no_token", "this session has no token (join again)")
        return tok

    # -- join -------------------------------------------------------------------------
    async def join(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        adapter = str(args.get("adapter") or "claude-code")
        client_sid = str(args.get("client_session_id") or "").strip()
        if not client_sid:
            raise CrewdError("no_session_id", "the hook payload has no session_id")
        agent_pid = args.get("agent_pid")
        if not isinstance(agent_pid, int) or agent_pid <= 1:
            raise CrewdError("no_agent_pid", "agent_pid is required")
        chain = list(peer.chain) if peer.chain is not None else (ancestors(peer.pid, self.ppid_table()) if peer.pid else [])
        if peer.pid is not None and agent_pid not in chain:
            raise CrewdError("agent_pid_not_ancestor", "agent_pid must be an ancestor of the calling process")
        cwd = str(args.get("cwd") or "")
        # a join may create this project's crew: forget "no crew" answers so the next session here joins it
        self.crew_exists_cache.clear()
        facts = B.repo_facts(cwd) if cwd else None
        if facts is None:
            raise CrewdError("not_a_repo", "crew mode needs a git checkout")
        agent_id = str(args.get("agent_id") or adapter)
        key = session_key(adapter, client_sid)
        cfg = self.config_loader(agent_id, ADAPTER_CONFIG_SOURCE.get(adapter))
        if not cfg.api_key:
            raise CrewdError("no_api_key", "no Remembra API key configured")
        api = self.api_for_cfg(cfg, agent_id)
        host = await self.ensure_host(cfg, api)
        project_id = args.get("project_id") or await self.resolve_project(api, cfg, cwd)
        zones_sha = None
        plan = ZC.plan_upload(facts.toplevel, facts.default_branch, None)
        if plan.result is not None and plan.result.ok:
            zones_sha = plan.result.sha
        worktree_id = _sha(facts.toplevel, 16)
        checkout_fp = hashlib.sha256(self.salt + facts.toplevel.encode()).hexdigest()[:32]
        body: dict[str, Any] = {
            "project_id": project_id,
            "agent_id": agent_id,
            "session_id": client_sid,
            "adapter": adapter,
            "client_kind": "hook",
            "host_id": host["host_id"],
            "checkout_fp": checkout_fp,
            "worktree_id": worktree_id,
            "branch": facts.branch,
            "head": facts.head,
            "zones_sha": zones_sha,
            "model": (str(args.get("model"))[:64] if args.get("model") else None),
            "source": str(args.get("source") or "startup")[:32],
        }
        old_token = self.tokens.get(key) or self._read_token(key)
        resp = await self.request(
            api, "POST", "/crews/join", json_body=body, host_token=host["host_token"], session_token=old_token
        )
        if resp.status == 401 and resp.error() in ("host_token_invalid", "invalid_host_token"):
            self.hosts.pop(self.host_fp(cfg), None)
            host = await self.ensure_host(cfg, api)
            body["host_id"] = host["host_id"]
            resp = await self.request(api, "POST", "/crews/join", json_body=body, host_token=host["host_token"])
        if not resp.ok:
            raise CrewdError("join_failed", f"join failed: HTTP {resp.status} {resp.error()}")
        data = resp.body or {}
        token = data.get("session_token") or old_token
        if not token:
            raise CrewdError("join_failed", "the server returned no session token")
        self.tokens[key] = token
        self._write_token(key, token)
        view = data.get("session") or {}
        sess: dict[str, Any] = {
            "v": 1,
            "key": key,
            "adapter": adapter,
            "agent_id": agent_id,
            "client_session_id": client_sid,
            "crew_id": data["crew_id"],
            "session_id": data["session_id"],
            "callsign": data.get("callsign"),
            "member_key": data.get("member_key"),
            "project_id": project_id,
            "host_fp": self.host_fp(cfg),
            "toplevel": facts.toplevel,
            "worktree_id": worktree_id,
            "checkout_fp": checkout_fp,
            "git_common_dir": facts.git_common_dir,
            "default_branch": facts.default_branch,
            "case_insensitive": facts.case_insensitive,
            "remote": facts.remote,
            "branch": facts.branch,
            "started_head": facts.head,
            "agent_pid": agent_pid,
            "transcript_path": args.get("transcript_path"),
            "enforcement": view.get("adapter_enforcement") or "advisory",
            "observe_only": bool(data.get("observe_only")),
            "joined_at": self.clock(),
            "last_activity_at": self.clock(),
            "last_action": None,
            "calls_since_checkpoint": 0,
            "files_since_checkpoint": 0,
            "cursor": int(data.get("seq") or 0),
            "news": [],
            "inject": None,
            "task_commits": {},
            "unconfirmed_zone_ids": [],
            "bypass": None,
            "human_name": str(self.config.get("human_name") or "the owner"),
            "githook_state": githook_state_repaired(self.layout.home, facts.toplevel)[0],
            "ended": False,
            "last_checkpoint": {"at": self.clock(), "head": facts.head, "footprints": 0, "tests": ""},
            "last_dirty": [],
        }
        self.sessions[key] = sess
        self.detector_states.setdefault(key, D.TailState())
        self.persist(key)
        if facts.head and facts.toplevel not in self.commits_seen:
            self.commits_seen[facts.toplevel] = facts.head  # commits after the join are scanned for trailers
            self.save_commits_seen()
        crew_id = str(data["crew_id"])
        if plan.upload:
            with contextlib.suppress(Exception):
                await self.upload_zones(sess, plan)
        await self.sync_snapshot(crew_id)
        self.spawn(self.maybe_send_tree(sess))
        shared = [
            s for s in self.sessions.values() if s is not sess and s.get("checkout_fp") == checkout_fp and not s.get("ended")
        ]
        return {
            "ok": True,
            "key": key,
            "crew_id": crew_id,
            "session_id": data["session_id"],
            "callsign": data.get("callsign"),
            "member_key": data.get("member_key"),
            "project_id": project_id,
            "batons_offered": data.get("batons_offered") or [],
            "auto_adopted": data.get("auto_adopted") or [],
            "rejoined": bool(data.get("rejoined")),
            "shared_checkout": bool(shared),
            "observe_only": bool(data.get("observe_only")),
            "githook_state": sess["githook_state"],
        }

    async def resolve_project(self, api: Api, cfg: RelayConfig, cwd: str) -> str:
        from remembra.relay import facts as factlib

        info = factlib.repo_info(Path(cwd), factlib.Deadline(3.0))
        locator = info.locator(Path(cwd), self.hostname)
        if cfg.project and cfg.project != "default":
            locator["hint_project"] = cfg.project
        resp = await self.request(api, "POST", "/projects/resolve", json_body=locator)
        if not resp.ok or not isinstance(resp.body, dict) or not resp.body.get("project_id"):
            raise CrewdError("project_unresolved", f"project resolution failed: HTTP {resp.status} {resp.error()}")
        return str(resp.body["project_id"])

    async def crew_exists(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only: does this checkout's project already have a crew (``POST /crews/resolve``, never creates)?"""
        from remembra.relay import facts as factlib

        adapter = str(args.get("adapter") or "claude-code")
        cwd = str(args.get("cwd") or "")
        if not cwd:
            return {"ok": True, "exists": False}
        cfg = self.config_loader(str(args.get("agent_id") or adapter), ADAPTER_CONFIG_SOURCE.get(adapter))
        if not cfg.api_key:
            return {"ok": True, "exists": False, "error": "no_api_key"}
        # SessionStart asks this in every git repo on the machine (global install): answer from a short
        # cache when the server said "no crew" or could not be reached, so an unrelated repo never waits
        # on the network twice (§10.4 SessionStart 10 s hard).
        now = self.clock()
        cache_key = f"{cfg.url}\0{os.path.realpath(cwd)}"
        unreachable_key = f"{cfg.url}\0<unreachable>"
        for k in (cache_key, unreachable_key):
            hit = self.crew_exists_cache.get(k)
            if hit is not None and hit[0] > now:
                return dict(hit[1])
        info = await asyncio.to_thread(factlib.repo_info, Path(cwd), factlib.Deadline(CREW_EXISTS_REPO_S))
        if not info.is_git:
            return {"ok": True, "exists": False}
        locator = info.locator(Path(cwd), self.hostname)
        locator.pop("host", None)
        try:
            resp = await self.request(
                self.api_for_cfg(cfg, str(args.get("agent_id") or adapter)),
                "POST",
                "/crews/resolve",
                json_body=locator,
                timeout=CREW_EXISTS_REQUEST_S,
            )
        except Unreachable:
            result = {"ok": False, "exists": False, "error": "unreachable"}
            self.crew_exists_cache[unreachable_key] = (now + CREW_EXISTS_UNREACHABLE_TTL_S, result)
            return result
        if resp.ok and isinstance(resp.body, dict):
            return {
                "ok": True,
                "exists": True,
                "crew_id": (resp.body.get("crew") or {}).get("id"),
                "project_id": resp.body.get("project_id"),
            }
        result = {"ok": True, "exists": False}
        if resp.status == 404:
            if len(self.crew_exists_cache) > 1000:
                self.crew_exists_cache = {k: v for k, v in self.crew_exists_cache.items() if v[0] > now}
            self.crew_exists_cache[cache_key] = (now + CREW_EXISTS_NEGATIVE_TTL_S, result)
        return result

    async def brief(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        sess = self.require(peer, args.get("key"))
        params = {
            "agent_id": sess["agent_id"],
            "project_id": sess["project_id"],
            "session_id": sess["client_session_id"],
            "recent_n": 8,
        }
        if sess.get("branch"):
            params["branch"] = sess["branch"]
        try:
            resp = await self.request(self.api_for(sess), "GET", "/session/brief", params=params, timeout=6.0)
        except Unreachable:
            return {"ok": False, "error": "unreachable"}
        if not resp.ok or not isinstance(resp.body, dict):
            return {"ok": False, "error": resp.error()}
        return {"ok": True, "rendered": str(resp.body.get("rendered") or ""), "crew": resp.body.get("crew")}

    # -- snapshot ------------------------------------------------------------------------
    def crew_sessions(self, crew_id: str) -> list[dict[str, Any]]:
        return [s for s in self.sessions.values() if s.get("crew_id") == crew_id and not s.get("ended")]

    def checkouts_for(self, crew_id: str) -> list[dict[str, Any]]:
        return [
            SN.checkout_entry(
                toplevel=str(s["toplevel"]),
                worktree_id=str(s["worktree_id"]),
                git_common_dir=str(s.get("git_common_dir") or ""),
                case_insensitive=bool(s.get("case_insensitive")),
                session_id=str(s["session_id"]),
                default_branch=s.get("default_branch"),
            )
            for s in self.crew_sessions(crew_id)
        ]

    async def crew_settings_for(self, crew_id: str, api: Api, version: int | None) -> dict[str, Any]:
        cached = self.crew_settings.get(crew_id)
        if cached and (version is None or cached[1] == version) and self.clock() - cached[0] < SETTINGS_EVERY_S:
            return cached[2]
        resp = await self.request(api, "GET", f"/crews/{crew_id}", timeout=5.0)
        if resp.ok and isinstance(resp.body, dict):
            settings = dict(resp.body.get("settings") or {})
            self.crew_settings[crew_id] = (self.clock(), int(resp.body.get("settings_version") or 0), settings)
            return settings
        return cached[2] if cached else {}

    async def sync_snapshot(self, crew_id: str) -> bool:
        members = self.crew_sessions(crew_id)
        if not members:
            return False
        api = self.api_for(members[0])
        etag = self.etags.get(crew_id)
        try:
            resp = await self.request(api, "GET", f"/crews/{crew_id}/snapshot", if_none_match=etag, timeout=5.0)
        except Unreachable:
            return False
        now = self.clock()
        if resp.status == 304 and crew_id in self.snapshots:
            server = dict(self.snapshots[crew_id])
            server["server_time"] = _ts(SN.server_now(server, now))
        elif resp.ok and isinstance(resp.body, dict):
            server = dict(resp.body)
            if resp.headers.get("etag"):
                self.etags[crew_id] = resp.headers["etag"]
        else:
            return False
        version = int((server.get("crew") or {}).get("settings_version") or 0) or None
        settings = await self.crew_settings_for(crew_id, api, version)
        host = self.hosts.get(str(members[0].get("host_fp") or "")) or {}
        local = SN.build_local_snapshot(
            {
                k: v
                for k, v in server.items()
                if k not in ("synced_at", "skew_s", "host_id", "checkouts", "settings", "bootstrap_zones")
            },
            settings=settings,
            host_id=str(host.get("host_id") or "hst_unknown"),
            checkouts=self.checkouts_for(crew_id),
            local_now=now,
            hmac_key=self.hmac_key,
        )
        self.snapshots[crew_id] = local
        SN.write_snapshot(self.layout.snapshot_file(crew_id), local)
        self.after_snapshot(crew_id, local)
        return True

    def after_snapshot(self, crew_id: str, snap: Mapping[str, Any]) -> None:
        """Arbiter seed, session views, advisory fence and baton bookkeeping after each sync."""
        views = {s.get("id"): s for s in snap.get("sessions") or ()}
        local = {
            str(s["session_id"]): LocalSessionRef(str(s["session_id"]), s.get("agent_pid"), s.get("toplevel"))
            for s in self.crew_sessions(crew_id)
        }
        if self.server_reachable:
            self.arbiter.seed(crew_id, snap, local)
        tasks = {t.get("id"): t for t in snap.get("tasks") or ()}
        for sess in self.crew_sessions(crew_id):
            view = views.get(sess["session_id"]) or {}
            changed = False
            for field_name, value in (
                ("enforcement", view.get("adapter_enforcement") or sess.get("enforcement")),
                ("state", view.get("state") or sess.get("state")),
                ("current_task_id", view.get("current_task_id")),
            ):
                if sess.get(field_name) != value:
                    sess[field_name] = value
                    changed = True
            task = tasks.get(view.get("current_task_id"))
            ref = f"T-{task['number']}" if task and task.get("number") else None
            if sess.get("task_ref") != ref:
                sess["task_ref"] = ref
                changed = True
            if changed:
                self.persist(str(sess["key"]))
        self.apply_fence(crew_id, snap)
        reserved_refs = {
            c.get("baton_ref") for c in snap.get("claims") or () if c.get("state") == "reserved" and c.get("baton_ref")
        }
        for ref, rec in self.batons.items():
            if rec.get("crew_id") == crew_id and not rec.get("closed_at") and ref not in reserved_refs and rec.get("reported"):
                rec["closed_at"] = self.clock()
        self.save_batons()

    def apply_fence(self, crew_id: str, snap: Mapping[str, Any]) -> None:
        settings = snap.get("settings") or {}
        if not settings.get("readonly_fence_for_advisory", True):
            return
        for sess in self.crew_sessions(crew_id):
            if sess.get("enforcement") != "advisory":
                continue
            top = str(sess["toplevel"])
            others_here = [s for s in self.crew_sessions(crew_id) if s is not sess and s.get("toplevel") == top]
            if others_here:
                # only in the advisory session's own worktree: once another session works here, lift any
                # fence applied before, or that session's granted writes would hit EACCES
                self.lift_fence(top, why=f"{sess.get('callsign')}: checkout shared")
                continue
            zones = zones_to_fence(snap, str(sess["session_id"]))
            try:
                res = self.fence.apply(top, zones, case_insensitive=bool(sess.get("case_insensitive")))
            except OSError as e:
                log.warning("fence failed for %s: %s", sess.get("callsign"), e)
                continue
            if res.fenced or res.restored:
                log.info("fence %s: +%d -%d", sess.get("callsign"), len(res.fenced), len(res.restored))

    def lift_fence(self, toplevel: str, *, why: str) -> None:
        """Restore every fenced mode of one checkout (no-op when nothing is fenced there)."""
        if not self.fence.fenced(toplevel):
            return
        try:
            res = self.fence.restore(toplevel)
        except OSError as e:
            log.warning("fence restore failed in %s (%s): %s", toplevel, why, e.__class__.__name__)
            return
        if res.restored or res.errors:
            log.info("fence lifted (%s): -%d, %d error(s)", why, len(res.restored), len(res.errors))

    def save_batons(self) -> None:
        write_json(self.layout.run / "batons.json", {"refs": self.batons})

    # -- heartbeat -----------------------------------------------------------------------
    async def scan_unattributed(self, sessions: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, list[dict[str, Any]]]]:
        """New commits without a ``Remembra-Member`` trailer in each live session's checkout (§8.4).

        Returns ``{session key: (head, commits)}`` for one reporting session per checkout; the
        checkout's scan mark moves to ``head`` only after the server took the heartbeat. The first
        scan of a checkout only records its HEAD (history before crew mode is never reported).
        """
        out: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        done: set[str] = set()
        for sess in sessions:
            top = str(sess.get("toplevel") or "")
            if not top or top in done or sess.get("ended"):
                continue
            done.add(top)
            try:
                head = await asyncio.wait_for(
                    asyncio.to_thread(B.try_out, ["rev-parse", "--verify", "-q", "HEAD"], top), timeout=GIT_DELTA_TIMEOUT_S
                )
            except (TimeoutError, OSError):
                continue
            if not head:
                continue
            seen = self.commits_seen.get(top)
            if seen is None:
                self.commits_seen[top] = head
                self.save_commits_seen()
                continue
            if seen == head:
                continue
            try:
                found = await asyncio.wait_for(
                    asyncio.to_thread(B.unattributed_commits, top, seen, head), timeout=GIT_DELTA_TIMEOUT_S * 4
                )
            except (TimeoutError, OSError, B.GitError):
                continue
            rels = []
            for c in found:
                files = [f for f in c["files"] if S.is_path_rel(f)]
                if files:
                    rels.append({"sha": c["sha"], "files": files[:100]})
            out[str(sess["key"])] = (head, rels[:20])
        return out

    def save_commits_seen(self) -> None:
        write_json(self.layout.run / "commits.json", {"seen": self.commits_seen})

    async def heartbeat(self) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for sess in self.sessions.values():
            if not sess.get("ended") and sess.get("host_fp") in self.hosts:
                groups.setdefault(str(sess["host_fp"]), []).append(sess)
        results: dict[str, Any] = {}
        crews_to_sync: set[str] = set()
        scans = await self.scan_unattributed([s for members in groups.values() for s in members if str(s["key"]) in self.tokens])
        for fp, members in groups.items():
            host = self.hosts[fp]
            api = self.api_for(members[0])
            items: list[dict[str, Any]] = []
            sent_fp: dict[str, list[str]] = {}
            for sess in members:
                key = str(sess["key"])
                if key not in self.tokens:
                    continue
                alive = self.is_alive(sess.get("agent_pid"))
                age = max(0, int(self.clock() - float(sess.get("last_activity_at") or sess.get("joined_at") or self.clock())))
                now = self.clock()
                fps = [
                    {**{k: v for k, v in f.items() if k != "at"}, "age_s": max(0, int(now - float(f.get("at") or now)))}
                    for f in list(self.pending_footprints.get(key, {}).values())[:500]
                ]
                sent_fp[key] = [f["path"] for f in fps]
                item: dict[str, Any] = {
                    "session_id": sess["session_id"],
                    "token": self.tokens[key],
                    "alive": alive,
                    "activity_age_s": age,
                    "last_action": self._last_action(sess),
                    "calls_since_checkpoint": int(sess.get("calls_since_checkpoint") or 0),
                    "footprints": fps,
                    "cursor": int(sess.get("cursor") or 0),
                    "githook_state": sess.get("githook_state") or "unknown",
                }
                if sess.get("limit"):
                    item["limit"] = sess["limit"]
                if key in scans and scans[key][1]:
                    item["unattributed_commits"] = scans[key][1]
                items.append(item)
            if not items:
                continue
            body = {"batch_id": f"hb{secrets.token_hex(8)}", "sessions": items}
            try:
                resp = await self.request(api, "POST", "/crew/heartbeat", json_body=body, host_token=host["host_token"])
            except Unreachable:
                results[fp] = "unreachable"
                continue
            if resp.status == 401:
                self.hosts.pop(fp, None)
                self.save_hosts()
                results[fp] = "host_token_invalid"
                continue
            if not resp.ok or not isinstance(resp.body, dict):
                results[fp] = f"http_{resp.status}"
                continue
            results[fp] = "ok"
            per = resp.body.get("per_session") or {}
            for sess in members:
                key = str(sess["key"])
                got = per.get(sess["session_id"])
                if not isinstance(got, dict):
                    continue
                if got.get("error"):
                    if got.get("error") == "session_ended":
                        self.mark_ended(key, "server_ended")
                    continue
                for path in sent_fp.get(key, []):
                    self.pending_footprints.get(key, {}).pop(path, None)
                if key in scans and sess.get("toplevel"):
                    self.commits_seen[str(sess["toplevel"])] = scans[key][0]
                    self.save_commits_seen()
                self.apply_heartbeat(sess, got)
                crews_to_sync.add(str(got.get("crew_id") or sess["crew_id"]))
        for crew_id in crews_to_sync:
            await self.sync_snapshot(crew_id)
        return results

    def _last_action(self, sess: Mapping[str, Any]) -> dict[str, Any] | None:
        la = sess.get("last_action")
        if not isinstance(la, dict) or not la.get("tool"):
            return None
        out: dict[str, Any] = {
            "tool": str(la["tool"])[:64],
            "age_s": max(0, int(self.clock() - float(la.get("at") or self.clock()))),
        }
        if la.get("path_rel") and S.is_path_rel(la["path_rel"]):
            out["path_rel"] = la["path_rel"]
        if la.get("verb"):
            out["verb"] = str(la["verb"])[:32]
        cleaned = outbound("heartbeat", out, repo_root=sess.get("toplevel"), home=str(self.layout.home))
        return cleaned if isinstance(cleaned, dict) else None

    def apply_heartbeat(self, sess: dict[str, Any], got: Mapping[str, Any]) -> None:
        key = str(sess["key"])
        sess["state"] = got.get("state") or sess.get("state")
        sess["lease_expires_at"] = got.get("lease_expires_at")
        sess["fence_at"] = got.get("fence_at")
        deltas = got.get("deltas_since_cursor") or {}
        events = [e for e in deltas.get("events") or () if isinstance(e, dict)]
        news = list(sess.get("news") or [])
        for e in events:
            if not e.get("mine"):
                news.append(
                    {
                        "seq": e.get("seq"),
                        "type": e.get("type"),
                        # under the brief's trust policy: a summary can quote agent-authored text
                        "summary": police_item(str(e.get("summary") or ""), clip_body=lambda t: S.clip_item(t, 140)),
                    }
                )
        sess["news"] = news[-MAX_NEWS:]
        if deltas.get("to_seq") is not None:
            sess["cursor"] = int(deltas["to_seq"])
        inject = got.get("inject_text")
        current = sess.get("inject") if isinstance(sess.get("inject"), dict) else None
        if inject:
            ident = _sha(str(inject), 12)
            if current is None or current.get("id") != ident:
                sess["inject"] = {"id": ident, "text": str(inject)[:300]}
        elif current is not None and sess.get("inject_delivered") == current.get("id"):
            sess["inject"] = None  # delivered once (PreToolUse context, the turn line or a rewake); kept until then
        self.persist(key)

    # -- activity (from the gate) ----------------------------------------------------------
    async def activity(self, peer: Peer | None, args: Mapping[str, Any]) -> dict[str, Any]:
        key = str(args.get("key") or "")
        sess = self.sessions.get(key) if peer is None else self.resolve_peer(peer, key)
        if sess is None:
            return {"ok": False, "error": "not_your_session"}
        at = float(args.get("at") or self.clock())
        phase = str(args.get("phase") or "start")
        tool = str(args.get("tool") or "")[:64]
        paths = [p for p in args.get("paths") or () if isinstance(p, str) and S.is_path_rel(p)]
        windows = self.tool_windows.setdefault(key, [])
        if phase == "start":
            windows.append([at, 0.0])
            del windows[:-MAX_TOOL_WINDOWS]
            sess["calls_since_checkpoint"] = int(sess.get("calls_since_checkpoint") or 0) + 1
        elif phase == "blocked":
            pass  # a denied call never runs (no PostToolUse follows): activity only, no tool window
        else:
            for w in reversed(windows):
                if w[1] == 0.0:
                    w[1] = at
                    break
        sess["last_activity_at"] = max(float(sess.get("last_activity_at") or 0), at)
        sess["last_action"] = {"tool": tool, "path_rel": paths[0] if paths else None, "verb": args.get("verb"), "at": at}
        state = self.detector_states.get(key)
        if state is not None and state.limit_at is not None and at > state.limit_at:
            state.last_tool_at = at
        if phase == "end":
            if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") or (tool.startswith("mcp__") and paths):
                for p in paths:
                    self.add_footprint(sess, p, "dirty", "certain")
                sess["files_since_checkpoint"] = int(sess.get("files_since_checkpoint") or 0) + len(paths)
            elif tool == "Bash":
                await self.posttool_delta(sess, args)
        self.presence_dirty.add(key)
        self.persist(key)
        return {"ok": True}

    def add_footprint(
        self, sess: Mapping[str, Any], path: str, state: str, attribution: str, *, last_commit: str | None = None
    ) -> None:
        key = str(sess["key"])
        rec = self.pending_footprints.setdefault(key, {}).get(path)
        attr = "certain" if (rec and rec.get("attribution") == "certain") or attribution == "certain" else "probable"
        # "at": when the write was seen (local clock); the heartbeat sends it as an age, so a write made
        # before this holder's lease horizon is never taken for a stale-epoch write when it arrives late
        entry: dict[str, Any] = {"path": path, "state": state, "attribution": attr, "at": self.clock()}
        epoch = self._epoch_for(sess, path)
        if epoch:
            entry["claim_epoch"] = epoch
        if last_commit:
            entry["last_commit"] = last_commit
        self.pending_footprints[key][path] = entry
        known = set(sess.get("known_footprints") or [])
        known.add(path)
        sess_mut = self.sessions.get(key)
        if sess_mut is not None:
            sess_mut["known_footprints"] = sorted(known)[-2000:]

    def _epoch_for(self, sess: Mapping[str, Any], path: str) -> int | None:
        snap = self.snapshots.get(str(sess.get("crew_id")))
        if not snap:
            return None
        from remembra.crew import gatecore as G

        ci = bool(sess.get("case_insensitive"))
        zone_ids = {
            z.get("id")
            for z in snap.get("zones") or ()
            if any(G.glob_match(str(g), path, case_insensitive=ci) for g in z.get("include_globs") or ())
        }
        for c in snap.get("claims") or ():
            if c.get("zone_id") in zone_ids and c.get("holder_session_id") == sess.get("session_id"):
                return int(c.get("epoch") or 1)
        return None

    async def posttool_delta(self, sess: dict[str, Any], args: Mapping[str, Any]) -> None:
        """The git delta after a non-read-only Bash command, filtered by attribution (§5.3), plus commit/push/test."""
        key = str(sess["key"])
        top = str(sess["toplevel"])
        entries = None
        if not args.get("read_only"):  # read-only commands (test runners included) change nothing to attribute
            try:
                entries = await asyncio.wait_for(asyncio.to_thread(B.dirty_entries, top), timeout=GIT_DELTA_TIMEOUT_S)
            except (TimeoutError, B.GitError):
                entries = None
        if entries is not None:
            current = sorted({p for _xy, p in entries})
            before = set(sess.get("last_dirty") or [])
            fresh = [p for p in current if p not in before]
            for p in self.attribute(sess, fresh):
                self.add_footprint(sess, p, "dirty", "probable")
            sess["last_dirty"] = current[:5000]
        git_op = args.get("git_op")
        if git_op == "commit":
            await self.on_commit(sess)
        elif git_op == "push":
            await self.on_push(sess)
        test = args.get("test")
        if isinstance(test, dict) and test.get("argv"):
            await self.on_test(sess, test)
        self.persist(key)

    def attribute(self, sess: Mapping[str, Any], paths: Sequence[str]) -> list[str]:
        """§5.3: drop paths another local session in this checkout touched or wrote during its own tool call."""
        top = sess.get("toplevel")
        others = [s for s in self.sessions.values() if s is not sess and s.get("toplevel") == top and not s.get("ended")]
        if not others:
            return list(paths)
        taken: set[str] = set()
        for o in others:
            taken.update(o.get("known_footprints") or [])
            taken.update(self.pending_footprints.get(str(o["key"]), {}).keys())
        keep: list[str] = []
        for p in paths:
            if p in taken:
                continue
            try:
                mtime = os.stat(os.path.join(str(top), p)).st_mtime
            except OSError:
                mtime = None
            in_window = False
            if mtime is not None:
                for o in others:
                    for start, end in self.tool_windows.get(str(o["key"]), []):
                        # an open window closes itself after 10 minutes (a tool that died without PostToolUse)
                        stop = end or min(self.clock(), start + OPEN_WINDOW_MAX_S)
                        if start - WINDOW_SLACK_S <= mtime <= stop + WINDOW_SLACK_S:
                            in_window = True
                            break
                    if in_window:
                        break
            if not in_window:
                keep.append(p)
        return keep

    def submit_event(self, sess: Mapping[str, Any], etype: str, payload: Mapping[str, Any]) -> None:
        O.spool(
            self.layout.outbox,
            "event",
            {"type": etype, "payload": dict(payload), "at": self.clock()},
            session_key=str(sess["key"]),
            crew_id=str(sess["crew_id"]),
        )
        self.spawn(self.flush_outbox())

    async def on_commit(self, sess: dict[str, Any]) -> None:
        top = str(sess["toplevel"])
        line = B.try_out(["log", "-1", "--format=%H%x00%s"], top)
        if not line or "\0" not in line:
            return
        sha, subject = line.split("\0", 1)
        files_text = B.try_out(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", sha], top) or ""
        files = [f for f in files_text.split("\0") if f and S.is_path_rel(f)]
        branch = B.try_out(["symbolic-ref", "--quiet", "--short", "HEAD"], top)
        payload = {"sha": sha, "subject_hash": hashlib.sha256(subject.encode()).hexdigest()[:32], "files": files[:100]}
        if branch:
            payload["branch"] = branch[:256]
        self.submit_event(sess, "activity.commit", payload)
        for f in files:
            self.add_footprint(sess, f, "committed", "certain", last_commit=sha)
        task_id = sess.get("current_task_id")
        if task_id:
            commits = dict(sess.get("task_commits") or {})
            commits[str(task_id)] = int(commits.get(str(task_id), 0)) + 1
            sess["task_commits"] = commits
        await self.checkpoint(sess, "commit")

    async def on_push(self, sess: dict[str, Any]) -> None:
        top = str(sess["toplevel"])
        upstream = B.try_out(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], top) or "upstream"
        head = B.try_out(["rev-parse", "HEAD"], top)
        branch = B.try_out(["symbolic-ref", "--quiet", "--short", "HEAD"], top)
        payload: dict[str, Any] = {
            "upstream": upstream[:256],
            "count": len(sess.get("task_commits") or {}),
            "default_branch": bool(branch and branch == sess.get("default_branch")),
        }
        if head:
            payload["head"] = head
        self.submit_event(sess, "activity.push", payload)
        await self.checkpoint(sess, "push")

    async def on_test(self, sess: dict[str, Any], test: Mapping[str, Any]) -> None:
        argv = [str(a) for a in test.get("argv") or ()][:12]
        fingerprint = str(outbound("checkpoint", " ".join(argv), repo_root=sess.get("toplevel"), home=str(self.layout.home)))[
            :128
        ]
        passed, failed = int(test.get("passed") or 0), int(test.get("failed") or 0)
        verdict = "fail" if failed > 0 else ("pass" if passed > 0 else "unknown")
        key = str(sess["key"])
        per = self.test_verdicts.setdefault(key, {})
        prev = per.get(fingerprint, {}).get("verdict", "unknown")
        per[fingerprint] = {"verdict": verdict, "passed": passed, "failed": failed, "at": self.clock()}
        sess["tests"] = {fp: v for fp, v in list(per.items())[-20:]}
        if verdict != prev:
            self.submit_event(
                sess,
                "activity.test_verdict_changed",
                {"fingerprint": fingerprint, "from": prev, "to": verdict, "passed": passed, "failed": failed},
            )
        last = float((sess.get("last_checkpoint") or {}).get("at") or 0)
        if self.clock() - last > TEST_CHECKPOINT_MIN_S:
            await self.checkpoint(sess, "test")

    # -- checkpoints -------------------------------------------------------------------------
    def checkpoint_facts(self, sess: Mapping[str, Any]) -> dict[str, Any]:
        top = str(sess["toplevel"])
        head = B.try_out(["rev-parse", "--verify", "--quiet", "HEAD"], top)
        last_head = (sess.get("last_checkpoint") or {}).get("head")
        commits: list[str] = []
        if head and last_head and head != last_head:
            commits = (B.try_out(["rev-list", "--max-count=50", f"{last_head}..{head}"], top) or "").split()
        try:
            dirty = B.changed_paths(B.dirty_entries(top))
        except B.GitError:
            dirty = []
        shortstat = B.try_out(["diff", "--shortstat", "HEAD"], top) or ""
        tests = [
            {"fingerprint": fp, "passed": v.get("passed", 0), "failed": v.get("failed", 0)}
            for fp, v in (sess.get("tests") or {}).items()
        ]
        facts: dict[str, Any] = {
            "branch": B.try_out(["symbolic-ref", "--quiet", "--short", "HEAD"], top) or "(detached)",
            "head": head,
            "commits": commits,
            "dirty_count": len(dirty),
            "dirty": dirty[:20],
            "diff_stat": shortstat[:200],
            "tests": tests[:20],
            "unpushed_commits": len(B.unpushed_commits(top)),
            "task": sess.get("task_ref"),
        }
        return facts

    def closing_facts(self, sess: Mapping[str, Any]) -> dict[str, Any]:
        """``checkpoint_facts`` for a stall, SessionEnd or orphan: ``commits`` is every commit this session made
        (``started_head..HEAD``), not only those since the last checkpoint, so the stalled or partial report the
        server writes from it lists the session's work (§13.3 step 4)."""
        facts = self.checkpoint_facts(sess)
        started = sess.get("started_head")
        if started:
            since_start = (B.try_out(["rev-list", "--max-count=50", f"{started}..HEAD"], str(sess["toplevel"])) or "").split()
            facts["commits"] = list(dict.fromkeys([*since_start, *(facts.get("commits") or [])]))[:50]
        return facts

    async def checkpoint(
        self, sess: dict[str, Any], trigger: str, *, extra: Mapping[str, Any] | None = None, force: bool = False
    ) -> dict[str, Any]:
        key = str(sess["key"])
        facts = await asyncio.to_thread(self.checkpoint_facts, sess)
        facts.update(extra or {})
        last = sess.get("last_checkpoint") or {}
        tests_digest = _sha(json.dumps(facts.get("tests"), sort_keys=True), 12)
        fps = len(self.pending_footprints.get(key, {})) + len(sess.get("known_footprints") or [])
        if trigger == "turn" and not force:
            changed = (
                facts.get("head") != last.get("head")
                or tests_digest != last.get("tests")
                or fps - int(last.get("footprints") or 0) >= 20
                or (
                    self.clock() - float(last.get("at") or 0) >= CHECKPOINT_INTERVAL_S and fps != int(last.get("footprints") or 0)
                )
            )
            if not changed:
                return {"ok": True, "skipped": "unchanged"}
        clean = outbound("checkpoint", facts, repo_root=sess.get("toplevel"), home=str(self.layout.home))
        body = {"session_id": sess["session_id"], "trigger": trigger, "facts": clean}
        if sess.get("current_task_id"):
            body["task_id"] = sess["current_task_id"]
        idem = f"ck{_sha(json.dumps(body, sort_keys=True, default=str), 24)}"
        result = await self.send_or_spool(sess, "checkpoint", "POST", f"/crews/{sess['crew_id']}/checkpoints", body, idem=idem)
        if result.get("ok"):
            sess["last_checkpoint"] = {"at": self.clock(), "head": facts.get("head"), "footprints": fps, "tests": tests_digest}
            sess["calls_since_checkpoint"] = 0
            sess["files_since_checkpoint"] = 0
            self.persist(key)
        return result

    async def send_or_spool(
        self, sess: Mapping[str, Any], kind: str, method: str, path: str, body: Mapping[str, Any], *, idem: str
    ) -> dict[str, Any]:
        token = self.tokens.get(str(sess["key"]))
        if token is None:
            return {"ok": False, "error": "no_token"}
        try:
            resp = await self.request(self.api_for(sess), method, path, json_body=dict(body), session_token=token, idem=idem)
        except Unreachable:
            O.spool(
                self.layout.outbox,
                kind,
                {"method": method, "path": path, "json": dict(body)},
                session_key=str(sess["key"]),
                crew_id=str(sess["crew_id"]),
                idem=idem,
            )
            return {"ok": True, "spooled": True}
        if resp.status >= 500 or resp.status == 429:
            O.spool(
                self.layout.outbox,
                kind,
                {"method": method, "path": path, "json": dict(body)},
                session_key=str(sess["key"]),
                crew_id=str(sess["crew_id"]),
                idem=idem,
            )
            return {"ok": True, "spooled": True, "status": resp.status}
        return {"ok": resp.ok, "status": resp.status, "body": resp.body, "error": None if resp.ok else resp.error()}

    # -- claims ----------------------------------------------------------------------------------
    async def claim(self, peer: Peer | None, args: Mapping[str, Any]) -> dict[str, Any]:
        """Auto-claim for the gate (row 17, D11): server first (≤700 ms), local arbiter while unreachable."""
        key = str(args.get("key") or "")
        sess = self.sessions.get(key) if peer is None else self.resolve_peer(peer, key)
        if sess is None:
            return {"ok": False, "result": "timeout", "error": "not_your_session"}
        zone_id = args.get("zone_id")
        mode = str(args.get("mode") or "exclusive")
        crew_id = str(sess["crew_id"])
        ref = LocalSessionRef(str(sess["session_id"]), sess.get("agent_pid"), sess.get("toplevel"))
        fail_closed = self._zone_fail_closed(crew_id, zone_id)
        if not self.server_reachable and zone_id:
            decision = self.arbiter.decide(crew_id, str(zone_id), ref, mode=mode, fail_closed=fail_closed)
            if decision.result == "conflict":
                return {
                    "ok": True,
                    "result": "conflict",
                    "winner_session_id": decision.winner_session_id,
                    "source": "local_arbiter",
                }
            self._mark_local_grant(crew_id, str(zone_id), sess, mode)
            return {"ok": True, "result": "granted", "source": "local_arbiter"}
        body: dict[str, Any] = {"mode": mode, "wait": False, "source": str(args.get("source") or "first_write")}
        if zone_id:
            body["zone_id"] = zone_id
        elif args.get("path_glob"):
            body["path_glob"] = args["path_glob"]
        elif args.get("resource"):
            body["resource"] = args["resource"]
        if args.get("task_id"):
            body["task_id"] = args["task_id"]
        token = self.tokens.get(key)
        try:
            resp = await self.request(
                self.api_for(sess),
                "POST",
                f"/crews/{crew_id}/claims",
                json_body=body,
                session_token=token,
                timeout=float(args.get("timeout") or AUTO_CLAIM_TIMEOUT_S),
            )
        except Unreachable:
            if zone_id:
                decision = self.arbiter.decide(crew_id, str(zone_id), ref, mode=mode, fail_closed=fail_closed)
                if decision.result == "conflict":
                    return {
                        "ok": True,
                        "result": "conflict",
                        "winner_session_id": decision.winner_session_id,
                        "source": "local_arbiter",
                    }
            return {"ok": True, "result": "timeout"}
        if resp.status in (200, 201):
            claim = (resp.body or {}).get("claim") or {}
            self.spawn(self.sync_snapshot(crew_id))
            return {"ok": True, "result": "granted", "claim_id": claim.get("id"), "epoch": claim.get("epoch")}
        if resp.status == 429:
            return {"ok": True, "result": "rate_limited"}
        if resp.status in (202, 409, 423):
            detail = resp.detail()
            code = str(detail.get("error") or detail.get("status") or "")
            blockers = detail.get("blockers") or []
            winner = next(
                (b.get("holder_session_id") for b in blockers if isinstance(b, dict) and b.get("holder_session_id")), None
            )
            return {
                "ok": True,
                "result": "cap" if code == "claim_cap" else "conflict",
                "winner_session_id": winner,
                "error": code,
            }
        if resp.status >= 500:
            return {"ok": True, "result": "timeout"}
        return {"ok": False, "result": "conflict", "error": resp.error(), "status": resp.status}

    def _zone_fail_closed(self, crew_id: str, zone_id: Any) -> bool:
        snap = self.snapshots.get(crew_id) or {}
        fail_closed = set((snap.get("settings") or {}).get("fail_closed_zones") or ())
        for z in snap.get("zones") or ():
            if z.get("id") == zone_id:
                return bool(z.get("fail_closed") or z.get("slug") in fail_closed)
        return False

    def _mark_local_grant(self, crew_id: str, zone_id: str, sess: Mapping[str, Any], mode: str) -> None:
        """Put a local-arbiter grant into the local snapshot so every local gate sees it at once."""
        snap = self.snapshots.get(crew_id)
        if snap is None:
            return
        claims = [c for c in snap.get("claims") or () if not (c.get("zone_id") == zone_id and c.get("source") == "local_arbiter")]
        claims.append(
            {
                "id": f"clm_local{_sha(zone_id + str(sess['session_id']), 12)}",
                "zone_id": zone_id,
                "mode": mode,
                "holder_kind": "session",
                "holder_session_id": sess["session_id"],
                "state": "active",
                "source": "local_arbiter",
                "epoch": 1,
                "unconfirmed": True,
                "fenced": False,
                "lease_expires_at": None,
                "version": 1,
            }
        )
        snap = {**snap, "claims": claims}
        snap["hmac"] = S.snapshot_hmac(self.hmac_key, snap)
        self.snapshots[crew_id] = snap
        SN.write_snapshot(self.layout.snapshot_file(crew_id), snap)

    async def on_reconnect(self) -> None:
        """Replay local-arbiter grants (server is the tie-breaker), flush the outbox, heartbeat."""
        for crew_id, zone_id, rec in self.arbiter.all_pending():
            sess = next((s for s in self.crew_sessions(crew_id) if s["session_id"] == rec.get("holder_session_id")), None)
            if sess is None:
                self.arbiter.lost_race(crew_id, zone_id)
                continue
            try:
                resp = await self.request(
                    self.api_for(sess),
                    "POST",
                    f"/crews/{crew_id}/claims",
                    json_body={
                        "zone_id": zone_id,
                        "mode": rec.get("mode") or "exclusive",
                        "wait": False,
                        "source": "local_arbiter",
                    },
                    session_token=self.tokens.get(str(sess["key"])),
                    idem=f"arb{_sha(zone_id + str(rec.get('granted_at')), 20)}",
                )
            except Unreachable:
                return
            if resp.ok:
                claim = (resp.body or {}).get("claim") or {}
                self.arbiter.confirm(crew_id, zone_id, claim_id=claim.get("id"), epoch=claim.get("epoch"))
            elif resp.status in (409, 423, 422):
                self.arbiter.lost_race(crew_id, zone_id)
        await self.flush_outbox()
        await self.heartbeat()

    async def commons(self, peer: Peer | None, args: Mapping[str, Any]) -> dict[str, Any]:
        """Row 12 effects: serialize micro-lease and the ``schema:<db>`` claim for new migrations."""
        key = str(args.get("key") or "")
        sess = self.sessions.get(key) if peer is None else self.resolve_peer(peer, key)
        if sess is None:
            return {"ok": False}
        effects = set(args.get("effects") or ())
        path = args.get("path_rel")
        if "micro_lease" in effects and isinstance(path, str) and S.is_path_rel(path):
            await self.claim(None, {"key": key, "path_glob": path, "mode": "exclusive", "source": "micro_lease", "timeout": 3.0})
        if "schema_claim" in effects:
            await self.claim(
                None,
                {
                    "key": key,
                    "resource": f"schema:{S.DEFAULT_SCHEMA_DB}",
                    "mode": "exclusive",
                    "source": "first_write",
                    "timeout": 3.0,
                },
            )
        return {"ok": True}

    # -- outbox ------------------------------------------------------------------------------
    async def flush_outbox(self) -> O.FlushResult:
        return await O.flush(self.layout.outbox, self._send_entry, now=self.clock())

    async def _send_entry(self, entry: O.Entry) -> str:
        sess = self.sessions.get(entry.session_key or "")
        if sess is None:
            return "drop" if entry.kind in ("event", "claim", "activity", "checkpoint") else "retry"
        if entry.kind == "activity":
            await self.activity(None, entry.body)
            return "done"
        if entry.kind == "event":
            ev = {
                "id": entry.idem,
                "type": entry.body.get("type"),
                "age_s": max(0, min(86400, int(self.clock() - float(entry.body.get("at") or entry.created_at)))),
                "payload": outbound(
                    "event", entry.body.get("payload") or {}, repo_root=sess.get("toplevel"), home=str(self.layout.home)
                ),
            }
            if S.validate_client_event(ev):
                return "drop"
            try:
                resp = await self.request(
                    self.api_for(sess),
                    "POST",
                    f"/crews/{sess['crew_id']}/events",
                    json_body={"events": [ev]},
                    session_token=self.tokens.get(str(sess["key"])),
                    idem=entry.idem,
                )
            except Unreachable:
                return "retry"
            if resp.ok or resp.status == 409:
                return "done"
            return "drop" if resp.status in (400, 401, 403, 413, 422) else "retry"
        if entry.kind == "claim":
            res = await self.claim(None, {"key": entry.session_key, **entry.body, "timeout": 5.0})
            if res.get("result") in ("granted", "conflict", "cap"):
                return "done"  # confirmed, or lost: the next snapshot shows the winner and the gate denies
            return "retry"
        if entry.kind == "checkpoint" and "path" not in entry.body:
            res = await self.checkpoint(sess, str(entry.body.get("trigger") or "turn"))
            return "done" if res.get("ok") and not res.get("spooled") else "retry"
        if entry.kind in ("leave", "stall") and entry.body.get("op"):
            args = {"key": entry.session_key, **(entry.body.get("args") or {})}
            res = await (self.end(None, args, grace=False) if entry.kind == "leave" else self.stall(None, args))
            if res.get("error") in ("session_ended", "not_your_session"):
                return "drop"
            return "done" if res.get("ok") and not res.get("spooled") else "retry"
        method, path, body = entry.body.get("method"), entry.body.get("path"), entry.body.get("json")
        if not method or not path:
            return "drop"
        try:
            resp = await self.request(
                self.api_for(sess),
                str(method),
                str(path),
                json_body=body,
                session_token=self.tokens.get(str(sess["key"])),
                idem=entry.idem,
            )
        except Unreachable:
            return "retry"
        if resp.ok or resp.status == 409:
            return "done"
        return "drop" if 400 <= resp.status < 500 and resp.status != 429 else "retry"

    async def events(self, peer: Peer | None, args: Mapping[str, Any]) -> dict[str, Any]:
        key = str(args.get("key") or "")
        sess = self.sessions.get(key) if peer is None else self.resolve_peer(peer, key)
        if sess is None:
            return {"ok": False, "error": "not_your_session"}
        for ev in args.get("events") or ():
            if isinstance(ev, dict) and ev.get("type") in S.CLIENT_EVENT_TYPES:
                O.spool(
                    self.layout.outbox,
                    "event",
                    {"type": ev["type"], "payload": ev.get("payload") or {}, "at": args.get("at") or self.clock()},
                    session_key=key,
                    crew_id=str(sess["crew_id"]),
                )
        self.spawn(self.flush_outbox())
        return {"ok": True}

    # -- baton refs -----------------------------------------------------------------------------
    def baton_label(self, sess: Mapping[str, Any]) -> str:
        return str(sess.get("task_ref") or sess["session_id"])

    async def make_baton(self, sess: Mapping[str, Any]) -> B.BatonRef | None:
        top = str(sess["toplevel"])
        try:
            baton = await asyncio.to_thread(B.create_baton_ref, top, self.baton_label(sess))
        except (B.GitError, ValueError) as e:
            log.warning("baton ref failed for %s: %s", sess.get("callsign"), e)
            return None
        if baton is None:
            return None
        push = str((self.snapshots.get(str(sess["crew_id"])) or {}).get("settings", {}).get("baton_push") or "auto")
        settings = (self.crew_settings.get(str(sess["crew_id"])) or (0, 0, {}))[2]
        push = str(settings.get("baton_push") or push)
        shares_store = any(
            s is not sess and s.get("git_common_dir") == sess.get("git_common_dir") and not s.get("ended")
            for s in self.sessions.values()
        )
        remote = sess.get("remote")
        if remote and (push == "always" or (push == "auto" and not shares_store)):
            await asyncio.to_thread(B.push_baton, top, baton, str(remote))
        self.batons[baton.ref] = {
            "crew_id": sess["crew_id"],
            "toplevel": top,
            "remote": remote if baton.pushed else None,
            "created_at": self.clock(),
            "closed_at": None,
            "reported": True,
        }
        self.save_batons()
        # baton.ref_created is server-emitted (§4.2): the stall / leave call that carries baton_ref records it
        return baton

    def _spool_fast(self, kind: str, sess: Mapping[str, Any], args: Mapping[str, Any]) -> Path:
        """The outbox fast-path record of a stall or SessionEnd (replayed if crewd dies mid-way)."""
        key = str(sess["key"])
        return O.spool(
            self.layout.outbox,
            kind,
            {"op": "end" if kind == "leave" else "stall", "args": dict(args)},
            session_key=key,
            crew_id=str(sess["crew_id"]),
            name=f"{kind}-{key}",
        )

    @staticmethod
    def _baton_from_args(sess: Mapping[str, Any], args: Mapping[str, Any]) -> B.BatonRef | None:
        raw = args.get("baton")
        if not isinstance(raw, dict) or not raw.get("ref"):
            return None
        if B.try_out(["rev-parse", "--verify", "--quiet", f"{raw['ref']}^{{commit}}"], str(sess["toplevel"])) is None:
            return None
        return B.BatonRef(
            ref=str(raw["ref"]),
            commit=str(raw.get("commit") or ""),
            parent=str(raw.get("parent") or ""),
            branch=raw.get("branch"),
            label=str(raw.get("label") or ""),
            seq=int(raw.get("seq") or 0),
            dirty_files=list(raw.get("dirty_files") or []),
            skipped=list(raw.get("skipped") or []),
            unpushed=list(raw.get("unpushed") or []),
            pushed=bool(raw.get("pushed")),
        )

    # -- stall / end / orphan --------------------------------------------------------------------
    async def stall(self, peer: Peer | None, args: Mapping[str, Any]) -> dict[str, Any]:
        """StopFailure or a detected limit: baton ref first (D30), then ``POST /sessions/{sid}/stall``."""
        key = str(args.get("key") or "")
        sess = self.sessions.get(key) if peer is None else self.resolve_peer(peer, key)
        if sess is None:
            return {"ok": False, "error": "not_your_session"}
        error = str(args.get("error") or "unknown")
        if error not in (*S.STOPFAILURE_ERRORS, "detected_limit"):
            error = "unknown"
        last_message = str(args.get("last_assistant_message") or "")[:2000]
        kind = classify_stall(error, last_message)
        async with self.lock(key):
            if sess.get("ended"):
                return {"ok": False, "error": "session_ended"}
            fast_args: dict[str, Any] = {"error": error, "last_assistant_message": last_message}
            fast = self._spool_fast("stall", sess, fast_args)
            baton = None
            if kind in ("quota", "auth"):
                baton = self._baton_from_args(sess, args) or await self.make_baton(sess)
                if baton is not None:  # a replay of this stall reuses the same baton ref
                    fast = self._spool_fast("stall", sess, {**fast_args, "baton": baton.as_dict()})
            facts = await asyncio.to_thread(self.closing_facts, sess)
            if baton is not None:
                facts["baton_ref"] = baton.ref
                facts["uncommitted_files"] = baton.dirty_files[:50]
            body: dict[str, Any] = {
                "error": error,
                "facts": outbound("stall", facts, repo_root=sess.get("toplevel"), home=str(self.layout.home)),
            }
            if baton is not None:
                body["baton_ref"] = baton.ref
            if last_message:
                body["last_assistant_message"] = outbound("stall", last_message, home=str(self.layout.home))
            try:
                resp = await self.request(
                    self.api_for(sess),
                    "POST",
                    f"/sessions/{sess['session_id']}/stall",
                    json_body=body,
                    session_token=self.tokens.get(key),
                    idem=f"stall{_sha(key + error + str(sess.get('cursor')), 20)}",
                )
            except Unreachable:
                return {"ok": True, "spooled": True, "kind": kind, "baton_ref": baton.ref if baton else None}
            with contextlib.suppress(OSError):
                fast.unlink()
            if kind in ("quota", "auth"):
                sess["state"] = "quota_blocked"
                sess["limit"] = {"level": "exhausted", "source": "detected" if error == "detected_limit" else "reported"}
            self.persist(key)
            self.spawn(self.sync_snapshot(str(sess["crew_id"])))
            return {
                "ok": resp.ok,
                "status": resp.status,
                "kind": kind,
                "baton_ref": baton.ref if baton else None,
                "body": resp.body,
            }

    async def end(self, peer: Peer | None, args: Mapping[str, Any], *, grace: bool = True) -> dict[str, Any]:
        """SessionEnd (§8.2): relay close, baton ref if dirty, ``leave`` (reserve or release), mark ended."""
        key = str(args.get("key") or "")
        sess = self.sessions.get(key) if peer is None else self.resolve_peer(peer, key)
        if sess is None:
            return {"ok": False, "error": "not_your_session"}
        reason = str(args.get("reason") or "other")[:64]
        if grace:
            await asyncio.sleep(END_GRACE_S)  # a StopFailure fired with this SessionEnd goes first (S0: order not stable)
        async with self.lock(key):
            if sess.get("ended"):
                return {"ok": True, "already": True}
            fast = self._spool_fast("leave", sess, {"reason": reason})
            top = str(sess["toplevel"])
            try:
                dirty = not await asyncio.to_thread(B.is_clean, top)
            except B.GitError:
                dirty = False
            baton = self._baton_from_args(sess, args)
            if baton is None and (dirty or reason == "clear"):
                baton = await self.make_baton(sess)
                if baton is not None:  # a replay of this SessionEnd reuses the same baton ref
                    fast = self._spool_fast("leave", sess, {"reason": reason, "baton": baton.as_dict()})
            await self.relay_close(sess, reason, args.get("transcript_path"))
            facts = await asyncio.to_thread(self.closing_facts, sess)
            if baton is not None:
                facts["baton_ref"] = baton.ref
                facts["uncommitted_files"] = baton.dirty_files[:50]  # as a stall does: the partial report lists them
            body: dict[str, Any] = {
                "reason": reason,
                "facts": outbound("checkpoint", facts, repo_root=top, home=str(self.layout.home)),
                "baton": bool(baton is not None or reason == "clear"),
            }
            if baton is not None:
                body["baton_ref"] = baton.ref
            try:
                resp = await self.request(
                    self.api_for(sess),
                    "POST",
                    f"/sessions/{sess['session_id']}/leave",
                    json_body=body,
                    session_token=self.tokens.get(key),
                    idem=f"leave{_sha(key, 20)}",
                )
            except Unreachable:
                return {"ok": True, "spooled": True}
            with contextlib.suppress(OSError):
                fast.unlink()
            self.mark_ended(key, reason)
            return {"ok": resp.ok or resp.status in (401, 409), "status": resp.status, "baton_ref": baton.ref if baton else None}

    async def relay_close(self, sess: Mapping[str, Any], reason: str, transcript_path: Any) -> None:
        """Step 1 of SessionEnd: the existing relay handoff (the crew side is ``leave``, which carries the baton ref)."""
        from remembra.relay import facts as factlib

        top = Path(str(sess["toplevel"]))

        def gather() -> dict[str, Any]:
            deadline = factlib.Deadline(4.0)
            transcript = None
            if transcript_path and sess.get("adapter") == "claude-code" and Path(str(transcript_path)).is_file():
                with contextlib.suppress(Exception):
                    transcript = factlib.parse_claude_transcript(Path(str(transcript_path)), factlib.Deadline(2.0), root=str(top))
            git_facts = factlib.git_facts(
                top, deadline, start_head=sess.get("started_head"), started_at=float(sess.get("joined_at") or 0)
            )
            merged = factlib.merge_facts(git_facts, transcript, str(top))
            merged["facts_source"] = "relay-cli:git+transcript" if transcript is not None else "relay-cli:git"
            return merged

        try:
            facts = await asyncio.to_thread(gather)
        except Exception as e:
            log.warning("relay facts failed: %s", e)
            facts = {"facts_source": "relay-cli:git"}
        body = {
            "agent_id": sess["agent_id"],
            "session_id": sess["client_session_id"],
            "facts": facts,
            "project_id": sess["project_id"],
            "end_reason": reason,
        }
        if nothing_to_hand_off(body):
            # The relay's order (0.16.1): an empty close is not sent. This is the crew session's one relay
            # close, so nothing of it is on the server to retire. The crew side (``leave``) still runs.
            log.info("relay close skipped: nothing to hand off for %s", str(sess["client_session_id"])[:40])
            return
        with contextlib.suppress(Unreachable):
            await self.request(self.api_for(sess), "POST", "/session/close", json_body=body, timeout=8.0)

    def mark_ended(self, key: str, reason: str) -> None:
        sess = self.sessions.get(key)
        if sess is None:
            return
        sess["ended"] = True
        sess["end_reason"] = reason
        self.persist(key)
        with contextlib.suppress(OSError):
            self.fence.restore(str(sess["toplevel"]))
        self.arbiter.release_session(str(sess["crew_id"]), str(sess["session_id"]))
        self._drop_token(key)
        self.sessions.pop(key, None)
        self.pending_footprints.pop(key, None)
        self.tool_windows.pop(key, None)

    async def liveness(self) -> list[str]:
        """pid liveness every 30 s: a dead agent with no ended marker is an orphan (§8.1)."""
        orphans: list[str] = []
        for key, sess in list(self.sessions.items()):
            if sess.get("ended") or self.is_alive(sess.get("agent_pid")):
                continue
            orphans.append(key)
            async with self.lock(key):
                if sess.get("ended"):
                    continue
                self.arbiter.mark_dead(str(sess["crew_id"]), str(sess["session_id"]))
                baton = None
                try:
                    if not await asyncio.to_thread(B.is_clean, str(sess["toplevel"])):
                        baton = await self.make_baton(sess)
                except B.GitError:
                    pass
                await self.relay_close(sess, "orphaned", sess.get("transcript_path"))
                facts = await asyncio.to_thread(self.closing_facts, sess)
                body: dict[str, Any] = {
                    "reason": "process_exited",
                    "facts": outbound("checkpoint", facts, repo_root=sess.get("toplevel"), home=str(self.layout.home)),
                    "baton": baton is not None,
                }
                if baton is not None:
                    body["baton_ref"] = baton.ref
                try:
                    await self.request(
                        self.api_for(sess),
                        "POST",
                        f"/sessions/{sess['session_id']}/leave",
                        json_body=body,
                        session_token=self.tokens.get(key),
                        idem=f"orphan{_sha(key, 20)}",
                    )
                except Unreachable:
                    O.spool(
                        self.layout.outbox,
                        "leave",
                        {"method": "POST", "path": f"/sessions/{sess['session_id']}/leave", "json": body},
                        session_key=key,
                        crew_id=str(sess["crew_id"]),
                        idem=f"orphan{_sha(key, 20)}",
                    )
                    continue
                self.mark_ended(key, "process_exited")
        return orphans

    # -- detector ---------------------------------------------------------------------------------
    async def detect(self) -> list[str]:
        hits: list[str] = []
        for key, sess in list(self.sessions.items()):
            if sess.get("ended"):
                continue
            path = sess.get("transcript_path")
            if not path:
                newest = D.newest_file(
                    D.default_transcript_dirs(self.layout.home, str(sess.get("adapter"))), since=float(sess.get("joined_at") or 0)
                )
                if newest is None:
                    continue
                path = str(newest)
            state = self.detector_states.setdefault(key, D.TailState())
            state = await asyncio.to_thread(D.scan, Path(str(path)), str(sess.get("adapter")), state, now=self.clock())
            self.detector_states[key] = state
            verdict = D.evaluate(
                state,
                now=self.clock(),
                process_alive=self.is_alive(sess.get("agent_pid")),
                last_hook_activity_at=float(sess.get("last_activity_at") or 0) if sess.get("last_action") else None,
            )
            if verdict.detected:
                state.reported = True
                hits.append(key)
                await self.stall(None, {"key": key, "error": "detected_limit"})
            self.persist(key)
        return hits

    # -- gate integrity, git hooks, zones, tree ------------------------------------------------------
    def gate_expected(self) -> bool:
        """``remembra-crew connect`` recorded agent hooks that run the gate (``install.json``).

        A deleted gate is then restored like a modified one: the agent hooks still point at it. After a
        full uninstall the manifest has no agents and a missing gate stays missing.
        """
        data = read_json(self.layout.root / "install.json") or {}
        agents = data.get("agents")
        return isinstance(agents, dict) and any(agents.values())

    def check_gate(self) -> dict[str, Any]:
        check = verify_gate(self.layout)
        restored = False
        tampered = (check.installed and not check.ok) or (not check.installed and self.gate_expected())
        if tampered and self.restore_gate:
            argv = (read_json(self.layout.crewd_cmd) or {}).get("argv")
            vendor_gate(self.layout, crewd_argv=argv if isinstance(argv, list) else None)
            restored = True
            self.events_log.append(
                {"type": "gate.tampered", "expected": check.expected_sha, "actual": check.actual_sha, "at": self.clock()}
            )
            log.warning(
                "gate.tampered: restored crew-gate.py (expected %s, found %s)", check.expected_sha[:12], check.actual_sha[:12]
            )
        for crew_id, snap in list(self.snapshots.items()):
            on_disk = SN.load_snapshot(self.layout.snapshot_file(crew_id))
            if on_disk is None or not SN.verify(on_disk, self.hmac_key) or on_disk.get("hmac") != snap.get("hmac"):
                SN.write_snapshot(self.layout.snapshot_file(crew_id), snap)
                self.events_log.append({"type": "snapshot.tampered", "crew_id": crew_id, "at": self.clock()})
        return {
            "ok": check.ok,
            "installed": check.installed,
            "restored": restored,
            "expected": check.expected_sha,
            "actual": check.actual_sha,
        }

    def check_githooks(self) -> None:
        for sess in list(self.sessions.values()):
            state, missing = githook_state_repaired(self.layout.home, str(sess["toplevel"]))
            prev = sess.get("githook_state")
            if state != prev:
                sess["githook_state"] = state
                self.persist(str(sess["key"]))
                if state == "missing":
                    for hook in missing:
                        self.submit_event(
                            sess, "githook.missing", {"hook": hook, "state": "missing", "worktree_id": sess.get("worktree_id")}
                        )

    async def upload_zones(self, sess: Mapping[str, Any], plan: ZC.UploadPlan) -> dict[str, Any]:
        if plan.file is None or plan.result is None:
            return {"ok": False, "error": plan.reason}
        crew_id = str(sess["crew_id"])
        ZC.write_compiled(self.layout.snapshots, crew_id, plan.file, plan.result)
        if not plan.upload:
            return {"ok": True, "uploaded": False, "reason": plan.reason}
        resp = await self.request(
            self.api_for(sess),
            "PUT",
            f"/crews/{crew_id}/zones/file",
            json_body=ZC.upload_body(plan),
            session_token=self.tokens.get(str(sess["key"])),
            idem=f"zones{plan.result.sha[:40]}",
        )
        if resp.ok:
            self.zones_uploaded[crew_id] = plan.result.sha
            write_json(self.layout.run / "zones.json", {"uploaded": self.zones_uploaded})
        return {
            "ok": resp.ok,
            "uploaded": resp.ok,
            "status": resp.status,
            "result": (resp.body or {}).get("result") if isinstance(resp.body, dict) else None,
        }

    async def check_zones(self) -> None:
        seen: set[str] = set()
        for sess in list(self.sessions.values()):
            crew_id = str(sess["crew_id"])
            if crew_id in seen or sess.get("ended"):
                continue
            seen.add(crew_id)
            plan = await asyncio.to_thread(
                ZC.plan_upload, str(sess["toplevel"]), sess.get("default_branch"), self.zones_uploaded.get(crew_id)
            )
            if plan.upload or (plan.result is not None and not plan.result.ok):
                with contextlib.suppress(Unreachable):
                    await self.upload_zones(sess, plan)

    async def maybe_send_tree(self, sess: Mapping[str, Any]) -> bool:
        crew_id = str(sess["crew_id"])
        if self.clock() - float(self.tree_sent.get(crew_id) or 0) < TREE_EVERY_S:
            return False
        text = B.try_out(["ls-files", "-z"], str(sess["toplevel"])) or ""
        tree = build_tree([f for f in text.split("\0") if f])
        if S.validate_tree(tree):
            return False
        try:
            resp = await self.request(
                self.api_for(sess),
                "PUT",
                f"/crews/{crew_id}/tree",
                json_body={"tree": tree},
                session_token=self.tokens.get(str(sess["key"])),
            )
        except Unreachable:
            return False
        if resp.ok:
            self.tree_sent[crew_id] = self.clock()
            write_json(self.layout.run / "tree.json", {"sent": self.tree_sent})
        return resp.ok

    def sweep_batons(self) -> list[str]:
        gone: list[str] = []
        for ref in B.expired(self.batons, self.clock()):
            rec = self.batons.pop(ref)
            with contextlib.suppress(B.GitError):
                B.delete_baton(str(rec.get("toplevel")), ref, remote=rec.get("remote"))
            gone.append(ref)
        if gone:
            self.save_batons()
        return gone

    # -- presence ----------------------------------------------------------------------------------
    def presence_frames(self, force: bool = False) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """``(session, frame)`` for sessions with new activity (≤1 per session per 5 s)."""
        out: list[tuple[dict[str, Any], dict[str, Any]]] = []
        by_crew: dict[str, list[dict[str, Any]]] = {}
        now = self.clock()
        for key in sorted(self.presence_dirty):
            sess = self.sessions.get(key)
            if sess is None or sess.get("ended"):
                continue
            if not force and now - self.presence_sent.get(key, 0.0) < PRESENCE_S:
                continue
            lane: dict[str, Any] = {
                "session_id": sess["session_id"],
                "state": "active",
                "stuck": False,
                "calls_since_checkpoint": int(sess.get("calls_since_checkpoint") or 0),
            }
            la = self._last_action(sess)
            if la:
                lane["last_action"] = la
            if sess.get("limit"):
                lane["limit"] = sess["limit"]
            lane = outbound("presence", lane, repo_root=sess.get("toplevel"), home=str(self.layout.home))
            by_crew.setdefault(str(sess["crew_id"]), []).append(lane)
            self.presence_sent[key] = now
            self.presence_dirty.discard(key)
        for crew_id, lanes in by_crew.items():
            sess = next(s for s in self.crew_sessions(crew_id))
            out.append((sess, {"type": "presence", "crew_id": crew_id, "lanes": lanes}))
        return out

    async def send_presence(self) -> int:
        if self.ws_connect is None:
            return 0
        sent = 0
        for sess, frame in self.presence_frames():
            if S.validate_ws_frame(frame):
                continue
            cfg = self.cfg_for(sess)
            link_key = self.host_fp(cfg)
            link = self.ws_links.get(link_key)
            try:
                if link is None:
                    url = cfg.url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"
                    headers = {"X-API-Key": cfg.api_key or ""}
                    link = await self.ws_connect(url, headers)
                    self.ws_links[link_key] = link
                await link.send(json.dumps(frame, separators=(",", ":")))
                sent += 1
            except Exception as e:
                log.info("presence link dropped: %s", e.__class__.__name__)
                self.ws_links.pop(link_key, None)
                with contextlib.suppress(Exception):
                    if link is not None:
                        await link.close()
        return sent

    # -- CLI ops ---------------------------------------------------------------------------------------
    async def whoami(self, peer: Peer) -> dict[str, Any]:
        sess = self.resolve_peer(peer)
        if sess is None:
            return {"ok": False, "error": "no_session"}
        return {
            "ok": True,
            "key": sess["key"],
            "session_id": sess["session_id"],
            "crew_id": sess["crew_id"],
            "callsign": sess.get("callsign"),
            "member_key": sess.get("member_key"),
            "adapter": sess.get("adapter"),
            "client_session_id": sess.get("client_session_id"),
            "task_ref": sess.get("task_ref"),
            "toplevel": sess.get("toplevel"),
            "project_id": sess.get("project_id"),
        }

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": CREWD_VERSION,
            "pid": os.getpid(),
            "server_reachable": self.server_reachable,
            "server_outage": self.server_outage,
            "sessions": [
                {
                    "callsign": s.get("callsign"),
                    "session_id": s.get("session_id"),
                    "crew_id": s.get("crew_id"),
                    "project_id": s.get("project_id"),
                    "adapter": s.get("adapter"),
                    "toplevel": s.get("toplevel"),
                    "state": s.get("state"),
                    "task_ref": s.get("task_ref"),
                    "enforcement": s.get("enforcement"),
                    "githook_state": s.get("githook_state"),
                }
                for s in self.sessions.values()
            ],
            "crews": sorted(self.snapshots),
        }

    async def find_task(self, sess: Mapping[str, Any], ref: str) -> dict[str, Any]:
        m = re.fullmatch(r"T-?(\d+)", ref.strip(), re.IGNORECASE)
        resp = await self.request(
            self.api_for(sess), "GET", f"/crews/{sess['crew_id']}/tasks", session_token=self.tokens.get(str(sess["key"]))
        )
        if not resp.ok:
            raise CrewdError("tasks_unavailable", resp.error())
        tasks = (resp.body or {}).get("tasks") or []
        for t in tasks:
            if (m and int(t.get("number") or 0) == int(m.group(1))) or t.get("id") == ref:
                return dict(t)
        raise CrewdError("task_not_found", f"no task {ref} in this crew")

    async def adopt(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        """``remembra-crew adopt T-n``: server adopt (D33 authorisation), then restore the baton ref (D30)."""
        sess = self.require(peer, args.get("key"))
        ref_arg = str(args.get("task") or "")
        task = await self.find_task(sess, ref_arg)
        key = str(sess["key"])
        baton_ref = None
        baton_id: str | None = None
        adopted: dict[str, Any] = {}
        async with self.lock(key):
            if not args.get("restore_only"):
                resp = await self.request(
                    self.api_for(sess), "POST", f"/tasks/{task['id']}/adopt", json_body={}, session_token=self.tokens.get(key)
                )
                if not resp.ok:
                    return {"ok": False, "status": resp.status, "error": resp.error(), "message": resp.detail().get("message")}
                adopted = resp.body or {}
                baton = (adopted.get("baton") or {}) if isinstance(adopted, dict) else {}
                baton_ref = baton.get("baton_ref")
                baton_id = baton.get("baton_id") if baton_ref else None
            if not baton_ref:
                resp = await self.request(
                    self.api_for(sess), "GET", f"/crews/{sess['crew_id']}/batons", params={"task_id": task["id"]}
                )
                if resp.ok:
                    items = (resp.body or {}).get("batons") or (resp.body or {}).get("items") or []
                    # newest first: the latest pass to this session that carries a ref
                    mine = [
                        b
                        for b in items
                        if isinstance(b, dict) and b.get("baton_ref") and b.get("to_session") in (None, sess.get("session_id"))
                    ]
                    if mine:
                        baton_ref, baton_id = mine[0].get("baton_ref"), mine[0].get("id")
            restore: dict[str, Any] | None = None
            if baton_ref:
                label = f"T-{task.get('number')}"
                retry = f"remembra-crew adopt {label} --restore-only"
                # The adopter now holds the task's zones: lift this checkout's read-only fence before the
                # restore writes into them (the next snapshot sync re-fences whatever still applies).
                self.lift_fence(str(sess["toplevel"]), why=f"{sess.get('callsign')} adopted {label}")
                try:
                    result = await asyncio.to_thread(
                        B.restore_baton, str(sess["toplevel"]), str(baton_ref), remote=sess.get("remote"), retry_command=retry
                    )
                except B.GitError as e:  # git itself failed before anything was changed (a timeout, git missing)
                    result = B.RestoreResult(False, "restore_failed", str(baton_ref), next_command=retry, error=str(e)[:300])
                restore = result.as_dict()
                if baton_id:
                    restore["reported"] = await self.report_restore(sess, str(baton_id), result)
                if result.restored:
                    rec = self.batons.setdefault(str(baton_ref), {"crew_id": sess["crew_id"], "toplevel": str(sess["toplevel"])})
                    rec["closed_at"] = self.clock()
                    self.save_batons()
            sess["current_task_id"] = task["id"]
            sess["task_ref"] = f"T-{task.get('number')}"
            self.persist(key)
        self.spawn(self.sync_snapshot(str(sess["crew_id"])))
        return {
            "ok": True,
            "task_ref": f"T-{task.get('number')}",
            "task_id": task["id"],
            "baton_ref": baton_ref,
            "restore": restore,
            "claims": [c.get("id") for c in (adopted.get("claims") or []) if isinstance(c, dict)]
            if isinstance(adopted, dict)
            else [],
        }

    async def report_restore(self, sess: Mapping[str, Any], baton_id: str, result: B.RestoreResult) -> bool:
        """Tell the server how the baton restore went (``baton.restored``, ``crew_batons.restored``); a
        failed restore becomes a Needs-you item there. Spooled for the outbox when the server is away."""
        key = str(sess["key"])
        body = {"restored": bool(result.restored), "status": result.reason, "files": len(result.files or [])}
        path = f"/crews/{sess['crew_id']}/batons/{baton_id}/restore"
        try:
            resp = await self.request(self.api_for(sess), "POST", path, json_body=body, session_token=self.tokens.get(key))
        except Unreachable:
            resp = None
        if resp is not None and (resp.ok or resp.status in (404, 422)):
            return bool(resp.ok)
        with contextlib.suppress(OSError, ValueError):
            self.spool_restore(sess, path, body)
        return False

    def spool_restore(self, sess: Mapping[str, Any], path: str, body: Mapping[str, Any]) -> None:
        O.spool(
            self.layout.outbox,
            "baton_restore",
            {"method": "POST", "path": path, "json": dict(body)},
            session_key=str(sess["key"]),
            crew_id=str(sess["crew_id"]),
        )

    async def api_op(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        """A session-authenticated server call for the CLI (claim, release, task, report, say, checkpoint)."""
        sess = self.require(peer, args.get("key"))
        method = str(args.get("method") or "GET").upper()
        path = str(args.get("path") or "")
        if not path.startswith("/") or ".." in path:
            raise CrewdError("bad_path", "invalid path")
        allowed = (
            f"/crews/{sess['crew_id']}/",
            "/claims/",
            "/tasks/",
            "/messages/",
            "/inbox/items/",
            "/collisions/",
        )
        if not path.startswith(allowed):
            raise CrewdError("forbidden_path", "the CLI may only call this crew's routes")
        try:
            resp = await self.request(
                self.api_for(sess),
                method,
                path,
                json_body=args.get("json"),
                params=args.get("params"),
                session_token=self.tokens.get(str(sess["key"])),
                idem=args.get("idem"),
                if_match=args.get("if_match"),
                timeout=float(args.get("timeout") or 15.0),
            )
        except Unreachable:
            return {"ok": False, "status": 0, "error": "unreachable"}
        if method != "GET" and resp.ok:
            self.spawn(self.sync_snapshot(str(sess["crew_id"])))
        return {"ok": resp.ok, "status": resp.status, "body": resp.body, "error": None if resp.ok else resp.error()}

    async def report(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        """``remembra-crew report T-n``: observed commits and test verdicts plus the agent's sections (§5.6)."""
        sess = self.require(peer, args.get("key"))
        task = await self.find_task(sess, str(args.get("task") or ""))
        top = str(sess["toplevel"])
        commits: list[str] = []
        started = task.get("started_head")
        if started:
            commits = (B.try_out(["rev-list", "--max-count=200", f"{started}..HEAD"], top) or "").split()
        tests = [
            {"command": fp, "passed": int(v.get("passed") or 0), "failed": int(v.get("failed") or 0)}
            for fp, v in (sess.get("tests") or {}).items()
        ][:50]
        sections = {k: [str(x)[:500] for x in (args.get("sections") or {}).get(k, [])][:50] for k in S.MCP_REPORT_SECTIONS}
        body: dict[str, Any] = {
            "session_id": sess["session_id"],
            "sections": {k: v for k, v in sections.items() if v},
            "criteria_evidence": [],
            "commits": commits,
            "tests": tests,
            "release": bool(args.get("release", True)),
        }
        if args.get("summary"):
            body["summary"] = str(args["summary"])[:2000]
        body = outbound("report", body, repo_root=top, home=str(self.layout.home))
        return await self.api_op(
            peer,
            {
                "key": sess["key"],
                "method": "POST",
                "path": f"/tasks/{task['id']}/reports",
                "json": body,
                "idem": f"rep{_sha(json.dumps(body, sort_keys=True), 24)}",
            },
        )

    async def bypass(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        """D34: a human at an interactive TTY, outside every session's process tree, names the session."""
        if peer.pid is None or not has_controlling_tty(peer.pid):
            raise CrewdError("tty_required", "the bypass is answered by a human at an interactive terminal")
        if self.resolve_peer(peer) is not None:
            raise CrewdError("agent_process", "an agent session cannot grant itself a bypass")
        target = str(args.get("session") or "")
        sess = next((s for s in self.sessions.values() if target in (s.get("callsign"), s.get("session_id"), s.get("key"))), None)
        if sess is None:
            raise CrewdError("unknown_session", f"no live session {target}")
        code = str(args.get("code") or "")
        minutes = max(1, min(S.BYPASS_CODE_MAX_MINUTES, int(args.get("minutes") or S.BYPASS_CODE_MAX_MINUTES)))
        if code:
            resp = await self.request(
                self.api_for(sess),
                "POST",
                "/bypass-codes/redeem",
                json_body={"code": code, "session_id": sess["session_id"], "surface": "tty"},
                session_token=self.tokens.get(str(sess["key"])),
            )
            if not resp.ok:
                return {"ok": False, "status": resp.status, "error": resp.error()}
            body = resp.body or {}
            # the code's own scope and remaining lifetime, never a wider local grant
            left = minutes * 60
            with contextlib.suppress(TypeError, ValueError):
                expires = datetime.fromisoformat(str(body.get("expires_at")).replace("Z", "+00:00"))
                left = int(min(left, max(0.0, expires.timestamp() - time.time())))
            grant: dict[str, Any] = {
                "scope": str(body.get("scope") or "none"),
                "expires_at": self.clock() + left,
                "used": False,
                "via": "code",
                "code_id": body.get("code_id"),
            }
        else:
            if self.server_reachable:
                raise CrewdError("code_required", "the server is reachable: ask for a bypass code from the dashboard")
            grant = {"scope": "all", "expires_at": self.clock() + minutes * 60, "used": False, "via": "offline_tty"}
        sess["bypass"] = grant
        self.persist(str(sess["key"]))
        expires_at = float(grant["expires_at"])
        self.events_log.append(
            {"type": "bypass.granted", "session": sess.get("callsign"), "via": grant["via"], "at": self.clock()}
        )
        self._audit_local({"action": "bypass_granted", "session_id": sess["session_id"], "via": grant["via"]})
        return {
            "ok": True,
            "callsign": sess.get("callsign"),
            "expires_in_s": int(expires_at - self.clock()),
            "via": grant["via"],
            "scope": grant["scope"],
        }

    async def bypass_redeem(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        """``REMEMBRA_BYPASS=<code> git …``: the server validates and consumes the human-issued code."""
        sess = self.require(peer, args.get("key"))
        code = str(args.get("code") or "")
        if not re.fullmatch(S.BYPASS_CODE_PATTERN, code):
            return {"ok": False, "error": "bad_code"}
        try:
            surface = str(args.get("surface") or "")
            zone = args.get("zone") if isinstance(args.get("zone"), str) else None
            resp = await self.request(
                self.api_for(sess),
                "POST",
                "/bypass-codes/redeem",
                json_body={"code": code, "session_id": sess["session_id"], "surface": surface, "zone": zone},
                session_token=self.tokens.get(str(sess["key"])),
            )
        except Unreachable:
            return {"ok": False, "error": "unreachable"}
        self._audit_local(
            {"action": "bypass_redeemed", "session_id": sess["session_id"], "ok": resp.ok, "surface": args.get("surface")}
        )
        body = resp.body if resp.ok and isinstance(resp.body, dict) else {}
        return {
            "ok": resp.ok,
            "status": resp.status,
            "error": None if resp.ok else resp.error(),
            "scope": body.get("scope"),
        }

    def bypass_consume(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        sess = self.resolve_peer(peer, str(args.get("key") or ""))
        if sess is None:
            return {"ok": False, "error": "not_your_session"}
        grant = sess.get("bypass")
        if not isinstance(grant, dict) or grant.get("used") or float(grant.get("expires_at") or 0) <= self.clock():
            return {"ok": False, "error": "no_grant"}
        zone = args.get("zone") if isinstance(args.get("zone"), str) else None
        if not S.bypass_scope_matches(str(grant.get("scope") or ""), str(args.get("surface") or ""), zone):
            return {"ok": False, "error": "scope_mismatch", "scope": grant.get("scope")}
        grant["used"] = True
        grant["used_at"] = self.clock()
        grant["surface"] = args.get("surface")
        self.persist(str(sess["key"]))
        self._audit_local(
            {"action": "bypass_used", "session_id": sess["session_id"], "via": grant.get("via"), "surface": args.get("surface")}
        )
        return {"ok": True}

    def _audit_local(self, record: Mapping[str, Any]) -> None:
        path = self.layout.log_dir / "audit.jsonl"
        with contextlib.suppress(OSError):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(json.dumps({**record, "at": self.clock()}, sort_keys=True) + "\n")

    async def zones_op(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "show")
        sess = self.resolve_peer(peer) or next(iter(self.sessions.values()), None)
        if sess is None:
            raise CrewdError("no_session", "no live crew session on this host")
        if action == "push":
            if peer.pid is None or not has_controlling_tty(peer.pid) or self.resolve_peer(peer) is not None:
                raise CrewdError("tty_required", "zones push is for a human at an interactive terminal")
            plan = ZC.plan_upload(
                str(sess["toplevel"]), sess.get("default_branch"), None, human_push=True, branch=sess.get("branch")
            )
            if plan.result is not None and not plan.result.ok:
                return {"ok": False, "errors": plan.result.errors}
            res = await self.upload_zones(sess, plan)
            self._audit_local({"action": "zones_push", "crew_id": sess["crew_id"], "result": res.get("result")})
            return res
        plan = ZC.plan_upload(str(sess["toplevel"]), sess.get("default_branch"), self.zones_uploaded.get(str(sess["crew_id"])))
        snap = self.snapshots.get(str(sess["crew_id"])) or {}
        return {
            "ok": True,
            "file": plan.file.source if plan.file else None,
            "valid": plan.result.ok if plan.result else None,
            "errors": plan.result.errors if plan.result else [],
            "pending_upload": plan.upload,
            "zones": [
                {"slug": z.get("slug"), "mode": z.get("mode"), "include": z.get("include_globs"), "source": z.get("source")}
                for z in snap.get("zones") or ()
            ],
            "bootstrap": bool(snap.get("bootstrap_zones")),
        }

    async def events_page(self, peer: Peer, args: Mapping[str, Any]) -> dict[str, Any]:
        sess = self.resolve_peer(peer) or next(iter(self.sessions.values()), None)
        if sess is None:
            raise CrewdError("no_session", "no live crew session on this host")
        params = {"since_seq": int(args.get("since_seq") or 0), "limit": 200}
        resp = await self.request(self.api_for(sess), "GET", f"/crews/{sess['crew_id']}/events", params=params)
        return {
            "ok": resp.ok,
            "status": resp.status,
            "body": resp.body,
            "crew_id": sess["crew_id"],
            "snapshot": self.snapshots.get(str(sess["crew_id"])),
        }

    def doctor(self) -> dict[str, Any]:
        gate = verify_gate(self.layout)
        pending = O.read_entries(self.layout.outbox)
        return {
            "ok": True,
            "version": CREWD_VERSION,
            "gate": {"installed": gate.installed, "ok": gate.ok},
            "socket": str(self.layout.socket),
            "server_reachable": self.server_reachable,
            "server_outage": self.server_outage,
            "outbox": {"pending": len(pending), "kinds": sorted({e.kind for e in pending})},
            "sessions": [
                {
                    "callsign": s.get("callsign"),
                    "githook_state": s.get("githook_state"),
                    "agent_alive": self.is_alive(s.get("agent_pid")),
                    "fenced": len(self.fence.fenced(str(s["toplevel"]))),
                }
                for s in self.sessions.values()
            ],
            "local_events": self.events_log[-20:],
        }

    # -- dispatch ----------------------------------------------------------------------------------
    async def handle(self, op: str, args: Mapping[str, Any], peer: Peer) -> dict[str, Any]:
        if peer.uid is not None and peer.uid != os.getuid():
            return {"ok": False, "error": "wrong_user"}
        try:
            if op == "ping":
                return {"ok": True, "version": CREWD_VERSION, "pid": os.getpid(), "server_reachable": self.server_reachable}
            if op == "status":
                return self.status()
            if op == "whoami":
                return await self.whoami(peer)
            if op == "join":
                return await self.join(peer, args)
            if op == "crew_exists":
                return await self.crew_exists(args)
            if op == "brief":
                return await self.brief(peer, args)
            if op == "activity":
                return await self.activity(peer, args)
            if op == "events":
                return await self.events(peer, args)
            if op == "claim":
                return await self.claim(peer, args)
            if op == "commons":
                return await self.commons(peer, args)
            if op == "flush":
                self.spawn(self.flush_outbox())
                return {"ok": True}
            if op == "refresh":
                crew_id = str(args.get("crew_id") or "")
                if args.get("sync"):
                    return {"ok": await self.sync_snapshot(crew_id)}
                self.spawn(self.sync_snapshot(crew_id))
                return {"ok": True}
            if op == "checkpoint":
                sess = self.require(peer, args.get("key"))
                return await self.checkpoint(
                    sess, str(args.get("trigger") or "turn"), extra=args.get("extra"), force=bool(args.get("force"))
                )
            if op == "delivered":
                sess = self.require(peer, args.get("key"))
                if (sess.get("inject") or {}).get("id") == args.get("inject_id"):
                    sess["inject_delivered"] = args.get("inject_id")
                    self.persist(str(sess["key"]))
                return {"ok": True}
            if op == "stall":
                return await self.stall(peer, args)
            if op == "end":
                return await self.end(peer, args)
            if op == "adopt":
                return await self.adopt(peer, args)
            if op == "api":
                return await self.api_op(peer, args)
            if op == "report":
                return await self.report(peer, args)
            if op == "renew":
                sess = self.require(peer, args.get("key"))
                results = await self.heartbeat()
                return {
                    "ok": True,
                    "heartbeat": results,
                    "lease_expires_at": self.sessions.get(str(sess["key"]), {}).get("lease_expires_at"),
                }
            if op == "bypass":
                return await self.bypass(peer, args)
            if op == "bypass_redeem":
                return await self.bypass_redeem(peer, args)
            if op == "bypass_consume":
                return self.bypass_consume(peer, args)
            if op == "zones":
                return await self.zones_op(peer, args)
            if op == "events_page":
                return await self.events_page(peer, args)
            if op == "doctor":
                return self.doctor()
            return {"ok": False, "error": "unknown_op"}
        except CrewdError as e:
            return {"ok": False, "error": e.code, "message": e.message, **e.extra}
        except Unreachable as e:
            return {"ok": False, "error": "unreachable", "message": str(e)}
        except B.GitError as e:
            log.warning("%s: git failed: %s", op, e)
            return {"ok": False, "error": "git_failed", "message": str(e)[:300]}
        except Exception as e:  # always answer: a caller with no reply cannot tell whether the op ran
            log.exception("%s failed: %s", op, e)
            return {"ok": False, "error": "internal_error", "message": f"crewd {op} failed ({e.__class__.__name__})"}

    # -- socket server -------------------------------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            peer = peer_credentials(sock) if sock is not None else Peer(None, None)
        except OSError:
            peer = Peer(None, None)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            req = json.loads(line or b"null")
            if not isinstance(req, dict):
                raise ValueError("not an object")
            op = str(req.get("op") or "")
            raw_args = req.get("args")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            if peer.pid is not None:
                # read the ancestry now, while the caller is connected (it may exit right after an ack)
                table = await asyncio.to_thread(self.ppid_table, peer.pid)
                peer = Peer(peer.pid, peer.uid, tuple(ancestors(peer.pid, table)))
            if req.get("reply") == "ack":
                writer.write(b'{"ok":true,"accepted":true}\n')
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
                self.spawn(self.handle(op, args, peer))
                return
            result = await self.handle(op, args, peer)
            writer.write(json.dumps(result, default=str, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (TimeoutError, ValueError, ConnectionError):
            pass
        except Exception as e:  # never let one client take the daemon down
            log.exception("socket client error: %s", e)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def start_server(self) -> None:
        path = self.layout.socket
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.parent != self.layout.run and not socket_dir_trusted(path.parent):
            raise CrewdError("socket_dir_untrusted", f"{path.parent} is not private to this user")
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self.server = await asyncio.start_unix_server(self._client, path=str(path))
        os.chmod(path, 0o600)

    async def startup(self) -> None:
        self.load_state()
        keep = [str(s["toplevel"]) for s in self.sessions.values() if s.get("enforcement") == "advisory"]
        self.fence.sweep(keep=keep)
        if self.restore_gate:
            with contextlib.suppress(Exception):
                self.check_gate()
        self.write_status()
        await self.start_server()
        for crew_id in {str(s["crew_id"]) for s in self.sessions.values()}:
            with contextlib.suppress(Exception):
                await self.sync_snapshot(crew_id)

    async def _every(self, interval: float, fn: Callable[[], Awaitable[Any]]) -> None:
        while not self.stopping.is_set():
            try:
                await fn()
            except Exception as e:
                log.warning("%s failed: %s", getattr(fn, "__name__", "loop"), e)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=interval)

    async def run(self) -> None:
        await self.startup()

        async def integrity() -> None:
            self.check_gate()

        async def hooks_and_status() -> None:
            self.check_githooks()
            self.write_status()

        async def batons() -> None:
            self.sweep_batons()

        loops = [
            self._every(HEARTBEAT_S, self.heartbeat),
            self._every(OUTBOX_S, self.flush_outbox),
            self._every(LIVENESS_S, self.liveness),
            self._every(INTEGRITY_S, integrity),
            self._every(DETECTOR_S, self.detect),
            self._every(PRESENCE_S, self.send_presence),
            self._every(ZONES_S, self.check_zones),
            self._every(HEARTBEAT_S, hooks_and_status),
            self._every(BATON_SWEEP_S, batons),
        ]
        tasks = [asyncio.ensure_future(c) for c in loops]
        await self.stopping.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.shutdown()

    async def shutdown(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
        for link in self.ws_links.values():
            with contextlib.suppress(Exception):
                await link.close()
        for api in self.apis.values():
            with contextlib.suppress(Exception):
                await api.close()
        with contextlib.suppress(OSError):
            self.layout.socket.unlink()
        for task in list(self.background):
            task.cancel()


# ===========================================================================
# Process entry (single instance, supervised)
# ===========================================================================


async def websockets_connect(url: str, headers: Mapping[str, str]) -> Any:
    import websockets

    return await websockets.connect(url, additional_headers=dict(headers), open_timeout=5, close_timeout=2)


def acquire_lock(layout: Layout, *, wait: bool) -> int | None:
    layout.run.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def setup_logging(layout: Layout) -> None:
    layout.log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(layout.log_dir / "crewd.log", maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("remembra.crewd")
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="remembra-crewd", description="Remembra Crew daemon (one per user).")
    parser.add_argument("--wait-lock", action="store_true", help="supervisor mode: wait for the single-instance lock")
    parser.add_argument("--print-unit", choices=["launchd", "systemd"], help="print the supervision unit and exit")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    layout = Layout.from_env()
    if args.version:
        print(f"remembra-crewd {CREWD_VERSION}")
        return 0
    if args.print_unit:
        print(launchd_plist(layout.home) if args.print_unit == "launchd" else systemd_unit(layout.home))
        return 0
    layout.ensure()
    fd = acquire_lock(layout, wait=args.wait_lock)
    if fd is None:
        return 0  # another crewd holds the lock: nothing to do
    setup_logging(layout)
    write_json(layout.pidfile, {"pid": os.getpid(), "started_at": time.time()})
    crewd = Crewd(layout, ws_connect=websockets_connect)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, crewd.stopping.set)
        await crewd.run()

    try:
        asyncio.run(run())
    finally:
        with contextlib.suppress(OSError):
            layout.pidfile.unlink()
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
