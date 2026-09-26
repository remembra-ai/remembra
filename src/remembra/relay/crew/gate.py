"""``crew-gate.py``: the per-tool, per-turn and git hook gate (spec §8.1, §8.2, §8.4, §10.4).

The gate is invoked by the agent's hooks and by the git hooks::

    <py> -I ~/.remembra/crew/bin/crew-gate.py pretool    --hook claude-code   # PreToolUse
    <py> -I ~/.remembra/crew/bin/crew-gate.py posttool   --hook claude-code   # PostToolUse (async, hands off ≤100 ms)
    <py> -I ~/.remembra/crew/bin/crew-gate.py turn       --hook claude-code   # UserPromptSubmit
    <py> -I ~/.remembra/crew/bin/crew-gate.py stop       --hook claude-code   # Stop (D16)
    <py> -I ~/.remembra/crew/bin/crew-gate.py precompact --hook claude-code   # PreCompact
    <py> -I ~/.remembra/crew/bin/crew-gate.py rewake     --hook claude-code   # asyncRewake waiter (exit 2 wakes the agent)
    <py> -I ~/.remembra/crew/bin/crew-gate.py precommit | trailer <msgfile> | prepush <remote> <url>  # git gates

Rules it follows (all from the spec; tests pin each one):

* **Stdlib only** and **no keys**: it reads the local snapshot, the session file crewd wrote and
  crewd's status file, and talks to crewd over the peer-authenticated unix socket. It never reads
  API keys or session tokens.
* **Fast exit**: a hook in a session crewd never joined (no session file) exits before importing
  the decision core (<10 ms budget). Read-only Bash and read-like MCP tools exit before the
  snapshot is read.
* **Allow = exit 0 with empty stdout.** Never ``"permissionDecision":"allow"``; never exit 2 in
  PreToolUse. Every internal error allows that call and spools ``gate.error`` (a buggy hook must
  not brick the agent, §10.3).
* **Auto-claim (D11)**: through crewd, ≤700 ms, refresh plus claim ≤900 ms. On timeout or 429 the
  one write is allowed, an ``unconfirmed`` claim is spooled, and further writes to that zone are
  denied until it is confirmed.
* **Mid-turn delivery (D13, S0 GO)**: only server- or human-generated items (from crewd's
  heartbeat ``inject_text``) are delivered as PreToolUse ``additionalContext``.
* **Git gates** resolve the committing session through crewd from the parent process chain, never
  from an environment variable; no session means a human, who is allowed.

The same module also vendors itself (:func:`vendor_gate`) with the decision core into
``~/.remembra/crew/bin`` and verifies that copy (:func:`verify_gate`, crewd checks it every 30 s).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

GATE_VERSION: Final = 1
DEFAULT_ADAPTER: Final = "claude-code"
PRETOOL_DEADLINE_S: Final = 1.2
CLAIM_TIMEOUT_S: Final = 0.7
CLAIM_AND_REFRESH_CAP_S: Final = 0.9
SYNC_REFRESH_TIMEOUT_S: Final = 0.4
FIRE_TIMEOUT_S: Final = 0.25
RESPAWN_INTERVAL_S: Final = 30.0
STATUS_STALE_S: Final = 180.0
TURN_COMPACT_AFTER: Final = 40
STDIN_WAIT_S: Final = 1.0
# Hook payloads are read in full up to this size (a Write of a large file carries its whole content).
# A larger payload is never silently skipped: the gate salvages the session, tool and path fields,
# records gate.error, and refuses a write it cannot check (§5.2 rows 1-5 deny in every mode).
STDIN_MAX_CHARS: Final = 64 * 1024 * 1024
SOCKET_PATH_MAX: Final = 100  # macOS sun_path is 104 bytes
EDIT_TOOLS: Final = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
COMPLETION_RE: Final = re.compile(
    r"\b(?:all (?:tests|checks) pass(?:ed|ing)?|(?:task|work|feature|implementation|fix) (?:is )?(?:done|complete|finished)|"
    r"i(?:'ve| have) (?:finished|completed|implemented)|(?:done|finished|completed)[.!]?\s*$)",
    re.IGNORECASE,
)
_NEWS_TYPES: Final = frozenset(
    {
        "task.done",
        "task.stalled",
        "baton.passed",
        "session.quota_blocked",
        "session.lost",
        "session.recovered",
        "claim.reserved",
        "claim.released",
        "claim.adopted",
        "collision.detected",
        "zone.frozen",
        "zone.unfrozen",
        "decision.confirmed",
        "human.override",
        "session.paused",
        "session.resumed",
    }
)


# ===========================================================================
# Local layout
# ===========================================================================


def session_key(adapter: str, client_session_id: str) -> str:
    """File key of one agent session: ``sha256(adapter \\x1f client session id)[:24]``."""
    return hashlib.sha256(f"{adapter}\x1f{client_session_id}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Layout:
    """``~/.remembra/crew`` (0700, files 0600; all inside the built-in ``crew-policy`` zone)."""

    home: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Layout:
        env = os.environ if environ is None else environ
        return cls(Path(env.get("HOME") or Path.home()))

    @property
    def root(self) -> Path:
        return self.home / ".remembra" / "crew"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def gate_script(self) -> Path:
        return self.bin / "crew-gate.py"

    @property
    def lib(self) -> Path:
        return self.bin / "lib"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def pidfile(self) -> Path:
        return self.run / "crewd.pid"

    @property
    def lockfile(self) -> Path:
        return self.run / "crewd.lock"

    @property
    def status_file(self) -> Path:
        return self.run / "status.json"

    @property
    def respawn_stamp(self) -> Path:
        return self.run / "respawn.stamp"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshot"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def keys(self) -> Path:
        return self.root / "keys"

    @property
    def arbiter(self) -> Path:
        return self.root / "arbiter.json"

    @property
    def outbox(self) -> Path:
        return self.root / "outbox"

    @property
    def fence(self) -> Path:
        return self.root / "fence"

    @property
    def captures(self) -> Path:
        return self.root / "captures"

    @property
    def adapters_file(self) -> Path:
        return self.root / "adapters.json"

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def log_dir(self) -> Path:
        return self.root / "log"

    @property
    def crewd_cmd(self) -> Path:
        return self.bin / "crewd.json"

    @property
    def socket(self) -> Path:
        """``run/crewd.sock``, or a short per-user path when that is too long for ``sun_path``."""
        preferred = self.run / "crewd.sock"
        if len(os.fsencode(str(preferred))) <= SOCKET_PATH_MAX:
            return preferred
        digest = hashlib.sha256(os.fsencode(str(self.root))).hexdigest()[:16]
        return Path("/tmp") / f"remembra-crew-{os.getuid()}" / f"{digest}.sock"

    def session_file(self, key: str) -> Path:
        return self.sessions / f"{key}.json"

    def turn_file(self, key: str) -> Path:
        return self.sessions / f"{key}.turn.json"

    def snapshot_file(self, crew_id: str) -> Path:
        safe = "".join(ch for ch in crew_id if ch.isalnum() or ch in "_-")[:80]
        return self.snapshots / f"{safe}.json"

    def ensure(self) -> None:
        for d in (self.root, self.run, self.snapshots, self.sessions, self.keys, self.outbox, self.fence, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        sock_dir = self.socket.parent
        if sock_dir != self.run:
            sock_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(sock_dir, 0o700)


def socket_dir_trusted(path: Path) -> bool:
    """The socket's directory must be ours and private (the /tmp fallback could be pre-created by another user)."""
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: Any) -> None:
    from remembra.relay.crew.outbox import atomic_write_json

    atomic_write_json(path, data)


# ===========================================================================
# crewd socket client
# ===========================================================================


def rpc(layout: Layout, op: str, args: Mapping[str, Any] | None = None, *, timeout: float = 0.5) -> dict[str, Any] | None:
    """One request/response on crewd's socket; ``None`` when crewd is unreachable or too slow."""
    return rpc_ex(layout, op, args, timeout=timeout)[0]


def rpc_ex(
    layout: Layout, op: str, args: Mapping[str, Any] | None = None, *, timeout: float = 0.5
) -> tuple[dict[str, Any] | None, bool]:
    """:func:`rpc` plus whether the request reached crewd (``sent``).

    ``(None, False)``: crewd is not running (connect or send failed), so the op certainly did not run.
    ``(None, True)``: crewd took the request but no reply came in time; the op may have run, so a
    caller must not blindly resend a non-idempotent op.
    """
    path = layout.socket
    if path.parent != layout.run and not socket_dir_trusted(path.parent):
        return None, False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(max(0.01, timeout))
    deadline = time.monotonic() + timeout
    sent = False
    try:
        sock.connect(str(path))
        sock.sendall(json.dumps({"op": op, "args": dict(args or {})}, separators=(",", ":")).encode() + b"\n")
        sent = True
        buf = b""
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, sent
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4 * 1024 * 1024:
                return None, sent
        data = json.loads(buf.split(b"\n", 1)[0] or b"null")
        return (data if isinstance(data, dict) else None), sent
    except (OSError, ValueError):
        return None, sent
    finally:
        sock.close()


def rpc_send(layout: Layout, op: str, args: Mapping[str, Any] | None = None, *, timeout: float = FIRE_TIMEOUT_S) -> bool:
    """Fire and forget: crewd authenticates the caller, acknowledges at once and does the work after.

    The caller waits only for the acknowledgement (so crewd can read its process ancestry while it
    is still alive), never for the work itself.
    """
    path = layout.socket
    if path.parent != layout.run and not socket_dir_trusted(path.parent):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall(json.dumps({"op": op, "args": dict(args or {}), "reply": "ack"}, separators=(",", ":")).encode() + b"\n")
        try:
            sock.recv(256)
        except OSError:
            pass  # sent; the acknowledgement was just slow
        return True
    except OSError:
        return False
    finally:
        sock.close()


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def crewd_status(layout: Layout) -> dict[str, Any]:
    """crewd's status file, marked ``running`` only when its pid is alive and the file is recent."""
    status = read_json(layout.status_file) or {}
    try:
        age = time.time() - float(status.get("updated_at") or 0)
    except (TypeError, ValueError):
        age = 1e9
    status["running"] = pid_alive(int(status.get("pid") or 0)) and age < STATUS_STALE_S
    return status


def respawn_crewd(layout: Layout, *, force: bool = False) -> bool:
    """Start crewd detached when it is not running, at most once per 30 s (§8.1)."""
    if not force:
        try:
            if time.time() - layout.respawn_stamp.stat().st_mtime < RESPAWN_INTERVAL_S:
                return False
        except OSError:
            pass
    cmd = read_json(layout.crewd_cmd) or {}
    argv = cmd.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return False
    try:
        layout.run.mkdir(parents=True, exist_ok=True)
        layout.respawn_stamp.touch()
        env = {k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_CREW")}
        subprocess.Popen(  # noqa: S603 - argv written by the installer, never from agent input
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        return True
    except OSError:
        return False


# ===========================================================================
# Hook context
# ===========================================================================


def _err(message: str) -> None:
    try:
        sys.stderr.write(f"remembra-crew gate: {message}\n")
    except Exception:
        pass


def read_stdin(wait: float = STDIN_WAIT_S) -> str:
    return read_stdin_ex(wait)[0]


def read_stdin_ex(wait: float = STDIN_WAIT_S, limit: int = STDIN_MAX_CHARS) -> tuple[str, bool]:
    """``(text, oversized)``: stdin up to ``limit`` characters, and whether more was left unread."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "", False
        ready, _, _ = select.select([sys.stdin], [], [], wait)
        if not ready:
            return "", False
        text = sys.stdin.read(limit)
        oversized = len(text) >= limit and bool(sys.stdin.read(1))
        return text, oversized
    except (OSError, ValueError):
        return "", False


# A key of a JSON object: preceded by ``{`` or ``,`` (inside a JSON string every ``"`` is escaped, so
# this never matches text that is part of a value such as a file's content).
_SALVAGE_KEY: Final = r'[{{,]\s*"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"'
_SALVAGE_TOP: Final = ("session_id", "hook_event_name", "tool_name", "cwd", "permission_mode", "transcript_path")
_SALVAGE_PATHS: Final = ("file_path", "notebook_path", "path", "absolute_path", "destination", "source", "target")


def _json_str(escaped: str) -> str | None:
    try:
        value = json.loads(f'"{escaped}"')
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def salvage_payload(raw: str) -> dict[str, Any]:
    """The fields the gate needs from a payload that is too large to read in full or does not parse.

    Top-level fields take their first occurrence (Claude Code writes them before ``tool_input``);
    path-like fields become a ``tool_input`` of just those paths, so the zone and crew-policy checks
    still run on them.
    """
    out: dict[str, Any] = {}
    for key in _SALVAGE_TOP:
        m = re.search(_SALVAGE_KEY.format(key=key), raw)
        value = _json_str(m.group(1)) if m else None
        if value is not None:
            out[key] = value
    tool_input: dict[str, Any] = {}
    for key in _SALVAGE_PATHS:
        m = re.search(_SALVAGE_KEY.format(key=key), raw)
        value = _json_str(m.group(1)) if m else None
        if value:
            tool_input[key] = value
    if tool_input:
        out["tool_input"] = tool_input
    return out


def parse_payload(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class HookContext:
    layout: Layout
    adapter: str
    payload: dict[str, Any]
    local_now: float = field(default_factory=time.time)
    started: float = field(default_factory=time.monotonic)
    degraded: str | None = None  # "oversized" / "unparseable": ``payload`` holds only the salvaged fields
    _session: dict[str, Any] | None = None
    _session_loaded: bool = False

    @property
    def client_session_id(self) -> str | None:
        sid = self.payload.get("session_id")
        return sid.strip() if isinstance(sid, str) and sid.strip() else None

    @property
    def key(self) -> str | None:
        sid = self.client_session_id
        return session_key(self.adapter, sid) if sid else None

    def session(self) -> dict[str, Any] | None:
        if not self._session_loaded:
            self._session_loaded = True
            key = self.key
            data = read_json(self.layout.session_file(key)) if key else None
            if data and data.get("crew_id") and data.get("session_id") and not data.get("ended"):
                self._session = data
        return self._session

    def elapsed(self) -> float:
        return time.monotonic() - self.started


def events_for(verdict_dict: Mapping[str, Any], *, surface: str, op: str) -> list[dict[str, Any]]:
    """Client events (§4.2 whitelist) for a gate verdict's effects."""
    out: list[dict[str, Any]] = []
    effects = set(verdict_dict.get("effects") or ())
    decision = verdict_dict.get("decision")
    holder = verdict_dict.get("holder")
    base = {
        "path_rel": verdict_dict.get("path_rel"),
        "zone": verdict_dict.get("zone"),
        "holder": holder,
        "rule": int(verdict_dict.get("rule") or 1) or 1,
        "op": op,
        "surface": surface,
        "coalesced": 1,
    }
    if "guard.tamper_blocked" in effects:
        kinds = verdict_dict.get("tamper_kinds") or ["crew_policy_write"]
        out.append({"type": "guard.tamper_blocked", "payload": {"kind": str(kinds[0]), "surface": surface}})
    if "guard.blocked" in effects and decision in ("deny", "ask"):
        out.append({"type": "guard.blocked", "payload": {**base, "decision": decision}})
    if "would_deny" in effects or decision == "warn":
        out.append({"type": "guard.blocked", "payload": {**base, "decision": "would_deny"}})
    return out


def _clean_event(ev: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in (ev.get("payload") or {}).items() if v is not None}
    rule = payload.get("rule")
    if isinstance(rule, int) and not 1 <= rule <= 19:
        payload["rule"] = 1
    return {"type": ev["type"], "payload": payload}


def submit_events(layout: Layout, session: Mapping[str, Any], key: str, events: Sequence[dict[str, Any]]) -> None:
    """Hand client events to crewd, or spool them when crewd is unreachable."""
    if not events:
        return
    cleaned = [_clean_event(e) for e in events]
    now = time.time()
    if rpc_send(layout, "events", {"key": key, "events": cleaned, "at": now}):
        return
    from remembra.relay.crew import outbox

    for ev in cleaned:
        try:
            outbox.spool(
                layout.outbox,
                "event",
                {"type": ev["type"], "payload": ev["payload"], "at": now},
                session_key=key,
                crew_id=str(session.get("crew_id") or ""),
            )
        except (OSError, ValueError):
            pass


def _rel(path: str | None, cwd: str, toplevel: str | None) -> str | None:
    if not path or not toplevel:
        return None
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    try:
        real_top = os.path.realpath(toplevel)
        real = os.path.realpath(full)
    except OSError:
        return None
    for top, p in ((real_top, real), (toplevel, full)):
        if p == top:
            return "."
        if p.startswith(top.rstrip("/") + "/"):
            return p[len(top.rstrip("/")) + 1 :]
    return None


def _activity(ctx: HookContext, session: Mapping[str, Any], phase: str, extra: Mapping[str, Any]) -> None:
    key = ctx.key
    if not key:
        return
    body = {"key": key, "phase": phase, "at": ctx.local_now, **extra}
    if rpc_send(ctx.layout, "activity", body):
        return
    from remembra.relay.crew import outbox

    try:
        outbox.spool(ctx.layout.outbox, "activity", body, session_key=key, crew_id=str(session.get("crew_id") or ""))
    except (OSError, ValueError):
        pass
    respawn_crewd(ctx.layout)


def _tool_op(tool: str) -> str:
    if tool == "Bash":
        return "command"
    if tool.startswith("mcp__"):
        return "mcp"
    return "write"


# ===========================================================================
# PreToolUse
# ===========================================================================


def _load_snapshot(layout: Layout, crew_id: str) -> dict[str, Any] | None:
    from remembra.relay.crew.snapshot import load_snapshot

    return load_snapshot(layout.snapshot_file(crew_id))


def _server_dt(snapshot: Mapping[str, Any] | None, local_now: float) -> Any:
    from datetime import UTC, datetime

    from remembra.relay.crew.snapshot import server_now

    return datetime.fromtimestamp(server_now(snapshot, local_now), UTC)


def _unconfirmed(layout: Layout, key: str, session: Mapping[str, Any]) -> set[str]:
    from remembra.relay.crew import outbox

    ids = {str(z) for z in session.get("unconfirmed_zone_ids") or ()}
    return ids | outbox.pending_claim_zone_ids(layout.outbox, key)


def _bypass_ok(ctx: HookContext, session: Mapping[str, Any], surface: str) -> bool:
    """A human-issued single-use bypass (D34), consumed through crewd so it is used once."""
    grant = session.get("bypass")
    if not isinstance(grant, dict) or grant.get("used"):
        return False
    try:
        if float(grant.get("expires_at") or 0) <= ctx.local_now:
            return False
    except (TypeError, ValueError):
        return False
    scope = str(grant.get("scope") or "all")
    if scope not in ("all", "*", surface, "gate"):
        return False
    res = rpc(ctx.layout, "bypass_consume", {"key": ctx.key, "surface": surface}, timeout=0.3)
    return bool(res and res.get("ok"))


def _verdict_info(verdict: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    d = dict(verdict.as_dict())
    target = verdict.target
    display = getattr(target, "display", None) if target is not None else None
    d["path_rel"] = display if display and not display.startswith(("/", "~")) else None
    holder = verdict.holder_session_id
    callsign = None
    if holder:
        for s in snapshot.get("sessions") or ():
            if s.get("id") == holder:
                callsign = s.get("callsign")
    if callsign is None and holder is None and verdict.decision in ("deny", "ask", "warn") and verdict.rule in (3, 9, 10):
        for c in snapshot.get("claims") or ():  # a human hold (freeze): the event names "human", never a person
            if c.get("holder_kind") == "human" and any(
                z.get("slug") == d.get("zone") and z.get("id") == c.get("zone_id") for z in snapshot.get("zones") or ()
            ):
                callsign = "human"
    d["holder"] = callsign
    return d


def _claim_fn(ctx: HookContext, key: str, budget_end: float) -> Any:
    from remembra.crew import gatecore as G

    def claim(req: Any) -> Any:
        remaining = min(CLAIM_TIMEOUT_S, budget_end - time.monotonic())
        if remaining <= 0.02:
            return G.ClaimResult("timeout")
        res = rpc(
            ctx.layout,
            "claim",
            {"key": key, "zone_id": req.zone_id, "zone": req.zone_slug, "mode": req.mode, "path_glob": req.path_glob},
            timeout=remaining,
        )
        if res is None:
            return G.ClaimResult("timeout")
        result = str(res.get("result") or "timeout")
        if result not in G.AUTO_CLAIM_RESULTS:
            result = "timeout"
        return G.ClaimResult(result, winner_session_id=res.get("winner_session_id"), claim_id=res.get("claim_id"))

    return claim


def _spool_unconfirmed(ctx: HookContext, key: str, session: Mapping[str, Any], verdict: Any) -> None:
    from remembra.relay.crew import outbox

    spooled = False
    for req, res in verdict.claims:
        if res.result not in ("timeout", "rate_limited"):
            continue
        spooled = True
        body = {
            "zone_id": req.zone_id,
            "zone": req.zone_slug,
            "mode": req.mode,
            "path_glob": req.path_glob,
            "source": "first_write",
        }
        try:
            outbox.spool(
                ctx.layout.outbox,
                "claim",
                body,
                session_key=key,
                crew_id=str(session.get("crew_id") or ""),
                name=outbox.claim_record_name(key, req.zone_id, req.path_glob),
            )
        except (OSError, ValueError):
            pass
    if spooled:
        rpc_send(ctx.layout, "flush")  # the confirmation normally lands within 2 s (D11)


def evaluate_pretool(ctx: HookContext, session: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Decide one PreToolUse call. Returns ``(stdout, verdict dict or None)``."""
    from remembra.crew import gatecore as G
    from remembra.crew import schemas as S
    from remembra.relay.crew.snapshot import ASYNC_REFRESH_S, FRESH_S, age_s

    key = ctx.key or ""
    payload = ctx.payload
    tool = str(payload.get("tool_name") or "")
    tool_input = _as_dict(payload.get("tool_input"))
    cwd = str(payload.get("cwd") or session.get("toplevel") or os.getcwd())
    # fast exits before the snapshot is read (§8.2 read-only allow-list, read-like MCP tools)
    verb = None
    if tool == "Bash":
        parsed = G.parse_bash_full(str(tool_input.get("command") or ""))
        if parsed.read_only:
            return "", None
        verb = _bash_verb(str(tool_input.get("command") or ""))
    elif tool.startswith("mcp__"):
        if S.classify_mcp_tool(tool, tool_input).kind == "read":
            return "", None
    elif tool not in EDIT_TOOLS:
        return "", None
    crew_id = str(session["crew_id"])
    snap = _load_snapshot(ctx.layout, crew_id)
    status = crewd_status(ctx.layout)
    if not status.get("running"):
        respawn_crewd(ctx.layout)
    if snap is None:
        rpc_send(ctx.layout, "refresh", {"crew_id": crew_id})
        submit_events(ctx.layout, session, key, [{"type": "gate.deadline", "payload": {"stage": "no_snapshot", "elapsed_ms": 0}}])
        _err("no crew snapshot yet: this write is allowed; crew rules apply once crewd has synced")
        return "", {"rule": 0, "decision": "allow", "variant": "no_snapshot"}
    budget_end = ctx.started + CLAIM_AND_REFRESH_CAP_S
    reachable = bool(status.get("server_reachable", True)) if status.get("running") else False
    outage = bool(status.get("server_outage")) and status.get("running")
    human = str(session.get("human_name") or "the owner")
    age = age_s(snap, ctx.local_now)

    def run(snapshot: Mapping[str, Any]) -> Any:
        return G.evaluate(
            tool,
            tool_input,
            snapshot=snapshot,
            caller=str(session["session_id"]),
            cwd=cwd,
            home=str(ctx.layout.home),
            now=_server_dt(snapshot, ctx.local_now),
            permission_mode=str(payload.get("permission_mode") or "default"),
            claim=_claim_fn(ctx, key, budget_end),
            server_reachable=reachable,
            server_outage=bool(outage),
            unconfirmed_zone_ids=_unconfirmed(ctx.layout, key, session),
            human=human,
        )

    if age is not None and FRESH_S < age <= ASYNC_REFRESH_S:
        rpc_send(ctx.layout, "refresh", {"crew_id": crew_id})
    verdict = run(snap)
    if age is not None and age > ASYNC_REFRESH_S and not verdict.claims and verdict.rule != 0:
        # a stale snapshot: one sync refresh (≤400 ms), skipped when an auto-claim already answered (§8.2)
        res = rpc(ctx.layout, "refresh", {"crew_id": crew_id, "sync": True}, timeout=SYNC_REFRESH_TIMEOUT_S)
        if res and res.get("ok"):
            fresh = _load_snapshot(ctx.layout, crew_id)
            if fresh is not None:
                snap = fresh
                verdict = run(snap)
    info = _verdict_info(verdict, snap)
    op = _tool_op(tool)
    events = events_for(info, surface="mcp" if op == "mcp" else "pretool", op=op)
    if "claim.unconfirmed" in verdict.effects:
        _spool_unconfirmed(ctx, key, session, verdict)
    if "gate.deadline" in verdict.effects:
        slug = next((r.zone_slug for r, res in verdict.claims if res.result == "timeout"), None)
        events.append(
            {
                "type": "gate.deadline",
                "payload": {"stage": "auto_claim", "elapsed_ms": int(ctx.elapsed() * 1000), "unconfirmed_zone": slug},
            }
        )
    if "offline_stale_allow" in verdict.effects:
        _err("crew server unreachable and the snapshot is old: remote holders are not enforced for this write")
    micro = [e for e in verdict.effects if e in ("micro_lease", "schema_claim", "notify_watchers")]
    if micro and verdict.decision in ("allow", "warn") and info.get("path_rel"):
        rpc_send(ctx.layout, "commons", {"key": key, "effects": micro, "path_rel": info["path_rel"]})
    out = verdict.hook_stdout()
    if verdict.decision in ("deny", "ask") and _bypass_ok(ctx, session, "pretool"):
        events = [e for e in events if e["type"] != "guard.blocked"]
        out = ""
        info["bypassed"] = True
    submit_events(ctx.layout, session, key, events)
    if not out:
        inject = session.get("inject")
        if isinstance(inject, dict) and inject.get("text") and inject.get("id"):
            turn = read_json(ctx.layout.turn_file(key)) or {}
            if inject["id"] not in (turn.get("delivered_inject") or []):
                try:
                    out = S.hook_pretool_context(S.clip_item(str(inject["text"]), S.TEXT_CAPS["pretool_context"]))
                    turn.setdefault("delivered_inject", []).append(inject["id"])
                    turn["delivered_inject"] = turn["delivered_inject"][-50:]
                    write_json(ctx.layout.turn_file(key), turn)
                    rpc_send(ctx.layout, "delivered", {"key": key, "inject_id": inject["id"]})
                except (ValueError, OSError):
                    out = ""
    paths = [_rel(str(tool_input.get(EDIT_TOOLS[tool]) or ""), cwd, session.get("toplevel"))] if tool in EDIT_TOOLS else []
    phase = "blocked" if verdict.decision == "deny" and not info.get("bypassed") else "start"
    _activity(ctx, session, phase, {"tool": tool, "paths": [p for p in paths if p], "verb": verb})
    return out, info


def _bash_verb(command: str) -> str | None:
    """The program name of the first command (metadata only; the command string never leaves the host)."""
    from remembra.crew import gatecore as G

    try:
        argvs = G.parse_bash_full(command).argvs
    except Exception:
        argvs = []
    for _cwd, argv in argvs:
        if argv:
            return os.path.basename(str(argv[0]))[:32] or None
    word = command.strip().split(" ", 1)[0] if command.strip() else ""
    return os.path.basename(word)[:32] or None


DEGRADED_DENY: Final = {
    "oversized": (
        "Crew: this tool call is too large for the crew gate to check (over 64 MB), so it is refused."
        " Split it into smaller edits or commands."
    ),
    "unparseable": "Crew: the crew gate could not read this tool call, so it is refused. Try it again.",
}


def degraded_pretool(ctx: HookContext, session: dict[str, Any]) -> str | None:
    """A payload the gate could not read in full: record it; refuse what cannot be checked.

    Returns the deny output, or ``None`` when the salvaged fields are enough for the normal check
    (an edit whose path was recovered, an MCP call).
    """
    from remembra.crew import schemas as S

    reason = ctx.degraded or "unparseable"
    _err(f"hook payload {reason}; checking the fields that could be recovered")
    submit_events(
        ctx.layout,
        session,
        ctx.key or "",
        [{"type": "gate.error", "payload": {"stage": "payload", "error_class": f"payload_{reason}"[:64]}}],
    )
    tool = str(ctx.payload.get("tool_name") or "")
    tool_input = _as_dict(ctx.payload.get("tool_input"))
    if tool.startswith("mcp__"):
        return None
    if tool in EDIT_TOOLS and tool_input.get(EDIT_TOOLS[tool]):
        return None
    # a shell command cut off mid-string, or an edit whose target is unknown: nothing can be checked
    return S.hook_pretool_deny(DEGRADED_DENY.get(reason, DEGRADED_DENY["unparseable"]))


def cmd_pretool(ctx: HookContext) -> int:
    session = ctx.session()
    if session is None:
        return 0
    if ctx.degraded:
        try:
            denied = degraded_pretool(ctx, session)
        except Exception as e:
            _err(f"pretool error ({e.__class__.__name__}) on an unreadable payload; refused")
            from remembra.crew import schemas as S

            denied = S.hook_pretool_deny(DEGRADED_DENY["unparseable"])
        if denied:
            sys.stdout.write(denied)
            sys.stdout.flush()
            return 0
    try:
        out, _ = evaluate_pretool(ctx, session)
    except Exception as e:  # never brick the agent (§10.3): allow, record gate.error
        _err(f"pretool error ({e.__class__.__name__}); this call is allowed")
        key = ctx.key or ""
        submit_events(
            ctx.layout,
            session,
            key,
            [{"type": "gate.error", "payload": {"stage": "pretool", "error_class": e.__class__.__name__[:64]}}],
        )
        return 0
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    return 0


# ===========================================================================
# PostToolUse (async; hands off to crewd and exits ≤100 ms)
# ===========================================================================

_TEST_SUMMARY_RES: Final = (
    re.compile(r"(?P<n>\d+) passed"),  # pytest, vitest
    re.compile(r"Tests:\s+(?:(?P<f>\d+) failed, )?(?P<n>\d+) passed"),  # jest
    re.compile(r"test result: \w+\. (?P<n>\d+) passed; (?P<f>\d+) failed"),  # cargo
)
_TEST_FAIL_RE: Final = re.compile(r"(?P<f>\d+) failed")


def test_counts(stdout: str) -> tuple[int, int] | None:
    """Pass/fail counts from a test runner's summary line (counts only ever leave the host)."""
    text = stdout[-4000:]
    passed = failed = None
    for rx in _TEST_SUMMARY_RES:
        for m in rx.finditer(text):
            passed = int(m.group("n"))
            f = m.groupdict().get("f")
            if f:
                failed = int(f)
    fm = list(_TEST_FAIL_RE.finditer(text))
    if fm:
        failed = int(fm[-1].group("f"))
    if passed is None and failed is None:
        if re.search(r"^(?:ok|PASS)\b", text, re.MULTILINE) and not re.search(r"^(?:FAIL|FAILED)\b", text, re.MULTILINE):
            return (1, 0)
        if re.search(r"^(?:FAIL|FAILED)\b", text, re.MULTILINE):
            return (0, 1)
        return None
    return (passed or 0, failed or 0)


_GIT_VALUE_OPTS: Final = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_GIT_OPS: Final = frozenset({"commit", "push", "merge", "rebase", "cherry-pick", "pull", "am", "revert"})


def _git_sub(argv: Sequence[str]) -> str | None:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _GIT_VALUE_OPTS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def classify_command(command: str) -> dict[str, Any]:
    """What crewd needs to know about a Bash command, without the command string (§11: metadata only)."""
    from remembra.crew import gatecore as G
    from remembra.crew import schemas as S

    info: dict[str, Any] = {"read_only": False, "git_op": None, "test_argv": None, "verb": None}
    try:
        parsed = G.parse_bash_full(command)
    except Exception:
        info["verb"] = _bash_verb(command)
        return info
    info["read_only"] = bool(parsed.read_only)
    for _cwd, argv in parsed.argvs:
        if not argv:
            continue
        head = os.path.basename(str(argv[0]))
        if info["verb"] is None:
            info["verb"] = head[:32]
        if head == "git":
            sub = _git_sub(argv)
            if sub in _GIT_OPS and info["git_op"] is None:
                info["git_op"] = sub
            continue
        normalised = [head, *[str(a) for a in argv[1:]]]
        joined = " ".join(normalised[:4])
        for runner in S.BASH_TEST_RUNNERS:
            if joined == runner or joined.startswith(runner + " "):
                info["test_argv"] = [a[:64] for a in normalised[:12]]
                break
    return info


def cmd_posttool(ctx: HookContext) -> int:
    session = ctx.session()
    if session is None:
        return 0
    try:
        payload = ctx.payload
        tool = str(payload.get("tool_name") or "")
        tool_input = _as_dict(payload.get("tool_input"))
        cwd = str(payload.get("cwd") or session.get("toplevel") or "")
        extra: dict[str, Any] = {"tool": tool}
        if tool == "Bash":
            info = classify_command(str(tool_input.get("command") or ""))
            extra.update({"verb": info["verb"], "read_only": info["read_only"], "git_op": info["git_op"]})
            response = _as_dict(payload.get("tool_response"))
            if info["test_argv"]:
                counts = test_counts(str(response.get("stdout") or "") + "\n" + str(response.get("stderr") or ""))
                extra["test"] = {
                    "argv": info["test_argv"],
                    "passed": counts[0] if counts else 0,
                    "failed": counts[1] if counts else 0,
                    "known": counts is not None,
                }
            extra["cwd_rel"] = _rel(cwd, cwd, session.get("toplevel"))
        elif tool in EDIT_TOOLS:
            p = _rel(str(tool_input.get(EDIT_TOOLS[tool]) or ""), cwd, session.get("toplevel"))
            extra["paths"] = [p] if p else []
        elif tool.startswith("mcp__"):
            from remembra.crew import schemas as S

            cls = S.classify_mcp_tool(tool, tool_input)
            extra["paths"] = [q for q in (_rel(p, cwd, session.get("toplevel")) for p in cls.paths) if q]
            extra["mcp_kind"] = cls.kind
        _activity(ctx, session, "end", extra)
    except Exception as e:
        _err(f"posttool error ({e.__class__.__name__})")
    return 0


# ===========================================================================
# UserPromptSubmit
# ===========================================================================


def _obligations(snapshot: Mapping[str, Any], caller: str) -> list[str]:
    out: list[str] = []
    for c in snapshot.get("collisions") or ():
        if c.get("session_a") == caller and c.get("kind") == "exclusive_breach" and c.get("state") == "open":
            zone = next((z.get("slug") for z in snapshot.get("zones") or () if z.get("id") == c.get("zone_id")), None)
            out.append(
                f"OPEN COLLISION {c.get('id')}: your write in zone {zone or '?'} overlaps another holder; tell them with crew_say"
            )
    return out[:2]


def render_turn_for(ctx: HookContext, session: dict[str, Any], snapshot: Mapping[str, Any]) -> str | None:
    """The per-turn delta, or ``None`` when the digest is unchanged since the last turn."""
    from remembra.crew import gatecore as G

    key = ctx.key or ""
    caller = str(session["session_id"])
    turn = read_json(ctx.layout.turn_file(key)) or {}
    news = [n for n in session.get("news") or () if isinstance(n, dict)]
    last_news = int(turn.get("news_seq") or 0)
    fresh_news = [n for n in news if int(n.get("seq") or 0) > last_news and n.get("type") in _NEWS_TYPES]
    obligations = _obligations(snapshot, caller)
    inject = session.get("inject") if isinstance(session.get("inject"), dict) else None
    pending_inject = inject if inject and inject.get("id") not in (turn.get("delivered_inject") or []) else None
    now = _server_dt(snapshot, ctx.local_now)
    digest = G.turn_digest(
        snapshot,
        caller,
        now,
        unread=int(session.get("unread") or 0),
        checkpoint_ids=[str(n.get("seq")) for n in fresh_news],
        obligations=[*obligations, *([str(pending_inject.get("id"))] if pending_inject else [])],
    )
    if digest == turn.get("digest") and not fresh_news:
        return None
    count = int(turn.get("injections") or 0)
    compact = count >= TURN_COMPACT_AFTER
    items = [str(n.get("summary") or "")[:140] for n in fresh_news][-4:]
    if pending_inject:
        obligations = [str(pending_inject.get("text"))[:200], *obligations]
    text = G.render_turn(
        snapshot,
        caller,
        now,
        new_items=items,
        obligations=obligations,
        files_since_checkpoint=int(session.get("files_since_checkpoint") or 0) or None,
        compact=compact,
        human=str(session.get("human_name") or "the owner"),
    )
    turn["digest"] = digest
    turn["injections"] = count + 1
    if fresh_news:
        turn["news_seq"] = max(int(n.get("seq") or 0) for n in fresh_news)
    if pending_inject:
        turn.setdefault("delivered_inject", []).append(pending_inject["id"])
        turn["delivered_inject"] = turn["delivered_inject"][-50:]
    write_json(ctx.layout.turn_file(key), turn)
    if pending_inject:
        rpc_send(ctx.layout, "delivered", {"key": key, "inject_id": pending_inject["id"]})
    return text


def cmd_turn(ctx: HookContext) -> int:
    session = ctx.session()
    if session is None:
        return 0
    try:
        from remembra.crew import schemas as S
        from remembra.relay.crew.snapshot import FRESH_S, age_s

        snap = _load_snapshot(ctx.layout, str(session["crew_id"]))
        if snap is None:
            rpc_send(ctx.layout, "refresh", {"crew_id": session["crew_id"]})
            return 0
        age = age_s(snap, ctx.local_now)
        if age is None or age > FRESH_S:
            rpc_send(ctx.layout, "refresh", {"crew_id": session["crew_id"]})
        text = render_turn_for(ctx, session, snap)
        if text:
            sys.stdout.write(S.hook_user_prompt(text))
            sys.stdout.flush()
    except Exception as e:
        _err(f"turn error ({e.__class__.__name__})")
    return 0


# ===========================================================================
# Stop (D16)
# ===========================================================================


def stop_decision(ctx: HookContext, session: dict[str, Any], snapshot: Mapping[str, Any]) -> str | None:
    """The one Stop block this turn may issue (server templates, ids only), or ``None``."""
    from remembra.crew import gatecore as G

    if ctx.payload.get("stop_hook_active"):
        return None
    key = ctx.key or ""
    caller = str(session["session_id"])
    turn = read_json(ctx.layout.turn_file(key)) or {}
    blocked = set(turn.get("stop_blocked") or ())
    zones = {z.get("id"): z for z in snapshot.get("zones") or ()}
    callsigns = {s.get("id"): s.get("callsign") for s in snapshot.get("sessions") or ()}
    reason: str | None = None
    marker: str | None = None
    # (1) a certain exclusive breach by this session (probable ones never reach the agent, §5.3)
    for c in snapshot.get("collisions") or ():
        if (
            c.get("kind") == "exclusive_breach"
            and c.get("attribution") == "certain"
            and c.get("session_a") == caller
            and c.get("state") == "open"
            and f"breach:{c.get('id')}" not in blocked
        ):
            zone = zones.get(c.get("zone_id")) or {}
            holder = callsigns.get(c.get("session_b")) or "the holder"
            reason = G.stop_breach(str(c.get("subject") or ""), str(zone.get("slug") or "zone"), str(holder))
            marker = f"breach:{c.get('id')}"
            break
    # (2) the task looks finished (completion text + ≥1 commit since start) but has no completion report
    if reason is None:
        last = str(ctx.payload.get("last_assistant_message") or "")
        commits = _as_dict(session.get("task_commits"))
        for t in snapshot.get("tasks") or ():
            if t.get("owner_session_id") != caller or t.get("status") != "in_progress" or t.get("current_report_id"):
                continue
            ref = f"T-{t.get('number')}"
            if f"task:{t.get('id')}" in blocked:
                continue
            if COMPLETION_RE.search(last) and int(commits.get(str(t.get("id")), 0) or 0) >= 1:
                reason = G.stop_report_missing(ref)
                marker = f"task:{t.get('id')}"
                break
    if reason is None or marker is None:
        return None
    turn["stop_blocked"] = sorted(blocked | {marker})[-100:]
    write_json(ctx.layout.turn_file(key), turn)
    return reason


def cmd_stop(ctx: HookContext) -> int:
    session = ctx.session()
    if session is None:
        return 0
    try:
        from remembra.crew import schemas as S

        key = ctx.key or ""
        if not rpc_send(ctx.layout, "checkpoint", {"key": key, "trigger": "turn"}):
            from remembra.relay.crew import outbox

            outbox.spool(ctx.layout.outbox, "checkpoint", {"trigger": "turn"}, session_key=key, crew_id=str(session["crew_id"]))
        snap = _load_snapshot(ctx.layout, str(session["crew_id"]))
        if snap is None:
            return 0
        reason = stop_decision(ctx, session, snap)
        if reason:
            sys.stdout.write(S.hook_stop_block(reason))
            sys.stdout.flush()
    except Exception as e:
        _err(f"stop error ({e.__class__.__name__})")
    return 0


def cmd_wake(ctx: HookContext) -> int:
    """asyncRewake waiter (S0: promoted to L0). Exit 0 at once when nothing is queued; otherwise print the
    one queued server- or human-generated item (ids-only template) on stderr and exit 2, which wakes an
    idle agent. Idempotent: an item is delivered once, so the Stop that follows a rewake is a no-op."""
    session = ctx.session()
    if session is None:
        return 0
    try:
        from remembra.crew import schemas as S

        inject = session.get("inject")
        if not isinstance(inject, dict) or not inject.get("text") or not inject.get("id"):
            return 0
        key = ctx.key or ""
        turn = read_json(ctx.layout.turn_file(key)) or {}
        if inject["id"] in (turn.get("delivered_inject") or []):
            return 0
        text = S.clip_item(str(inject["text"]), S.TEXT_CAPS["pretool_context"])
        if S.check_agent_text(text, "pretool_context"):
            return 0
        turn.setdefault("delivered_inject", []).append(inject["id"])
        turn["delivered_inject"] = turn["delivered_inject"][-50:]
        write_json(ctx.layout.turn_file(key), turn)
        rpc_send(ctx.layout, "delivered", {"key": key, "inject_id": inject["id"]})
        sys.stderr.write(text + "\n")
        return 2
    except Exception as e:
        _err(f"wake error ({e.__class__.__name__})")
        return 0


def cmd_precompact(ctx: HookContext) -> int:
    session = ctx.session()
    if session is None:
        return 0
    try:
        key = ctx.key or ""
        if not rpc_send(ctx.layout, "checkpoint", {"key": key, "trigger": "precompact"}):
            from remembra.relay.crew import outbox

            outbox.spool(
                ctx.layout.outbox, "checkpoint", {"trigger": "precompact"}, session_key=key, crew_id=str(session["crew_id"])
            )
            respawn_crewd(ctx.layout)
    except Exception as e:
        _err(f"precompact error ({e.__class__.__name__})")
    return 0


# ===========================================================================
# Git gates (§8.4)
# ===========================================================================


def _git(args: Sequence[str], cwd: str, timeout: float = 2.0) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=timeout, check=False)  # noqa: S603,S607
    except (OSError, subprocess.SubprocessError):
        return None


def _whoami(layout: Layout) -> dict[str, Any] | None:
    res = rpc(layout, "whoami", {}, timeout=0.6)
    if res is None:
        respawn_crewd(layout)
        return None
    return res if res.get("ok") and res.get("key") else None


_MARKER_NAMES: Final = re.compile(r"(?:^|/)(?:\.claude/settings[^/]*\.json|\.husky/[^/]+|lefthook[^/]*|\.lefthook[^/]*)$")


def _staged(toplevel: str) -> list[tuple[str, str]]:
    res = _git(["diff", "--cached", "--name-status", "-z", "--no-renames"], toplevel)
    if res is None or res.returncode != 0:
        return []
    parts = res.stdout.decode("utf-8", "replace").split("\0")
    out: list[tuple[str, str]] = []
    for i in range(0, len(parts) - 1, 2):
        status, path = parts[i], parts[i + 1]
        if status and path:
            out.append((status[0], path))
    return out


@dataclass
class GitGateResult:
    allowed: bool
    reason: str | None = None
    rule: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


def _git_evaluate(
    layout: Layout,
    who: Mapping[str, Any],
    files: Iterable[tuple[str, str]],
    *,
    toplevel: str,
    surface: str,
    deny_rules: set[int] | None,
    claim_live: bool,
) -> GitGateResult:
    from remembra.crew import gatecore as G
    from remembra.crew import schemas as S

    key = str(who["key"])
    session = read_json(layout.session_file(key)) or {}
    crew_id = str(who.get("crew_id") or session.get("crew_id") or "")
    snap = _load_snapshot(layout, crew_id) if crew_id else None
    if snap is None:
        return GitGateResult(True)
    # §8.5: the git gates enforce for every agent, including advisory (unverified or observe-mode) adapters
    # whose pre-write hooks only log would_deny; only the crew-level setting (observe / off) relaxes them.
    mode = G.mode_for({**snap, "sessions": []}, "")
    if mode is None:
        return GitGateResult(True)
    status = crewd_status(layout)
    now_local = time.time()
    ctx = HookContext(layout, str(who.get("adapter") or DEFAULT_ADAPTER), {"session_id": who.get("client_session_id")})
    budget_end = time.monotonic() + CLAIM_AND_REFRESH_CAP_S
    claim = _claim_fn(ctx, key, budget_end) if claim_live else (lambda req: G.ClaimResult("granted"))
    events: list[dict[str, Any]] = []
    for status_letter, path in files:
        abs_path = os.path.join(toplevel, path)
        if status_letter == "D":
            tool, tin = "Bash", {"command": "rm -- " + _shell_quote(path)}
        else:
            content = None
            if _MARKER_NAMES.search(path):
                shown = _git(["show", f":{path}"], toplevel)
                content = shown.stdout.decode("utf-8", "replace") if shown and shown.returncode == 0 else None
            tool, tin = "Write", {"file_path": abs_path, **({"content": content} if content is not None else {})}
        verdict = G.evaluate(
            tool,
            tin,
            snapshot=snap,
            caller=str(who.get("session_id") or session.get("session_id") or ""),
            cwd=toplevel,
            home=str(layout.home),
            now=_server_dt(snap, now_local),
            mode=mode,
            claim=claim,
            server_reachable=bool(status.get("server_reachable", True)) if status.get("running") else False,
            server_outage=bool(status.get("server_outage")),
            human=str(session.get("human_name") or "the owner"),
        )
        if verdict.decision in ("deny", "ask") and (deny_rules is None or verdict.rule in deny_rules):
            info = _verdict_info(verdict, snap)
            events.extend(events_for({**info, "decision": "deny"}, surface=surface, op="write"))
            reason = verdict.reason or f"BLOCKED by Remembra Crew: {path} (rule {verdict.rule})"
            try:
                S.check_agent_text(reason, "deny")
            except Exception:
                pass
            return GitGateResult(False, reason, verdict.rule, events)
    return GitGateResult(True, events=events)


def _shell_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def _toplevel(cwd: str) -> str | None:
    res = _git(["rev-parse", "--show-toplevel"], cwd, timeout=1.0)
    if res is None or res.returncode != 0:
        return None
    return res.stdout.decode().strip() or None


def _bypass_code_ok(layout: Layout, who: Mapping[str, Any], surface: str) -> bool:
    """``REMEMBRA_BYPASS=<human-issued code> git …`` (D34): validated by the server through crewd, single use."""
    from remembra.crew import schemas as S

    code = os.environ.get("REMEMBRA_BYPASS", "").strip()
    if not code or not re.fullmatch(S.BYPASS_CODE_PATTERN, code):
        return False
    res = rpc(layout, "bypass_redeem", {"key": who.get("key"), "code": code, "surface": surface}, timeout=3.0)
    return bool(res and res.get("ok"))


def git_gate_precommit(layout: Layout, cwd: str) -> GitGateResult:
    top = _toplevel(cwd)
    if top is None:
        return GitGateResult(True)
    who = _whoami(layout)
    if who is None:
        return GitGateResult(True)  # a human at a terminal (or crewd down): allowed; the server flags unattributed changes
    files = _staged(top)
    if not files:
        return GitGateResult(True)
    result = _git_evaluate(layout, who, files, toplevel=top, surface="precommit", deny_rules=None, claim_live=True)
    if not result.allowed and _bypass_code_ok(layout, who, "precommit"):
        return GitGateResult(True, events=[])
    session = read_json(layout.session_file(str(who["key"]))) or {}
    submit_events(layout, session, str(who["key"]), result.events)
    return result


def _push_files(top: str, lines: Iterable[str]) -> list[tuple[str, str]]:
    files: dict[str, str] = {}
    zero = "0" * 40
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_ref, local_sha, _remote_ref, remote_sha = parts[:4]
        if local_sha.strip("0") == "":
            continue  # a delete push touches no files
        rng = (
            [local_sha, "--not", "--remotes"]
            if remote_sha == zero or remote_sha.strip("0") == ""
            else [f"{remote_sha}..{local_sha}"]
        )
        revs = _git(["rev-list", *rng], top, timeout=1.5)
        if revs is None or revs.returncode != 0:
            continue
        for sha in revs.stdout.decode().split()[:500]:
            res = _git(
                ["diff-tree", "--no-commit-id", "--name-status", "-r", "-z", "-m", "--first-parent", "--no-renames", sha], top
            )
            if res is None or res.returncode != 0:
                continue
            parts2 = res.stdout.decode("utf-8", "replace").split("\0")
            for i in range(0, len(parts2) - 1, 2):
                st, path = parts2[i], parts2[i + 1]
                if st and path:
                    files[path] = st[0]
    return [(st, p) for p, st in sorted(files.items())]


def git_gate_prepush(layout: Layout, cwd: str, lines: Iterable[str]) -> GitGateResult:
    top = _toplevel(cwd)
    if top is None:
        return GitGateResult(True)
    who = _whoami(layout)
    if who is None:
        return GitGateResult(True)
    files = _push_files(top, lines)
    if not files:
        return GitGateResult(True)
    # push-time: commits touching zones held exclusive or reserved by ANOTHER session (and frozen zones)
    result = _git_evaluate(layout, who, files, toplevel=top, surface="prepush", deny_rules={3, 9, 10}, claim_live=False)
    if not result.allowed and _bypass_code_ok(layout, who, "prepush"):
        return GitGateResult(True)
    session = read_json(layout.session_file(str(who["key"]))) or {}
    submit_events(layout, session, str(who["key"]), result.events)
    return result


def git_gate_trailer(layout: Layout, cwd: str, msgfile: str) -> bool:
    """``prepare-commit-msg``: add ``Remembra-Member`` and ``Remembra-Task`` trailers (client-attested)."""
    who = _whoami(layout)
    if who is None or not who.get("member_key"):
        return False
    trailers = [f"Remembra-Member: {who['member_key']}"]
    if who.get("task_ref"):
        trailers.append(f"Remembra-Task: {who['task_ref']}")
    try:
        text = Path(msgfile).read_text(encoding="utf-8")
    except OSError:
        return False
    missing = [t for t in trailers if t.split(":", 1)[0] + ":" not in text]
    if not missing:
        return False
    args = ["interpret-trailers", "--in-place"]
    for t in missing:
        args += ["--trailer", t]
    res = _git([*args, msgfile], cwd)
    return bool(res is not None and res.returncode == 0)


# ===========================================================================
# Vendoring (installed copy under ~/.remembra/crew/bin) and integrity
# ===========================================================================

VENDORED_MODULES: Final = (
    "remembra.crew.schemas",
    "remembra.crew.gatecore",
    "remembra.relay.crew.gate",
    "remembra.relay.crew.outbox",
    "remembra.relay.crew.snapshot",
)
VENDORED_PACKAGES: Final = ("remembra", "remembra.crew", "remembra.relay", "remembra.relay.crew")
HEADER_RE: Final = re.compile(r"^# crew-gate v(?P<v>\d+) sha256:(?P<h>[0-9a-f]{64})$")
LAUNCHER_BODY: Final = '''"""Remembra Crew gate (vendored by remembra-crew; crewd restores this file when it changes)."""
import os as _os
import sys as _sys

_lib = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "lib")
if _lib not in _sys.path:
    _sys.path.insert(0, _lib)
from remembra.relay.crew.gate import main as _main  # noqa: E402

raise SystemExit(_main(_sys.argv[1:]))
'''


def _module_source(name: str) -> bytes:
    import importlib.util

    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise FileNotFoundError(name)
    return Path(spec.origin).read_bytes()


def bundle_files() -> dict[str, bytes]:
    """``lib/``-relative path → content for the vendored copy (the package's own sources)."""
    files: dict[str, bytes] = {}
    for pkg in VENDORED_PACKAGES:
        files[pkg.replace(".", "/") + "/__init__.py"] = b""
    for name in VENDORED_MODULES:
        files[name.replace(".", "/") + ".py"] = _module_source(name)
    return files


def bundle_sha(launcher_body: str, files: Mapping[str, bytes]) -> str:
    h = hashlib.sha256()
    h.update(b"crew-gate\0" + launcher_body.encode())
    for rel in sorted(files):
        h.update(b"\0" + rel.encode() + b"\0" + hashlib.sha256(files[rel]).digest())
    return h.hexdigest()


def installed_files(layout: Layout) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    base = layout.lib
    if not base.is_dir():
        return out
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        out[path.relative_to(base).as_posix()] = path.read_bytes()
    return out


@dataclass(frozen=True)
class GateCheck:
    ok: bool
    expected_sha: str
    actual_sha: str
    installed: bool


def verify_gate(layout: Layout) -> GateCheck:
    """Compare the installed gate with its header sha and with the package it was vendored from."""
    try:
        text = layout.gate_script.read_text(encoding="utf-8")
    except OSError:
        return GateCheck(False, bundle_sha(LAUNCHER_BODY, bundle_files()), "0" * 64, False)
    first, _, body = text.partition("\n")
    m = HEADER_RE.match(first.strip())
    try:
        actual = bundle_sha(body, installed_files(layout))
    except OSError:
        actual = "0" * 64
    expected = bundle_sha(LAUNCHER_BODY, bundle_files())
    header_ok = bool(m and m.group("h") == actual)
    return GateCheck(header_ok and actual == expected, expected, actual, True)


def launcher_text(files: Mapping[str, bytes] | None = None) -> str:
    """``bin/crew-gate.py`` exactly as :func:`vendor_gate` writes it: the sha header, then the launcher."""
    sha = bundle_sha(LAUNCHER_BODY, bundle_files() if files is None else files)
    return f"# crew-gate v{GATE_VERSION} sha256:{sha}\n{LAUNCHER_BODY}"


def vendored_source() -> str:
    """The text of ``bin/crew-gate.py`` for the installer (WP-10 ``remembra-crew connect``).

    The launcher imports the decision core from ``bin/lib``; the installer writes those files from
    :func:`bundle_files` next to it, so :func:`verify_gate` accepts the result.
    """
    return launcher_text()


def vendor_gate(layout: Layout, *, crewd_argv: Sequence[str] | None = None, python: str | None = None) -> Path:
    """Install (or restore) ``bin/crew-gate.py`` and its ``lib/`` copy; returns the script path.

    Everything is written 0600 in 0700 directories (inside ``crew-policy``); stale files under
    ``lib/`` that are not part of the bundle are removed so the sha covers exactly what runs.
    """
    layout.ensure()
    files = bundle_files()
    lib = layout.lib
    lib.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.bin, 0o700)
    os.chmod(lib, 0o700)
    wanted = set(files)
    for existing in list(lib.rglob("*")):
        rel = existing.relative_to(lib).as_posix()
        if existing.is_file() and "__pycache__" not in existing.parts and rel not in wanted:
            existing.unlink()
    for rel, content in files.items():
        dest = lib / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(dest.parent, 0o700)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    script = layout.gate_script
    tmp_script = script.with_name(script.name + ".tmp")
    tmp_script.write_text(launcher_text(files), encoding="utf-8")
    os.chmod(tmp_script, 0o600)
    os.replace(tmp_script, script)
    argv = list(crewd_argv) if crewd_argv else [python or sys.executable, "-m", "remembra.relay.crew.crewd"]
    write_json(layout.crewd_cmd, {"argv": argv, "python": python or sys.executable})
    return script


def gate_command(layout: Layout, sub: str, *, python: str | None = None, hook: str = DEFAULT_ADAPTER) -> str:
    """The hook command line for one gate subcommand (with the ``# remembra-crew`` marker, §8.2)."""
    import shlex

    g = shlex.quote(str(layout.gate_script))
    # a missing gate file must exit 0: `python -I missing.py` exits 2, a blocking hook error (§8.2)
    return f"test ! -f {g} || exec {shlex.quote(python or sys.executable)} -I {g} {sub} --hook {hook} # remembra-crew"


# ===========================================================================
# main
# ===========================================================================

HOOK_COMMANDS: Final = {
    "pretool": cmd_pretool,
    "posttool": cmd_posttool,
    "turn": cmd_turn,
    "stop": cmd_stop,
    "precompact": cmd_precompact,
    "wake": cmd_wake,
    "rewake": cmd_wake,  # the verb `remembra-crew connect` installs (adapters/crew_hooks.GATE_VERBS)
}


def _parse(argv: Sequence[str]) -> tuple[str, str, list[str]]:
    args = list(argv)
    sub = args.pop(0) if args else ""
    hook = DEFAULT_ADAPTER
    rest: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--hook" and i + 1 < len(args):
            hook = args[i + 1]
            i += 2
            continue
        if args[i].startswith("--hook="):
            hook = args[i].split("=", 1)[1]
            i += 1
            continue
        rest.append(args[i])
        i += 1
    return sub, hook, rest


def main(argv: Sequence[str] | None = None, *, layout: Layout | None = None) -> int:
    sub, hook, rest = _parse(sys.argv[1:] if argv is None else argv)
    layout = layout or Layout.from_env()
    if sub in HOOK_COMMANDS:
        raw, oversized = read_stdin_ex()
        payload = parse_payload(raw)
        degraded: str | None = None
        if oversized or (not payload and raw.strip()):
            # never a silent skip (§10.3): recover the session and the fields the checks need
            payload = salvage_payload(raw)
            degraded = "oversized" if oversized else "unparseable"
        sid = payload.get("session_id")
        if not isinstance(sid, str) or not sid or not layout.session_file(session_key(hook, sid)).exists():
            if degraded:
                _err(f"hook payload {degraded} and no crew session found in it; not checked")
            return 0  # not a crew session: the <10 ms fast exit
        ctx = HookContext(layout, hook, payload, degraded=degraded)
        if degraded and sub != "pretool":
            session = ctx.session()
            if session is not None:
                _err(f"hook payload {degraded}; handled with the fields that could be recovered")
                submit_events(
                    layout,
                    session,
                    ctx.key or "",
                    [{"type": "gate.error", "payload": {"stage": sub[:32], "error_class": f"payload_{degraded}"}}],
                )
        return HOOK_COMMANDS[sub](ctx)
    try:
        if sub == "precommit":
            res = git_gate_precommit(layout, os.getcwd())
            if not res.allowed:
                _err(res.reason or "commit blocked")
                sys.stderr.write((res.reason or "") + "\n")
                return 1
            return 0
        if sub == "prepush":
            lines = read_stdin(0.5).splitlines()
            res = git_gate_prepush(layout, os.getcwd(), lines)
            if not res.allowed:
                sys.stderr.write((res.reason or "push blocked by Remembra Crew") + "\n")
                return 1
            return 0
        if sub == "trailer":
            if rest:
                git_gate_trailer(layout, os.getcwd(), rest[0])
            return 0
        if sub == "version":
            sys.stdout.write(f"crew-gate v{GATE_VERSION}\n")
            return 0
    except Exception as e:  # a git gate error never blocks the commit (§10.3), but is reported
        _err(f"{sub} error ({e.__class__.__name__}); allowed")
        return 0
    _err(f"unknown subcommand {sub!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
